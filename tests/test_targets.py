import json
import unittest
from datetime import datetime, timezone

from surya_distro.github import Response
from surya_distro.repo import gather
from surya_distro.targets import Checker, catalog, fitting, render
from tests.fakes import FakeGitHub, FakeWeb, b64, lantern_routes, ok, repo_meta

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def list_routes(name="someone/awesome-things", readme="# Awesome\n- [Other](https://github.com/x/y)\n", **meta):
    base = {"pushed_at": "2026-11-01T00:00:00Z", "stargazers_count": 500}
    base.update(meta)
    return {f"repos/{name}": repo_meta(name, **base), f"repos/{name}/readme": b64(readme)}


class Checks(unittest.TestCase):
    def setUp(self):
        self.routes = lantern_routes()
        self.facts = gather(FakeGitHub(self.routes), "example/lantern")

    def check(self, target, routes=None, web=None):
        gh = FakeGitHub({**self.routes, **(routes or {})})
        return Checker(gh, web or FakeWeb(), now=NOW).check(self.facts, target)

    def test_open_list(self):
        r = self.check({"id": "a", "name": "A", "github": "someone/awesome-things"}, list_routes())
        self.assertEqual(r.status, "open")
        self.assertIn("500 stars", r.detail)

    def test_already_listed(self):
        routes = list_routes(readme="- [Lantern](https://github.com/Example/Lantern) logs")
        self.assertEqual(self.check({"id": "a", "name": "A", "github": "someone/awesome-things"}, routes).status, "listed")

    def test_archived_stale_and_gone(self):
        t = {"id": "a", "name": "A", "github": "someone/awesome-things"}
        self.assertEqual(self.check(t, list_routes(archived=True)).status, "archived")
        r = self.check(t, list_routes(pushed_at="2025-01-01T00:00:00Z"))
        self.assertEqual(r.status, "stale")
        self.assertIn("days", r.detail)
        self.assertEqual(self.check(t, {}).status, "gone")

    def test_age_rule(self):
        t = {"id": "a", "name": "A", "github": "someone/awesome-things", "rules": {"min_age_days": 14, "or_min_stars": 100}}
        r = self.check(t, list_routes())
        self.assertEqual(r.status, "wait")
        self.assertIn("2026-11-15", r.detail)
        self.facts.stars = 150
        self.assertEqual(self.check(t, list_routes()).status, "open")

    def test_pages(self):
        t = {"id": "p", "name": "P", "check_url": "https://example.org/submit"}
        self.assertEqual(self.check(t, web=FakeWeb({"https://example.org": ok("https://example.org/submit")})).status, "open")
        self.assertEqual(self.check(t, web=FakeWeb()).status, "gone")
        blocked = self.check(t, web=FakeWeb({"https://example.org": Response(403, "https://example.org/submit")}))
        self.assertEqual(blocked.status, "unchecked")
        self.assertIn("blocks automated", blocked.detail)
        down = self.check(t, web=FakeWeb({"https://example.org": Response(0, "https://example.org/submit", "URLError: no route")}))
        self.assertEqual(down.status, "unchecked")
        moved = self.check(t, web=FakeWeb({"https://example.org": ok("https://example.org/new-place")}))
        self.assertIn("redirects to https://example.org/new-place", moved.detail)

    def test_page_must_still_mention_the_address(self):
        t = {"id": "p", "name": "P", "check_url": "https://example.org/c", "expect": "hello@example.org"}
        self.assertEqual(self.check(t, web=FakeWeb({"https://example.org": ok("https://example.org/c", "Email hello@example.org")})).status, "open")
        self.assertEqual(self.check(t, web=FakeWeb({"https://example.org": ok("https://example.org/c", "Use our form")})).status, "unchecked")

    def test_mcp_registry(self):
        t = {"id": "m", "name": "M", "registry": "mcp"}
        listed = {"servers": [{"server": {"name": "io.github.example/lantern", "version": "0.3.0",
                                          "repository": {"url": "https://github.com/example/lantern.git"}}}]}
        web = FakeWeb({"https://registry.modelcontextprotocol.io": ok("x", json.dumps(listed))})
        r = self.check(t, web=web)
        self.assertEqual(r.status, "listed")
        self.assertIn("io.github.example/lantern", r.detail)

        empty = ok("x", json.dumps({"servers": []}))
        r = self.check(t, web=FakeWeb({"https://registry.modelcontextprotocol.io": empty}))
        self.assertEqual(r.status, "needs")
        self.assertIn("lantern-log isn't on PyPI", r.detail)

        r = self.check(t, web=FakeWeb({"https://registry.modelcontextprotocol.io": empty, "https://pypi.org/pypi/lantern-log": ok("p")}))
        self.assertEqual(r.status, "open")

    def test_render_puts_open_places_first(self):
        gh = FakeGitHub({**self.routes, **list_routes()})
        c = Checker(gh, FakeWeb(), now=NOW)
        results = [c.check(self.facts, {"id": "g", "name": "Gone", "check_url": "https://gone.example"}),
                   c.check(self.facts, {"id": "a", "name": "Open list", "github": "someone/awesome-things"})]
        table = render(results, "2026-11-11")
        self.assertLess(table.index("Open list"), table.index("Gone"))


class Catalog(unittest.TestCase):
    def test_entries_are_complete_and_checkable(self):
        entries = catalog()["targets"]
        ids = [t["id"] for t in entries]
        self.assertEqual(len(ids), len(set(ids)))
        for t in entries:
            with self.subTest(t["id"]):
                for key in ("id", "name", "kind", "fits", "how", "submit"):
                    self.assertTrue(t.get(key), key)
                self.assertTrue(t.get("github") or t.get("check_url") or t.get("registry"), "no live check")

    def test_targets_follow_the_repo_kinds(self):
        facts = gather(FakeGitHub(lantern_routes()), "example/lantern")
        ids = {t["id"] for t in fitting(facts)}
        self.assertTrue({"claude-directory", "mcp-registry", "pycoders"} <= ids)
        facts.kinds = ["node", "open-source"]
        ids = {t["id"] for t in fitting(facts)}
        self.assertNotIn("claude-directory", ids)
        self.assertIn("changelog-news", ids)


if __name__ == "__main__":
    unittest.main()
