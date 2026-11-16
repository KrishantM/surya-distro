"""Turn a repo's facts into the HTML for each image and video.

Nothing here invents a fact. Words come from the README, the description and the
release. Anything only the maker knows, such as the problem the tool solves,
stays a visible `[[fill: ...]]` marker until they supply it. `check` refuses an
asset that still shows one.

The templates are in `templates/`. `{{name}}` is HTML-escaped. `{{{name}}}` is raw
HTML that this file built.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .media import Spec
from .readme import sentences

TEMPLATES = Path(__file__).with_name("templates")
SLOT = re.compile(r"\{\{\{(\w+)\}\}\}|\{\{(\w+)\}\}")
FILL = re.compile(r"\[\[fill:[^\]]*\]\]")
PROBLEM_FILL = "[[fill: the problem this solves, in your own words]]"
INSTALL_FILL = "[[fill: the install command]]"
MAX_SECONDS, MIN_SECONDS = 30.0, 15.0


class TemplateError(KeyError):
    pass


def fill(template: str, values: Dict[str, object]) -> str:
    """Fill `{{name}}` (escaped) and `{{{name}}}` (raw). A name with no value is an error, so a typo can't ship blank."""
    def sub(m):
        raw, esc = m.group(1), m.group(2)
        key = raw or esc
        if key not in values:
            raise TemplateError(f"the template wants {key!r} and nothing supplied it")
        v = str(values[key])
        return v if raw else html.escape(v, quote=True)
    return SLOT.sub(sub, template)


def read_template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


# ------------------------------------------------------------------ content --
@dataclass
class Content:
    name: str
    repo: str
    url: str
    tagline: str
    problem: str
    features: List[Tuple[str, str]]            # (lead, sub)
    install: List[str]
    claude: bool                               # the install lines are Claude Code commands
    license: str = ""
    release: str = ""
    shot: Optional[Path] = None
    shot_alt: str = ""
    inputs: Dict[str, str] = field(default_factory=dict)

    @property
    def url_display(self) -> str:
        return re.sub(r"^https?://", "", self.url).rstrip("/")

    @property
    def initial(self) -> str:
        words = re.findall(r"[A-Za-z0-9]+", self.name)
        return ("".join(w[0] for w in words[:2]) or "?").upper()


def _cut(text: str, limit: int) -> str:
    """The text, shortened at a word boundary, only if it must be."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def tagline(facts: dict) -> str:
    first = (sentences(facts.get("summary", "")) or [""])[0]
    for cand in (first, facts.get("description", ""), facts.get("summary", "")):
        cand = re.sub(r"\s+", " ", cand or "").strip()
        if cand and len(cand) <= 120:
            return cand if cand.endswith((".", "!", "?")) else cand
    return _cut(first or facts.get("description", "") or facts.get("name", ""), 120)


def features(facts: dict, n: int = 4) -> List[Tuple[str, str]]:
    out = []
    for b in facts.get("readme", {}).get("bullets", []):
        text = b.get("text", "")
        lead = (b.get("lead") or "").strip()
        rest = text
        if lead and text.lower().startswith(lead.lower()):
            rest = text[len(lead):].lstrip(" .:-")
        if not lead:
            lead = _cut((sentences(text) or [text])[0], 48)
            rest = ""
        sub = (sentences(rest) or [""])[0]
        out.append((_cut(lead, 56), sub if 0 < len(sub) <= 110 else ""))
        if len(out) == n:
            break
    return out


def build_content(facts: dict, inputs: Optional[Dict[str, str]] = None, shot: Optional[Path] = None) -> Content:
    inputs = {k: v.strip() for k, v in (inputs or {}).items() if v and v.strip()}
    rd = facts.get("readme", {})
    install = [l.strip() for l in (rd.get("install") or "").splitlines() if l.strip() and not l.strip().startswith("#")]
    install = install[:3]
    rel = facts.get("release") or {}
    return Content(
        name=inputs.get("name") or facts.get("name", ""), repo=facts.get("repo", ""), url=facts.get("url", ""),
        tagline=inputs.get("tagline") or tagline(facts), problem=inputs.get("problem") or PROBLEM_FILL,
        features=features(facts), install=([inputs["install"]] if inputs.get("install") else install) or [INSTALL_FILL],
        claude=bool(install) and install[0].startswith("/"), license=facts.get("license", ""),
        release="" if rel.get("is_tag_only") else (rel.get("tag") or ""), shot=shot, inputs=inputs)


# -------------------------------------------------------------------- images --
def _chips(c: Content, n: int = 3) -> str:
    leads = [l for l, _ in c.features[:n]]
    return '<div class="chips">' + "".join(f'<span class="chip">{esc(l)}</span>' for l in leads) + "</div>" if leads else ""


def _code(lines: List[str], claude: bool) -> str:
    prompt = ">" if claude else "$"
    return '<div class="code">' + "".join(f'<div><span class="p">{prompt}</span> {esc(l)}</div>' for l in lines) + "</div>"


def _foot(c: Content) -> str:
    tags = [t for t in (c.license and f"{c.license} license", c.release) if t]
    return '<div class="foot">' + "".join(f'<span class="tag">{esc(t)}</span>' for t in tags) + "</div>" if tags else ""


def _shot(c: Content) -> str:
    if not c.shot:
        return ""
    return ('<div class="shot"><div class="win"><div class="bar"><i></i><i></i><i></i></div>'
            f'<img alt="{esc(c.shot_alt)}" src="{data_uri(c.shot)}"></div></div>')


H1 = {"og": 11.0, "hero": 11.0, "wide": 11.0, "square": 10.0}


def _base(spec: Spec, c: Content, tokens: Dict[str, str]) -> Dict[str, object]:
    u = round(min(spec.width, spec.height) / 100.0, 3)
    v = {**tokens, "u": u, "width": spec.width, "height": spec.height, "name": c.name, "initial": c.initial,
         "url_display": c.url_display, "variant": spec.variant, "fit_js": read_template("fit.js")}
    v["base_css"] = fill(read_template("base.css"), v)
    return v


def _gallery_body(spec: Spec, c: Content) -> Tuple[str, str, str]:
    n = spec.variant
    if n == "1":
        return "What it is", f'<h1 data-fit>{esc(c.tagline)}</h1>', "1 of 3"
    if n == "2":
        rows = "".join(f'<div class="row"><b>{esc(l)}</b>' + (f"<span>{esc(s)}</span>" if s else "") + "</div>"
                       for l, s in c.features[:3]) or f'<h1>{esc(c.tagline)}</h1>'
        return "What it does", f'<div class="rows">{rows}</div>', "2 of 3"
    extra = "".join(f'<div class="row"><b>{esc(t)}</b></div>' for t in (c.license and f"{c.license} license", c.release) if t)
    return "How to install", f'<div class="rows">{_code(c.install, c.claude)}{extra}</div>', "3 of 3"


def image_html(spec: Spec, c: Content, tokens: Dict[str, str]) -> str:
    v = _base(spec, c, tokens)
    if spec.template == "thumb":
        return fill(read_template("thumb.html"), v)
    if spec.template == "gallery":
        kicker, body, step = _gallery_body(spec, c)
        v.update(kicker=kicker, content_html=body, step=step)
        return fill(read_template("gallery.html"), v)
    wide = spec.variant in ("wide", "square", "hero")
    v.update(tagline=c.tagline, h1_size=H1.get(spec.variant, 8.4),
             chips_html=_chips(c) if spec.variant in ("wide", "square") else "",
             code_html=_code(c.install, c.claude) if wide and not (c.shot and spec.variant != "square") else "",
             foot_html=_foot(c) if spec.variant in ("og", "hero") else "",
             shot_html=_shot(c) if c.shot and spec.variant in ("wide", "hero") else "")
    return fill(read_template("card.html"), v)


# -------------------------------------------------------------------- video --
STEP = 0.16            # seconds between kinetic words
TYPE_S = 0.036         # seconds per typed character
FADE = 0.4


def fit_px(text: str, box_w: float, box_h: float, max_px: float, min_px: float = 18.0, aspect: float = 0.5, lh: float = 1.14) -> int:
    """The largest font size at which `text` wraps inside the box, by a greedy word wrap."""
    words = FILL.sub(lambda m: m.group(0).replace(" ", " "), text).split()
    f = max_px
    while f > min_px:
        cw, lines, cur = aspect * f * 1.04, 1, 0.0
        for w in words:
            need = len(w) * cw
            if cur and cur + cw + need > box_w:
                lines, cur = lines + 1, need
            else:
                cur += (cw if cur else 0) + need
        if lines * lh * f <= box_h and max((len(w) for w in words), default=0) * cw <= box_w:
            break
        f *= 0.94
    return int(max(f, min_px))


def kinetic(text: str, t0: float, step: float, accent: int = 0) -> str:
    """Words that rise in one by one. A `[[fill: ...]]` marker stays in one piece so it can still be found."""
    out, i = [], 0
    for part in re.split(r"(\[\[fill:[^\]]*\]\])", text):
        toks = [part] if FILL.fullmatch(part) else part.split()
        for tok in toks:
            if not tok:
                continue
            cls = "w a" if (i < accent or FILL.fullmatch(tok)) else "w"
            out.append(f'<span class="{cls}" style="--t:{t0 + i * step:.3f}s">{esc(tok)}</span>')
            i += 1
    return " ".join(out)


def n_words(text: str) -> int:
    return len([t for part in re.split(r"(\[\[fill:[^\]]*\]\])", text) for t in ([part] if FILL.fullmatch(part) else part.split()) if t])


def _scene(kind: str, s: float, d: float, inner: str, last: bool = False) -> str:
    return (f'<section class="scene {kind}{" last" if last else ""}" style="--s:{s:.3f}s;--d:{d:.3f}s"><div class="stage">'
            f"{inner}</div></section>")


def _caption(text: str, s: float, d: float, w: int, h: int, u: float, max_u: float, accent: int = 0, sub: str = "") -> str:
    px = fit_px(text, w - 16 * u, (h - 21 * u) * (0.6 if sub else 0.78), max_u * u)
    n = n_words(text)
    step = min(STEP, 0.6 * d / max(n, 1))
    html_ = f'<div class="cap" style="font-size:{px}px">{kinetic(text, s + FADE, step, accent)}</div>'
    if sub:
        sp = fit_px(sub, w - 16 * u, (h - 21 * u) * 0.25, 3.6 * u, 14.0, 0.5, 1.3)
        html_ += f'<div class="sub" style="font-size:{sp}px;--t:{s + FADE + n * step + 0.2:.3f}s">{esc(sub)}</div>'
    return html_


def script(c: Content) -> List[Tuple[str, str]]:
    """What a voice reads, scene by scene. It is the caption text."""
    out = [("problem", c.problem), ("what", f"{c.name}. {c.tagline}")]
    for i, (lead, sub) in enumerate(c.features[:3], 1):
        out.append((f"feature{i}", f"{lead}. {sub}".strip() if sub else lead))
    out += [("install", "Install it like this."), ("close", f"{c.name}. Find it on GitHub at {c.repo.replace('/', ' slash ')}.")]
    return out


def plan_scenes(c: Content, voice: Optional[Dict[str, float]] = None) -> List[dict]:
    """Scenes with their lengths, in order. Cut features if the total passes 30 seconds."""
    voice = voice or {}

    def dur(key: str, default: float) -> float:
        return max(default, voice[key] + 0.9) if key in voice else default

    def build(nf: int) -> List[dict]:
        sc = [{"key": "problem", "dur": dur("problem", min(max(2.0 + 0.28 * n_words(c.problem), 3.6), 5.2))},
              {"key": "what", "dur": dur("what", min(max(2.4 + 0.18 * n_words(c.tagline), 4.2), 5.2))}]
        if c.shot:
            sc.append({"key": "shot", "dur": 4.6})
        for i in range(1, nf + 1):
            sc.append({"key": f"feature{i}", "dur": dur(f"feature{i}", 2.9)})
        longest = max(len(l) for l in c.install)
        lines = len(c.install)
        typing = sum(len(l) for l in c.install) * TYPE_S + 0.5 * (lines - 1)
        sc.append({"key": "install", "dur": dur("install", max(typing + 2.8, 5.0))})
        sc.append({"key": "close", "dur": dur("close", 3.4)})
        return sc
    nf = min(len(c.features), 3)
    scenes = build(nf)
    while sum(s["dur"] for s in scenes) > MAX_SECONDS and nf > 0:
        nf -= 1
        scenes = build(nf)
    total = sum(s["dur"] for s in scenes)
    if total < MIN_SECONDS:
        scenes[-1]["dur"] += MIN_SECONDS - total
    return scenes


def video_html(spec: Spec, c: Content, tokens: Dict[str, str], voice: Optional[Dict[str, float]] = None
               ) -> Tuple[str, float, List[Tuple[str, float, str]]]:
    """(html, seconds, [(scene, start, voice text)])."""
    w, h = spec.width, spec.height
    u = min(w, h) / 100.0
    scenes = plan_scenes(c, voice)
    total = round(sum(s["dur"] for s in scenes), 2)
    texts = dict(script(c))
    parts, segs, t = [], [], 0.0
    for idx, sc in enumerate(scenes):
        key, d, s = sc["key"], sc["dur"], round(t, 3)
        last = idx == len(scenes) - 1
        label = {"problem": "The problem", "what": "What it does", "shot": "In use", "install": "Install", "close": "Open source"}.get(key, "What it does")
        head = f'<div class="label">{esc(label)}</div>'
        if key == "problem":
            inner = head + _caption(c.problem, s, d, w, h, u, 10.0)
        elif key == "what":
            namepx = int(5.2 * u)
            inner = (head + f'<div class="cap name" style="font-size:{namepx}px;margin-bottom:{int(2 * u)}px">{kinetic(c.name, s + FADE, 0.12, 99)}</div>'
                     + _caption(c.tagline, s + 0.3, d - 0.3, w, h - 12 * u, u, 8.4))
        elif key == "shot":
            inner = (head + f'<div class="win shotwin"><div class="bar"><i></i><i></i><i></i></div><div class="clip"><img alt="{esc(c.shot_alt)}" '
                     f'src="{data_uri(c.shot)}"></div></div>')
        elif key.startswith("feature"):
            lead, sub = c.features[int(key[7:]) - 1]
            inner = head + _caption(lead, s, d, w, h, u, 11.0, sub=sub)
        elif key == "install":
            inner = head + _terminal(c, s, d, w, h, u)
        else:
            chips = "".join(f'<span class="chip">{esc(x)}</span>' for x in (c.license and f"{c.license} license", c.release) if x)
            urlpx = fit_px(c.url_display, w - 16 * u, 12 * u, 4.6 * u, 14.0, 0.62, 1.2)
            inner = (head + _caption(c.name, s, d, w, h, u, 11.0)
                     + f'<div class="url" style="font-size:{urlpx}px">{esc(c.url_display)}</div>'
                     + (f'<div class="chips">{chips}</div>' if chips else ""))
        parts.append(_scene("close" if key == "close" else key, s, d, inner, last))
        if key in texts and key != "shot":
            segs.append((key, s + FADE, texts[key]))
        t += d
    v = {**tokens, "u": round(u, 3), "width": w, "height": h, "name": c.name, "initial": c.initial,
         "variant": spec.variant, "fit_js": read_template("fit.js"), "seconds": f"{total:g}", "scenes": "\n".join(parts)}
    v["base_css"] = fill(read_template("base.css"), v)
    return fill(read_template("video.html"), v), total, segs


def _terminal(c: Content, s: float, d: float, w: int, h: int, u: float) -> str:
    longest = max(len(l) for l in c.install)
    px = int(min(3.5 * u, (w - 16 * u - 7 * u) / ((longest + 3) * 0.61)))
    rows, t = [], s + 1.0
    for i, line in enumerate(c.install):
        dur = max(len(line), 1) * TYPE_S
        end = t + dur
        last = i == len(c.install) - 1
        rows.append(f'<div class="line" style="--t:{t:.3f}s"><span class="p">{">" if c.claude else "$"}</span>'
                    f'<span class="typed{"" if last else " done"}" style="--n:{len(line)};--dur:{dur:.3f}s;--end:{end:.3f}s">{esc(line)}</span></div>')
        t = end + 0.5
    title = "Claude Code" if c.claude else "Terminal"
    return (f'<div class="win"><div class="bar"><i></i><i></i><i></i><span>{title}</span></div>'
            f'<div class="term" style="font-size:{px}px">{"".join(rows)}</div></div>')
