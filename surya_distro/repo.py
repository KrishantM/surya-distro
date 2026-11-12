"""Everything a launch kit knows about a repo, gathered in one place."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from .github import GitHub
from .readme import Readme, parse


@dataclass
class Release:
    tag: str
    name: str = ""
    published: str = ""
    body: str = ""
    is_tag_only: bool = False


@dataclass
class Facts:
    repo: str
    url: str
    name: str
    summary: str
    description: str = ""
    homepage: str = ""
    license: str = ""
    stars: int = 0
    forks: int = 0
    created: str = ""
    pushed: str = ""
    default_branch: str = "main"
    topics: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    kinds: List[str] = field(default_factory=list)
    package: str = ""
    release: Optional[Release] = None
    readme: Readme = field(default_factory=Readme)

    def to_json(self) -> dict:
        d = asdict(self)
        d["readme"] = {k: v for k, v in d["readme"].items() if k != "sections"}
        return d


def _pyproject_name(text: str) -> str:
    m = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if m:
        n = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', m.group(1), re.M)
        if n:
            return n.group(1)
    return ""


def detect_kinds(readme_text: str, root: List[str], languages: List[str], topics: List[str],
                 plugin: Optional[dict], repo_name: str) -> List[str]:
    kinds = []
    low = readme_text.lower()
    words = set(t.lower() for t in topics) | set(str(k).lower() for k in (plugin or {}).get("keywords", []))
    if plugin is not None:
        kinds.append("claude-plugin")
    if plugin is not None or "claude code" in low or "claude-code" in words:
        kinds.append("claude-code")
    if ".mcp.json" in root or "server.json" in root or "mcp" in words or "mcp server" in low:
        kinds.append("mcp-server")
    py_dir = repo_name.replace("-", "_") in root
    if {"pyproject.toml", "setup.py", "setup.cfg"} & set(root) or "Python" in languages or py_dir:
        kinds.append("python")
    if "package.json" in root or {"JavaScript", "TypeScript"} & set(languages):
        kinds.append("node")
    if "scripts" in root or "bin" in root or "cmd" in root:
        kinds.append("cli")
    if "self-hosted" in words or "self-hosted" in low:
        kinds.append("self-hosted")
    kinds.append("open-source")
    kinds.append("developer-tool")
    return kinds


def gather(gh: GitHub, repo: str) -> Facts:
    meta = gh.api(f"repos/{repo}")
    text = gh.readme(repo)
    rd = parse(text)
    root = [e.get("name", "") for e in (gh.api(f"repos/{repo}/contents", missing_ok=True) or []) if isinstance(e, dict)]
    languages = list((gh.api(f"repos/{repo}/languages", missing_ok=True) or {}).keys())

    plugin = None
    if ".claude-plugin" in root:
        raw = gh.file(repo, ".claude-plugin/plugin.json")
        if raw:
            try:
                plugin = json.loads(raw)
            except ValueError:
                plugin = {}

    package = ""
    if "pyproject.toml" in root:
        package = _pyproject_name(gh.file(repo, "pyproject.toml") or "")
    elif "package.json" in root:
        try:
            package = json.loads(gh.file(repo, "package.json") or "{}").get("name", "")
        except ValueError:
            pass

    release = None
    rel = gh.api(f"repos/{repo}/releases/latest", missing_ok=True)
    if rel:
        release = Release(rel.get("tag_name", ""), rel.get("name") or "", rel.get("published_at") or "", rel.get("body") or "")
    else:
        tags = gh.api(f"repos/{repo}/tags?per_page=1", missing_ok=True) or []
        if tags:
            release = Release(tags[0].get("name", ""), is_tag_only=True)

    name = rd.title or (plugin or {}).get("displayName") or meta.get("name", repo.split("/")[1])
    topics = meta.get("topics") or []
    return Facts(
        repo=meta.get("full_name", repo),
        url=meta.get("html_url", f"https://github.com/{repo}"),
        name=name,
        summary=rd.summary or (plugin or {}).get("description") or meta.get("description") or "",
        description=meta.get("description") or "",
        homepage=meta.get("homepage") or "",
        license=((meta.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", ""),
        stars=meta.get("stargazers_count", 0),
        forks=meta.get("forks_count", 0),
        created=meta.get("created_at", ""),
        pushed=meta.get("pushed_at", ""),
        default_branch=meta.get("default_branch", "main"),
        topics=topics,
        languages=languages,
        kinds=detect_kinds(text, root, languages, topics, plugin, repo.split("/")[1]),
        package=package or repo.split("/")[1],
        release=release,
        readme=rd,
    )
