import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from surya_distro import cli
from surya_distro.github import GitHubError, parse_repo
from tests.fakes import FakeGitHub, lantern_routes
from tests.test_measure_learn import hn_net
from tests.test_study_playbook import SearchGitHub, study_net
from tests.test_traffic import routes as traffic_routes


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        routes = {**lantern_routes(), **{k: v for k, v in traffic_routes(range(1, 4), range(1, 4)).items() if "traffic" in k}}
        self.gh = SearchGitHub(routes)
        self.net = study_net()
        self.net.gets.update(hn_net().gets)
        for target, value in (("make_github", self.gh), ("make_net", self.net)):
            p = mock.patch.object(cli, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict("os.environ", {"SURYA_DISTRO_ENGINE": "heuristic", "SURYA_DISTRO_MEMORY": str(Path(self.home) / "mem"),
                                             "REDDIT_CLIENT_ID": "", "PRODUCT_HUNT_TOKEN": "", "TYPESAFE_API_KEY": ""})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *argv, tty=False, answer=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin") as stdin, mock.patch("builtins.input", return_value=answer):
            stdin.isatty.return_value = tty
            code = cli.main(["--home", self.home, *argv])
        return code, out.getvalue(), err.getvalue()

    def kit(self):
        return Path(self.home) / "example" / "lantern"

    def test_offline_flow(self):
        code, out, _ = self.run_cli("draft", "https://github.com/example/lantern", "--offline")
        self.assertEqual(code, 0)
        self.assertIn("Nothing has been posted", out)
        self.assertIn("channel rules only", out)
        self.assertEqual(len(list((self.kit() / "kit/drafts").glob("*.md"))), 10)
        self.assertFalse((self.kit() / "study").exists())
        code, out, _ = self.run_cli("check", "example/lantern")
        self.assertEqual(code, 1)
        self.assertIn("fill in", out)
        code, out, _ = self.run_cli("show", "example/lantern")
        self.assertEqual(code, 0)
        self.assertIn("1 study     not run", out)
        code, _, err = self.run_cli("approve", "example/lantern", "x")
        self.assertEqual(code, 2)
        self.assertIn("own terminal", err)
        code, _, err = self.run_cli("posted", "example/lantern", "x")
        self.assertIn("isn't approved", err)
        code, out, _ = self.run_cli("outbox", "example/lantern")
        self.assertEqual((code, json.loads(out)), (0, []))

    def test_the_whole_flow(self):
        # draft studies, proposes a playbook, and drafts from the approved one (none yet).
        code, out, err = self.run_cli("draft", "example/lantern", "--query", "Claude Code")
        self.assertEqual(code, 0, err)
        self.assertIn("Studying example/lantern's space with heuristic", out)
        self.assertIn("approve example/lantern playbook", out)
        self.assertTrue((self.kit() / "study/study.md").exists())
        self.assertTrue((self.kit() / "playbook.proposed.md").exists())
        hn = (self.kit() / "kit/drafts/hn.md").read_text()
        self.assertIn("playbook: none", hn)
        # --preview drafts from the waiting change, and check holds those drafts back.
        code, out, _ = self.run_cli("draft", "example/lantern", "--preview", "--force", "--offline")
        self.assertIn("playbook proposal (preview)", out)
        code, out, _ = self.run_cli("check", "example/lantern", "hn")
        self.assertIn("isn't approved yet", out)
        # Approving the playbook needs a terminal and the word.
        code, _, err = self.run_cli("approve", "example/lantern", "playbook")
        self.assertIn("own terminal", err)
        code, out, _ = self.run_cli("approve", "example/lantern", "playbook", tty=True, answer="no")
        self.assertEqual(code, 2)
        code, out, err = self.run_cli("approve", "example/lantern", "playbook", tty=True, answer="playbook")
        self.assertEqual(code, 0, err)
        self.assertIn("Approved playbook v1", out)
        log = Path(self.home) / "mem/surya-distro/distro-decisions.md"
        self.assertIn("playbook v1 approved", log.read_text())
        code, out, _ = self.run_cli("check", "example/lantern", "hn")
        self.assertNotIn("isn't approved yet", out)
        code, out, _ = self.run_cli("show", "example/lantern")
        self.assertIn("v1 approved", out)
        # Fill, approve and post the HN draft, then measure, report and learn.
        p = self.kit() / "kit/drafts/hn.md"
        text = re.sub(r"\[\[fill:[^\]]*\]\]", "Tired of losing track of what changed?", p.read_text())
        p.write_text(text)
        code, out, err = self.run_cli("approve", "example/lantern", "hn", tty=True, answer="hn")
        self.assertEqual(code, 0, out + err)
        code, out, _ = self.run_cli("posted", "example/lantern", "hn", "--url", "https://news.ycombinator.com/item?id=4242")
        self.assertIn("1 hour, 24 hours and 7 days", out)
        code, out, _ = self.run_cli("measure", "example/lantern")
        self.assertIn("40 points, 12 comments", out)
        self.assertIn("traffic", out)
        code, out, _ = self.run_cli("measure")            # every watched repo
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("report", "example/lantern")
        self.assertIn("| [hn](https://news.ycombinator.com/item?id=4242) | hn |", out)
        code, out, err = self.run_cli("learn", "example/lantern")
        self.assertEqual(code, 0, err)
        notes = list((Path(self.home) / "mem/surya-distro").glob("distro-lantern-hn-*.md"))
        self.assertEqual(len(notes), 1)
        self.assertIn("confidence: early", notes[0].read_text())

    def test_measure_typed_numbers_need_a_draft(self):
        code, _, err = self.run_cli("measure", "--likes", "3")
        self.assertEqual(code, 2)
        self.assertIn("--likes N", err)

    def test_measure_with_nothing_to_measure(self):
        code, _, err = self.run_cli("measure")
        self.assertEqual(code, 2)
        self.assertIn("no repos", err)

    def test_strategy_before_study(self):
        self.run_cli("draft", "example/lantern", "--offline")
        code, _, err = self.run_cli("strategy", "example/lantern")
        self.assertEqual(code, 2)
        self.assertIn("surya-distro study", err)

    def test_study_command(self):
        code, out, err = self.run_cli("study", "example/lantern", "--sources", "hn,github", "--query", "agent log")
        self.assertEqual(code, 0, err)
        self.assertIn("hn", out)
        self.assertIn("Next: surya-distro strategy", out)
        code, out, _ = self.run_cli("strategy", "example/lantern")
        self.assertIn("playbook (proposed)", out)

    def test_show_before_draft(self):
        code, _, err = self.run_cli("show", "example/lantern")
        self.assertEqual(code, 2)
        self.assertIn("surya-distro draft", err)

    def test_flow_schedule_and_help(self):
        code, out, _ = self.run_cli("flow")
        self.assertIn("approve   [manual]", out)
        code, out, _ = self.run_cli("schedule")
        self.assertIn("measure</string><string>--due", out)
        self.assertIn("Nothing below has been installed", out)
        code, out, _ = self.run_cli()
        self.assertIn("never posts", out)
        self.assertIn("study → strategy → draft", out)

    def test_parse_repo(self):
        self.assertEqual(parse_repo("https://github.com/a-b/c.d.git"), "a-b/c.d")
        self.assertEqual(parse_repo("github.com/a/b/tree/main"), "a/b")
        self.assertEqual(parse_repo(" a/b/ "), "a/b")
        for bad in ("a", "a/b/c", "../x", "a b/c"):
            with self.assertRaises(GitHubError):
                parse_repo(bad)


if __name__ == "__main__":
    unittest.main()
