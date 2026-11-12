import json
import os
import unittest
from unittest import mock

from surya_distro import engines, formats, stats
from surya_distro.net import Net, Response
from tests.fakes import FakeNet


class NetTests(unittest.TestCase):
    def test_paces_each_host(self):
        t = [0.0]
        slept = []
        n = Net(opener=lambda req, timeout: Response(200, req.full_url, "{}"), sleep=slept.append, clock=lambda: t[0])
        n.get("https://hn.algolia.com/a")
        n.get("https://hn.algolia.com/b")
        n.get("https://api.bsky.app/c")
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 0.3)

    def test_retries_a_429_once_with_a_capped_wait(self):
        seen = []

        def opener(req, timeout):
            seen.append(req.full_url)
            return Response(429, req.full_url, "", {"Retry-After": "900"}) if len(seen) == 1 else Response(200, req.full_url, '{"ok": 1}')
        slept = []
        n = Net(opener=opener, sleep=slept.append, clock=lambda: 1e9)
        self.assertEqual(n.get_json("https://example.org/x"), {"ok": 1})
        self.assertEqual(len(seen), 2)
        self.assertIn(30.0, slept)

    def test_sends_carry_the_user_agent(self):
        f = FakeNet(sends={"http://127.0.0.1:11434/api/chat": {"message": {"content": "{}"}}})
        f.send_json("http://127.0.0.1:11434/api/chat", {"a": 1})
        headers = {k.lower(): v for k, v in f.calls[0][3].items()}
        self.assertTrue(headers["user-agent"].startswith("surya-distro/"))


class FormatTests(unittest.TestCase):
    def test_title_shapes(self):
        cases = {"Show HN: Amnesia – audit Claude Code's memory": "name-what", "Show HN: Lantern, a log for agents": "name-what",
                 "I built a memory sync for Claude Code": "i-built", "We just shipped v2": "i-built",
                 "How I use Claude Code": "how-why", "Is MCP dead?": "question", "10 tips for MCP servers": "number",
                 "Claude Code sends your email address to websites": "statement", "": "none"}
        for title, shape in cases.items():
            self.assertEqual(formats.title_shape(title), shape, title)

    def test_link_and_length_and_opening(self):
        self.assertEqual(formats.link_tag("https://a.b then words words words words words"), "link-first")
        self.assertEqual(formats.link_tag("words " * 10 + "https://a.b"), "link-end")
        self.assertEqual(formats.link_tag("no link here"), "no-link")
        self.assertEqual(formats.link_tag("", link_post=True), "link-post")
        self.assertEqual(formats.length_tag("one two three", "short-form"), "short")
        self.assertEqual(formats.length_tag("w " * 100, "long-form"), "medium")
        self.assertEqual(formats.opening("First one. Second one."), "First one.")
        self.assertEqual(formats.media_from_link("https://youtu.be/x"), "video")

    def test_times(self):
        self.assertEqual(formats.iso(formats.parse_time("2026-11-11T22:21:46.479Z")), "2026-11-11T22:21:46Z")
        self.assertEqual(formats.iso(formats.parse_time("2026-11-01T10:00:00+13:00")), "2026-10-31T21:00:00Z")
        self.assertEqual(formats.iso(formats.parse_time(1784566431)), "2026-07-20T16:53:51Z")
        self.assertIsNone(formats.parse_time("not a date"))
        self.assertEqual(formats.when("2026-11-11T22:21:46Z"), ("Wed", "18-24 UTC"))


class StatsTests(unittest.TestCase):
    def test_regression_recovers_a_known_line(self):
        import math
        xs = [{"size": s, "age_days": 100 + i} for i, s in enumerate([1, 5, 10, 50, 100, 500, 1000, 5000, 10, 20, 200, 2000, 3, 30])]
        eng = [math.expm1(1.0 + 0.5 * math.log1p(x["size"])) for x in xs]
        m = stats.fit(xs, eng, ["size", "age_days"])
        self.assertEqual(m["kind"], "regression")
        self.assertAlmostEqual(m["coef"][1], 0.5, delta=0.05)
        self.assertAlmostEqual(stats.score(m, eng[3], xs[3]), 0.0, delta=0.1)
        self.assertGreater(stats.score(m, eng[3] * 20, xs[3]), 2)

    def test_small_samples_use_the_median(self):
        m = stats.fit([{"size": 1}] * 3, [1, 10, 100], ["size"])
        self.assertEqual(m["kind"], "median")
        self.assertEqual(m["features"], [])

    def test_compare_and_confidence(self):
        r = stats.compare([1.0, 1.2, 0.8] * 6, [0.0, -0.2, 0.2] * 10)
        self.assertGreater(r["effect"], 0.9)
        self.assertEqual(stats.confidence(r["n"], r["t"]), "high")
        self.assertEqual(stats.confidence(4, 9.0), "low")
        self.assertEqual(stats.confidence(8, 1.6), "medium")


POSTS = [{"title": "Show HN: Lantern, a log for agents", "text": "I kept losing track of what my agent changed.", "link_post": True},
         {"title": "How to set up MCP servers", "text": "Step one."},
         {"title": "Claude Code vs Codex", "text": "Which is faster?"}]


class EngineTests(unittest.TestCase):
    def test_heuristic(self):
        h = engines.Heuristic(["claude code"])
        out = h.label(POSTS, "Claude Code")
        self.assertEqual([o["kind"] for o in out], ["launch", "tutorial", "comparison"])
        self.assertEqual(out[0]["hook"], "problem")
        self.assertEqual(out[2]["relevant"], 0.8)
        self.assertTrue(all(o["engine"] == "heuristic" for o in out))

    def test_ollama_labels_and_falls_back_per_post(self):
        answer = {"labels": [{"id": "0", "relevant": 0.9, "kind": "launch", "kind_p": 0.8, "hook": "problem", "hook_p": 0.7},
                             {"id": "1", "relevant": 0.2, "kind": "tutorial", "kind_p": 0.9, "hook": "what-it-is", "hook_p": 0.6},
                             {"id": "2", "relevant": 0.5, "kind": "nonsense", "kind_p": 1, "hook": "problem", "hook_p": 1}]}
        f = FakeNet(gets={"http://127.0.0.1:11434/api/tags": {"models": [{"name": "qwen3:8b"}]}},
                    sends={"http://127.0.0.1:11434/api/chat": {"message": {"content": json.dumps(answer)}}})
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "", "SURYA_DISTRO_OLLAMA_MODEL": "", "SURYA_DISTRO_ENGINE": ""}):
            e = engines.choose("auto", f)
        self.assertEqual(e.name, "ollama:qwen3:8b")
        out = e.label(POSTS, "Claude Code")
        self.assertEqual(out[0]["engine"], "ollama:qwen3:8b")
        self.assertEqual(out[1]["relevant"], 0.2)
        self.assertTrue(out[2]["engine"].startswith("heuristic (fallback: ollama"))
        body = json.loads(f.calls[-1][2])
        self.assertEqual((body["model"], body["stream"], body["think"]), ("qwen3:8b", False, False))
        self.assertEqual(body["format"]["required"], ["labels"])

    def test_auto_without_ollama_is_the_heuristic(self):
        with mock.patch.dict(os.environ, {"SURYA_DISTRO_ENGINE": "", "OLLAMA_HOST": ""}):
            self.assertEqual(engines.choose(None, FakeNet()).name, "heuristic")
            with self.assertRaises(engines.EngineError):
                engines.choose("ollama", FakeNet())
            with self.assertRaises(engines.EngineError):
                engines.choose("nope", FakeNet())

    def test_jev_needs_a_key_and_reads_typed_answers(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            with self.assertRaises(engines.EngineError):
                engines.choose("jev", FakeNet())
        reply = {"model": "jev-1.13.0", "usage": {"input_tokens": 9, "output_tokens": 3}, "answers": {
            "relevant": {"type": "noul", "noul": 0.93},
            "kind": {"type": "choice", "choice": "launch", "confidence": 0.7, "probabilities": {"launch": 0.81, "news": 0.19}},
            "hook": {"type": "choice", "choice": "problem", "confidence": 0.6, "probabilities": {"problem": 0.66}}}}
        f = FakeNet(sends={"https://api.typesafe.ai/v1/systemone": reply})
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test-secret"}):
            e = engines.choose("jev", f)
        self.assertNotIn("sk-test", repr(e))
        out = e.label(POSTS[:1], "Claude Code")
        self.assertEqual(out[0], {"relevant": 0.93, "kind": "launch", "kind_p": 0.81, "hook": "problem", "hook_p": 0.66, "engine": "jev:jev-1.13.0"})
        sent = json.loads(f.calls[0][2])
        self.assertEqual(sent["model"], "jev-latest")
        self.assertEqual(sent["questions"]["relevant"]["type"], "noul")
        self.assertEqual(set(sent["questions"]["kind"]["criteria"]), set(engines.KINDS))
        self.assertEqual({k.lower(): v for k, v in f.calls[0][3].items()}["authorization"], "Bearer sk-test-secret")

    def test_jev_bad_key_stops_and_odd_shape_falls_back(self):
        f = FakeNet(sends={"https://api.typesafe.ai/v1/systemone": Response(401, "x", '{"detail": "bad key"}')})
        e = engines.Jev(f, "sk-test")
        with self.assertRaises(engines.EngineError) as cm:
            e.label(POSTS[:1], "x")
        self.assertNotIn("sk-test", str(cm.exception))
        f = FakeNet(sends={"https://api.typesafe.ai/v1/systemone": {"model": "jev", "answers": {}}})
        out = engines.Jev(f, "k").label(POSTS[:1], "x")
        self.assertTrue(out[0]["engine"].startswith("heuristic (fallback: jev"))

    def test_unite(self):
        answer = {"labels": [{"id": "0", "relevant": 0.7, "kind": "launch", "kind_p": 0.6, "hook": "first-person", "hook_p": 0.5}]}
        calls = []

        def runner(args, stdin, timeout):
            calls.append(args)
            return 0, json.dumps(answer) + "\n\n---\nThe local model (qwen3:8b), 900 in and 80 out.\n", ""
        e = engines.Unite("/bin/surya-unite", runner=runner)
        out = e.label(POSTS[:1], "x")
        self.assertEqual(out[0]["hook"], "first-person")
        self.assertEqual(out[0]["engine"], "unite:The local model (qwen3:8b), 900 in and 80 out")
        self.assertEqual(calls[0], ["/bin/surya-unite", "ask", "chore", "--quiet"])
        with mock.patch.dict(os.environ, {"SURYA_UNITE": ""}), mock.patch("shutil.which", return_value=None):
            with self.assertRaises(engines.EngineError):
                engines.choose("unite", FakeNet())


if __name__ == "__main__":
    unittest.main()
