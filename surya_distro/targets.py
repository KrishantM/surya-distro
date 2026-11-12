"""Where to submit a repo, with each place checked live.

The catalog in `data/targets.json` says how each place takes submissions. The
checks here say whether it still does, and whether this repo is already there.
"""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from .github import Fetcher, GitHub, GitHubError, http_get
from .repo import Facts

CATALOG = Path(__file__).resolve().parent / "data" / "targets.json"
STALE_DAYS = 180
MCP_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"

# Most urgent first. The worst finding for a target is its status.
ORDER = ["gone", "archived", "listed", "wait", "needs", "stale", "unchecked", "open"]
LABELS = {
    "open": "open", "listed": "already listed", "wait": "not yet", "needs": "needs work first",
    "stale": "quiet", "archived": "archived", "gone": "gone", "unchecked": "check by hand",
}


@dataclass
class Result:
    id: str
    name: str
    kind: str
    status: str
    detail: str
    how: str
    submit: str
    email: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def fitting(facts: Facts, entries: Optional[List[dict]] = None) -> List[dict]:
    kinds = set(facts.kinds)
    return [t for t in (entries if entries is not None else catalog()["targets"]) if kinds & set(t.get("fits", []))]


def _parse_time(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _norm_url(u: str) -> str:
    u = (u or "").strip().lower().rstrip("/")
    return u[:-4] if u.endswith(".git") else u


class Checker:
    def __init__(self, gh: GitHub, fetch: Fetcher = http_get, now: Optional[datetime] = None):
        self.gh, self.fetch = gh, fetch
        self.now = now or datetime.now(timezone.utc)

    def check(self, facts: Facts, t: dict) -> Result:
        findings: List[tuple] = []
        if t.get("github"):
            findings += self._github_list(facts, t)
        if t.get("check_url"):
            findings += self._page(t)
        if t.get("registry") == "mcp":
            findings += self._mcp_registry(facts)
        if t.get("rules"):
            findings += self._rules(facts, t["rules"])
        if not findings:
            findings = [("unchecked", "No live check is defined for this place.")]
        findings.sort(key=lambda f: ORDER.index(f[0]))
        status = findings[0][0]
        detail = " ".join(d for _, d in findings if d)
        return Result(t["id"], t["name"], t.get("kind", ""), status, detail, t.get("how", ""),
                      t.get("submit", ""), t.get("email", ""))

    def check_all(self, facts: Facts, entries: Optional[List[dict]] = None) -> List[Result]:
        return [self.check(facts, t) for t in fitting(facts, entries)]

    # -- the checks ---------------------------------------------------------
    def _github_list(self, facts: Facts, t: dict) -> List[tuple]:
        try:
            meta = self.gh.api(f"repos/{t['github']}", missing_ok=True)
        except GitHubError as e:
            return [("unchecked", f"GitHub didn't answer: {e}")]
        if meta is None:
            return [("gone", f"{t['github']} no longer exists.")]
        out: List[tuple] = []
        if meta.get("archived"):
            out.append(("archived", f"{t['github']} is archived and takes no changes."))
        pushed = _parse_time(meta.get("pushed_at", ""))
        if pushed and (self.now - pushed).days > STALE_DAYS:
            out.append(("stale", f"No commits for {(self.now - pushed).days} days."))
        readme = self.gh.readme(t["github"])
        if facts.repo.lower() in readme.lower():
            out.append(("listed", f"{facts.repo} is already in the list."))
        if not out:
            out.append(("open", f"Active, last commit {meta.get('pushed_at', '')[:10]}, {meta.get('stargazers_count', 0):,} stars."))
        return out

    def _page(self, t: dict) -> List[tuple]:
        r = self.fetch(t["check_url"])
        if r.status == 200:
            if t.get("expect") and t["expect"].lower() not in r.body.lower():
                return [("unchecked", f"The page loads, but no longer mentions {t['expect']}. Check the route before using it.")]
            moved = f" It now redirects to {r.url}." if _norm_url(r.url) != _norm_url(t["check_url"]) else ""
            return [("open", "The submission page loads." + moved)]
        if r.status in (404, 410):
            return [("gone", f"The submission page returns HTTP {r.status}.")]
        if r.status in (401, 403, 429):
            return [("unchecked", f"The site blocks automated checks (HTTP {r.status}). Open the page yourself.")]
        if r.status == 0:
            return [("unchecked", f"Couldn't reach the page: {r.body[:120]}")]
        return [("unchecked", f"The page returns HTTP {r.status}.")]

    def _mcp_registry(self, facts: Facts) -> List[tuple]:
        name = facts.repo.split("/")[1]
        r = self.fetch(f"{MCP_REGISTRY}?search={urllib.parse.quote(name)}&limit=100")
        if r.status != 200:
            return [("unchecked", f"The registry API returned HTTP {r.status}.")]
        try:
            servers = json.loads(r.body).get("servers", [])
        except ValueError:
            return [("unchecked", "The registry API returned something that isn't JSON.")]
        mine = _norm_url(facts.url)
        for s in servers:
            srv = s.get("server", s)
            if _norm_url((srv.get("repository") or {}).get("url", "")) == mine:
                return [("listed", f"Listed as {srv.get('name', '?')} {srv.get('version', '')}.".replace(" .", "."))]
        return self._package(facts)

    def _package(self, facts: Facts) -> List[tuple]:
        kinds, pkg = set(facts.kinds), facts.package
        if "python" in kinds:
            r = self.fetch(f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json")
            if r.status == 200:
                return [("open", f"Not in the registry yet. A PyPI project named {pkg} exists. If it's yours, add the registry's ownership marker and publish.")]
            if r.status == 404:
                return [("needs", f"Not in the registry. It needs a package first, and {pkg} isn't on PyPI.")]
        if "node" in kinds:
            r = self.fetch(f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@')}")
            if r.status == 200:
                return [("open", f"Not in the registry yet. An npm package named {pkg} exists. If it's yours, add mcpName to package.json and publish.")]
            if r.status == 404:
                return [("needs", f"Not in the registry. It needs a package first, and {pkg} isn't on npm.")]
        return [("needs", "Not in the registry. It needs a published package or a public remote endpoint first.")]

    def _rules(self, facts: Facts, rules: dict) -> List[tuple]:
        created = _parse_time(facts.created)
        min_age, min_stars = rules.get("min_age_days"), rules.get("or_min_stars")
        if not created or not min_age:
            return []
        ready = created + timedelta(days=min_age)
        if self.now >= ready or (min_stars and facts.stars >= min_stars):
            return []
        stars = f", or once it has {min_stars} stars" if min_stars else ""
        return [("wait", f"Eligible from {ready:%Y-%m-%d}, {min_age} days after the repo was created{stars}. It also wants commits after the first day.")]


def render(results: List[Result], checked_at: str) -> str:
    lines = [f"Checked live {checked_at}.", ""]
    lines.append("| Place | Status | What was found | How to submit |")
    lines.append("|---|---|---|---|")
    for r in sorted(results, key=lambda r: (ORDER.index(r.status) if r.status != "open" else -1, r.kind)):
        link = f"[{r.name}]({r.submit})" if r.submit else r.name
        how = r.how + (f" Address: {r.email}." if r.email else "")
        lines.append(f"| {link} | {LABELS.get(r.status, r.status)} | {r.detail} | {how} |")
    return "\n".join(lines)
