import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from surya_distro import traffic
from surya_distro.github import GitHubError
from surya_distro.kit import Kit
from tests.fakes import FakeGitHub, repo_meta


def window(days, base=10):
    return [{"timestamp": f"2026-11-{d:02d}T00:00:00Z", "count": base + d, "uniques": d} for d in days]


def routes(view_days, clone_days, stars=5, base=10):
    r = "repos/example/lantern"
    return {
        r: repo_meta(stargazers_count=stars),
        f"{r}/traffic/views": {"count": 0, "uniques": 0, "views": window(view_days, base)},
        f"{r}/traffic/clones": {"count": 0, "uniques": 0, "clones": window(clone_days, base)},
        f"{r}/traffic/popular/referrers": [{"referrer": "news.ycombinator.com", "count": 40, "uniques": 30}],
        f"{r}/traffic/popular/paths": [{"path": "/example/lantern", "title": "x", "count": 9, "uniques": 4}],
    }


class Traffic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, rts, day):
        return traffic.fetch(FakeGitHub(rts), "example/lantern", self.base, now=datetime(2026, 11, day, tzinfo=timezone.utc))

    def test_history_outlives_the_14_day_window(self):
        self.fetch(routes(range(1, 8), range(1, 8)), 7)
        store = self.fetch(routes(range(5, 12), range(5, 12), stars=9, base=100), 11)
        self.assertEqual(sorted(store["days"])[0], "2026-11-01")
        self.assertEqual(len(store["days"]), 11)
        self.assertEqual(store["days"]["2026-11-02"]["views"], 12)       # kept from the first fetch
        self.assertEqual(store["days"]["2026-11-06"]["views"], 106)      # the newer fetch wins
        self.assertEqual(store["days"]["2026-11-06"]["clone_uniques"], 6)
        self.assertEqual(store["stars"]["2026-11-07"]["stars"], 5)
        self.assertEqual(store["stars"]["2026-11-11"]["stars"], 9)
        self.assertEqual(store["fetches"], ["2026-11-07", "2026-11-11"])
        saved = json.loads((self.base / "example" / "lantern" / "traffic.json").read_text())
        self.assertEqual(saved, store)

    def test_fetching_twice_a_day_is_idempotent(self):
        self.fetch(routes(range(1, 4), range(1, 4)), 3)
        store = self.fetch(routes(range(1, 4), range(1, 4)), 3)
        self.assertEqual(store["fetches"], ["2026-11-03"])
        self.assertEqual(len(store["days"]), 3)

    def test_fetch_adds_the_repo_to_the_daily_list(self):
        self.fetch(routes([1], [1]), 1)
        self.assertEqual(traffic.watched(self.base), ["example/lantern"])
        traffic.watch(self.base, "example/lantern")
        self.assertEqual(traffic.watched(self.base), ["example/lantern"])

    def test_no_push_access_says_so(self):
        rts = routes([1], [1])
        rts["repos/example/lantern/traffic/views"] = GitHubError("gh: Must have push access to repository (HTTP 403)")
        with self.assertRaises(GitHubError) as e:
            self.fetch(rts, 1)
        self.assertIn("push access", str(e.exception))

    def test_report_shows_post_dates_beside_traffic(self):
        self.fetch(routes(range(1, 6), range(1, 6)), 5)
        kit = Kit(self.base, "example/lantern")
        kit.write_drafts({"hn": ("hn", {"title": "Show HN: Lantern", "url": "https://x.y"})})
        d = kit.draft("hn")
        d.meta.update(status="approved", approved_hash=d.digest())
        d.save()
        kit.posted("hn")
        d = kit.draft("hn")
        d.meta["posted_at"] = "2026-11-04T09:00:00Z"
        d.save()
        out = traffic.report(self.base, "example/lantern")
        line = next(l for l in out.splitlines() if l.startswith("2026-11-04"))
        self.assertTrue(line.rstrip().endswith("hn"))
        self.assertIn("news.ycombinator.com", out)

    def test_report_before_any_fetch(self):
        self.assertIn("No traffic stored", traffic.report(self.base, "example/lantern"))

    def test_schedule_is_printed_not_installed(self):
        text = traffic.schedule_text(self.base)
        self.assertIn("launchctl load", text)
        self.assertIn("crontab -e", text)
        self.assertIn("Nothing below has been installed", text)
        self.assertFalse(any(self.base.iterdir()))


if __name__ == "__main__":
    unittest.main()
