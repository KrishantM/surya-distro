"""Read-only access to GitHub through the official CLI.

Everything here reads. `gh api` always runs with `--method GET`, and
`tests/test_never_sends.py` checks that the package stays that way.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
from typing import Callable, Optional, Tuple

from .net import Net, Response, USER_AGENT  # noqa: F401  (Response is re-exported)
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class GitHubError(RuntimeError):
    pass


def parse_repo(text: str) -> str:
    """Accept `owner/name` or a github.com URL and return `owner/name`."""
    t = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+/[^/\s#?]+)", t)
    if m:
        t = m.group(1)
    t = t.rstrip("/")
    if t.endswith(".git"):
        t = t[:-4]
    if not REPO_RE.match(t):
        raise GitHubError(f"not a GitHub repo: {text!r}. Use owner/name.")
    return t


Runner = Callable[[list], Tuple[int, str, str]]


def _run(args: list) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise GitHubError("The GitHub CLI isn't installed. Get it from https://cli.github.com and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        return 1, "", "timed out after 60 seconds"
    return p.returncode, p.stdout, p.stderr


class GitHub:
    """`gh api` for GET requests only."""

    def __init__(self, runner: Optional[Runner] = None):
        self.runner = runner or _run

    def api(self, path: str, missing_ok: bool = False):
        code, out, err = self.runner(["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json", path])
        if code != 0:
            msg = (err or out).strip()
            if missing_ok and ("404" in msg or "Not Found" in msg):
                return None
            raise GitHubError(f"gh api {path}: {msg[:300]}")
        return json.loads(out) if out.strip() else None

    def file(self, repo: str, path: str) -> Optional[str]:
        data = self.api(f"repos/{repo}/contents/{path}", missing_ok=True)
        if not isinstance(data, dict) or "content" not in data:
            return None
        return base64.b64decode(data["content"]).decode("utf-8", "replace")

    def readme(self, repo: str) -> str:
        data = self.api(f"repos/{repo}/readme", missing_ok=True)
        if not isinstance(data, dict) or "content" not in data:
            return ""
        return base64.b64decode(data["content"]).decode("utf-8", "replace")


Fetcher = Callable[[str], Response]

_default_net = Net()


def http_get(url: str) -> Response:
    """GET a public page. Network errors come back as status 0, never raised."""
    return _default_net.get(url, {"Accept": "*/*"})
