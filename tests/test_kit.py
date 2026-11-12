import tempfile
import unittest
from pathlib import Path
from unittest import mock

from surya_distro import cli
from surya_distro.channels import first_drafts
from surya_distro.kit import Draft, Kit, KitError, readiness, write_docs
from surya_distro.repo import gather
from surya_distro.targets import fitting
from tests.fakes import FakeGitHub, lantern_routes


def fill(kit, draft_id):
    d = kit.draft(draft_id)
    d.fields = {k: v.replace("[[fill: your name]]", "A. Person") for k, v in d.fields.items()}
    import re
    d.fields = {k: re.sub(r"\[\[fill:[^\]]*\]\]", "Written by a person.", v) for k, v in d.fields.items()}
    d.save()
    return d


class KitOnDisk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.facts = gather(FakeGitHub(lantern_routes()), "example/lantern")
        self.kit = Kit(self.base, "example/lantern")
        self.kit.save_facts(self.facts.to_json())
        emails = [t for t in fitting(self.facts) if t.get("email")]
        self.first = first_drafts(self.facts, emails)
        self.kit.write_drafts(self.first)

    def tearDown(self):
        self.tmp.cleanup()

    def test_drafts_round_trip(self):
        for d in self.kit.drafts():
            channel, fields = self.first[d.id]
            fields = {k: v for k, v in fields.items() if k != "note"}
            self.assertEqual(d.channel, channel)
            self.assertEqual(d.fields, {k: v.strip() for k, v in fields.items()}, d.id)
        self.assertIn("note", self.kit.draft("reddit-Python").meta)

    def test_rewriting_keeps_edits_unless_forced(self):
        d = self.kit.draft("x")
        d.fields["post"] = "Edited by hand https://github.com/example/lantern"
        d.save()
        self.assertEqual(self.kit.write_drafts(self.first)["x"], "kept (already drafted)")
        self.assertIn("Edited by hand", self.kit.draft("x").fields["post"])
        self.kit.write_drafts(self.first, force=True)
        self.assertNotIn("Edited by hand", self.kit.draft("x").fields["post"])

    def test_force_never_overwrites_an_approved_draft(self):
        fill(self.kit, "linkedin")
        self.kit.approve("linkedin", lambda d: True)
        self.assertEqual(self.kit.write_drafts(self.first, force=True)["linkedin"], "kept (approved)")
        self.assertEqual(self.kit.draft("linkedin").state, "approved")

    def test_approval_needs_a_clean_draft_and_a_yes(self):
        with self.assertRaises(KitError) as e:
            self.kit.approve("hn", lambda d: True)
        self.assertIn("fill", str(e.exception))
        fill(self.kit, "hn")
        with self.assertRaises(KitError):
            self.kit.approve("hn", lambda d: False)
        self.assertEqual(self.kit.draft("hn").state, "draft")
        self.assertEqual(self.kit.approve("hn", lambda d: True).state, "approved")

    def test_editing_after_approval_needs_a_new_approval(self):
        fill(self.kit, "hn")
        self.kit.approve("hn", lambda d: True)
        d = self.kit.draft("hn")
        d.fields["title"] = "Show HN: Lantern, now different"
        d.save()
        self.assertEqual(self.kit.draft("hn").state, "changed")
        with self.assertRaises(KitError):
            self.kit.posted("hn")
        self.kit.approve("hn", lambda d: True)
        p = self.kit.posted("hn", "https://news.ycombinator.com/item?id=1")
        self.assertEqual((p.state, p.meta["post_url"]), ("posted", "https://news.ycombinator.com/item?id=1"))

    def test_posting_needs_approval(self):
        with self.assertRaises(KitError) as e:
            self.kit.posted("x")
        self.assertIn("approve", str(e.exception).lower())

    def test_outbox_has_only_approved_emails(self):
        self.assertEqual(self.kit.outbox(), [])
        fill(self.kit, "email-console-dev")
        fill(self.kit, "linkedin")
        self.kit.approve("email-console-dev", lambda d: True)
        self.kit.approve("linkedin", lambda d: True)
        out = self.kit.outbox()
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]), {"to", "subject", "body"})
        self.assertEqual(out[0]["to"], "hello@console.dev")

    def test_agents_cannot_approve_without_a_terminal(self):
        fill(self.kit, "hn")
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaises(KitError) as e:
                self.kit.approve("hn", cli._terminal_confirm)
        self.assertIn("own terminal", str(e.exception))
        self.assertEqual(self.kit.draft("hn").state, "draft")

    def test_unknown_draft(self):
        with self.assertRaises(KitError) as e:
            self.kit.draft("myspace")
        self.assertIn("hn", str(e.exception))

    def test_review_sheet(self):
        path = write_docs(self.kit)
        text = path.read_text()
        self.assertIn("Nothing here has been posted, submitted or sent.", text)
        for d in self.kit.drafts():
            self.assertIn(f"### {d.id} ", text)
        self.assertIn("Not checked yet", text)
        self.assertTrue((self.kit.dir / "brief.md").exists())
        self.assertIn("pip install lantern-log", (self.kit.dir / "brief.md").read_text())

    def test_draft_with_code_and_separators_survives(self):
        p = self.base / "t.md"
        Draft("t", p, {"channel": "x", "status": "draft"}, {"post": "a\n\n```\ncode\n```", "thread": "one\n\n---\n\ntwo"}).save()
        d = Draft.load(p)
        self.assertEqual(d.fields["thread"], "one\n\n---\n\ntwo")
        self.assertIn("```\ncode\n```", d.fields["post"])


class Readiness(unittest.TestCase):
    def facts(self, **routes):
        return gather(FakeGitHub(lantern_routes(**routes)), "example/lantern").to_json()

    def test_a_ready_repo(self):
        items = dict((t, ok) for ok, t in readiness(self.facts()))
        self.assertTrue(items["Latest release v0.3.0."])
        self.assertTrue(items["GitHub description matches the README."])

    def test_gaps_are_flagged(self):
        from tests.fakes import repo_meta
        f = self.facts(**{"repos/example/lantern": repo_meta(topics=[], description="An old line about something else entirely")})
        f["release"] = None
        flagged = [t for ok, t in readiness(f) if not ok]
        self.assertTrue(any("No release" in t for t in flagged))
        self.assertTrue(any("No topics" in t for t in flagged))
        self.assertTrue(any("doesn't match" in t for t in flagged))
        self.assertTrue(any("screenshot" in t for t in flagged))


if __name__ == "__main__":
    unittest.main()
