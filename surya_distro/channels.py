"""Channel rules, the checks that hold a draft to them, and the first drafts.

First drafts are built only from facts in the README and the latest release.
Anything they can't know, like why you built it, is left as a `[[fill: ...]]`
placeholder. `check` refuses a draft while a placeholder is left in it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .readme import sentences
from .repo import Facts

URL_RE = re.compile(r"https?://\S+")
PLACEHOLDER_RE = re.compile(r"\[\[fill:[^\]]*\]\]")


@dataclass(frozen=True)
class Rule:
    field: str
    required: bool = True
    max: Optional[int] = None
    weighted: bool = False         # X counts links as 23 and some characters as 2
    prefix: Optional[str] = None
    graphemes: bool = False        # Bluesky counts graphemes


@dataclass(frozen=True)
class Channel:
    key: str
    name: str
    submit: str                    # where a person posts it by hand
    rules: tuple
    notes: tuple = ()
    plain_text: bool = True        # no Markdown rendering


CHANNELS: Dict[str, Channel] = {c.key: c for c in (
    Channel(
        "hn", "Show HN", "https://news.ycombinator.com/submit",
        (Rule("title", max=80, prefix="Show HN: "), Rule("url"), Rule("text", required=False)),
        (
            "Show HN is for something people can try now, ideally without a signup (news.ycombinator.com/showhn.html).",
            "The project must be your own work, and you need to be around to answer questions.",
            "A version bump on its own isn't a Show HN. A major overhaul probably is.",
            "Don't ask anyone to upvote or comment.",
            "Titles stop at 80 characters. HN doesn't render Markdown, so leave a blank line between paragraphs.",
        ),
    ),
    Channel(
        "reddit", "Reddit", "https://www.reddit.com/r/{subreddit}/submit",
        (Rule("subreddit"), Rule("title", max=300), Rule("body", max=40000), Rule("media", required=False)),
        (
            "Reddit's API needs an app key to read subreddit rules, so these weren't checked live. Read each sidebar before posting.",
            "Many subreddits limit self-promotion, or keep it to a weekly thread.",
            "Post to one subreddit at a time. The same post in several at once reads as spam.",
        ),
        plain_text=False,
    ),
    Channel(
        "x", "X", "https://x.com/compose/post",
        (Rule("post", max=280, weighted=True), Rule("thread", required=False, max=280, weighted=True), Rule("media", required=False)),
        (
            "A post is 280 characters. Every link counts as 23.",
            "Thread posts are separated by a line with only `---` on it.",
        ),
    ),
    Channel(
        "bluesky", "Bluesky", "https://bsky.app/",
        (Rule("post", max=300, graphemes=True), Rule("thread", required=False, max=300, graphemes=True), Rule("media", required=False)),
        (
            "A post is 300 characters. A link counts in full as written, so let it become a link card where you can.",
            "Thread posts are separated by a line with only `---` on it.",
        ),
    ),
    Channel(
        "linkedin", "LinkedIn", "https://www.linkedin.com/feed/",
        (Rule("post", max=3000), Rule("comment", required=False, max=1250), Rule("media", required=False)),
        ("A post is up to 3,000 characters. The first two lines show before \"see more\".",),
    ),
    Channel(
        "producthunt", "Product Hunt", "https://www.producthunt.com/posts/new",
        (Rule("name", max=40), Rule("tagline", max=60), Rule("description", max=260),
         Rule("first comment"), Rule("topics", required=False)),
        (
            "Limits are from launch guides: name 40, tagline 60, description 260 characters. Product Hunt's site blocks automated checks.",
            "A launch needs a thumbnail and gallery images, which this kit doesn't make yet.",
        ),
    ),
    Channel(
        "email", "Email", "",
        (Rule("to"), Rule("subject", max=120), Rule("body")),
        ("Send it from your own mail client, or export approved emails with `surya-distro outbox`.",),
    ),
)}

# Phrases that read as marketing copy or as machine-written. A warning, not an error.
CLICHES = (
    "game-changer", "game changer", "revolutionary", "revolutionize", "revolutionise", "seamless",
    "supercharge", "unlock", "unleash", "leverage", "cutting-edge", "cutting edge", "next-level",
    "excited to announce", "thrilled to", "delve", "elevate", "empower", "effortless", "blazing",
    "world-class", "best-in-class", "in today's", "look no further", "say goodbye", "ever-evolving",
    "harness the power", "10x", "magic", "journey", "landscape", "unparalleled", "state-of-the-art",
)
SUPERLATIVES = ("best", "fastest", "first", "only", "ultimate", "easiest", "most powerful", "simplest")


@dataclass
class Issue:
    level: str      # error | warning
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.field}: {self.message}"


def weighted_length(text: str) -> int:
    """X's count: links are 23, and characters outside the Latin ranges are 2."""
    n = 0
    rest = URL_RE.sub("", text)
    n += 23 * len(URL_RE.findall(text))
    for ch in rest:
        cp = ord(ch)
        light = cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247
        n += 1 if light else 2
    return n


def grapheme_length(text: str) -> int:
    """Close to a grapheme count: combining marks and joiners don't add one."""
    return sum(1 for ch in text if not unicodedata.combining(ch) and ch not in "\u200d\ufe0f")


def thread_posts(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"^\s*---\s*$", text, flags=re.M) if p.strip()]


def size_of(rule: Rule, text: str) -> int:
    """How the channel counts this text against the rule's limit."""
    return weighted_length(text) if rule.weighted else (grapheme_length(text) if rule.graphemes else len(text))


def lint(channel_key: str, fields: Dict[str, str]) -> List[Issue]:
    ch = CHANNELS.get(channel_key)
    if ch is None:
        return [Issue("error", "channel", f"unknown channel {channel_key!r}")]
    issues: List[Issue] = []
    for rule in ch.rules:
        value = (fields.get(rule.field) or "").strip()
        if not value:
            if rule.required:
                issues.append(Issue("error", rule.field, "missing"))
            continue
        parts = thread_posts(value) if rule.field == "thread" else [value]
        for i, part in enumerate(parts, 1):
            label = f"{rule.field} {i}" if rule.field == "thread" else rule.field
            size = size_of(rule, part)
            if rule.max and size > rule.max:
                issues.append(Issue("error", label, f"{size} characters, the limit is {rule.max}"))
        if rule.prefix and not value.startswith(rule.prefix):
            issues.append(Issue("error", rule.field, f"must start with {rule.prefix!r}"))
    for name, value in fields.items():
        if not value:
            continue
        for ph in PLACEHOLDER_RE.findall(value):
            issues.append(Issue("error", name, f"fill in {ph}"))
        low = value.lower()
        found = [c for c in CLICHES if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", low)]
        if found:
            issues.append(Issue("warning", name, "reads as marketing copy: " + ", ".join(found)))
        if "—" in value or "–" in value:
            issues.append(Issue("warning", name, "em or en dash, which many readers take as machine-written"))
        if value.count("!") > 1:
            issues.append(Issue("warning", name, "more than one exclamation mark"))
        if channel_key in ("x", "linkedin", "bluesky") and len(re.findall(r"(?<!\w)#\w+", value)) > 3:
            issues.append(Issue("warning", name, "more than three hashtags"))
    if channel_key == "hn":
        title = (fields.get("title") or "").lower()
        hits = [s for s in SUPERLATIVES if re.search(r"\b" + re.escape(s) + r"\b", title)]
        if hits:
            issues.append(Issue("warning", "title", "HN readers distrust superlatives: " + ", ".join(hits)))
        if re.search(r"[\U0001F300-\U0001FAFF☀-➿]", "".join(fields.values())):
            issues.append(Issue("warning", "text", "HN strips most emoji"))
    return issues


# ------------------------------------------------------------- first drafts ---
CONNECTORS = {"and", "or", "for", "with", "to", "the", "a", "an", "in", "through", "of", "on", "by",
              "your", "its", "from", "via", "that", "which", "is", "are", "as", "at"}


def fit(text: str, limit: int) -> str:
    """Shorten to `limit` characters at a clause or word boundary, with no ellipsis."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    comma = head.rfind(", ")
    if comma >= int(limit * 0.6):
        cut = head[:comma]
    else:
        cut = head[:head.rfind(" ")] if " " in head else head[:limit]
    words = cut.rstrip(" ,;:-(").split(" ")
    while len(words) > 1 and words[-1].lower().strip(",;:") in CONNECTORS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-(")


def fit_sentences(text: str, limit: int) -> str:
    out = ""
    for s in sentences(text):
        nxt = (out + " " + s).strip()
        if len(nxt) > limit:
            break
        out = nxt
    return out or fit(text, limit)


def _first_sentence(f: Facts) -> str:
    s = sentences(f.summary)
    return (s[0] if s else f.summary).rstrip(".")


def _lower_first(s: str) -> str:
    if len(s) > 1 and s[0].isupper() and s[1].islower():
        return s[0].lower() + s[1:]
    return s


def _claim(f: Facts) -> str:
    """The first sentence without the name in front: "Foo is a bar" becomes "a bar"."""
    first = _first_sentence(f)
    if first.lower().startswith(f.name.lower()):
        rest = first[len(f.name):].strip()
        rest = re.sub(r"^(?:is|are|:|,|-)\s+", "", rest)
        return rest or first
    return first


def _tagline(f: Facts, limit: int, prefix: str = "", sep: str = ", ") -> str:
    """`<prefix><name><sep><claim>`, shortened to fit. A first sentence that
    already reads "Name does X" is used as it is."""
    first = _first_sentence(f)
    rest = first[len(f.name):].lstrip() if first.lower().startswith(f.name.lower()) else None
    if rest and not re.match(r"^(?:is|are)\s|^[:,-]", rest):
        return prefix + fit(first, limit - len(prefix))
    lead = f"{prefix}{f.name}{sep}"
    return lead + fit(_lower_first(_claim(f)), limit - len(lead))


def _bullets(f: Facts, n: int, lead_only: bool = False) -> List[str]:
    out = []
    for b in f.readme.bullets[:n]:
        out.append(b.lead if (lead_only and b.lead) else b.text)
    return out


def _version(f: Facts) -> str:
    return f.release.tag if f.release and f.release.tag else ""


def draft_hn(f: Facts) -> Dict[str, str]:
    paras = [f.summary, "[[fill: why you built it, in a sentence or two of your own]]"]
    bl = _bullets(f, 5)
    if bl:
        paras.append("What it does:")
        paras.extend("- " + b for b in bl)
    if f.readme.install:
        paras.append("To try it:")
        paras.append("\n".join("  " + line for line in f.readme.install.splitlines()))
    if f.readme.limits:
        paras.append("Limits: " + " ".join(l.rstrip(".") + "." for l in f.readme.limits[:3]))
    paras.append("[[fill: the feedback you want most]]")
    return {"title": _tagline(f, 80, "Show HN: "), "url": f.url, "text": "\n\n".join(paras)}


SUBREDDITS = (
    # name, kinds it fits, format, note. Priority is the order here.
    ("ClaudeAI", {"claude-code"}, "plain", ""),
    ("ClaudeCode", {"claude-code"}, "plain", ""),
    ("mcp", {"mcp-server"}, "plain", ""),
    ("Python", {"python"}, "python-showcase",
     "Recent showcase posts use three headings: What My Project Does, Target Audience and Comparison. Confirm in the sidebar."),
    ("commandline", {"cli"}, "plain", ""),
    ("selfhosted", {"self-hosted"}, "plain", ""),
    ("opensource", {"open-source"}, "plain", ""),
    ("SideProject", {"developer-tool"}, "plain", ""),
)


def pick_subreddits(f: Facts, limit: int = 4) -> List[tuple]:
    kinds = set(f.kinds)
    return [s for s in SUBREDDITS if s[1] & kinds][:limit]


def draft_reddit(f: Facts, sub: tuple) -> Dict[str, str]:
    name, _kinds, fmt, note = sub
    install = f"```\n{f.readme.install}\n```" if f.readme.install else ""
    feats = "\n".join(f"- {b}" for b in _bullets(f, 5))
    limits = "\n".join(f"- {l}" for l in f.readme.limits[:3])
    if fmt == "python-showcase":
        parts = [
            "## What My Project Does", f.summary, feats, install,
            "## Target Audience", "[[fill: who it's for, and whether it's a toy, a side project or ready for real use]]",
            "## Comparison", f.readme.comparison or "[[fill: how it differs from the tools people use now]]",
            f"Source: {f.url}",
        ]
    else:
        parts = [
            f.summary, "[[fill: why you built it, in a sentence or two of your own]]", feats,
            ("**Install**\n\n" + install) if install else "",
            ("**Limits**\n\n" + limits) if limits else "",
            f"Repo: {f.url}", "[[fill: the question you want this subreddit's answer to]]",
        ]
    title = _tagline(f, 120)
    out = {"subreddit": name, "title": title, "body": "\n\n".join(p for p in parts if p)}
    if note:
        out["note"] = note
    return out


def draft_x(f: Facts) -> Dict[str, str]:
    post = _tagline(f, 280 - 23 - 2, sep=": ")
    thread = [fit(b, 280) for b in _bullets(f, 3)]
    install = "To try it:\n" + f.readme.install.strip()
    if f.readme.install and weighted_length(install) <= 280:
        thread.append(install)
    return {"post": f"{post}\n\n{f.url}", "thread": "\n\n---\n\n".join(thread)}


def draft_bluesky(f: Facts) -> Dict[str, str]:
    post = _tagline(f, 300 - len(f.url) - 2, sep=": ")
    thread = [fit(b, 300) for b in _bullets(f, 3)]
    install = "To try it:\n" + f.readme.install.strip()
    if f.readme.install and grapheme_length(install) <= 300:
        thread.append(install)
    return {"post": f"{post}\n\n{f.url}", "thread": "\n\n---\n\n".join(thread)}


def draft_linkedin(f: Facts) -> Dict[str, str]:
    v = _version(f)
    head = f"{f.name} {v} is out." if v else f"I've released {f.name}."
    parts = [head, f.summary, "[[fill: why you built it, in a sentence or two]]"]
    bl = _bullets(f, 4, lead_only=True)
    if bl:
        parts.append("What it does:\n" + "\n".join("- " + b for b in bl))
    lic = f" under the {f.license} license" if f.license else ""
    parts.append(f"It's open source{lic}: {f.url}")
    parts.append("[[fill: the feedback or help you want]]")
    return {"post": "\n\n".join(parts)}


TOPICS = {"claude-code": "Artificial Intelligence", "mcp-server": "Artificial Intelligence", "python": "Developer Tools",
          "cli": "Developer Tools", "open-source": "Open Source", "developer-tool": "Developer Tools",
          "self-hosted": "Privacy"}


def draft_producthunt(f: Facts) -> Dict[str, str]:
    topics: List[str] = []
    for k in f.kinds:
        t = TOPICS.get(k)
        if t and t not in topics:
            topics.append(t)
    comment = [
        "Hi everyone. [[fill: who you are and why you built it]]", f.summary,
        "What it does:\n" + "\n".join("- " + b for b in _bullets(f, 4)) if f.readme.bullets else "",
        f"It's free and open source: {f.url}", "[[fill: the question you want answered]]",
    ]
    return {
        "name": f.name[:40],
        "tagline": fit(_claim(f)[:1].upper() + _claim(f)[1:], 60),
        "description": fit_sentences(f.summary, 260),
        "first comment": "\n\n".join(p for p in comment if p),
        "topics": ", ".join(topics),
    }


def draft_email(f: Facts, target: dict) -> Dict[str, str]:
    bl = _bullets(f, 3)
    lic = f" under {f.license}" if f.license else ""
    body = [
        "Hello,",
        f"{f.name} is open source{lic}: {f.url}",
        f.summary,
        "\n".join("- " + b for b in bl) if bl else "",
        f"[[fill: one line on why it suits {target['name']}]]",
        "Thanks for reading.\n[[fill: your name]]",
    ]
    return {"to": target["email"], "subject": f"Tool suggestion: {f.name}", "body": "\n\n".join(p for p in body if p)}


def first_drafts(f: Facts, email_targets: List[dict], subreddits: Optional[List[str]] = None) -> Dict[str, tuple]:
    """Draft id -> (channel key, fields)."""
    out: Dict[str, tuple] = {"hn": ("hn", draft_hn(f))}
    subs = pick_subreddits(f)
    if subreddits is not None:
        known = {s[0].lower(): s for s in SUBREDDITS}
        subs = [known.get(s.lower(), (s, set(), "plain", "")) for s in subreddits]
    for s in subs:
        out[f"reddit-{s[0]}"] = ("reddit", draft_reddit(f, s))
    out["x"] = ("x", draft_x(f))
    out["bluesky"] = ("bluesky", draft_bluesky(f))
    out["linkedin"] = ("linkedin", draft_linkedin(f))
    out["producthunt"] = ("producthunt", draft_producthunt(f))
    for t in email_targets:
        out[f"email-{t['id']}"] = ("email", draft_email(f, t))
    return out


# -------------------------------------------------------- following a playbook --
MAIN = {"hn": "text", "reddit": "body", "x": "post", "bluesky": "post", "linkedin": "post", "producthunt": "first comment"}
WHY = re.compile(r"\[\[fill: why you built it[^\]]*\]\]")
SHORT_WORDS = {"long-form": 60, "short-form": 15}


def _statement(f: Facts) -> str:
    """'Name does X', a plain statement, from the description or the claim."""
    desc = sentences(f.description)[0].rstrip(".") if f.description else ""
    if desc and re.match(r"^[A-Z][a-z]+s\b", desc):
        return f"{f.name} {_lower_first(desc)}"
    return f"{f.name} is {_lower_first(_claim(f))}"


def _paras(text: str) -> List[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def follow(channel: str, fields: Dict[str, str], rules: List[dict], f: Facts) -> Dict[str, str]:
    """Reshape a first draft to the playbook's rules for its channel, where a template
    can. What only the maker can write becomes a placeholder. `check` has the final say."""
    fields = dict(fields)
    main = MAIN.get(channel)
    url = f.url
    for r in rules:
        dim, value, do = r["dim"], r["value"], r["do"]
        if dim == "title_shape" and channel in ("hn", "reddit"):
            prefix = "Show HN: " if channel == "hn" else ""
            limit = 80 if channel == "hn" else 120
            from .formats import title_shape
            now = title_shape(fields.get("title", ""))
            if do and value == "i-built":
                fields["title"] = prefix + fit(f"I built {f.name}, {_lower_first(_claim(f))}", limit - len(prefix))
            elif do and value == "name-what":
                fields["title"] = f"{prefix}{f.name}, " + fit(_lower_first(_claim(f)), limit - len(prefix) - len(f.name) - 2)
            elif (do and value == "statement") or (not do and now == value):
                fields["title"] = prefix + fit(_statement(f), limit - len(prefix))
        elif dim == "hook" and do and main in fields:
            text = fields[main]
            lead = {
                "problem": "[[fill: the problem this solves, in one sentence of your own]]",
                "question": "[[fill: the question your reader is already asking themselves]]",
                "result": "[[fill: a real number or result from your own use]]",
                "first-person": f"I built {f.name} because [[fill: why, in your own words]].",
                "announcement": f"{f.name} {_version(f)} is out." if _version(f) else f"Today I'm releasing {f.name}.",
            }.get(value)
            if lead and lead not in text:
                if value == "first-person":
                    text = WHY.sub("", text)
                fields[main] = "\n\n".join([lead] + _paras(text))
        elif dim == "length" and do and value == "short" and main in fields:
            from .playbook import FORM
            cap = SHORT_WORDS[FORM[channel]]
            keep = _paras(fields[main])
            from .formats import words
            while len(keep) > 1 and words("\n\n".join(keep)) >= cap:
                drop = next((i for i in range(len(keep) - 1, 0, -1) if "[[fill" not in keep[i] and url not in keep[i]), None)
                if drop is None:
                    break
                keep.pop(drop)
            fields[main] = "\n\n".join(keep)
        elif dim == "length" and do and value == "long" and main in fields:
            from .playbook import FORM
            from .formats import LENGTHS, words
            if words(fields[main]) <= LENGTHS[FORM[channel]][1]:
                more = "[[fill: more in your own words: how you use it day to day, what surprised you, what's still rough]]"
                keep = _paras(fields[main])
                at = len(keep) - 1 if keep and "[[fill" in keep[-1] else len(keep)
                fields[main] = "\n\n".join(keep[:at] + [more] + keep[at:])
        elif dim == "link" and do and channel in ("x", "bluesky", "linkedin") and main in fields:
            body = "\n\n".join(p for p in _paras(fields[main]) if p.strip() != url).replace(url, "").strip()
            if value == "no-link":
                fields[main] = body
                if channel == "linkedin":
                    fields["comment"] = f"The repo: {url}"
                else:
                    fields["thread"] = "\n\n---\n\n".join([f"The repo: {url}"] + thread_posts(fields.get("thread", "")))
            elif value == "link-first":
                first, _, rest = body.partition("\n")
                fields[main] = f"{first}\n{url}\n{rest}".strip()
            elif value in ("link-end", "link-card"):
                fields[main] = f"{body}\n\n{url}"
        elif dim == "media" and do and value in ("image", "video") and channel in ("x", "bluesky", "linkedin", "reddit"):
            fields.setdefault("media", "[[fill: what you'll attach: " + ("a short demo video" if value == "video" else "a screenshot") + "]]")
        elif dim == "media" and do and value in ("image", "video") and channel == "hn":
            what = "a short demo video or GIF" if value == "video" else "a screenshot"
            fields.setdefault("media", f"[[fill: a link to {what}, to put in the text]]")
    return fields
