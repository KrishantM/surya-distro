"""Format tags: the shape of a post, never its words.

These tags are structural and come from rules, so they are the same on every
run. What kind of post it is and how it opens are judgements, so a scoring
engine labels those (see engines.py). The heuristic engine reuses the
functions here.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

URL_RE = re.compile(r"https?://[^\s)>\]]+")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "loom.com", "vimeo.com", "asciinema.org", "streamable.com")

# Words that bound short, medium and long, by the kind of post.
LENGTHS = {"long-form": (60, 250), "short-form": (15, 40)}

TITLE_SHAPES = {
    "name-what": "the name, then what it does",
    "i-built": "first person: I built, we made",
    "question": "a question",
    "how-why": "starts with how, why or what",
    "number": "leads with a number",
    "statement": "a plain statement",
    "none": "no title",
}
HOOK_TEXT = {
    "problem": "opens with a problem the reader knows",
    "what-it-is": "opens by saying what it is or does",
    "first-person": "opens with the author's own experience",
    "result": "opens with a number or a result",
    "question": "opens with a question",
    "announcement": "opens with an announcement",
}
KIND_TEXT = {
    "launch": "shows a new tool people can try",
    "release": "announces a new version",
    "tutorial": "teaches how to do something",
    "comparison": "compares tools or approaches",
    "story": "tells what happened, or lessons learned",
    "opinion": "argues a view or starts a discussion",
    "question": "asks readers for help or views",
    "news": "reports news",
}


def strip_html(text: str) -> str:
    text = re.sub(r"<p>|<br\s*/?>", "\n\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def strip_prefix(title: str) -> str:
    return re.sub(r"^(show|ask|tell|launch)\s+hn\s*:\s*", "", (title or "").strip(), flags=re.I)


def title_shape(title: str) -> str:
    t = strip_prefix(title)
    if not t:
        return "none"
    if re.match(r"^(i|we)(\s+just|\s+finally)?\s+(built|made|wrote|created|launched|shipped|open[- ]?sourced|released|"
                r"spent|turned|got|added|vibe[- ]coded)\b", t, re.I) or \
            re.match(r"^(i'm|i am|we're|we are|i've|we've)\s+(been\s+)?(building|built|made|releasing|launching|open[- ]?sourc)", t, re.I):
        return "i-built"
    if t.rstrip().endswith("?"):
        return "question"
    if re.match(r"^(how|why|what|when|where|which)\b", t, re.I):
        return "how-why"
    if re.match(r"^[\w.+#@/'-]+(?:\s+[\w.+#@/'-]+){0,3}\s*(?:[:|–—]|\s-|,)\s+\S", t):
        return "name-what"
    if re.match(r"^\W*\d", t):
        return "number"
    return "statement"


def words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", URL_RE.sub("", text or "")))


def length_tag(text: str, form: str) -> str:
    lo, hi = LENGTHS[form]
    n = words(text)
    return "short" if n < lo else ("long" if n > hi else "medium")


def opening(text: str, limit: int = 220) -> str:
    """The first sentence or line, the part a reader sees before deciding."""
    t = (text or "").strip()
    if not t:
        return ""
    line = t.split("\n", 1)[0].strip()
    m = re.match(r"^(.+?[.!?])(\s|$)", line)
    first = m.group(1) if m else line
    return first[:limit]


def link_tag(text: str, link_post: bool = False, card: bool = False) -> str:
    """Where the link sits: the post itself is a link, a card, early, late, or none."""
    if link_post:
        return "link-post"
    if card:
        return "link-card"
    t = text or ""
    m = URL_RE.search(t)
    if not m:
        return "no-link"
    pos = m.start() / max(len(t), 1)
    if pos < 0.25:
        return "link-first"
    return "link-end" if pos > 0.66 else "link-middle"


def media_from_link(url: str, text: str = "") -> str:
    blob = f"{url} {text}".lower()
    if any(h in blob for h in VIDEO_HOSTS) or re.search(r"\.(mp4|mov|webm|gif)\b", blob):
        return "video"
    if re.search(r"\.(png|jpe?g|webp)\b", blob):
        return "image"
    if re.search(r"\bdemo\b", blob):
        return "demo-link"
    return "none"


TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_time(value) -> Optional[datetime]:
    """ISO 8601 (with Z, an offset or fractions) or a Unix time, as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    m = TIME_RE.match(str(value).strip())
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0), tzinfo=timezone.utc)
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        dt -= sign * timedelta(hours=int(tz[1:3]), minutes=int(tz[-2:]))
    return dt


def iso(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def when(value) -> tuple:
    dt = parse_time(value)
    if not dt:
        return "", ""
    h = (dt.hour // 6) * 6
    return WEEKDAYS[dt.weekday()], f"{h:02d}-{h + 6:02d} UTC"


def tag(post: dict) -> dict:
    """Add the structural tags to a normalised post, in place."""
    form = post.get("form", "long-form")
    text = post.get("text", "")
    head = post.get("title") or ""
    day, hour = when(post.get("posted_at"))
    post["tags"] = {
        "title_shape": title_shape(head) if head and form == "long-form" else "none",
        "length": length_tag(text, form) if text.strip() else "none",
        "media": post.get("media") or "none",
        "link": post.get("link_place") or link_tag(text, bool(post.get("link_post"))),
        "day": day,
        "hour": hour,
    }
    post.setdefault("opening", opening(text) or head)
    return post
