"""Make the images and videos for a launch, from the playbook and the study.

    surya-distro create owner/repo

Which assets get made follows the evidence, in this order:

1. A rule in the approved playbook about media on that channel. If X's rule says
   a short demo video did best, X gets videos. If it says text alone did best, X gets none.
2. A media format from the study that beat its baseline on that channel
   (`format.<channel>.<format>`, see study.media_formats).
3. "default", when neither has anything to say. Then the channel gets its usual set.

Every asset records which one it followed. The rule is to replicate a trend's
format and never its content. Only shapes are read here: whether a format won,
its aspect, its length. No post's words or media are copied.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import compose, look, media as mediamod, render
from . import playbook as pbmod
from .media import BY_ID, SPECS, Manifest, Spec
from .study import media_formats
from .traffic import write_json

# What each channel gets when the evidence doesn't say otherwise.
CHANNEL_ASSETS = {
    "og": ("og-card",),
    "readme": ("readme-hero",),
    "x": ("x-image", "x-video-16x9", "x-video-1x1"),
    "bluesky": ("bluesky-image", "bluesky-video-16x9"),
    "linkedin": ("linkedin-image", "linkedin-video-1x1", "linkedin-video-9x16"),
    "producthunt": ("producthunt-thumb", "producthunt-gallery-1", "producthunt-gallery-2", "producthunt-gallery-3"),
}
VIDEO_FORMATS, IMAGE_FORMATS = ("video", "gif"), ("image", "screenshot")
SHOT_HOSTS = ("github.com", "raw.githubusercontent.com")
SHOT_SUFFIX = ".githubusercontent.com"


class CreateError(RuntimeError):
    pass


@dataclass
class Plan:
    spec: Spec
    follows: str
    why: str


@dataclass
class Decision:
    make: bool
    follows: str
    why: str


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------- the decision ----
def decide(channel: str, kind: str, pb: Optional[dict], fmts: Dict[str, dict]) -> Decision:
    """Make this kind of media for this channel? Follow a playbook rule, then a study format, then the default."""
    for r in pbmod.active(pb, channel):
        if r.get("dim") != "media":
            continue
        v, do = r["value"], r["do"]
        wants_motion, wants_still = v in ("video",), v in ("image",)
        if channel == "readme":        # a hero still serves either: a screenshot or a demo
            wants_still = wants_still or wants_motion
        if do and ((kind == "video" and wants_motion) or (kind == "image" and wants_still)):
            return Decision(True, r["id"], r["text"])
        if not do and ((kind == "video" and v == "video") or (kind == "image" and v == "image")):
            return Decision(False, r["id"], r["text"])
        if do and v == "none":
            return Decision(False, r["id"], r["text"])
        if not do and v == "none":
            return Decision(True, r["id"], r["text"])
    info = fmts.get(channel)
    if info:
        rows = {r["format"]: r for r in info["results"]}
        mine = VIDEO_FORMATS if kind == "video" else IMAGE_FORMATS
        win = next((rows[f] for f in mine if f in rows and rows[f]["beats"]), None)
        if win:
            return Decision(True, win["id"], f"{win['format']} beat its baseline here ({win['n']} posts, {win['confidence']} confidence)")
        lose = next((rows[f] for f in mine if f in rows and rows[f]["loses"]), None)
        if lose:
            return Decision(False, lose["id"], f"{lose['format']} did worse than the rest here ({lose['n']} posts, {lose['confidence']} confidence)")
        txt = rows.get("text-only")
        if txt and txt["beats"]:
            return Decision(False, txt["id"], f"text alone beat the rest here ({txt['n']} posts, {txt['confidence']} confidence)")
    return Decision(True, "default", "no evidence for this channel, so the usual set")


def select(pb: Optional[dict], fmts: Dict[str, dict], only: Optional[List[str]] = None) -> Tuple[List[Plan], List[Tuple[str, str, str]]]:
    """(assets to make, [(channel, kind, why) for what was left out])."""
    plans: List[Plan] = []
    skipped: List[Tuple[str, str, str]] = []
    cache: Dict[Tuple[str, str], Decision] = {}
    for spec in SPECS:
        if only is not None and spec.id not in only:
            continue
        d = cache.setdefault((spec.channel, spec.kind), decide(spec.channel, spec.kind, pb, fmts))
        if d.make or only is not None:
            plans.append(Plan(spec, d.follows, d.why))
        else:
            note = (spec.channel, spec.kind, f"{d.follows}: {d.why}")
            if note not in skipped:
                skipped.append(note)
    return plans, skipped


def describe(follows: str, pb: Optional[dict], fmts: Dict[str, dict]) -> str:
    """What a `follows` value means, in words."""
    if follows == "default":
        return "No rule or study finding applies, so the channel's usual set."
    for r in (pb or {}).get("rules", []):
        if r["id"] == follows:
            e = r.get("evidence") or {}
            n = f", {e['n']} posts, {r['confidence']} confidence" if e else f", {r['confidence']} confidence"
            return f"Playbook rule: {r['text']}{n}."
    for ch, info in fmts.items():
        for r in info["results"]:
            if r["id"] == follows:
                return (f"Study finding: {r['format']} on {ch} scored {r['mean']:+.2f} against {r['mean_rest']:+.2f} for the rest "
                        f"({r['n']} posts, {r['confidence']} confidence).")
    return follows


# ------------------------------------------------------------- the inputs ----
def parse_sets(pairs: Optional[List[str]]) -> Dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise CreateError(f"--set wants key=value, such as problem=\"...\". Got {p!r}")
        k, v = p.split("=", 1)
        if k.strip() not in ("problem", "tagline", "name", "install"):
            raise CreateError(f"--set keys: problem, tagline, name, install. Got {k.strip()!r}")
        out[k.strip()] = v.strip()
    return out


def load_inputs(media_dir: Path) -> Dict[str, str]:
    try:
        return dict(json.loads((media_dir / "inputs.json").read_text(encoding="utf-8")).get("set", {}))
    except (OSError, ValueError):
        return {}


def _shot_url(facts: dict, url: str) -> Optional[str]:
    """A README picture as an https URL on GitHub's own hosts, or None."""
    if not re.match(r"^https?://", url):
        path = url.lstrip("./").split("?")[0]
        return f"https://raw.githubusercontent.com/{facts['repo']}/{facts.get('default_branch', 'main')}/{path}"
    host = re.match(r"^https?://([^/]+)", url).group(1).lower()
    if host in SHOT_HOSTS or host.endswith(SHOT_SUFFIX):
        return url if url.startswith("https://") else None
    return None


def _image_ok(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:6] in (b"GIF87a", b"GIF89a") \
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def find_screenshot(facts: dict, media_dir: Path, net=None, given: Optional[str] = None,
                    say: Callable[[str], None] = lambda s: None) -> Tuple[Optional[Path], str]:
    """The README's first picture, saved here so the page can inline it. `given` is a file a person chose."""
    if given:
        p = Path(given).expanduser()
        if not p.is_file() or not _image_ok(p.read_bytes()[:16]):
            raise CreateError(f"--screenshot {given}: not a PNG, JPEG, GIF or WebP file")
        return p, ""
    shots = facts.get("readme", {}).get("screenshots") or []
    if not shots:
        if facts.get("readme", {}).get("images"):
            say("The README has an image, but this kit's facts don't hold its address. Run `draft` again, or pass --screenshot FILE.")
        return None, ""
    for s in shots:
        url = _shot_url(facts, s["url"])
        if not url or net is None:
            continue
        dest = media_dir / "assets" / ("shot-" + re.sub(r"\W+", "-", url.split("//", 1)[-1])[-60:] + ".img")
        if not dest.exists():
            data = net.get_bytes(url)
            if not data or len(data) > 5_000_000 or not _image_ok(data):
                say(f"Couldn't read the README picture at {url}.")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return dest, s.get("alt", "")
    return None, ""


# ------------------------------------------------------------- the render ----
@dataclass
class Result:
    asset: dict
    rendered: bool
    note: str = ""


def _asset(plan: Plan, kit_dir: Path, seconds: Optional[float], sha: str, status: str, created: str) -> dict:
    s = plan.spec
    md = kit_dir / "media"
    a = {"id": s.id, "channel": s.channel, "kind": s.kind, "path": str(md / f"{s.id}.{mediamod.EXT[s.kind]}"),
         "html": str(md / f"{s.id}.html"), "width": s.width, "height": s.height}
    if s.kind == "video":
        a["seconds"] = seconds
    a.update(template=s.template, follows=plan.follows, status=status, sha256=sha, created=created)
    return a


def make(plans: List[Plan], facts: dict, kit_dir: Path, tokens: Dict[str, str], inputs: Dict[str, str],
         shot: Optional[Path], shot_alt: str, voice: bool = False, say: Callable[[str], None] = lambda s: None,
         chrome: Optional[str] = None, ffmpeg: Optional[str] = None) -> Tuple[List[Result], List[str]]:
    """Write each asset's HTML, render it if a browser is there, and update the manifest.
    Returns the results and the ids of approved assets that this render reset to draft."""
    md = kit_dir / "media"
    md.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(kit_dir)
    chrome = chrome or render.find_chrome()
    ffmpeg = ffmpeg or render.find_ffmpeg()
    content = compose.build_content(facts, inputs, shot)
    content.shot_alt = shot_alt
    results: List[Result] = []
    reset: List[str] = []
    voice_cache: Dict[str, Tuple[Dict[str, float], Dict[str, Path]]] = {}
    for plan in plans:
        s = plan.spec
        html_path = md / f"{s.id}.html"
        out = md / f"{s.id}.{mediamod.EXT[s.kind]}"
        seconds, audio, note = None, [], ""
        if s.kind == "image":
            html_path.write_text(compose.image_html(s, content, tokens), encoding="utf-8")
        else:
            vdur: Dict[str, float] = {}
            files: Dict[str, Path] = {}
            if voice:
                if "v" not in voice_cache:
                    voice_cache["v"] = _voices(content, md, ffmpeg, say)
                vdur, files = voice_cache["v"]
            text, seconds, segs = compose.video_html(s, content, tokens, vdur)
            html_path.write_text(text, encoding="utf-8")
            audio = [(str(files[k]), int(start * 1000)) for k, start, _ in segs if k in files]
        old = manifest.get(s.id)
        can = bool(chrome) and (s.kind == "image" or bool(ffmpeg))
        sha = ""
        if can:
            say(f"rendering {s.id} ({s.width}x{s.height}{f', {seconds:g} s' if seconds else ''})")
            if s.kind == "image":
                render.render_png(html_path, out, s.width, s.height, 1.0, chrome)
            else:
                render.render_video(html_path, out, s.width, s.height, seconds, render.FPS, audio, chrome, ffmpeg, say)
                info = render.probe(out)
                if info and info.get("seconds"):
                    seconds = round(info["seconds"], 2)
            sha = mediamod.sha256_file(out)
        else:
            out.unlink(missing_ok=True)
            note = (render.CHROME_HINT if not chrome else render.FFMPEG_HINT) + " The HTML was written, so you can open it."
        keep = old and old.get("status") == "approved" and old.get("sha256") == sha and sha
        if old and old.get("status") == "approved" and not keep:
            reset.append(s.id)
        a = _asset(plan, kit_dir, seconds, sha, "approved" if keep else "draft", old["created"] if keep else _now())
        manifest.put(a)
        results.append(Result(a, can, note))
    manifest.save()
    return results, reset


def _voices(c: compose.Content, md: Path, ffmpeg: Optional[str], say) -> Tuple[Dict[str, float], Dict[str, Path]]:
    """Speak each scene's caption text. Each file's length sets how long its scene lasts."""
    d = md / "voice"
    d.mkdir(parents=True, exist_ok=True)
    durs: Dict[str, float] = {}
    files: Dict[str, Path] = {}
    for key, text in compose.script(c):
        if compose.FILL.search(text):
            say(f"No voice for {key}: it still has a [[fill: ...]] marker.")
            continue
        f = render.speak(text, d / f"{key}.aiff")
        info = render.probe(f)
        if not info or not info.get("seconds"):
            raise render.RenderError("can't read the length of the voice file. ffprobe is needed for --voice")
        durs[key], files[key] = info["seconds"], f
    return durs, files


# ---------------------------------------------------------- the draft links ---
def attach(kit, manifest: Manifest) -> List[str]:
    """List each channel's assets on its drafts. A draft that was approved goes back to draft, because
    what it covers has changed. Posted drafts are left alone. Returns notes for the person."""
    notes = []
    by_channel: Dict[str, List[dict]] = {}
    for a in manifest.assets:
        by_channel.setdefault(a["channel"], []).append(a)
    for d in kit.drafts():
        if d.meta.get("status") == "posted":
            continue
        assets = by_channel.get(d.channel, [])
        ids = ",".join(a["id"] for a in assets)
        was_approved = d.meta.get("status") == "approved"
        changed = d.meta.get("media", "") != ids
        if not assets and not d.meta.get("media"):
            continue
        if ids:
            d.meta["media"] = ids
        else:
            d.meta.pop("media", None)
        if "media" in d.fields and d.meta.get("status", "draft") == "draft":
            cur = d.fields.get("media", "")
            if not cur.strip() or compose.FILL.search(cur):
                d.fields["media"] = "Attached: " + ", ".join(
                    f"{Path(a['path']).name} ({a['width']}x{a['height']}" + (f", {a['seconds']:g} s" if a.get("seconds") else "") + ")"
                    for a in assets)
        if was_approved and (changed or d.state == "changed"):
            for k in ("approved_at", "approved_hash"):
                d.meta.pop(k, None)
            d.meta["status"] = "draft"
            notes.append(f"{d.id} was approved and its media changed, so it is a draft again. Check it and approve it again.")
        d.save()
    return notes


def run(kit, facts: dict, base: Path, net=None, only: Optional[List[str]] = None, voice: bool = False,
        style: Optional[str] = None, sets: Optional[List[str]] = None, screenshot: Optional[str] = None,
        preview: bool = False, say: Callable[[str], None] = lambda s: None, study_data: Optional[dict] = None) -> dict:
    from . import study as studymod
    if only:
        bad = [i for i in only if i not in BY_ID]
        if bad:
            raise CreateError(f"no such asset: {', '.join(bad)}. Assets: {', '.join(BY_ID)}")
    pb, _ = kit.playbooks.for_drafting(preview=preview)
    st = study_data if study_data is not None else studymod.load(base, kit.repo)
    fmts = media_formats(st)
    plans, skipped = select(pb, fmts, only)
    md = kit.dir / "media"
    inputs = load_inputs(md)
    inputs.update(parse_sets(sets))
    md.mkdir(parents=True, exist_ok=True)
    write_json(md / "inputs.json", {"note": "Words you gave `create --set`. They are used again on the next run.", "set": inputs})
    write_json(md / "formats.json", {"repo": kit.repo, "study": (st or {}).get("at", ""), "formats": fmts})
    tokens = look.resolve(facts, style)
    shot, alt = find_screenshot(facts, md, net, screenshot, say)
    results, reset = make(plans, facts, kit.dir, tokens, inputs, shot, alt, voice, say)
    manifest = Manifest(kit.dir)
    notes = attach(kit, manifest)
    return {"plans": plans, "skipped": skipped, "results": results, "reset": reset, "notes": notes, "formats": fmts,
            "playbook": pb, "screenshot": shot}


def fill_markers(asset: dict) -> List[str]:
    return sorted(set(mediamod.fills(Path(asset["html"]))))
