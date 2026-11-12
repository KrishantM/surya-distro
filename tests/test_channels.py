import unittest

from surya_distro.channels import (CHANNELS, PLACEHOLDER_RE, first_drafts, fit, lint, pick_subreddits,
                                   thread_posts, weighted_length)
from surya_distro.repo import gather
from surya_distro.targets import fitting
from tests.fakes import FakeGitHub, lantern_routes


def filled(fields):
    return {k: PLACEHOLDER_RE.sub("Filled by a person.", v) for k, v in fields.items()}


class Limits(unittest.TestCase):
    def test_links_count_as_23_on_x(self):
        self.assertEqual(weighted_length("hi https://example.com/a/very/long/path/that/goes/on"), 3 + 23)

    def test_wide_characters_count_double_on_x(self):
        self.assertEqual(weighted_length("日本"), 4)

    def test_fit_cuts_at_a_boundary_without_an_ellipsis(self):
        text = "One memory, in plain files you own, for every tool and every machine you use"
        out = fit(text, 40)
        self.assertLessEqual(len(out), 40)
        self.assertFalse(out.endswith(("...", "…", ",", " for", " and")))
        self.assertTrue(text.startswith(out))
        self.assertEqual(fit("short", 40), "short")

    def test_thread_posts_split_on_a_dashed_line(self):
        self.assertEqual(thread_posts("one\n\n---\n\ntwo"), ["one", "two"])


class Checks(unittest.TestCase):
    def test_placeholder_is_an_error(self):
        issues = lint("linkedin", {"post": "Hello [[fill: why]]"})
        self.assertTrue(any(i.level == "error" and "fill" in i.message for i in issues))

    def test_over_the_limit_is_an_error(self):
        issues = lint("producthunt", {"name": "x", "tagline": "y" * 61, "description": "d", "first comment": "c"})
        self.assertEqual([(i.field, i.level) for i in issues], [("tagline", "error")])

    def test_show_hn_prefix_is_required(self):
        issues = lint("hn", {"title": "My tool", "url": "https://x.y"})
        self.assertTrue(any("Show HN" in i.message for i in issues))

    def test_style_warnings(self):
        issues = lint("linkedin", {"post": "A seamless way to supercharge work — wow!!"})
        msgs = " ".join(i.message for i in issues)
        self.assertIn("seamless", msgs)
        self.assertIn("supercharge", msgs)
        self.assertIn("dash", msgs)
        self.assertIn("exclamation", msgs)
        self.assertTrue(all(i.level == "warning" for i in issues))

    def test_cliche_match_is_whole_word(self):
        self.assertEqual(lint("linkedin", {"post": "It unlocks nothing. Magical thinking aside."}), [])

    def test_each_thread_post_has_its_own_limit(self):
        issues = lint("x", {"post": "ok", "thread": "fine\n\n---\n\n" + "z" * 281})
        self.assertEqual([i.field for i in issues], ["thread 2"])

    def test_missing_required_field(self):
        self.assertEqual([i.field for i in lint("email", {"to": "a@b.c", "body": "hi"})], ["subject"])

    def test_unknown_channel(self):
        self.assertEqual(lint("myspace", {})[0].level, "error")

    def test_hn_superlatives_warn(self):
        issues = lint("hn", {"title": "Show HN: The fastest log", "url": "https://x.y"})
        self.assertTrue(any("superlative" in i.message for i in issues))


class FirstDrafts(unittest.TestCase):
    def setUp(self):
        self.facts = gather(FakeGitHub(lantern_routes()), "example/lantern")
        emails = [t for t in fitting(self.facts) if t.get("email")]
        self.drafts = first_drafts(self.facts, emails)

    def test_one_draft_per_channel(self):
        self.assertEqual(list(self.drafts), ["hn", "reddit-ClaudeAI", "reddit-ClaudeCode", "reddit-mcp", "reddit-Python",
                                             "x", "bluesky", "linkedin", "producthunt", "email-console-dev"])

    def test_bluesky_counts_graphemes_and_fits(self):
        from surya_distro.channels import grapheme_length
        self.assertEqual(grapheme_length("e\u0301"), 1)
        _, fields = self.drafts["bluesky"]
        self.assertLessEqual(grapheme_length(fields["post"]), 300)
        self.assertTrue(any("300" in str(i) for i in lint("bluesky", {"post": "x" * 301})))

    def test_following_a_link_rule_moves_the_link(self):
        from surya_distro.channels import follow
        rule = {"dim": "link", "value": "no-link", "do": True}
        out = follow("x", dict(self.drafts["x"][1]), [rule], self.facts)
        self.assertNotIn(self.facts.url, out["post"])
        self.assertTrue(out["thread"].startswith("The repo: " + self.facts.url))
        out = follow("linkedin", dict(self.drafts["linkedin"][1]), [rule], self.facts)
        self.assertEqual(out["comment"], "The repo: " + self.facts.url)
        media = follow("x", dict(self.drafts["x"][1]), [{"dim": "media", "value": "video", "do": True}], self.facts)
        self.assertIn("[[fill: what you'll attach: a short demo video]]", media["media"])

    def test_every_draft_passes_once_a_person_fills_it_in(self):
        for draft_id, (channel, fields) in self.drafts.items():
            fields = {k: v for k, v in filled(fields).items() if k != "note"}
            with self.subTest(draft=draft_id):
                self.assertEqual([str(i) for i in lint(channel, fields) if i.level == "error"], [])

    def test_unfilled_drafts_are_held_back(self):
        _, fields = self.drafts["hn"]
        self.assertTrue(any(i.level == "error" for i in lint("hn", fields)))

    def test_hn_title(self):
        title = self.drafts["hn"][1]["title"]
        self.assertEqual(title, "Show HN: Lantern keeps a log of what your AI agent changed")
        self.assertLessEqual(len(title), 80)

    def test_name_is_x_reads_as_name_comma(self):
        routes = lantern_routes()
        routes["repos/example/lantern/readme"]["content"] = __import__("base64").b64encode(
            b"# Lantern\n\nLantern is a log of what your agent changed.\n").decode()
        facts = gather(FakeGitHub(routes), "example/lantern")
        self.assertEqual(first_drafts(facts, [])["hn"][1]["title"], "Show HN: Lantern, a log of what your agent changed")

    def test_drafts_use_only_readme_facts(self):
        text = " ".join(" ".join(f.values()) for _, f in self.drafts.values())
        self.assertIn("pip install lantern-log", text)
        self.assertIn("https://github.com/example/lantern", text)

    def test_python_showcase_format_and_comparison(self):
        body = self.drafts["reddit-Python"][1]["body"]
        for h in ("## What My Project Does", "## Target Audience", "## Comparison"):
            self.assertIn(h, body)
        self.assertIn("hosted dashboard", body)

    def test_subreddits_follow_kinds_and_can_be_overridden(self):
        self.assertEqual([s[0] for s in pick_subreddits(self.facts)], ["ClaudeAI", "ClaudeCode", "mcp", "Python"])
        custom = first_drafts(self.facts, [], ["rust", "Python"])
        self.assertIn("reddit-rust", custom)
        self.assertIn("## Comparison", custom["reddit-Python"][1]["body"])

    def test_email_goes_to_the_listed_address(self):
        _, fields = self.drafts["email-console-dev"]
        self.assertEqual(fields["to"], "hello@console.dev")
        self.assertIn("[[fill: your name]]", fields["body"])

    def test_every_channel_has_rules_and_notes(self):
        for ch in CHANNELS.values():
            self.assertTrue(ch.rules and ch.notes, ch.key)


if __name__ == "__main__":
    unittest.main()
