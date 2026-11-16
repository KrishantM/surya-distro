"""Every network call outside `gh` goes through here.

Reads are GETs. A few reads and scoring calls need a request body, so they go
out with `send()`. `send()` only reaches the endpoints in `SEND_ALLOWED`, and
none of them can publish anything. `tests/test_never_sends.py` holds the
package to that.

Requests carry a generic User-Agent with no email address, and each host gets
a minimum gap between calls. A 429 is retried once, after the wait the server
asks for, up to 30 seconds.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, Optional

from . import __version__

USER_AGENT = f"surya-distro/{__version__} (+https://github.com/KrishantM/surya-distro)"

# (host, path) pairs that may receive a request body. Each one reads or scores.
SEND_ALLOWED = frozenset({
    ("api.typesafe.ai", "/v1/systemone"),        # Jev scoring engine, opt-in
    ("127.0.0.1", "/api/chat"),                  # Ollama on this machine
    ("localhost", "/api/chat"),
    ("www.reddit.com", "/api/v1/access_token"),  # an app-only read token
    ("api.producthunt.com", "/v2/api/graphql"),  # GraphQL reads use a body
})

# Seconds between calls to one host.
GAPS = {
    "hn.algolia.com": 0.3, "hacker-news.firebaseio.com": 0.05, "api.bsky.app": 0.3,
    "public.api.bsky.app": 0.3, "www.reddit.com": 2.0, "oauth.reddit.com": 1.0,
    "api.producthunt.com": 1.0, "api.typesafe.ai": 0.1,
}


class NetError(RuntimeError):
    pass


class Response:
    def __init__(self, status: int, url: str, body: str = "", headers: Optional[dict] = None, raw: Optional[bytes] = None):
        self.status, self.url, self.body = status, url, body
        self.headers = headers or {}
        self.raw = raw if raw is not None else body.encode("utf-8")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def allowed(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return (u.hostname or "", u.path) in SEND_ALLOWED and u.scheme in ("https", "http") and (
        u.scheme == "https" or u.hostname in ("127.0.0.1", "localhost"))


Opener = Callable[[urllib.request.Request, float], Response]


def _open(req: urllib.request.Request, timeout: float) -> Response:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(5_000_000)
            return Response(r.status, r.geturl(), data.decode("utf-8", "replace"), dict(r.headers), raw=data)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(20_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return Response(e.code, req.full_url, body, dict(e.headers or {}))
    except Exception as e:  # DNS, TLS, timeout, refused
        return Response(0, req.full_url, f"{type(e).__name__}: {e}")


class Net:
    def __init__(self, opener: Optional[Opener] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.opener = opener
        self.sleep, self.clock = sleep, clock
        self._last: Dict[str, float] = {}

    def _pace(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).hostname or ""
        gap = GAPS.get(host, 0.2)
        wait = self._last.get(host, -1e9) + gap - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last[host] = self.clock()

    def _do(self, req: urllib.request.Request, timeout: float) -> Response:
        for attempt in (1, 2):
            self._pace(req.full_url)
            r = (self.opener or _open)(req, timeout)
            if r.status != 429 or attempt == 2:
                return r
            try:
                wait = float(r.headers.get("Retry-After") or r.headers.get("retry-after") or 5)
            except ValueError:
                wait = 5.0
            self.sleep(min(max(wait, 1.0), 30.0))
        return r

    def get(self, url: str, headers: Optional[dict] = None, timeout: float = 20) -> Response:
        """GET a public page or API. Network errors come back as status 0, never raised."""
        h = {"User-Agent": USER_AGENT, "Accept": "application/json, */*"}
        h.update(headers or {})
        return self._do(urllib.request.Request(url, headers=h, method="GET"), timeout)

    def send(self, url: str, body: bytes, content_type: str, headers: Optional[dict] = None,
             timeout: float = 120) -> Response:
        """A request with a body, to a read or scoring endpoint in SEND_ALLOWED only."""
        if not allowed(url):
            raise NetError(f"refusing to send to {url}: it isn't a read or scoring endpoint")
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": content_type}
        h.update(headers or {})
        return self._do(urllib.request.Request(url, data=body, headers=h, method="POST"), timeout)

    def send_json(self, url: str, payload, headers: Optional[dict] = None, timeout: float = 120) -> Response:
        return self.send(url, json.dumps(payload).encode("utf-8"), "application/json", headers, timeout)

    def get_bytes(self, url: str, headers: Optional[dict] = None, timeout: float = 30) -> Optional[bytes]:
        """GET a file, such as a README picture. None unless it came back OK."""
        r = self.get(url, {"Accept": "image/*,*/*", **(headers or {})}, timeout)
        return r.raw if r.ok else None

    def get_json(self, url: str, headers: Optional[dict] = None, timeout: float = 20):
        r = self.get(url, headers, timeout)
        return r.json() if r.ok else None


def qs(params: dict) -> str:
    return urllib.parse.urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)
