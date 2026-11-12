"""Measure your posts, and join them to the GitHub traffic that followed.

    <home>/<owner>/<repo>/posts.json    one entry per posted draft, with its snapshots

Each posted draft is read at about 1 hour, 24 hours and 7 days, where the
platform has a public API: Hacker News, Bluesky, Reddit (with an app key) and
Product Hunt (with a token). X and LinkedIn have no free read API, so you type
their numbers in: `measure <repo> <draft> --likes 30 --views 1200`.

A run outside those windows still stores a snapshot, with its age, so nothing
is lost by measuring late.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import traffic
from .formats import iso, parse_time
from .net import Net, qs
from .sources import BSKY, REDDIT_KEY_NOTE, _reddit_token
from .traffic import repo_dir, write_json

# name, hours, earliest, latest
CHECKPOINTS = (("1h", 1, 0.5, 4), ("24h", 24, 16, 48), ("7d", 168, 120, 264))
REF_HOSTS = {"hn": ("news.ycombinator.com",), "reddit": ("reddit.com",), "bluesky": ("bsky.app",),
             "x": ("t.co", "x.com", "twitter.com"), "linkedin": ("linkedin.com", "lnkd.in"), "producthunt": ("producthunt.com",)}
MANUAL = ("x", "linkedin")


class MeasureError(RuntimeError):
    pass


def platform(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for name, hosts in (("hn", ("news.ycombinator.com",)), ("reddit", ("reddit.com", "redd.it")), ("bluesky", ("bsky.app",)),
                        ("producthunt", ("producthunt.com",)), ("x", ("x.com", "twitter.com")), ("linkedin", ("linkedin.com",))):
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return "other"


def engagement(plat: str, m: dict) -> float:
    if plat == "hn":
        return float(m.get("points", 0))
    if plat == "reddit":
        return float(m.get("score", 0))
    if plat == "producthunt":
        return float(m.get("votes", 0))
    return float(m.get("likes", 0) + m.get("reposts", 0) + m.get("replies", 0) + m.get("quotes", 0))


def fetch(plat: str, url: str, net: Net) -> Tuple[dict, dict]:
    """(metrics, author) for one post, from the platform's public API."""
    if plat == "hn":
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        item_id = (q.get("id") or [""])[0]
        if not item_id.isdigit():
            raise MeasureError(f"no item id in {url}")
        item = net.get_json(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if not item:
            raise MeasureError("Hacker News didn't return the item")
        user = net.get_json(f"https://hacker-news.firebaseio.com/v0/user/{urllib.parse.quote(item.get('by', ''))}.json") or {}
        return ({"points": int(item.get("score") or 0), "comments": int(item.get("descendants") or 0)},
                {"name": item.get("by", ""), "size": user.get("karma"), "created": iso(parse_time(user.get("created")))})
    if plat == "bluesky":
        m = re.match(r"https://bsky\.app/profile/([^/]+)/post/([^/?#]+)", url)
        if not m:
            raise MeasureError(f"not a Bluesky post link: {url}")
        prof = net.get_json(BSKY + "app.bsky.actor.getProfile?" + qs({"actor": m.group(1)}))
        if not prof:
            raise MeasureError("Bluesky didn't return the profile")
        data = net.get_json(BSKY + "app.bsky.feed.getPosts?" + qs({"uris": f"at://{prof['did']}/app.bsky.feed.post/{m.group(2)}"})) or {}
        posts = data.get("posts") or []
        if not posts:
            raise MeasureError("Bluesky didn't return the post")
        p = posts[0]
        return ({"likes": int(p.get("likeCount") or 0), "reposts": int(p.get("repostCount") or 0),
                 "replies": int(p.get("replyCount") or 0), "quotes": int(p.get("quoteCount") or 0)},
                {"name": prof.get("handle", ""), "size": prof.get("followersCount"), "created": prof.get("createdAt", "")})
    if plat == "reddit":
        m = re.search(r"/comments/([a-z0-9]+)", url)
        if not m:
            raise MeasureError(f"no post id in {url}")
        token = _reddit_token(net)
        if not token:
            raise MeasureError(REDDIT_KEY_NOTE)
        h = {"Authorization": f"Bearer {token}"}
        d = (((net.get_json(f"https://oauth.reddit.com/api/info?id=t3_{m.group(1)}", h) or {}).get("data") or {}).get("children") or [{}])[0].get("data") or {}
        if not d:
            raise MeasureError("Reddit didn't return the post")
        u = (net.get_json(f"https://oauth.reddit.com/user/{urllib.parse.quote(d.get('author', ''))}/about", h) or {}).get("data") or {}
        return ({"score": int(d.get("score") or 0), "comments": int(d.get("num_comments") or 0)},
                {"name": d.get("author", ""), "size": u.get("total_karma"), "created": iso(parse_time(u.get("created_utc")))})
    if plat in MANUAL:
        raise MeasureError(f"{plat} has no free read API. Type the numbers in: measure <repo> <draft> --likes N --replies N --reposts N --views N")
    raise MeasureError(f"can't measure posts on {plat} yet")


class Posts:
    def __init__(self, base: Path, repo: str):
        self.repo = repo
        self.path = repo_dir(base, repo) / "posts.json"
        try:
            self.data: Dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        write_json(self.path, self.data)

    def entry(self, d) -> dict:
        url = d.meta.get("post_url", "")
        rec = self.data.setdefault(d.id, {"draft": d.id, "channel": d.channel, "snapshots": []})
        rec.update(url=url, platform=platform(url) if url else d.channel, posted_at=d.meta.get("posted_at", ""))
        return rec


def _checkpoint(age_h: float, taken: List[str]) -> Optional[str]:
    for name, _h, lo, hi in CHECKPOINTS:
        if lo <= age_h <= hi and name not in taken:
            return name
    return None


def snapshot(rec: dict, metrics: dict, author: dict, now: datetime, source: str = "api") -> dict:
    posted = parse_time(rec.get("posted_at")) or now
    age_h = round((now - posted).total_seconds() / 3600, 1)
    taken = [s["checkpoint"] for s in rec["snapshots"] if s.get("checkpoint")]
    snap = {"at": iso(now), "age_hours": age_h, "checkpoint": _checkpoint(age_h, taken), "metrics": metrics,
            "engagement": engagement(rec.get("platform", ""), metrics), "author": author, "source": source}
    rec["snapshots"].append(snap)
    if author:
        rec["author"] = author
    return snap


def due(rec: dict, now: datetime) -> bool:
    posted = parse_time(rec.get("posted_at"))
    if not posted:
        return False
    age_h = (now - posted).total_seconds() / 3600
    taken = [s["checkpoint"] for s in rec["snapshots"] if s.get("checkpoint")]
    return _checkpoint(age_h, taken) is not None


def best(rec: dict) -> Optional[dict]:
    """The snapshot that says most: 7 days, then 24 hours, then 1 hour, then the latest."""
    snaps = rec.get("snapshots") or []
    for name in ("7d", "24h", "1h"):
        for s in snaps:
            if s.get("checkpoint") == name:
                return s
    return snaps[-1] if snaps else None


def run(base: Path, kit, net: Net, now: Optional[datetime] = None, due_only: bool = False) -> List[str]:
    now = now or datetime.now(timezone.utc)
    store = Posts(base, kit.repo)
    lines = []
    for d in kit.drafts():
        if d.meta.get("status") != "posted" or not d.meta.get("post_url"):
            continue
        rec = store.entry(d)
        if rec["platform"] in MANUAL:
            if not due_only or due(rec, now):
                lines.append(f"{d.id}: {rec['platform']} has no free read API. Type the numbers in: "
                             f"surya-distro measure {kit.repo} {d.id} --likes N --replies N --reposts N --views N")
            continue
        if due_only and not due(rec, now):
            continue
        try:
            metrics, author = fetch(rec["platform"], rec["url"], net)
        except MeasureError as e:
            lines.append(f"{d.id}: {e}")
            continue
        s = snapshot(rec, metrics, author, now)
        lines.append(f"{d.id}: {describe(rec['platform'], metrics)} at {s['age_hours']} hours"
                     + (f" (the {s['checkpoint']} reading)" if s["checkpoint"] else ""))
    store.save()
    return lines


def record(base: Path, kit, draft_id: str, metrics: dict, followers: Optional[float], now: Optional[datetime] = None) -> dict:
    d = kit.draft(draft_id)
    if d.meta.get("status") != "posted":
        raise MeasureError(f"{draft_id} isn't recorded as posted. Run: surya-distro posted {kit.repo} {draft_id} --url LINK")
    store = Posts(base, kit.repo)
    rec = store.entry(d)
    author = {"size": followers} if followers is not None else {}
    s = snapshot(rec, {k: v for k, v in metrics.items() if v is not None}, author, now or datetime.now(timezone.utc), source="typed")
    store.save()
    return s


def describe(plat: str, m: dict) -> str:
    if plat == "hn":
        return f"{m.get('points', 0)} points, {m.get('comments', 0)} comments"
    if plat == "reddit":
        return f"{m.get('score', 0)} upvotes, {m.get('comments', 0)} comments"
    if plat == "producthunt":
        return f"{m.get('votes', 0)} votes, {m.get('comments', 0)} comments"
    parts = [f"{int(m.get(k, 0))} {k}" for k in ("likes", "reposts", "replies") if k in m]
    if m.get("views"):
        parts.append(f"{int(m['views'])} views")
    return ", ".join(parts) or "no numbers"


def github_after(base: Path, repo: str, posted_at: str, plat: str, days: int = 7) -> Optional[dict]:
    """Views, clones and stars in the days after a post, against the same span before it."""
    store = traffic.load(base, repo)
    posted = parse_time(posted_at)
    if not posted or not store.get("fetches"):
        return None
    day0 = posted.date()
    span_after = {(day0 + timedelta(days=i)).isoformat() for i in range(days)}
    span_before = {(day0 - timedelta(days=i)).isoformat() for i in range(1, days + 1)}

    def total(span, key):
        return sum(store["days"].get(d, {}).get(key, 0) for d in span)

    stars = store.get("stars", {})
    on_or_before = [d for d in sorted(stars) if d <= day0.isoformat()]
    after = [d for d in sorted(stars) if d >= (day0 + timedelta(days=days - 1)).isoformat()] or sorted(stars)[-1:]
    star_delta = (stars[after[0]]["stars"] - stars[on_or_before[-1]]["stars"]) if on_or_before and after else None
    hosts = REF_HOSTS.get(plat, ())
    fetch_days = [d for d in store.get("referrers", {}) if d >= day0.isoformat()]
    refs = store["referrers"].get(min(fetch_days, key=lambda d: abs((parse_time(d).date() - day0).days - days))) if fetch_days else []
    ref_views = sum(r.get("count", 0) for r in refs or [] if any(h in (r.get("referrer") or "").lower() for h in hosts))
    complete = max(store["days"] or {"": 0}) >= (day0 + timedelta(days=days - 1)).isoformat()
    return {"views_before": total(span_before, "views"), "views_after": total(span_after, "views"),
            "clones_before": total(span_before, "clones"), "clones_after": total(span_after, "clones"),
            "stars_after": star_delta, "referrer_views": ref_views, "days": days, "complete": complete}


def report(base: Path, kit, study: Optional[dict] = None, days: int = 30) -> str:
    store = Posts(base, kit.repo)
    lines = [f"# {kit.repo}: posts and traffic", ""]
    if store.data:
        lines += ["| Draft | Where | Posted | 1h | 24h | 7d | GitHub, 7 days after |", "|---|---|---|---|---|---|---|"]
        for rec in store.data.values():
            cells = []
            for name in ("1h", "24h", "7d"):
                s = next((s for s in rec["snapshots"] if s.get("checkpoint") == name), None)
                cells.append(describe(rec["platform"], s["metrics"]) if s else "")
            g = github_after(base, kit.repo, rec.get("posted_at", ""), rec.get("platform", ""))
            gh = (f"{g['views_after']} views ({g['views_before']} before), {g['clones_after']} clones, "
                  f"{'+' if (g['stars_after'] or 0) >= 0 else ''}{g['stars_after'] if g['stars_after'] is not None else '?'} stars, "
                  f"{g['referrer_views']} views referred" + ("" if g["complete"] else ", still counting")) if g else "no traffic stored"
            lines.append(f"| [{rec['draft']}]({rec.get('url', '')}) | {rec.get('platform', '')} | {rec.get('posted_at', '')[:16].replace('T', ' ')} | "
                         + " | ".join(cells) + f" | {gh} |")
        lines.append("")
    else:
        lines += ["No posts recorded yet. After posting a draft, run `surya-distro posted <repo> <draft> --url LINK`.", ""]
    lines.append(traffic.report(base, kit.repo, days))
    return "\n".join(lines)
