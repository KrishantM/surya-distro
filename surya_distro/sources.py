"""Where the study reads posts from. Public data and official APIs only.

    hn           Hacker News: the Algolia search API, and the official Firebase API for karma and account age
    bluesky      Bluesky's AppView API: searchPosts and getProfiles, no login
    reddit       Reddit's API. Unauthenticated reads get HTTP 403, so it needs an app key:
                 REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET, for an app-only read token
    producthunt  Product Hunt's GraphQL API, with PRODUCT_HUNT_TOKEN (a developer token)
    github       GitHub search through `gh`, for repos in the same space
    x, linkedin  no free read API, so you paste posts into study/manual.csv

None of these log in as you. Each collector returns normalised posts and a note on
what it couldn't read.
"""
from __future__ import annotations

import base64
import csv
import json
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .formats import iso, link_tag, media_from_link, parse_time, strip_html
from .github import GitHub, GitHubError
from .net import Net, qs
from .readme import parse as parse_readme

# Features each source's baseline uses (see stats.py).
FEATURES = {
    "hn": ["size", "age_days"], "reddit": ["size", "age_days", "room"], "bluesky": ["size", "age_days"],
    "producthunt": [], "github": ["size", "age_days", "days"], "x": ["size", "age_days"], "linkedin": ["size", "age_days"],
}
NAMES = {"hn": "Hacker News", "reddit": "Reddit", "bluesky": "Bluesky", "producthunt": "Product Hunt",
         "github": "GitHub", "x": "X (pasted)", "linkedin": "LinkedIn (pasted)", "manual": "X and LinkedIn (pasted)"}
UNITS = {"hn": "points", "reddit": "upvotes", "bluesky": "interactions", "producthunt": "votes",
         "github": "stars", "x": "interactions", "linkedin": "interactions"}
SIZE_UNITS = {"hn": "karma", "reddit": "karma", "bluesky": "followers", "github": "followers",
              "x": "followers", "linkedin": "followers", "producthunt": ""}
ALL = ("hn", "bluesky", "reddit", "producthunt", "github", "manual")
MANUAL_COLUMNS = ["channel", "url", "posted_at", "author", "followers", "account_created",
                  "views", "likes", "reposts", "replies", "text"]
AUTHOR_TTL_DAYS = 14


@dataclass
class Ctx:
    net: Net
    gh: Optional[GitHub]
    queries: List[str]
    now: datetime
    days: int = 90
    subreddits: List[str] = field(default_factory=list)
    skip_repo: str = ""
    study_dir: Optional[Path] = None
    cache: Dict[str, dict] = field(default_factory=dict)
    progress: Callable[[str], None] = lambda s: None
    max_authors: int = 200

    @property
    def since(self) -> datetime:
        return self.now - timedelta(days=self.days)

    @property
    def until(self) -> datetime:
        return self.now - timedelta(days=1)   # a day old at least, so the numbers have settled


def _age_days(created, at) -> Optional[float]:
    a, b = parse_time(created), parse_time(at)
    return round((b - a).total_seconds() / 86400, 1) if a and b else None


def _num(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _cached(ctx: Ctx, key: str) -> Optional[dict]:
    hit = ctx.cache.get(key)
    if hit and (ctx.now - (parse_time(hit.get("at")) or ctx.now)).days < AUTHOR_TTL_DAYS:
        return hit
    return None


def _post(source: str, **kw) -> dict:
    p = {"source": source, "id": "", "url": "", "title": "", "text": "", "author": "", "posted_at": "",
         "metrics": {}, "engagement": 0.0, "discussion": None, "x": {}, "media": "none",
         "link_post": False, "form": "long-form", "query": ""}
    p.update(kw)
    return p


def _set_author(p: dict, size, created) -> None:
    p["x"]["size"] = _num(size)
    p["x"]["age_days"] = _age_days(created, p["posted_at"])
    p["author_created"] = iso(parse_time(created)) if created else ""


# ----------------------------------------------------------------------- HN ---
def hn(ctx: Ctx) -> Tuple[List[dict], str]:
    seen: Dict[str, dict] = {}
    failed = 0
    window = f"created_at_i>{int(ctx.since.timestamp())},created_at_i<{int(ctx.until.timestamp())}"
    for q in ctx.queries:
        for endpoint, n in (("search", 50), ("search_by_date", 30)):
            data = ctx.net.get_json(f"https://hn.algolia.com/api/v1/{endpoint}?" + qs(
                {"query": q, "tags": "story", "numericFilters": window, "hitsPerPage": n}))
            if data is None:
                failed += 1
                continue
            for h in data.get("hits", []):
                oid = str(h.get("objectID", ""))
                if not oid or oid in seen or not h.get("title"):
                    continue
                pts, com = int(h.get("points") or 0), int(h.get("num_comments") or 0)
                link, text = h.get("url") or "", strip_html(h.get("story_text") or "")
                seen[oid] = _post(
                    "hn", id=oid, url=f"https://news.ycombinator.com/item?id={oid}", title=h["title"], text=text,
                    link=link, author=h.get("author", ""), posted_at=iso(parse_time(h.get("created_at_i"))),
                    metrics={"points": pts, "comments": com}, engagement=float(pts),
                    discussion=round(com / max(pts, 1), 2), media=media_from_link(link, text),
                    link_post=bool(link), query=q, show="show_hn" in (h.get("_tags") or []))
    posts = list(seen.values())
    authors = sorted({p["author"] for p in posts if p["author"]})[:ctx.max_authors]
    ctx.progress(f"hn: {len(posts)} posts, looking up {len(authors)} authors")
    for a in authors:
        key = f"hn:{a}"
        info = _cached(ctx, key)
        if not info:
            u = ctx.net.get_json(f"https://hacker-news.firebaseio.com/v0/user/{urllib.parse.quote(a)}.json")
            if not u:
                continue
            info = ctx.cache[key] = {"size": u.get("karma"), "created": iso(parse_time(u.get("created"))), "at": iso(ctx.now)}
        for p in posts:
            if p["author"] == a:
                _set_author(p, info.get("size"), info.get("created"))
    note = f"{failed} search request(s) failed" if failed else ""
    return posts, note


# ------------------------------------------------------------------ Bluesky ---
BSKY = "https://api.bsky.app/xrpc/"


def _bsky_media(embed: dict) -> Tuple[str, bool, str]:
    """(media, has a link card, the card's URL)."""
    t = (embed or {}).get("$type", "")
    if "recordWithMedia" in t:
        return _bsky_media(embed.get("media") or {})
    if "images" in t:
        return "image", False, ""
    if "video" in t:
        return "video", False, ""
    if "external" in t:
        uri = (embed.get("external") or {}).get("uri", "")
        m = media_from_link(uri)
        return (m if m == "video" else "none"), True, uri
    return "none", False, ""


def _bsky_link_place(record: dict, card: bool) -> str:
    text = record.get("text", "")
    raw = text.encode("utf-8")
    starts = [f.get("index", {}).get("byteStart", 0) for f in record.get("facets") or []
              if any(str(x.get("$type", "")).endswith("#link") for x in f.get("features") or [])]
    if starts:
        pos = min(starts) / max(len(raw), 1)
        return "link-first" if pos < 0.25 else ("link-end" if pos > 0.66 else "link-middle")
    return link_tag(text, card=card)


def bluesky(ctx: Ctx) -> Tuple[List[dict], str]:
    seen: Dict[str, dict] = {}
    failed = 0
    for q in ctx.queries:
        for sort, n in (("top", 50), ("latest", 30)):
            r = ctx.net.get(BSKY + "app.bsky.feed.searchPosts?" + qs(
                {"q": q, "sort": sort, "since": iso(ctx.since), "until": iso(ctx.until), "limit": n, "lang": "en"}))
            data = r.json() if r.ok else None
            if data is None:
                failed += 1
                continue
            for item in data.get("posts", []):
                rec = item.get("record") or {}
                uri = item.get("uri", "")
                if not uri or uri in seen or "reply" in rec:
                    continue
                media, card, card_uri = _bsky_media(item.get("embed") or {})
                likes, reposts = int(item.get("likeCount") or 0), int(item.get("repostCount") or 0)
                replies, quotes = int(item.get("replyCount") or 0), int(item.get("quoteCount") or 0)
                author = item.get("author") or {}
                text = rec.get("text", "")
                seen[uri] = _post(
                    "bluesky", id=uri, url=f"https://bsky.app/profile/{author.get('handle', '')}/post/{uri.rsplit('/', 1)[-1]}",
                    text=text, author=author.get("did", ""), handle=author.get("handle", ""),
                    posted_at=iso(parse_time(rec.get("createdAt"))),
                    metrics={"likes": likes, "reposts": reposts, "replies": replies, "quotes": quotes},
                    engagement=float(likes + reposts + replies + quotes), discussion=round(replies / max(likes, 1), 2),
                    media=media if media != "none" else media_from_link(card_uri, text) if card_uri else "none",
                    link_place=_bsky_link_place(rec, card), form="short-form", query=q,
                    author_created=author.get("createdAt", ""))
    posts = list(seen.values())
    dids = sorted({p["author"] for p in posts if p["author"]})[:ctx.max_authors * 2]
    need = [d for d in dids if not _cached(ctx, f"bsky:{d}")]
    ctx.progress(f"bluesky: {len(posts)} posts, looking up {len(need)} authors")
    for i in range(0, len(need), 25):
        data = ctx.net.get_json(BSKY + "app.bsky.actor.getProfiles?" + qs({"actors": need[i:i + 25]}))
        for prof in (data or {}).get("profiles", []):
            ctx.cache[f"bsky:{prof.get('did')}"] = {"size": prof.get("followersCount"), "created": prof.get("createdAt", ""), "at": iso(ctx.now)}
    for p in posts:
        info = _cached(ctx, f"bsky:{p['author']}")
        if info:
            _set_author(p, info.get("size"), info.get("created") or p.get("author_created"))
    if not seen and failed:
        return [], f"Bluesky search failed on all {failed} requests"
    return posts, (f"{failed} search request(s) failed" if failed else "")


# ------------------------------------------------------------------- Reddit ---
REDDIT_KEY_NOTE = ("Reddit refuses reads without an app key (HTTP 403). Create a script app at "
                   "reddit.com/prefs/apps, then set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET.")


def _reddit_token(net: Net) -> Optional[str]:
    cid, secret = os.environ.get("REDDIT_CLIENT_ID", ""), os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not (cid and secret):
        return None
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = net.send("https://www.reddit.com/api/v1/access_token", b"grant_type=client_credentials",
                     "application/x-www-form-urlencoded", {"Authorization": f"Basic {basic}"}, timeout=20)
    return (r.json() or {}).get("access_token") if r.ok else None


def reddit(ctx: Ctx) -> Tuple[List[dict], str]:
    if not ctx.subreddits:
        return [], "no subreddits fit this repo"
    token = _reddit_token(ctx.net)
    if token:
        base, sfx, headers = "https://oauth.reddit.com", "", {"Authorization": f"Bearer {token}"}
    else:
        base, sfx, headers = "https://www.reddit.com", ".json", {}
    seen: Dict[str, dict] = {}
    for sub in ctx.subreddits:
        urls = [f"{base}/r/{sub}/top{sfx}?" + qs({"t": "month", "limit": 50, "raw_json": 1})]
        urls += [f"{base}/r/{sub}/search{sfx}?" + qs({"q": q, "restrict_sr": 1, "sort": "top", "t": "month", "limit": 25, "raw_json": 1})
                 for q in ctx.queries[:2]]
        for url in urls:
            r = ctx.net.get(url, headers)
            if r.status in (401, 403):
                return [], REDDIT_KEY_NOTE if not token else f"Reddit refused the app key (HTTP {r.status})"
            for child in ((r.json() or {}).get("data") or {}).get("children", []) if r.ok else []:
                d = child.get("data") or {}
                pid = d.get("id")
                if not pid or pid in seen or d.get("stickied"):
                    continue
                score_, com = int(d.get("score") or 0), int(d.get("num_comments") or 0)
                text = d.get("selftext") or ""
                media = "video" if d.get("is_video") else ("image" if d.get("post_hint") == "image" else
                                                           media_from_link("" if d.get("is_self") else d.get("url", ""), text))
                p = _post("reddit", id=pid, url="https://www.reddit.com" + d.get("permalink", ""), title=d.get("title", ""),
                          text=text, author=d.get("author", ""), posted_at=iso(parse_time(d.get("created_utc"))),
                          metrics={"score": score_, "comments": com}, engagement=float(score_),
                          discussion=round(com / max(score_, 1), 2), media=media, link_post=not d.get("is_self", True),
                          subreddit=d.get("subreddit", sub))
                p["x"]["room"] = _num(d.get("subreddit_subscribers"))
                if parse_time(p["posted_at"]) and parse_time(p["posted_at"]) <= ctx.until:
                    seen[pid] = p
    posts = list(seen.values())
    for a in sorted({p["author"] for p in posts if p["author"] not in ("", "[deleted]")})[:60]:
        key = f"reddit:{a}"
        info = _cached(ctx, key)
        if not info:
            d = (ctx.net.get_json(f"{base}/user/{urllib.parse.quote(a)}/about{sfx}", headers) or {}).get("data") or {}
            if not d:
                continue
            karma = d.get("total_karma") or (d.get("link_karma", 0) + d.get("comment_karma", 0))
            info = ctx.cache[key] = {"size": karma, "created": iso(parse_time(d.get("created_utc"))), "at": iso(ctx.now)}
        for p in posts:
            if p["author"] == a:
                room = p["x"].get("room")
                _set_author(p, info.get("size"), info.get("created"))
                p["x"]["room"] = room
    return posts, ""


# -------------------------------------------------------------- Product Hunt --
PH_QUERY = """query($after: DateTime!, $topic: String) {
  posts(order: VOTES, postedAfter: $after, topic: $topic, first: 50) {
    edges { node { id name tagline description url votesCount commentsCount createdAt media { type } } }
  }
}"""


def producthunt(ctx: Ctx, topic: str = "developer-tools") -> Tuple[List[dict], str]:
    token = os.environ.get("PRODUCT_HUNT_TOKEN", "").strip()
    if not token:
        return [], "Product Hunt needs PRODUCT_HUNT_TOKEN, a developer token from producthunt.com/v2/oauth/applications"
    r = ctx.net.send_json("https://api.producthunt.com/v2/api/graphql",
                          {"query": PH_QUERY, "variables": {"after": iso(ctx.since), "topic": topic}},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if not r.ok:
        return [], f"Product Hunt returned HTTP {r.status}"
    posts = []
    for edge in (((r.json() or {}).get("data") or {}).get("posts") or {}).get("edges", []):
        n = edge.get("node") or {}
        votes, com = int(n.get("votesCount") or 0), int(n.get("commentsCount") or 0)
        types = {m.get("type") for m in n.get("media") or []}
        posts.append(_post("producthunt", id=str(n.get("id")), url=n.get("url", ""), title=f"{n.get('name', '')}: {n.get('tagline', '')}",
                           text=n.get("description") or "", posted_at=iso(parse_time(n.get("createdAt"))),
                           metrics={"votes": votes, "comments": com}, engagement=float(votes),
                           discussion=round(com / max(votes, 1), 2),
                           media="video" if "video" in types else ("image" if types else "none")))
    return posts, "author size isn't in Product Hunt's API, so its baseline is the median"


# ------------------------------------------------------------------- GitHub ---
def github(ctx: Ctx) -> Tuple[List[dict], str]:
    if ctx.gh is None:
        return [], "no GitHub client"
    since = (ctx.now - timedelta(days=max(ctx.days, 180))).strftime("%Y-%m-%d")
    seen: Dict[str, dict] = {}
    for q in ctx.queries[:3]:
        path = "search/repositories?" + qs({"q": f"{q} created:>{since}", "sort": "stars", "order": "desc", "per_page": 40})
        try:
            data = ctx.gh.api(path, missing_ok=True) or {}
        except GitHubError as e:
            return list(seen.values()), f"GitHub search failed: {str(e)[:120]}"
        for it in data.get("items", []):
            full = it.get("full_name", "")
            if not full or full in seen or full.lower() == ctx.skip_repo.lower() or it.get("fork"):
                continue
            stars = int(it.get("stargazers_count") or 0)
            p = _post("github", id=full, url=it.get("html_url", ""), title=it.get("description") or full,
                      text=it.get("description") or "", author=(it.get("owner") or {}).get("login", ""),
                      posted_at=iso(parse_time(it.get("created_at"))), metrics={"stars": stars, "forks": it.get("forks_count", 0)},
                      engagement=float(stars), query=q, topics=it.get("topics") or [])
            p["x"]["days"] = _age_days(it.get("created_at"), iso(ctx.now))
            seen[full] = p
    # Keep the 60 gaining stars fastest, so every owner can be looked up.
    posts = sorted(seen.values(), key=lambda p: p["engagement"] / max(p["x"].get("days") or 1, 1), reverse=True)[:60]
    owners = sorted({p["author"] for p in posts if p["author"]})
    for login in owners:
        key = f"github:{login}"
        info = _cached(ctx, key)
        if not info:
            try:
                u = ctx.gh.api(f"users/{login}", missing_ok=True) or {}
            except GitHubError:
                continue
            info = ctx.cache[key] = {"size": u.get("followers"), "created": u.get("created_at", ""), "at": iso(ctx.now)}
        for p in posts:
            if p["author"] == login:
                days = p["x"].get("days")
                _set_author(p, info.get("size"), info.get("created"))
                p["x"]["days"] = days
    # The README is the post on GitHub. Read it for the repos gaining stars fastest.
    fastest = sorted(posts, key=lambda p: p["engagement"] / max(p["x"].get("days") or 1, 1), reverse=True)[:12]
    ctx.progress(f"github: {len(posts)} repos, reading {len(fastest)} READMEs")
    for p in fastest:
        try:
            text = ctx.gh.readme(p["id"])
        except GitHubError:
            continue
        rd = parse_readme(text)
        p["text"] = rd.summary or p["text"]
        p["media"] = "video" if media_from_link("", text) == "video" else ("image" if rd.images else "none")
        p["readme"] = {"images": rd.images, "install": bool(rd.install), "comparison": bool(rd.comparison),
                       "limits": bool(rd.limits)}
    return posts, ""


# ------------------------------------------------------------------- manual ---
def manual(ctx: Ctx) -> Tuple[List[dict], str]:
    if ctx.study_dir is None:
        return [], ""
    path = ctx.study_dir / "manual.csv"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(MANUAL_COLUMNS) + "\n", encoding="utf-8")
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("url") or "").strip()]
    if not rows:
        return [], "X and LinkedIn have no free read API. Paste posts into study/manual.csv to include them"
    posts = []
    for i, r in enumerate(rows):
        ch = (r.get("channel") or "").strip().lower()
        ch = "x" if ch in ("x", "twitter") else ("linkedin" if ch == "linkedin" else "")
        if not ch:
            continue
        likes, reposts, replies = (_num(r.get(k)) or 0 for k in ("likes", "reposts", "replies"))
        views = _num(r.get("views"))
        text = r.get("text") or ""
        metrics = {"likes": likes, "reposts": reposts, "replies": replies}
        if views is not None:
            metrics["views"] = views
        p = _post(ch, id=f"manual-{i}", url=r["url"].strip(), text=text, author=r.get("author", ""),
                  posted_at=iso(parse_time(r.get("posted_at"))), metrics=metrics, engagement=likes + reposts + replies,
                  discussion=round(replies / max(likes, 1), 2), media=media_from_link("", text),
                  form="short-form" if ch == "x" else "long-form")
        _set_author(p, r.get("followers"), r.get("account_created"))
        posts.append(p)
    return posts, "pasted by hand"


COLLECTORS = {"hn": hn, "bluesky": bluesky, "reddit": reddit, "producthunt": producthunt, "github": github, "manual": manual}


def load_cache(path: Path) -> Dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
