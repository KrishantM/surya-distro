"""The media files in a kit, the manifest that lists them, and the checks that hold them to a channel.

    <home>/<owner>/<repo>/kit/media/
        media.json      every asset, in the shape the web app reads
        <id>.png|mp4    the rendered file
        <id>.html       its source, which a person can open in a browser
        formats.json    which media formats beat their baseline, per channel
        inputs.json     words the maker supplied with `create --set`

An asset is a draft until a person approves it from their own terminal. The
manifest records the SHA-256 of the file. If the file changes, the approval is
gone, and so is the approval of any draft that lists it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .channels import Issue, PLACEHOLDER_RE
from .render import png_size
from .traffic import write_json

VERSION = 1
KEYS = ("id", "channel", "kind", "path", "html", "width", "height", "seconds", "template", "follows", "status", "sha256", "created")


@dataclass(frozen=True)
class Spec:
    id: str
    channel: str
    kind: str          # image | video
    template: str
    variant: str
    width: int
    height: int
    label: str


SPECS: Tuple[Spec, ...] = (
    Spec("og-card", "og", "image", "card", "og", 1200, 630, "Open-graph card for link previews"),
    Spec("readme-hero", "readme", "image", "card", "hero", 1280, 640, "README hero and GitHub social preview"),
    Spec("x-image", "x", "image", "card", "wide", 1600, 900, "X image"),
    Spec("bluesky-image", "bluesky", "image", "card", "wide", 1600, 900, "Bluesky image"),
    Spec("linkedin-image", "linkedin", "image", "card", "square", 1200, 1200, "LinkedIn square image"),
    Spec("producthunt-thumb", "producthunt", "image", "thumb", "thumb", 240, 240, "Product Hunt thumbnail"),
    Spec("producthunt-gallery-1", "producthunt", "image", "gallery", "1", 1270, 760, "Product Hunt gallery 1: what it is"),
    Spec("producthunt-gallery-2", "producthunt", "image", "gallery", "2", 1270, 760, "Product Hunt gallery 2: what it does"),
    Spec("producthunt-gallery-3", "producthunt", "image", "gallery", "3", 1270, 760, "Product Hunt gallery 3: how to install"),
    Spec("x-video-16x9", "x", "video", "video", "16x9", 1920, 1080, "X explainer, 16:9"),
    Spec("x-video-1x1", "x", "video", "video", "1x1", 1080, 1080, "X explainer, 1:1"),
    Spec("bluesky-video-16x9", "bluesky", "video", "video", "16x9", 1920, 1080, "Bluesky explainer, 16:9"),
    Spec("linkedin-video-1x1", "linkedin", "video", "video", "1x1", 1080, 1080, "LinkedIn explainer, 1:1"),
    Spec("linkedin-video-9x16", "linkedin", "video", "video", "9x16", 1080, 1920, "LinkedIn explainer, 9:16"),
)
BY_ID: Dict[str, Spec] = {s.id: s for s in SPECS}
EXT = {"image": "png", "video": "mp4"}


def allowed_sizes(channel: str, kind: str) -> List[Tuple[int, int]]:
    return sorted({(s.width, s.height) for s in SPECS if s.channel == channel and s.kind == kind})


def limits() -> Dict[str, dict]:
    p = Path(__file__).with_name("data") / "media_limits.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("channels", {})
    except (OSError, ValueError):
        return {}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_CACHE: Dict[tuple, str] = {}


def file_hash(path: Path) -> str:
    """SHA-256 of a file, or "" when it isn't there. Remembered while the file's size and time are unchanged."""
    try:
        st = Path(path).stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        _CACHE[key] = sha256_file(path)
    return _CACHE[key]


class Manifest:
    def __init__(self, kit_dir: Path):
        self.dir = Path(kit_dir) / "media"
        self.path = self.dir / "media.json"
        self.assets: List[dict] = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.assets = list(data.get("assets", []))
        except (OSError, ValueError):
            pass

    def get(self, asset_id: str) -> Optional[dict]:
        return next((a for a in self.assets if a["id"] == asset_id), None)

    def put(self, asset: dict) -> None:
        asset = {k: asset[k] for k in KEYS if k in asset}
        for i, a in enumerate(self.assets):
            if a["id"] == asset["id"]:
                self.assets[i] = asset
                return
        self.assets.append(asset)

    def save(self) -> None:
        order = {s.id: i for i, s in enumerate(SPECS)}
        self.assets.sort(key=lambda a: (order.get(a["id"], 99), a["id"]))
        write_json(self.path, {"version": VERSION, "assets": self.assets})

    def refresh(self) -> List[str]:
        """Make the manifest tell the truth. An approved asset whose file changed or vanished goes back to draft.
        Returns the ids that were reset."""
        reset = []
        changed = False
        for a in self.assets:
            now = file_hash(Path(a["path"]))
            if a.get("status") == "approved" and now != a.get("sha256"):
                a["status"] = "draft"
                reset.append(a["id"])
                changed = True
            if now and now != a.get("sha256"):
                a["sha256"] = now
                changed = True
        if changed:
            self.save()
        return reset


def hash_of(media_dir: Path, asset_id: str) -> str:
    """The file hash a draft's approval covers for one asset: the file on disk, or 'missing'."""
    m = Manifest(Path(media_dir).parent)
    a = m.get(asset_id)
    if not a:
        return "missing"
    return file_hash(Path(a["path"])) or "missing"


# ------------------------------------------------------------------ checks ---
def fills(html_path: Path) -> List[str]:
    try:
        text = Path(html_path).read_text(encoding="utf-8")
    except OSError:
        return []
    return PLACEHOLDER_RE.findall(text)


def issues(a: dict, probe: Optional[Callable[[Path], Optional[dict]]] = None) -> List[Issue]:
    """What stops an asset being approved: a marker still showing, the wrong size, a video over the limit, a missing file."""
    out: List[Issue] = []
    name = a["id"]
    path = Path(a["path"])
    for ph in fills(Path(a["html"])):
        out.append(Issue("error", name, f"shows {ph}. Supply it with `create --set` or write it in, then render again"))
    if not path.exists():
        out.append(Issue("error", name, "not rendered yet. Install Chrome" + (" and ffmpeg" if a["kind"] == "video" else "")
                         + ", then run `create` again. The HTML source is there to open by hand"))
        return out
    sizes = allowed_sizes(a["channel"], a["kind"])
    if sizes and (a["width"], a["height"]) not in sizes:
        out.append(Issue("error", name, f"{a['width']}x{a['height']} isn't a size for {a['channel']} {a['kind']}s. "
                                        f"Use " + " or ".join(f"{w}x{h}" for w, h in sizes)))
    if a["kind"] == "image":
        got = png_size(path)
        if got and got != (a["width"], a["height"]):
            out.append(Issue("error", name, f"the file is {got[0]}x{got[1]}, but the manifest says {a['width']}x{a['height']}"))
    elif probe:
        info = probe(path)
        if info:
            if (info.get("width"), info.get("height")) != (a["width"], a["height"]):
                out.append(Issue("error", name, f"the file is {info.get('width')}x{info.get('height')}, "
                                                f"but the manifest says {a['width']}x{a['height']}"))
            if info.get("seconds"):
                a = dict(a, seconds=max(float(a.get("seconds") or 0), float(info["seconds"])))
    if a["kind"] == "video":
        cap = (limits().get(a["channel"]) or {}).get("max_seconds")
        if cap and float(a.get("seconds") or 0) > cap:
            out.append(Issue("error", name, f"{a['seconds']:.0f} seconds is over {a['channel']}'s {cap} second limit"))
    if a.get("sha256") and file_hash(path) != a["sha256"]:
        out.append(Issue("error", name, "the file changed after it was rendered. Run `create` again"))
    return out


def approvable(a: dict, probe=None) -> List[Issue]:
    return [i for i in issues(a, probe) if i.level == "error"]


MEDIA_REF = re.compile(r"^media:(.+)$")
