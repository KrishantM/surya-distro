import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from surya_distro import compose, create, look, media, study
from surya_distro.kit import Kit
from tests.test_cli import Cli
from tests.test_render import Fake

FACTS = {
    "repo": "example/lantern", "url": "https://github.com/example/lantern", "name": "Lantern",
    "summary": "Lantern keeps a log of what your AI agent changed. It writes one plain file per session.",
    "description": "Keeps a log.", "license": "MIT", "default_branch": "main", "release": {"tag": "v0.3.0"},
    "readme": {"bullets": [{"lead": "One file per session", "text": "One file per session. Each session's changes go in a Markdown file."},
                           {"lead": "No server", "text": "No server. It runs on your machine."}],
               "install": "pip install lantern-log\nlantern start", "screenshots": []},
}


def rule(channel, dim, value, do=True, rid=None):
    return {"id": rid or f"{channel}.{dim}.{value}", "channel": channel, "dim": dim, "value": value, "do": do, "status": "active",
            "text": f"rule {channel} {value}", "confidence": "medium", "evidence": {"n": 9}}


def fmt(channel, name, beats=False, loses=False, n=8):
    return {"id": f"format.{channel}.{name}", "format": name, "n": n, "mean": 0.5 if beats else -0.5, "mean_rest": 0.0,
            "effect": 0.6 if beats else -0.6, "t": 2.0, "confidence": "medium", "beats": beats, "loses": loses, "posts": []}


class Templates(unittest.TestCase):
    def test_fill_escapes_and_raw(self):
        self.assertEqual(compose.fill("<b>{{a}}</b>{{{b}}}", {"a": "x<y", "b": "<i>"}), "<b>x&lt;y</b><i>")

    def test_a_missing_value_is_an_error(self):
        with self.assertRaises(compose.TemplateError):
            compose.fill("{{nope}}", {})

    def test_every_template_fills_from_real_facts(self):
        c = compose.build_content(FACTS, {"problem": "Logs vanish."})
        tokens = look.tokens_for(FACTS)
        for spec in media.SPECS:
            if spec.kind == "image":
                text = compose.image_html(spec, c, tokens)
            else:
                text, secs, _ = compose.video_html(spec, c, tokens)
                self.assertTrue(15 <= secs <= 30, secs)
            self.assertNotIn("{{", text, spec.id)
            self.assertIn("Lantern", text)

    def test_unknown_facts_become_visible_markers(self):
        c = compose.build_content(FACTS)
        text, _, _ = compose.video_html(media.BY_ID["x-video-16x9"], c, look.tokens_for(FACTS))
        self.assertIn("[[fill: the problem this solves", text)

    def test_no_install_in_readme_is_a_marker_not_an_invention(self):
        f = json.loads(json.dumps(FACTS))
        f["readme"]["install"] = ""
        self.assertEqual(compose.build_content(f).install, [compose.INSTALL_FILL])

    def test_voice_lengths_set_scene_lengths_and_the_cap_holds(self):
        c = compose.build_content(FACTS, {"problem": "Logs vanish."})
        base = {s["key"]: s["dur"] for s in compose.plan_scenes(c)}
        long = {s["key"]: s["dur"] for s in compose.plan_scenes(c, {"what": 9.0})}
        self.assertGreater(long["what"], base["what"])
        self.assertLessEqual(sum(s["dur"] for s in compose.plan_scenes(c, {"what": 25.0})), 30.0 + 20)  # features are cut first
        self.assertEqual([k for k, _ in compose.script(c)][:2], ["problem", "what"])

    def test_style_overrides_and_refuses_unsafe_values(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.json"
            p.write_text('{"accent": "#ff6a00"}')
            self.assertEqual(look.resolve(FACTS, p)["accent"], "#ff6a00")
            p.write_text('{"accent": "red; } body {"}')
            with self.assertRaises(look.StyleError):
                look.load_style(p)
            p.write_text('{"nope": "#fff"}')
            with self.assertRaises(look.StyleError):
                look.load_style(p)
        self.assertEqual(look.tokens_for(FACTS), look.tokens_for(FACTS))


class Selection(unittest.TestCase):
    def ids(self, pb, fmts, only=None):
        plans, skipped = create.select(pb, fmts, only)
        return {p.spec.id: p.follows for p in plans}, skipped

    def test_no_evidence_is_default_for_everything(self):
        got, skipped = self.ids(None, {})
        self.assertEqual(len(got), len(media.SPECS))
        self.assertEqual(set(got.values()), {"default"})
        self.assertEqual(skipped, [])

    def test_video_rule_makes_the_x_videos_and_names_the_rule(self):
        pb = {"rules": [rule("x", "media", "video")]}
        got, _ = self.ids(pb, {})
        self.assertEqual(got["x-video-16x9"], "x.media.video")
        self.assertEqual(got["x-video-1x1"], "x.media.video")
        self.assertEqual(got["x-image"], "default")

    def test_text_rule_skips_x_media_and_says_why(self):
        pb = {"rules": [rule("x", "media", "none")]}
        got, skipped = self.ids(pb, {})
        self.assertFalse([i for i in got if i.startswith("x-")])
        self.assertIn("bluesky-image", got)
        self.assertTrue(any(c == "x" and "x.media.none" in why for c, _, why in skipped))

    def test_avoid_rule_skips_that_kind_only(self):
        got, _ = self.ids({"rules": [rule("linkedin", "media", "video", do=False)]}, {})
        self.assertIn("linkedin-image", got)
        self.assertNotIn("linkedin-video-1x1", got)

    def test_finding_is_used_when_there_is_no_rule(self):
        fm = {"bluesky": {"results": [fmt("bluesky", "video", beats=True), fmt("bluesky", "text-only", loses=True)]}}
        got, _ = self.ids(None, fm)
        self.assertEqual(got["bluesky-video-16x9"], "format.bluesky.video")
        self.assertEqual(got["bluesky-image"], "default")

    def test_text_only_winning_skips_media(self):
        fm = {"linkedin": {"results": [fmt("linkedin", "text-only", beats=True)]}}
        got, skipped = self.ids(None, fm)
        self.assertNotIn("linkedin-image", got)
        self.assertTrue(any("format.linkedin.text-only" in why for _, _, why in skipped))

    def test_a_rule_beats_a_finding(self):
        pb = {"rules": [rule("x", "media", "video")]}
        fm = {"x": {"results": [fmt("x", "video", loses=True)]}}
        got, _ = self.ids(pb, fm)
        self.assertEqual(got["x-video-16x9"], "x.media.video")

    def test_only_forces_and_keeps_the_follows(self):
        got, _ = self.ids({"rules": [rule("x", "media", "none")]}, {}, ["x-video-1x1"])
        self.assertEqual(got, {"x-video-1x1": "x.media.none"})

    def test_readme_hero_follows_a_readme_media_rule(self):
        got, _ = self.ids({"rules": [rule("readme", "media", "video")]}, {})
        self.assertEqual(got["readme-hero"], "readme.media.video")


def post(src, i, fmt_, score, kind="launch", link=""):
    return {"source": src, "id": str(i), "url": f"https://x/{i}", "relevant": 0.9, "score": score, "link": link, "text": "",
            "tags": {"media": fmt_, "kind": kind}}


class Formats(unittest.TestCase):
    def test_formats_beat_baseline_with_n_and_confidence(self):
        posts = [post("bluesky", i, "video", 1.5 + i * 0.01) for i in range(6)]
        posts += [post("bluesky", 10 + i, "none", -0.3 + (i % 3) * 0.1) for i in range(12)]
        posts += [post("bluesky", 30 + i, "video", 0.9, link="https://x/a.gif") for i in range(3)]
        fm = study.media_formats({"at": "2026-11-12T00:00:00Z", "posts": posts})
        rows = {r["format"]: r for r in fm["bluesky"]["results"]}
        self.assertTrue(rows["video"]["beats"])
        self.assertEqual(rows["video"]["n"], 6)
        self.assertIn(rows["video"]["confidence"], ("medium", "high"))
        self.assertTrue(rows["text-only"]["loses"])
        self.assertEqual(rows["gif"]["n"], 3)
        self.assertEqual(rows["video"]["id"], "format.bluesky.video")
        self.assertEqual(fm["x"]["source"], "bluesky")   # X has no read API, so Bluesky stands in

    def test_too_few_posts_gives_nothing(self):
        self.assertEqual(study.media_formats({"posts": [post("hn", 1, "video", 1)]}), {})
        self.assertEqual(study.media_formats(None), {})

    def test_readme_screenshot_is_its_own_format(self):
        posts = [post("github", i, "image", 1.0) for i in range(4)] + [post("github", 9 + i, "none", -0.5) for i in range(8)]
        rows = {r["format"] for r in study.media_formats({"posts": posts})["readme"]["results"]}
        self.assertEqual(rows, {"screenshot", "text-only"})


class Checks(Fake):
    def asset(self, **kw):
        a = {"id": "x-image", "channel": "x", "kind": "image", "path": str(self.dir / "x-image.png"), "html": str(self.dir / "x-image.html"),
             "width": 1600, "height": 900, "template": "card", "follows": "default", "status": "draft", "sha256": "", "created": "t"}
        a.update(kw)
        return a

    def render(self, a):
        from surya_distro import render
        Path(a["html"]).write_text("<html>ok</html>")
        render.render_png(Path(a["html"]), Path(a["path"]), a["width"], a["height"], 1.0, str(self.chrome))

    def test_good_asset_passes(self):
        a = self.asset()
        self.render(a)
        self.assertEqual(media.issues(a), [])

    def test_fill_marker_blocks(self):
        a = self.asset()
        self.render(a)
        Path(a["html"]).write_text("<p>[[fill: the problem]]</p>")
        self.assertTrue(any("fill: the problem" in i.message for i in media.approvable(a)))

    def test_wrong_size_for_channel(self):
        a = self.asset(width=1200, height=630)
        self.render(a)
        self.assertTrue(any("isn't a size for x" in i.message for i in media.issues(a)))

    def test_file_that_is_not_the_manifest_size(self):
        a = self.asset()
        self.render(a)
        a["width"], a["height"] = 1600, 900
        Path(a["path"]).write_bytes(Path(a["path"]).read_bytes())
        from surya_distro import render
        render.render_png(Path(a["html"]), Path(a["path"]), 800, 450, 1.0, str(self.chrome))
        self.assertTrue(any("the file is 800x450" in i.message for i in media.issues(a)))

    def test_video_over_the_channel_limit(self):
        for ch, secs, bad in (("x", 141, True), ("x", 140, False), ("bluesky", 181, True), ("linkedin", 601, True), ("linkedin", 600, False)):
            size = media.allowed_sizes(ch, "video")[0]
            a = self.asset(id="v", channel=ch, kind="video", path=str(self.dir / "v.mp4"), width=size[0], height=size[1], seconds=secs)
            Path(a["path"]).write_bytes(b"x")
            Path(a["html"]).write_text("<html></html>")
            self.assertEqual(any("limit" in i.message for i in media.issues(a)), bad, (ch, secs))

    def test_missing_file_says_to_install_chrome(self):
        a = self.asset()
        Path(a["html"]).write_text("x")
        self.assertTrue(any("not rendered yet" in i.message for i in media.issues(a)))

    def test_limits_have_sources(self):
        lim = media.limits()
        self.assertEqual({k: v["max_seconds"] for k, v in lim.items()}, {"x": 140, "linkedin": 600, "bluesky": 180})
        self.assertTrue(all(v["source"] for v in lim.values()))


class Flow(Cli):
    """create, check, approve and the draft's hash, end to end, with a fake browser."""

    def setUp(self):
        super().setUp()
        self.fake = Fake()
        self.fake.setUp()
        self.addCleanup(self.fake.tearDown)
        env = mock.patch.dict(os.environ, {"SURYA_DISTRO_CHROME": str(self.fake.chrome), "FAKE_LOG": str(self.fake.log)})
        env.start()
        self.addCleanup(env.stop)
        self.run_cli("draft", "example/lantern", "--offline")

    def test_create_check_approve_and_reset(self):
        code, out, err = self.run_cli("create", "example/lantern", "--only", "x-image,og-card,producthunt-thumb")
        self.assertEqual(code, 0, err)
        m = json.loads((self.kit() / "kit/media/media.json").read_text())
        self.assertEqual(m["version"], 1)
        a = {x["id"]: x for x in m["assets"]}
        self.assertEqual(set(a), {"x-image", "og-card", "producthunt-thumb"})
        for x in a.values():
            self.assertEqual(set(x) - {"seconds"}, {"id", "channel", "kind", "path", "html", "width", "height", "template", "follows", "status", "sha256", "created"})
            self.assertEqual(x["status"], "draft")
            self.assertTrue(Path(x["path"]).exists() and Path(x["html"]).exists())
            self.assertEqual(len(x["sha256"]), 64)
        self.assertEqual((a["x-image"]["width"], a["x-image"]["height"]), (1600, 900))
        self.assertTrue(str(self.kit() / "kit/media/x-image.png") == a["x-image"]["path"])
        # drafts list their media, and the x draft's media field is filled in
        x = (self.kit() / "kit/drafts/x.md").read_text()
        self.assertIn("media: x-image", x)
        self.assertIn("x-image", (self.kit() / "kit/review.md").read_text())
        code, out, _ = self.run_cli("media", "example/lantern")
        self.assertIn("x-image", out)
        # check: a marker-free image is fine
        code, out, _ = self.run_cli("check", "example/lantern", "media:x-image")
        self.assertEqual(code, 0, out)
        # approval is terminal only, and needs the id
        code, _, err = self.run_cli("approve", "example/lantern", "media:x-image")
        self.assertIn("own terminal", err)
        code, _, err = self.run_cli("approve", "example/lantern", "media:x-image", tty=True, answer="no")
        self.assertEqual(code, 2)
        code, out, err = self.run_cli("approve", "example/lantern", "media:x-image", tty=True, answer="x-image")
        self.assertEqual(code, 0, err)
        m = json.loads((self.kit() / "kit/media/media.json").read_text())
        self.assertEqual({x["id"]: x["status"] for x in m["assets"]}["x-image"], "approved")
        # a draft's approval covers its media file hash
        d = Kit(Path(self.home), "example/lantern").draft("x")
        before = d.digest()
        p = Path(a["x-image"]["path"])
        p.write_bytes(p.read_bytes() + b"0")
        self.assertNotEqual(Kit(Path(self.home), "example/lantern").draft("x").digest(), before)
        # a changed file is a draft again
        code, out, _ = self.run_cli("media", "example/lantern")
        self.assertIn("changed after it was approved", out)
        m = json.loads((self.kit() / "kit/media/media.json").read_text())
        self.assertEqual({x["id"]: x["status"] for x in m["assets"]}["x-image"], "draft")

    def test_rendering_again_resets_an_approved_draft(self):
        self.run_cli("create", "example/lantern", "--only", "x-image")
        kit = Kit(Path(self.home), "example/lantern")
        p = self.kit() / "kit/drafts/x.md"
        p.write_text(re.sub(r"\[\[fill:[^\]]*\]\]", "A real number.", p.read_text()))
        code, out, err = self.run_cli("approve", "example/lantern", "x", tty=True, answer="x")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(kit.draft("x").state, "approved")
        self.run_cli("approve", "example/lantern", "media:x-image", tty=True, answer="x-image")
        # the same render again is the same bytes, so the approvals stand
        self.run_cli("create", "example/lantern", "--only", "x-image")
        self.assertEqual(kit.draft("x").state, "approved")
        # a render that comes out different takes both approvals away
        style = self.kit() / "style.json"
        style.write_text('{"accent": "#33aa66", "bg": "#101820"}')
        code, out, err = self.run_cli("create", "example/lantern", "--only", "x-image", "--style", str(style))
        self.assertEqual(code, 0, err)
        self.assertEqual(kit.draft("x").meta["status"], "draft")
        self.assertIn("took the approval away", out)
        m = json.loads((self.kit() / "kit/media/media.json").read_text())
        self.assertEqual(m["assets"][0]["status"], "draft")

    def test_fill_marker_blocks_check_and_approval(self):
        code, out, err = self.run_cli("create", "example/lantern", "--only", "x-video-16x9")
        self.assertEqual(code, 0, err)
        self.assertIn("[[fill: the problem this solves", out)
        code, out, _ = self.run_cli("check", "example/lantern", "media:x-video-16x9")
        self.assertEqual(code, 1)
        self.assertIn("shows [[fill:", out)
        code, _, err = self.run_cli("approve", "example/lantern", "media:x-video-16x9", tty=True, answer="x-video-16x9")
        self.assertEqual(code, 2)
        self.assertIn("problem(s) to fix", err)
        # supplying the words clears it, and the words are kept for next time
        self.run_cli("create", "example/lantern", "--only", "x-video-16x9", "--set", "problem=Logs vanish.")
        self.assertNotIn("[[fill", (self.kit() / "kit/media/x-video-16x9.html").read_text())
        self.assertEqual(json.loads((self.kit() / "kit/media/inputs.json").read_text())["set"]["problem"], "Logs vanish.")

    def test_no_chrome_still_writes_the_html_and_says_what_to_install(self):
        with mock.patch.dict(os.environ, {"SURYA_DISTRO_CHROME": "/nope/chrome"}), \
                mock.patch("surya_distro.render.shutil.which", return_value=None):
            code, out, err = self.run_cli("create", "example/lantern", "--only", "og-card")
        self.assertEqual(code, 0, err)
        self.assertIn("SURYA_DISTRO_CHROME", out)
        self.assertTrue((self.kit() / "kit/media/og-card.html").exists())
        self.assertFalse((self.kit() / "kit/media/og-card.png").exists())
        code, out, _ = self.run_cli("check", "example/lantern", "media:og-card")
        self.assertEqual(code, 1)
        self.assertIn("not rendered yet", out)

    def test_bad_asset_id_and_style(self):
        code, _, err = self.run_cli("create", "example/lantern", "--only", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no such asset", err)
        code, out, err = self.run_cli("create", "example/lantern", "--only", "og-card", "--style", str(self.kit() / "missing.json"))
        self.assertEqual(code, 2)

    def test_create_needs_a_kit(self):
        code, _, err = self.run_cli("create", "example/other")
        self.assertEqual(code, 2)
        self.assertIn("surya-distro draft", err)


for _n in dir(Cli):                      # Flow reuses Cli's setUp only, not its tests
    if _n.startswith("test_") and _n not in Flow.__dict__:
        setattr(Flow, _n, None)


if __name__ == "__main__":
    unittest.main()
