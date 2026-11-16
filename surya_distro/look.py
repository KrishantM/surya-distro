"""Style tokens for the media: colours and fonts.

The repo picks them, so the same repo always looks the same: a hue from the
repo's name, flat colours, and fonts from the system stack, so nothing loads
from the network. `--style FILE` is a JSON object that overrides any token.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import re
from pathlib import Path
from typing import Dict

SANS = 'ui-sans-serif, -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
MONO = 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace'
SERIF = '"New York", "Iowan Old Style", Charter, Georgia, "Times New Roman", serif'

TOKENS = ("bg", "panel", "fg", "muted", "accent", "accent_fg", "line", "font_display", "font_sans", "font_mono")
COLOUR = re.compile(r"^(#[0-9a-fA-F]{3,8}|(rgb|hsl)a?\([0-9 ,.%/-]+\))$")
FONT = re.compile(r'^[A-Za-z0-9 ,"\'-]+$')


class StyleError(ValueError):
    pass


def _hex(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def tokens_for(facts: dict) -> Dict[str, str]:
    """Colours and fonts for a repo. Dark, flat, one accent."""
    seed = int(hashlib.sha256(facts.get("repo", facts.get("name", "")).lower().encode()).hexdigest()[:8], 16)
    hue = seed % 360
    serif = (seed >> 9) % 4 == 0
    return {
        "bg": _hex(hue, 0.16, 0.075), "panel": _hex(hue, 0.15, 0.125), "fg": _hex(hue, 0.20, 0.95),
        "muted": _hex(hue, 0.10, 0.66), "accent": _hex(hue, 0.72, 0.64), "accent_fg": _hex(hue, 0.30, 0.08),
        "line": _hex(hue, 0.12, 0.22),
        "font_display": SERIF if serif else SANS, "font_sans": SANS, "font_mono": MONO,
    }


def load_style(path) -> Dict[str, str]:
    """A person's overrides, from a JSON file. Unknown keys and unsafe values are refused."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StyleError(f"can't read the style file {path}: {e}")
    if not isinstance(raw, dict):
        raise StyleError("the style file must be a JSON object, like {\"accent\": \"#ff6a00\"}")
    out = {}
    for k, v in raw.items():
        if k not in TOKENS:
            raise StyleError(f"unknown style key {k!r}. Keys: {', '.join(TOKENS)}")
        ok = FONT.match(str(v)) if k.startswith("font_") else COLOUR.match(str(v))
        if not ok:
            raise StyleError(f"{k}: {v!r} isn't a plain {'font stack' if k.startswith('font_') else 'colour (hex, rgb() or hsl())'}")
        out[k] = str(v)
    return out


def resolve(facts: dict, style_file=None) -> Dict[str, str]:
    t = tokens_for(facts)
    if style_file:
        t.update(load_style(style_file))
    return t
