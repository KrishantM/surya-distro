"""Surya Distro reads, scores and records. It must never post, submit or send."""
import re
import unittest
from pathlib import Path

from surya_distro import net, sources
from surya_distro.net import Net, NetError

PKG = Path(__file__).resolve().parent.parent / "surya_distro"
if not PKG.exists():  # running against an installed copy
    import surya_distro
    PKG = Path(surya_distro.__file__).resolve().parent

# Endpoints that publish, on any platform the package knows. None may appear in the code.
PUBLISHING = ("createRecord", "com.atproto.repo", "/api/submit", "/api/comment", "statuses/update", "/2/tweets",
              "ugcPosts", "/rest/posts", "mutation", "smtp", "sendmail", "api/v1/compose")


class NeverSends(unittest.TestCase):
    def sources(self):
        files = sorted(PKG.glob("*.py"))
        self.assertTrue(files)
        return {p.name: p.read_text(encoding="utf-8") for p in files}

    def test_no_sending_libraries(self):
        for name, src in self.sources().items():
            for lib in ("smtplib", "http.client", "requests", "httpx", "aiohttp", "webbrowser", "socket"):
                self.assertNotRegex(src, rf"^\s*(import|from)\s+{re.escape(lib)}\b", f"{name} imports {lib}")

    def test_only_net_opens_urls(self):
        for name, src in self.sources().items():
            if name != "net.py":
                self.assertNotIn("urlopen(", src, name)
                self.assertNotIn("urllib.request", src, name)
        self.assertEqual(self.sources()["net.py"].count("urlopen("), 1)

    def test_write_verbs_only_in_the_guarded_send(self):
        srcs = self.sources()
        for name, src in srcs.items():
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                if name == "net.py" and verb == "POST":
                    continue
                self.assertNotRegex(src, rf"[\"']{verb}[\"']", f"{name} uses {verb}")
        n = srcs["net.py"]
        self.assertEqual(len(re.findall(r"[\"']POST[\"']", n)), 1)
        send = n[n.index("    def send("):n.index("    def send_json(")]
        self.assertIn('method="POST"', send)
        self.assertLess(send.index("if not allowed(url)"), send.index('method="POST"'))

    def test_no_publishing_endpoints(self):
        for name, src in self.sources().items():
            for ep in PUBLISHING:
                self.assertNotIn(ep, src, f"{name} mentions {ep}")

    def test_the_allowlist_is_reads_and_scoring_only(self):
        self.assertEqual(net.SEND_ALLOWED, frozenset({
            ("api.typesafe.ai", "/v1/systemone"), ("127.0.0.1", "/api/chat"), ("localhost", "/api/chat"),
            ("www.reddit.com", "/api/v1/access_token"), ("api.producthunt.com", "/v2/api/graphql")}))
        self.assertTrue(sources.PH_QUERY.lstrip().startswith("query"))
        for url in ("https://bsky.social/xrpc/com.atproto.repo.createRecord", "https://oauth.reddit.com/api/submit",
                    "https://api.x.com/2/tweets", "http://api.typesafe.ai/v1/systemone", "https://www.reddit.com/api/v1/access_token/x",
                    "http://example.com/api/chat", "https://api.producthunt.com/v2/api/graphql/../x"):
            self.assertFalse(net.allowed(url), url)

    def test_send_refuses_before_any_request(self):
        def opener(req, timeout):
            raise AssertionError("no request should be made")
        n = Net(opener=opener, sleep=lambda s: None)
        with self.assertRaises(NetError):
            n.send("https://oauth.reddit.com/api/submit", b"x", "text/plain")

    def test_github_calls_are_get_only(self):
        src = self.sources()["github.py"]
        self.assertIn('"--method", "GET"', src)
        self.assertEqual(src.count("self.runner("), 1)

    def test_subprocesses_are_gh_unite_and_local_rendering_only(self):
        for name, src in self.sources().items():
            if "subprocess.run(" in src or "subprocess.Popen(" in src:
                self.assertIn(name, ("github.py", "engines.py", "render.py"), name)
        # render.py runs a local browser, ffmpeg, ffprobe and a text-to-speech command. It makes no network call of its own.
        render = self.sources()["render.py"]
        for word in ("import urllib", "import http", "import socket", "import requests", "import ssl"):
            self.assertNotIn(word, render)
        eng = self.sources()["engines.py"]
        self.assertIn('[self.exe, "ask", "chore", "--quiet"]', eng)
        self.assertNotIn("--write", eng)

    def test_user_agent_has_no_email(self):
        self.assertNotIn("@", net.USER_AGENT)
        self.assertTrue(net.USER_AGENT.startswith("surya-distro/"))


if __name__ == "__main__":
    unittest.main()
