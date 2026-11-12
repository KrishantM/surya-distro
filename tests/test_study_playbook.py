import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from surya_distro import engines, playbook, study
from surya_distro.channels import first_drafts, follow
from surya_distro.repo import gather
from tests.fakes import FakeGitHub, FakeNet, b64, lantern_routes

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
TITLES = ["Show HN: Tool{i}, a log for agents", "Claude Code tip number {i}", "Why agents forget, part {i}", "Is Claude Code memory solved? ({i})"]


def hn_hits(req):
    page = []
    for i in range(24):
        page.append({"objectID": str(1000 + i), "title": TITLES[i % 4].format(i=i), "url": "https://github.com/x/y" if i % 4 == 0 else "",
                     "story_text": "" if i % 4 == 0 else "<p>I kept losing context.</p>", "author": f"user{i % 6}",
                     "points": (300 if i % 4 == 0 else 4) + i, "num_comments": 10 + i, "created_at_i": 1790000000 + i * 3600,
                     "_tags": ["story", "show_hn"] if i % 4 == 0 else ["story"]})
    return {"hits": page}


def hn_user(req):
    name = req.full_url.rsplit("/", 1)[-1].replace(".json", "")
    n = int(name[-1])
    return {"id": name, "karma": 10 ** n, "created": 1700000000 - n * 86400 * 200}


def bsky_search(req):
    q = parse_qs(urlsplit(req.full_url).query)
    out = []
    for i in range(14):
        out.append({"uri": f"at://did:plc:a{i % 5}/app.bsky.feed.post/r{i}{q['sort'][0]}", "author": {"did": f"did:plc:a{i % 5}", "handle": f"a{i % 5}.bsky.social", "createdAt": "2025-01-01T00:00:00Z"},
                    "record": {"text": ("I built a thing for Claude Code memory. " if i % 3 == 0 else "Claude Code news today. ") + "https://x.y/z",
                               "createdAt": "2026-11-01T10:00:00Z", "facets": [{"index": {"byteStart": 40, "byteEnd": 52}, "features": [{"$type": "app.bsky.richtext.facet#link"}]}]},
                    "likeCount": 200 if i % 3 == 0 else 3, "repostCount": 5, "replyCount": 2, "quoteCount": 0,
                    "embed": {"$type": "app.bsky.embed.images#view"} if i % 3 == 0 else {}})
    out.append({"uri": "at://did:plc:z/app.bsky.feed.post/reply", "author": {"did": "did:plc:z"}, "record": {"text": "reply", "reply": {}}})
    return {"posts": out}


def bsky_profiles(req):
    actors = parse_qs(urlsplit(req.full_url).query)["actors"]
    return {"profiles": [{"did": a, "followersCount": 50 * (int(a[-1]) + 1), "createdAt": "2024-06-01T00:00:00Z"} for a in actors]}


def study_net():
    return FakeNet(gets={
        "https://hn.algolia.com/api/v1/search?": hn_hits,
        "https://hn.algolia.com/api/v1/search_by_date?": {"hits": []},
        "https://hacker-news.firebaseio.com/v0/user/": hn_user,
        "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts": bsky_search,
        "https://api.bsky.app/xrpc/app.bsky.actor.getProfiles": bsky_profiles,
        "https://www.reddit.com/": __import__("surya_distro.net", fromlist=["Response"]).Response(403, "x"),
    })


class SearchGitHub(FakeGitHub):
    def _run(self, args):
        path = args[-1]
        if path.startswith("search/repositories?"):
            self.calls.append(args)
            items = [{"full_name": f"o{i}/r{i}", "html_url": f"https://github.com/o{i}/r{i}", "description": f"A tool number {i}",
                      "stargazers_count": 10 * i, "created_at": "2026-09-01T00:00:00Z", "owner": {"login": f"o{i}"}} for i in range(14)]
            items.append({"full_name": "example/lantern", "owner": {"login": "example"}})
            return 0, json.dumps({"items": items}), ""
        if path.startswith("users/"):
            self.calls.append(args)
            return 0, json.dumps({"followers": 7, "created_at": "2020-01-01T00:00:00Z"}), ""
        if path.startswith("repos/o") and path.endswith("/readme"):
            self.calls.append(args)
            return 0, json.dumps(b64("# R\n\nR does a thing for agents.\n\n![shot](shot.png)\n")), ""
        return super()._run(args)


class Counting(engines.Heuristic):
    def __init__(self):
        super().__init__()
        self.labelled = 0

    def label(self, posts, about, progress=None):
        self.labelled += len(posts)
        return super().label(posts, about, progress)


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.gh = SearchGitHub(lantern_routes())
        self.facts = gather(self.gh, "example/lantern").to_json()

    def run_study(self, engine=None, **kw):
        return study.run(self.facts, study_net(), self.gh, engine or engines.Heuristic(), self.base, now=NOW,
                         queries=["Claude Code"], **kw)

    def test_plan_picks_queries_from_the_repo(self):
        p = study.plan(self.facts)
        self.assertEqual(p["queries"][0], "Claude Code")
        self.assertIn("Claude Code", p["about"])
        self.assertEqual(study.plan(self.facts, ["a b"], "the space")["queries"], ["a b"])

    def test_a_study_reads_scores_and_writes(self):
        s = self.run_study()
        src = s["sources"]
        self.assertEqual(src["hn"]["posts"], 24)
        self.assertEqual(src["hn"]["model"]["kind"], "regression")
        self.assertEqual(src["bluesky"]["posts"], 28)          # top and latest, replies skipped
        self.assertEqual(src["github"]["posts"], 14)           # the repo itself is skipped
        self.assertIn("app key", src["reddit"]["note"])
        self.assertIn("PRODUCT_HUNT_TOKEN", src["producthunt"]["note"])
        self.assertIn("manual.csv", src["manual"]["note"])
        hn = [p for p in s["posts"] if p["source"] == "hn"]
        launch = [p for p in hn if p["tags"]["title_shape"] == "name-what"]
        self.assertTrue(all(p["score"] > 0 for p in launch))
        self.assertTrue(all("size" in p["x"] for p in hn))
        md = (self.base / "example/lantern/study/study.md").read_text()
        self.assertIn("## Outliers", md)
        self.assertIn("come from **heuristic**", md)
        self.assertTrue((self.base / "example/lantern/study/manual.csv").read_text().startswith("channel,url"))
        self.assertTrue((self.base / "cache/authors.json").exists())
        self.assertFalse(study.is_stale(study.load(self.base, "example/lantern"), NOW))

    def test_labels_are_reused_on_a_refresh(self):
        e = Counting()
        self.run_study(e)
        first = e.labelled
        self.assertGreater(first, 0)
        self.run_study(e)
        self.assertEqual(e.labelled, first)

    def test_manual_rows_are_read(self):
        d = self.base / "example/lantern/study"
        d.mkdir(parents=True)
        (d / "manual.csv").write_text("channel,url,posted_at,author,followers,account_created,views,likes,reposts,replies,text\n"
                                      "x,https://x.com/a/status/1,2026-11-01T10:00:00Z,a,120,2025-01-01,5000,80,10,12,I built a tool\n")
        s = self.run_study(wanted=["manual"])
        p = s["posts"][0]
        self.assertEqual((p["source"], p["engagement"], p["metrics"]["views"]), ("x", 102.0, 5000.0))


def synthetic(n_good=16, n_bad=16, good=1.2, bad=-0.8, extra=None):
    posts = []
    for i in range(n_good + n_bad):
        win = i < n_good
        posts.append({"source": "hn", "id": str(i), "url": f"https://news.ycombinator.com/item?id={i}", "relevant": 0.9,
                      "engagement": 10, "x": {}, "show": True,
                      "score": (good if win else bad) + (i % 3) * 0.05,
                      "tags": {"title_shape": "name-what" if win else "question", "hook": "problem" if win else "what-it-is",
                               "kind": "launch", "length": "short", "media": "none", "link": "link-post",
                               "day": "Tue" if win else "Sat", "hour": "12-18 UTC"}})
    return {"at": "2026-11-12T00:00:00Z", "engine": "heuristic", "about": "agents", "posts": posts + (extra or []),
            "sources": {"hn": {"posts": len(posts), "relevant": len(posts)}}}


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.gh = FakeGitHub(lantern_routes())
        self.f = gather(self.gh, "example/lantern")
        self.facts = self.f.to_json()

    def test_rules_cite_their_evidence(self):
        pb = playbook.propose(self.facts, synthetic(), [], None, "2026-11-12")
        rules = {r["id"]: r for r in pb["rules"]}
        do = rules["hn.title_shape.name-what"]
        self.assertEqual((do["check"], do["level"], do["confidence"]), ("count", "error", "high"))
        self.assertEqual(do["evidence"]["n"], 16)
        self.assertEqual(len(do["evidence"]["posts"]), 3)
        # The avoid-rule only mirrors the do-rule, at the same confidence, so it isn't kept.
        self.assertNotIn("hn.title_shape.not-question", rules)
        self.assertEqual(rules["hn.hook.problem"]["check"], "label")
        self.assertEqual(rules["hn.hook.problem"]["level"], "warning")
        self.assertEqual(rules["hn.day.Tue"]["text"], "Post on a Tuesday.")
        self.assertNotIn("hn.day.not-Sat", rules)
        md = playbook.render(pb, "proposed")
        self.assertIn("Among Hacker News launch posts, 16 with this score", md)
        self.assertIn("Not enough relevant posts from Reddit", md)
        self.assertNotIn("—", md)

    def test_rule_wording_and_ids(self):
        self.assertEqual(playbook.rule_text("hour", "18-24 UTC", True, "short-form"), "Post between 18:00 and 24:00 UTC.")
        self.assertEqual(playbook.rule_id("x", "hour", "18-24 UTC", True), "x.hour.18-24")
        self.assertEqual(playbook.rule_text("link", "link-middle", False, "short-form"), "Don't put the link in the middle of the post.")
        md = playbook.render(playbook.propose(self.facts, synthetic(), [], None, "2026-11-12"), "proposed")
        self.assertIn("Too few LinkedIn posts pasted into study/manual.csv", md)
        self.assertNotIn("(pasted) (", md)

    def test_check_enforces_what_it_can_count(self):
        pb = playbook.propose(self.facts, synthetic(), [], None, "2026-11-12")
        bad = playbook.check(pb, "hn", {"title": "Show HN: Is this the way?", "text": "Lantern keeps a log.", "url": "u"})
        self.assertTrue(any(i.level == "error" and "hn.title_shape.name-what" in i.message for i in bad))
        self.assertTrue(any(i.level == "warning" and "hn.hook.problem" in i.message for i in bad))
        good = playbook.check(pb, "hn", {"title": "Show HN: Lantern, a log", "text": "Tired of losing track? Lantern keeps a log.", "url": "u"})
        self.assertEqual([i for i in good if i.level == "error"], [])
        self.assertEqual(playbook.check(None, "hn", {"title": "?"}), [])

    def test_drafts_follow_the_playbook(self):
        pb = playbook.propose(self.facts, synthetic(), [], None, "2026-11-12")
        drafts = first_drafts(self.f, [])
        ch, fields = drafts["hn"]
        out = follow(ch, fields, playbook.active(pb, "hn"), self.f)
        self.assertEqual(__import__("surya_distro.formats", fromlist=["x"]).title_shape(out["title"]), "name-what")
        self.assertTrue(out["text"].startswith("[[fill: the problem this solves"))
        self.assertEqual([i for i in playbook.check(pb, "hn", out) if i.level == "error"], [])

    def test_a_preview_draft_is_held_to_the_change_it_followed(self):
        from surya_distro.kit import Draft
        pb = playbook.propose(self.facts, synthetic(), [], None, "2026-11-12")
        d = Draft("hn", self.base / "hn.md", {"channel": "hn", "playbook": f"proposal {playbook.digest(pb)}"},
                  {"title": "Show HN: Is this the way?", "url": "u", "text": "Lantern keeps a log."})
        msgs = [i.message for i in d.issues(None, pb) if i.level == "error"]
        self.assertTrue(any("isn't approved yet" in m for m in msgs))
        self.assertTrue(any("hn.title_shape.name-what" in m for m in msgs))
        # Once that change is replaced by a newer one, only the approval error is left.
        msgs = [i.message for i in d.issues(None, None) if i.level == "error"]
        self.assertEqual(len(msgs), 1)

    def test_a_long_rule_asks_for_more_words(self):
        rule = {"dim": "length", "value": "long", "do": True}
        fields = {"title": "Show HN: Lantern", "url": "u", "text": "Lantern keeps a log.\n\n[[fill: the feedback you want most]]"}
        out = follow("hn", fields, [rule], self.f)["text"]
        self.assertIn("[[fill: more in your own words", out)
        self.assertTrue(out.endswith("[[fill: the feedback you want most]]"))

    def test_approval_versions_logs_and_supersedes(self):
        store = playbook.Playbooks(self.base, "example/lantern")
        self.assertEqual(store.save_proposal(playbook.propose(self.facts, synthetic(), [], None, "2026-11-12")), "proposed")
        self.assertIn("+++ playbook (proposed)", store.diff())
        with self.assertRaises(playbook.PlaybookError):
            store.approve(lambda diff: False, "2026-11-12T00:00:00Z")
        logged = []
        pb = store.approve(lambda diff: True, "2026-11-12T00:00:00Z", lambda after, before: logged.append(playbook.changes(before, after)))
        self.assertEqual(pb["version"], 1)
        self.assertIsNone(store.proposed())
        self.assertTrue(all(c["what"] == "added" for c in logged[0]))
        # The same evidence again changes no rule: no proposal, numbers refreshed in place.
        self.assertEqual(store.save_proposal(playbook.propose(self.facts, synthetic(), [], store.approved(), "2026-11-13")), "unchanged")
        # The question titles stop losing: that rule is superseded, and kept.
        flat = synthetic(good=0.2, bad=0.1)
        self.assertEqual(store.save_proposal(playbook.propose(self.facts, flat, [], store.approved(), "2026-11-20")), "proposed")
        nxt = store.approve(lambda d: True, "2026-11-20T00:00:00Z", lambda a, b: logged.append(playbook.changes(b, a)))
        gone = [r for r in nxt["rules"] if r["status"] == "superseded"]
        self.assertTrue(gone)
        self.assertEqual(nxt["version"], 2)
        self.assertIn("superseded", {c["what"] for c in logged[1]})
        self.assertIn("## Superseded", store.md.read_text())

    def test_your_launches_add_early_rules_and_can_disagree(self):
        finding = {"status": "active", "score": 1.6, "channel": "bluesky", "name": "distro-x", "url": "https://bsky.app/p",
                   "date": "2026-11-20", "rules": [], "tags": {"hook": "question", "media": "image"}}
        pb = playbook.propose(self.facts, synthetic(), [finding], None, "2026-11-21")
        early = {r["id"]: r for r in pb["rules"]}["bluesky.hook.question"]
        self.assertTrue(early["early"])
        self.assertEqual((early["confidence"], early["level"]), ("low", "warning"))
        self.assertIn("sample of 1", playbook.render(pb, "proposed"))
        bad = {"status": "active", "score": -1.5, "channel": "hn", "name": "distro-y", "url": "u", "date": "d",
               "rules": ["hn.title_shape.name-what"], "tags": {}}
        pb = playbook.propose(self.facts, synthetic(), [bad], None, "2026-11-21")
        r = {r["id"]: r for r in pb["rules"]}["hn.title_shape.name-what"]
        self.assertEqual((r["confidence"], r.get("disagree")), ("medium", True))


if __name__ == "__main__":
    unittest.main()
