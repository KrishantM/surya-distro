"""Pull the facts a launch post needs out of a README.

The README is the source of truth. Nothing is invented here. A fact the README
doesn't state is left for a person to fill in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+(.*)$")
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)|<img\b[^>]*\bsrc=[\"']([^\"']+)", re.I)
IMAGE_ALT = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)|<img\b([^>]*)>", re.I)
BADGE = re.compile(r"badge|shields\.io|/workflows/|travis-ci|codecov|coveralls", re.I)
BADGE_LINE = re.compile(r"^\s*(?:\[?!\[|<(?:p|div|img|a|picture|br)\b)", re.I)


def plain(text: str) -> str:
    """Markdown to plain text: links, emphasis and code marks removed."""
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip())
    return [p.strip() for p in parts if p.strip()]


@dataclass
class Bullet:
    lead: str   # the bold lead-in, if the item has one
    text: str   # the whole item as plain text


@dataclass
class Section:
    heading: str
    level: int
    lines: List[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class Readme:
    title: str = ""
    summary: str = ""                       # first paragraph after the title, plain
    bullets: List[Bullet] = field(default_factory=list)
    install: str = ""                       # first code block in an install section
    limits: List[str] = field(default_factory=list)
    comparison: str = ""
    sections: List[Section] = field(default_factory=list)
    images: int = 0
    screenshots: List[dict] = field(default_factory=list)   # [{"url", "alt"}], badges left out
    has_tests: bool = False
    has_install: bool = False

    def section(self, *words: str) -> Optional[Section]:
        for s in self.sections:
            h = s.heading.lower()
            if any(w in h for w in words):
                return s
        return None


def _blocks(lines: List[str]):
    """Yield ("code", text) and ("text", line) items, keeping fences whole."""
    code: Optional[List[str]] = None
    for line in lines:
        if FENCE.match(line):
            if code is None:
                code = []
            else:
                yield "code", "\n".join(code)
                code = None
            continue
        if code is not None:
            code.append(line)
        else:
            yield "text", line
    if code:
        yield "code", "\n".join(code)


def _list_items(lines: List[str]) -> List[str]:
    """The first list in these lines, with wrapped lines joined."""
    items: List[str] = []
    started = False
    for kind, line in _blocks(lines):
        if kind == "code":
            if started:
                break
            continue
        m = LIST_ITEM.match(line)
        if m:
            started = True
            items.append(m.group(1))
        elif started and line.strip() and line.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
        elif started and not line.strip():
            continue
        elif started:
            break
    return items


def _bullet(item: str) -> Bullet:
    m = re.match(r"^\s*(\*\*|__)(.+?)\1\s*(.*)$", item)
    lead = plain(m.group(2)).rstrip(".:") if m else ""
    return Bullet(lead=lead, text=plain(item))


def _screenshots(text: str, limit: int = 3) -> List[dict]:
    """The README's own pictures, in order: where the file is and its alt text. Badges are left out."""
    out: List[dict] = []
    for m in IMAGE_ALT.finditer(text):
        if m.group(2):
            url, alt = m.group(2), m.group(1)
        else:
            tag = m.group(3) or ""
            src = re.search(r"\bsrc=[\"']([^\"']+)", tag)
            if not src:
                continue
            a = re.search(r"\balt=[\"']([^\"']*)", tag)
            url, alt = src.group(1), a.group(1) if a else ""
        if BADGE.search(url) or url.startswith("data:"):
            continue
        out.append({"url": url, "alt": plain(alt)})
        if len(out) == limit:
            break
    return out


def parse(text: str) -> Readme:
    r = Readme()
    lines = text.replace("\r\n", "\n").split("\n")
    r.images = sum(1 for m in IMAGE.finditer(text) if not BADGE.search(m.group(1) or m.group(2) or ""))
    r.screenshots = _screenshots(text)

    # Split into sections. Lines before the first heading belong to a nameless one.
    current = Section("", 0)
    r.sections.append(current)
    in_code = False
    for line in lines:
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            current = Section(plain(m.group(2)), len(m.group(1)))
            r.sections.append(current)
        else:
            current.lines.append(line)

    top = next((s for s in r.sections if s.level == 1), None)
    r.title = top.heading if top else ""
    intro = top or r.sections[0]

    # Summary: the first real paragraph of the intro.
    para: List[str] = []
    for kind, line in _blocks(intro.lines):
        if kind == "code":
            if para:
                break
            continue
        if not line.strip():
            if para:
                break
            continue
        if BADGE_LINE.match(line) or LIST_ITEM.match(line) or line.lstrip().startswith(">"):
            if para:
                break
            continue
        para.append(line.strip())
    r.summary = plain(" ".join(para))

    # Features: the first list in the intro, or else the first list anywhere.
    items = _list_items(intro.lines)
    if not items:
        for s in r.sections:
            items = _list_items(s.lines)
            if items:
                break
    r.bullets = [_bullet(i) for i in items]

    inst = r.section("install", "quick start", "quickstart", "getting started")
    r.has_install = inst is not None
    if inst:
        idx = r.sections.index(inst)
        # An install section's code may sit under its subheadings.
        scope = [inst] + [s for s in r.sections[idx + 1:idx + 6] if s.level > inst.level]
        for s in scope:
            code = next((t for k, t in _blocks(s.lines) if k == "code" and t.strip()), "")
            if code:
                r.install = code.strip()
                break

    lim = r.section("limit", "known issue", "caveat")
    if lim:
        r.limits = [plain(i) for i in _list_items(lim.lines)]

    comp = r.section("compar", "alternative", "why not", "prior art")
    if comp:
        r.comparison = plain(" ".join(l for k, l in _blocks(comp.lines) if k == "text"))

    r.has_tests = r.section("test") is not None
    return r
