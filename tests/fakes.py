"""Fake GitHub, web and network, so tests never touch the network."""
import base64
import json

from surya_distro.github import GitHub
from surya_distro.net import Net, Response

README = """# Lantern

[![CI](https://example.com/badge.svg)](https://example.com)

Lantern keeps a log of what your AI agent changed. It writes one plain file per session, so you can read it without a database.

- **One file per session.** Each session's changes go in a Markdown file you can open anywhere.
- **No server.** It runs on your machine and stores nothing elsewhere.
- **Small.** Python standard library and nothing else.

## Install

You need Python 3.9 or newer.

```bash
pip install lantern-log
```

```bash
lantern start
```

## Compared to other tools

Most agent logs need a hosted dashboard. Lantern writes files you already know how to read.

## Tests

```bash
python3 -m unittest
```

## Limits

- Built and tested on macOS.
- It doesn't redact secrets yet.
"""

PLUGIN = {"name": "lantern", "displayName": "Lantern", "keywords": ["logging", "mcp"]}


def b64(text):
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def repo_meta(full="example/lantern", **kw):
    meta = {
        "full_name": full, "name": full.split("/")[1], "html_url": f"https://github.com/{full}",
        "description": "Keeps a log of what your AI agent changed, one plain file per session.",
        "homepage": "", "license": {"spdx_id": "MIT"}, "stargazers_count": 12, "forks_count": 1,
        "subscribers_count": 2, "created_at": "2026-11-01T10:00:00Z", "pushed_at": "2026-11-10T10:00:00Z",
        "default_branch": "main", "topics": ["ai", "logging"], "archived": False,
    }
    meta.update(kw)
    return meta


def lantern_routes(**overrides):
    routes = {
        "repos/example/lantern": repo_meta(),
        "repos/example/lantern/readme": b64(README),
        "repos/example/lantern/contents": [{"name": n} for n in (".claude-plugin", ".mcp.json", "pyproject.toml", "README.md", "scripts", "lantern")],
        "repos/example/lantern/languages": {"Python": 9000},
        "repos/example/lantern/contents/.claude-plugin/plugin.json": b64(json.dumps(PLUGIN)),
        "repos/example/lantern/contents/pyproject.toml": b64('[project]\nname = "lantern-log"\nversion = "0.3.0"\n'),
        "repos/example/lantern/releases/latest": {"tag_name": "v0.3.0", "name": "0.3.0", "published_at": "2026-11-09T00:00:00Z", "body": "Adds session export."},
    }
    routes.update(overrides)
    return routes


class FakeGitHub(GitHub):
    """Answers `gh api` from a dict of path -> JSON. A missing path is a 404."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        super().__init__(runner=self._run)

    def _run(self, args):
        self.calls.append(args)
        assert args[:4] == ["gh", "api", "--method", "GET"], args
        path = args[-1]
        if path not in self.routes:
            return 1, "", "gh: Not Found (HTTP 404)"
        value = self.routes[path]
        if isinstance(value, Exception):
            return 1, "", str(value)
        return 0, json.dumps(value), ""


class FakeWeb:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for prefix, resp in self.pages.items():
            if url.startswith(prefix):
                return resp
        return Response(404, url)


def ok(url, body=""):
    return Response(200, url, body)


class FakeNet(Net):
    """Answers from tables of URL prefix -> value. A value is a Response, a callable
    taking the request, or JSON. GETs and sends have separate tables, and the
    real allowlist in Net.send still applies."""

    def __init__(self, gets=None, sends=None):
        self.gets, self.sends = dict(gets or {}), dict(sends or {})
        self.calls = []
        self.slept = []
        super().__init__(opener=self._open, sleep=self.slept.append, clock=lambda: 0.0)

    def _open(self, req, timeout):
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url, req.data, dict(req.header_items())))
        table = self.gets if method == "GET" else self.sends
        hits = [k for k in table if url.startswith(k)]
        if not hits:
            return Response(404, url)
        value = table[max(hits, key=len)]
        if callable(value):
            value = value(req)
        if isinstance(value, Response):
            return value
        return Response(200, url, json.dumps(value))

    def urls(self, method="GET"):
        return [c[1] for c in self.calls if c[0] == method]
