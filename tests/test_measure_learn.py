import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from surya_distro import learn, measure, playbook, traffic
from surya_distro.channels import first_drafts
from surya_distro.kit import Kit
from surya_distro.repo import gather
from tests.fakes import FakeGitHub, FakeNet, lantern_routes
from tests.test_study_playbook import synthetic

POSTED = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
HN_URL = "https://news.ycombinator.com/item?id=4242"


def hn_net(points=40):
    return FakeNet(gets={
        "https://hacker-news.firebaseio.com/v0/item/4242.json": {"id": 4242, "score": points, "descendants": 12, "by": "maker", "time": 1},
        "https://hacker-news.firebaseio.com/v0/user/maker.json": {"karma": 150, "created": 1700000000},
        "https://api.bsky.app/xrpc/app.bsky.actor.getProfile": {"did": "did:plc:me", "handle": "me.bsky.social", "followersCount": 300, "createdAt": "2024-01-01T00:00:00Z"},
        "https://api.bsky.app/xrpc/app.bsky.feed.getPosts": {"posts": [{"likeCount": 30, "repostCount": 4, "replyCount": 6, "quoteCount": 1}]},
    })


def posted_kit(base: Path, url=HN_URL, draft="hn") -> Kit:
    gh = FakeGitHub(lantern_routes())
    f = gather(gh, "example/lantern")
    kit = Kit(base, f.repo)
    kit.save_facts(f.to_json())
    kit.write_drafts(first_drafts(f, []))
    d = kit.draft(draft)
    d.fields = {k: re.sub(r"\[\[fill:[^\]]*\]\]", "Written by a person.", v) for k, v in d.fields.items()}
    d.save()
    kit.approve(draft, lambda d: True)
    d = kit.posted(draft, url)
    d.meta["posted_at"] = POSTED.strftime("%Y-%m-%dT%H:%M:%SZ")
    d.save()
    return kit


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_platforms(self):
        for url, plat in ((HN_URL, "hn"), ("https://www.reddit.com/r/x/comments/abc/t/", "reddit"), ("https://bsky.app/profile/a/post/b", "bluesky"),
                          ("https://x.com/a/status/1", "x"), ("https://www.linkedin.com/posts/a", "linkedin"), ("https://example.org", "other")):
            self.assertEqual(measure.platform(url), plat)

    def test_fetch_from_public_apis(self):
        m, a = measure.fetch("hn", HN_URL, hn_net())
        self.assertEqual((m, a["size"]), ({"points": 40, "comments": 12}, 150))
        m, a = measure.fetch("bluesky", "https://bsky.app/profile/me.bsky.social/post/3abc", hn_net())
        self.assertEqual((m["likes"], a["size"]), (30, 300))
        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "", "REDDIT_CLIENT_SECRET": ""}):
            with self.assertRaises(measure.MeasureError) as cm:
                measure.fetch("reddit", "https://www.reddit.com/r/x/comments/abc/t/", FakeNet())
        self.assertIn("app key", str(cm.exception))
        with self.assertRaises(measure.MeasureError):
            measure.fetch("x", "https://x.com/a/status/1", FakeNet())

    def test_checkpoints(self):
        kit = posted_kit(self.base)
        out = measure.run(self.base, kit, hn_net(), now=POSTED + timedelta(hours=2), due_only=True)
        self.assertIn("the 1h reading", out[0])
        self.assertEqual(measure.run(self.base, kit, hn_net(), now=POSTED + timedelta(hours=3), due_only=True), [])
        measure.run(self.base, kit, hn_net(90), now=POSTED + timedelta(hours=20), due_only=True)
        measure.run(self.base, kit, hn_net(120), now=POSTED + timedelta(days=7), due_only=True)
        rec = measure.Posts(self.base, kit.repo).data["hn"]
        self.assertEqual([s["checkpoint"] for s in rec["snapshots"]], ["1h", "24h", "7d"])
        self.assertEqual(measure.best(rec)["engagement"], 120)
        self.assertFalse(measure.due(rec, POSTED + timedelta(days=30)))

    def test_typed_numbers_for_x(self):
        kit = posted_kit(self.base, "https://x.com/me/status/1", "x")
        self.assertIn("no free read API", measure.run(self.base, kit, FakeNet(), now=POSTED + timedelta(hours=1))[0])
        s = measure.record(self.base, kit, "x", {"likes": 30, "replies": 4, "views": 2000}, 800, now=POSTED + timedelta(hours=25))
        self.assertEqual((s["checkpoint"], s["engagement"], s["source"]), ("24h", 34.0, "typed"))
        with self.assertRaises(Exception):
            measure.record(self.base, kit, "hn", {"likes": 1}, None)

    def test_github_join_and_report(self):
        kit = posted_kit(self.base)
        store = traffic.load(self.base, kit.repo)
        views = {"views": [{"timestamp": f"2026-11-{d:02d}T00:00:00Z", "count": 100 if d >= 10 else 5, "uniques": 1} for d in range(3, 17)]}
        traffic.merge(store, views, {"clones": []}, [], [], {"stargazers_count": 5}, "2026-11-09")
        traffic.merge(store, views, {"clones": []}, [{"referrer": "news.ycombinator.com", "count": 300, "uniques": 90}], [],
                      {"stargazers_count": 25}, "2026-11-17")
        traffic.write_json(traffic.repo_dir(self.base, kit.repo) / "traffic.json", store)
        g = measure.github_after(self.base, kit.repo, POSTED.strftime("%Y-%m-%dT%H:%M:%SZ"), "hn")
        self.assertEqual((g["views_after"], g["views_before"], g["stars_after"], g["referrer_views"]), (700, 35, 20, 300))
        measure.run(self.base, kit, hn_net(), now=POSTED + timedelta(hours=2))
        text = measure.report(self.base, kit)
        self.assertIn("40 points, 12 comments", text)
        self.assertIn("700 views (35 before)", text)


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "home"
        self.home = Path(self.tmp.name) / "user"
        self.home.mkdir()

    def test_where_notes_go(self):
        loc = lambda flag=None, **env: learn.locate(flag, self.base, env, self.home)
        self.assertEqual(loc()[:2], (self.base / "learnings", False))
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text(json.dumps({"autoMemoryDirectory": "/m/claude"}))
        self.assertEqual(loc()[:2], (Path("/m/claude/surya-distro"), True))
        (self.home / ".surya-memory").mkdir()
        (self.home / ".surya-memory/store-path").write_text("/m/surya\n")
        self.assertEqual(loc()[0], Path("/m/surya/surya-distro"))
        self.assertEqual(loc(SURYA_MEMORY_DIR="/m/env")[0], Path("/m/env/surya-distro"))
        self.assertEqual(loc(SURYA_DISTRO_MEMORY="/m/mine", SURYA_MEMORY_DIR="/m/env")[0], Path("/m/mine/surya-distro"))
        self.assertEqual(loc("/m/flag", SURYA_DISTRO_MEMORY="/m/mine")[0], Path("/m/flag/surya-distro"))
        self.assertEqual(loc("off")[:2], (self.base / "learnings", False))

    def finding(self, **kw):
        f = {"name": "distro-lantern-hn-2026-11-10", "description": "Show HN post did well (+1.4)", "repo": "example/lantern",
             "channel": "hn", "draft": "hn", "url": HN_URL, "date": "2026-11-17", "checkpoint": "7d", "score": 1.4,
             "tags": {"title_shape": "name-what", "hook": "problem"}, "rules": ["hn.title_shape.name-what"],
             "confidence": "early", "sample_size": 1, "status": "active", "body": "# hn\n\nBody.\n"}
        f.update(kw)
        return f

    def test_notes_round_trip_and_never_overwrite_others(self):
        mem = Path(self.tmp.name) / "memory"
        (mem / "surya-distro").mkdir(parents=True)
        (mem / "MEMORY.md").write_text("# Memory Index\n\n- [Other](other.md) — theirs\n")
        foreign = mem / "surya-distro" / "distro-lantern-hn-2026-11-10.md"
        foreign.write_text("---\nname: mine\n---\nNot from Distro.\n")
        store = learn.Learnings(mem / "surya-distro", True)
        path = store.write(self.finding())
        self.assertNotEqual(path, foreign)
        self.assertEqual(foreign.read_text(), "---\nname: mine\n---\nNot from Distro.\n")
        notes = store.notes()
        self.assertEqual(len(notes), 1)
        n = notes[0]
        self.assertEqual((n["score"], n["rules"], n["tags"]["hook"], n["status"], n["confidence"], n["sample_size"]),
                         (1.4, ["hn.title_shape.name-what"], "problem", "active", "early", "1"))
        self.assertIn("evidence: " + HN_URL, path.read_text())
        store.write_index()
        store.write_index()
        index = (mem / "MEMORY.md").read_text()
        self.assertEqual(index.count(learn.POINTER), 1)
        self.assertIn("- [Other](other.md) — theirs", index)
        self.assertEqual(store.supersede(["hn.title_shape.name-what"], "2026-12-01"), 1)
        self.assertEqual(store.notes()[0]["status"], "superseded")
        self.assertIn("superseded_on: 2026-12-01", path.read_text())

    def test_decisions_log_appends(self):
        store = learn.Learnings(self.base / "learnings", False)
        after = {"version": 2}
        changed = [{"what": "added", "id": "hn.hook.problem", "before": "", "after": "Open with a problem.", "evidence": "16 posts"}]
        store.record_decision("example/lantern", after, None, changed, "2026-11-20")
        store.record_decision("example/lantern", {"version": 3}, after, [], "2026-11-27")
        text = (self.base / "learnings/distro-decisions.md").read_text()
        self.assertIn("## 2026-11-20: example/lantern playbook v2 approved", text)
        self.assertIn("  - Before: none", text)
        self.assertIn("No rule changed.", text)
        self.assertFalse((self.base / "MEMORY.md").exists())

    def test_findings_score_your_post_against_the_study(self):
        kit = posted_kit(self.base)
        measure.run(self.base, kit, hn_net(400), now=POSTED + timedelta(days=7))
        study = synthetic()
        study["sources"]["hn"]["model"] = {"kind": "median", "n": 32, "features": [], "coef": [2.0], "sd": 1.0, "fill": {}}
        pb = playbook.propose(kit.facts(), synthetic(), [], None, "2026-11-12")
        found = learn.findings(self.base, kit, study, pb, now=POSTED + timedelta(days=7))
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertGreater(f["score"], 3)
        self.assertEqual((f["confidence"], f["sample_size"], f["checkpoint"]), ("early", 1, "7d"))
        self.assertIn("hn.day.Tue", f["rules"])
        self.assertNotIn("hn.title_shape.name-what", f["rules"])
        self.assertIn("Early: this rests on one launch.", f["body"])
        self.assertNotIn("—", f["body"] + f["description"])


if __name__ == "__main__":
    unittest.main()
