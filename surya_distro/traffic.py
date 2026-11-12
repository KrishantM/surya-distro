"""Keep GitHub traffic past the 14 days GitHub holds it.

One JSON file per repo. Each fetch merges the latest 14-day window into it, so
running once a day keeps a full history. `surya-distro measure` does the fetch. Reading traffic needs push access to
the repo, which the signed-in `gh` user has for their own repos.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .github import GitHub, GitHubError


def home(override: Optional[str] = None) -> Path:
    return Path(override or os.environ.get("SURYA_DISTRO_HOME") or Path.home() / ".surya-distro").expanduser()


def repo_dir(base: Path, repo: str) -> Path:
    owner, name = repo.split("/")
    return base / owner / name


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(base: Path, repo: str) -> dict:
    p = repo_dir(base, repo) / "traffic.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"repo": repo, "days": {}, "stars": {}, "referrers": {}, "paths": {}, "fetches": []}


def merge(store: dict, views: dict, clones: dict, referrers: list, paths: list, meta: dict, today: str) -> dict:
    days: Dict[str, dict] = store.setdefault("days", {})
    for kind, data in (("views", views), ("clones", clones)):
        for row in (data or {}).get(kind, []):
            day = row.get("timestamp", "")[:10]
            if not day:
                continue
            d = days.setdefault(day, {})
            # The newest fetch knows most about a day, so it wins.
            d[kind] = row.get("count", 0)
            d[kind[:-1] + "_uniques"] = row.get("uniques", 0)
    store["days"] = dict(sorted(days.items()))
    store.setdefault("stars", {})[today] = {
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "watchers": meta.get("subscribers_count", 0),
    }
    store.setdefault("referrers", {})[today] = referrers or []
    store.setdefault("paths", {})[today] = paths or []
    fetches = store.setdefault("fetches", [])
    if today not in fetches:
        fetches.append(today)
    return store


def fetch(gh: GitHub, repo: str, base: Path, now: Optional[datetime] = None) -> dict:
    try:
        views = gh.api(f"repos/{repo}/traffic/views")
        clones = gh.api(f"repos/{repo}/traffic/clones")
        referrers = gh.api(f"repos/{repo}/traffic/popular/referrers")
        paths = gh.api(f"repos/{repo}/traffic/popular/paths")
    except GitHubError as e:
        if "403" in str(e) or "push access" in str(e).lower():
            raise GitHubError(f"{repo}: reading traffic needs push access to the repo. {e}")
        raise
    meta = gh.api(f"repos/{repo}")
    store = merge(load(base, repo), views, clones, referrers, paths, meta, _today(now))
    write_json(repo_dir(base, repo) / "traffic.json", store)
    watch(base, repo)
    return store


def watched(base: Path) -> List[str]:
    p = base / "watch.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def watch(base: Path, repo: str) -> None:
    repos = watched(base)
    if repo not in repos:
        write_json(base / "watch.json", sorted(repos + [repo]))


def post_dates(base: Path, repo: str) -> Dict[str, List[str]]:
    """Day -> drafts posted that day, read from the repo's kit."""
    from .kit import Kit  # local import: kit imports this module
    out: Dict[str, List[str]] = {}
    kit = Kit(base, repo)
    if not kit.drafts_dir.exists():
        return out
    for d in kit.drafts():
        at = d.meta.get("posted_at", "")
        if at:
            out.setdefault(at[:10], []).append(d.id)
    return out


def report(base: Path, repo: str, days: int = 30) -> str:
    store = load(base, repo)
    if not store.get("fetches"):
        return f"No traffic stored for {repo} yet. Run: surya-distro measure {repo}"
    posts = post_dates(base, repo)
    all_days = sorted(set(store["days"]) | set(store.get("stars", {})) | set(posts))[-days:]
    lines = [f"{repo}: traffic kept since {min(store['days'] or store['fetches'])}, fetched on {len(store['fetches'])} days", ""]
    lines.append(f"{'day':<11} {'views':>6} {'uniq':>5} {'clones':>7} {'uniq':>5} {'stars':>6}  posted")
    for day in all_days:
        d = store["days"].get(day, {})
        s = store.get("stars", {}).get(day, {})
        stars = str(s.get("stars", "")) if s else ""
        lines.append(f"{day:<11} {d.get('views', 0):>6} {d.get('view_uniques', 0):>5} {d.get('clones', 0):>7} "
                     f"{d.get('clone_uniques', 0):>5} {stars:>6}  {', '.join(posts.get(day, []))}")
    total_v = sum(d.get("views", 0) for d in store["days"].values())
    total_c = sum(d.get("clones", 0) for d in store["days"].values())
    lines += ["", f"All stored days: {total_v} views, {total_c} clones."]
    latest = store["fetches"][-1]
    refs = store.get("referrers", {}).get(latest, [])
    if refs:
        lines.append(f"Top referrers in GitHub's last 14 days (as of {latest}):")
        for r in refs[:5]:
            lines.append(f"  {r.get('referrer', '?'):<28} {r.get('count', 0):>5} views, {r.get('uniques', 0)} unique")
    return "\n".join(lines)


def schedule_text(base: Path) -> str:
    """An hourly job to install by hand. Nothing is installed from here."""
    py = sys.executable
    gh = shutil.which("gh") or "/usr/local/bin/gh"
    path = ":".join(dict.fromkeys([os.path.dirname(gh), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
    pkg_root = str(Path(__file__).resolve().parent.parent)
    log = base / "measure.log"
    label = "com.surya-distro.measure"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{py}</string><string>-m</string><string>surya_distro</string><string>measure</string><string>--due</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string><key>PYTHONPATH</key><string>{pkg_root}</string><key>SURYA_DISTRO_HOME</key><string>{base}</string></dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>"""
    cron = f"17 * * * * PATH={path} PYTHONPATH={pkg_root} SURYA_DISTRO_HOME={base} {py} -m surya_distro measure --due >> {log} 2>&1"
    return "\n".join([
        "Run `measure --due` every hour, for every repo in the watch list. Nothing below has been installed.",
        "Each run reads posts that reached their 1 hour, 24 hour or 7 day mark, and fetches GitHub traffic once a day.",
        "",
        f"macOS: save this as ~/Library/LaunchAgents/{label}.plist, then run",
        f"  launchctl load ~/Library/LaunchAgents/{label}.plist",
        "",
        plist,
        "",
        "Linux: add this line with `crontab -e`:",
        "",
        cron,
        "",
        "A missed day of traffic costs nothing while the gap is under 14 days. Each fetch fills in the window GitHub still holds.",
    ])
