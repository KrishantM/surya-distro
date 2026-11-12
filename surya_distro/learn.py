"""Learn from your own posts: one Markdown note per finding, and a decisions log.

Where the notes go, first match wins:

    --memory DIR or $SURYA_DISTRO_MEMORY        a memory directory you name ("off" turns this off)
    $SURYA_MEMORY_DIR, ~/.surya-memory/store-path   Surya Memory's store, if it's set up
    autoMemoryDirectory in ~/.claude/settings.json  Claude Code's memory directory
    ~/.surya-distro/learnings/                  otherwise

In a memory directory the notes go in a `surya-distro/` folder, with one index
note. If the directory has a MEMORY.md index, one line pointing at that index
note is added to it, once. That is how the agents you use see what worked.
Nothing else in a memory directory is touched.

A finding from one launch is early: a sample of one. Notes are never deleted.
When the rules a note supports are superseded, the note is marked superseded.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from . import formats, measure, stats
from .formats import iso, parse_time
from .sources import FEATURES, NAMES

POINTER = "surya-distro/surya-distro-learnings.md"
SOURCE_FOR = {"hn": "hn", "reddit": "reddit", "bluesky": "bluesky", "x": "x", "linkedin": "linkedin", "producthunt": "producthunt"}


def _surya_memory_dir(env: Mapping[str, str], home: Path) -> Optional[Path]:
    if env.get("SURYA_MEMORY_DIR"):
        return Path(env["SURYA_MEMORY_DIR"]).expanduser()
    pinned = Path(env.get("SURYA_MEMORY_HOME") or home / ".surya-memory") / "store-path"
    try:
        value = pinned.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value).expanduser() if value else None


def _claude_memory_dir(home: Path) -> Optional[Path]:
    try:
        data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    return Path(value).expanduser() if isinstance(value, str) and value else None


def locate(flag: Optional[str], base: Path, env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> Tuple[Path, bool, str]:
    """(directory for the notes, is it a memory directory, why)."""
    env = os.environ if env is None else env
    home = home or Path.home()
    default = base / "learnings"
    chosen = flag or env.get("SURYA_DISTRO_MEMORY")
    if chosen:
        if chosen.strip().lower() in ("off", "none", "no"):
            return default, False, "memory turned off"
        return Path(chosen).expanduser() / "surya-distro", True, "--memory" if flag else "SURYA_DISTRO_MEMORY"
    sm = _surya_memory_dir(env, home)
    if sm:
        return sm / "surya-distro", True, "Surya Memory's store"
    cc = _claude_memory_dir(home)
    if cc:
        return cc / "surya-distro", True, "Claude Code's autoMemoryDirectory"
    return default, False, "the default store"


# ---------------------------------------------------------------- note files --
def _front(meta: Dict[str, str], top: Dict[str, str]) -> str:
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False) if k == 'description' else v}" for k, v in top.items()]
    lines.append("metadata:")
    lines += [f"  {k}: {v}" for k, v in meta.items()]
    return "\n".join(lines + ["---", ""])


def parse_note(text: str) -> Dict[str, str]:
    """Flat key/value pairs from a note's frontmatter, nested keys included."""
    out: Dict[str, str] = {}
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.strip().partition(":")
        if sep and v.strip():
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            out[k.strip()] = v
    return out


class Learnings:
    def __init__(self, root: Path, memory: bool, why: str = ""):
        self.root, self.memory, self.why = root, memory, why

    def notes(self) -> List[dict]:
        out = []
        for p in sorted(self.root.glob("distro-*.md")):
            meta = parse_note(p.read_text(encoding="utf-8"))
            if meta.get("source") != "surya-distro":
                continue
            meta["path"] = str(p)
            meta["rules"] = [r for r in meta.get("rules", "").split(", ") if r]
            meta["tags"] = dict(t.split("=", 1) for t in meta.get("tags", "").split(", ") if "=" in t)
            try:
                meta["score"] = float(meta["score"]) if meta.get("score") not in (None, "", "none") else None
            except ValueError:
                meta["score"] = None
            out.append(meta)
        return out

    def write(self, f: dict) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{f['name']}.md"
        if path.exists() and parse_note(path.read_text(encoding="utf-8")).get("source") != "surya-distro":
            path = self.root / f"{f['name']}-distro.md"   # never overwrite a note this tool didn't write
        meta = {"type": "project", "source": "surya-distro", "repo": f["repo"], "channel": f["channel"], "draft": f["draft"],
                "date": f["date"], "status": f.get("status", "active"), "confidence": f["confidence"],
                "sample_size": str(f["sample_size"]), "evidence": f["url"], "checkpoint": f.get("checkpoint") or "latest",
                "score": f"{f['score']:+.2f}" if f.get("score") is not None else "none",
                "rules": ", ".join(f.get("rules", [])), "tags": ", ".join(f"{k}={v}" for k, v in f.get("tags", {}).items())}
        path.write_text(_front(meta, {"name": f["name"], "description": f["description"]}) + f["body"], encoding="utf-8")
        return path

    def supersede(self, rule_ids: List[str], date: str) -> int:
        """Mark notes superseded when every rule they support has been superseded."""
        gone, n = set(rule_ids), 0
        for note in self.notes():
            if note.get("status") == "active" and note["rules"] and set(note["rules"]) <= gone:
                p = Path(note["path"])
                text = p.read_text(encoding="utf-8")
                text = re.sub(r"^(  status: )active$", r"\1superseded", text, count=1, flags=re.M)
                text = re.sub(r"^(  status: superseded)$", r"\1\n  superseded_on: " + date, text, count=1, flags=re.M)
                p.write_text(text, encoding="utf-8")
                n += 1
        return n

    def record_decision(self, repo: str, after: dict, before: Optional[dict], changed: List[dict], date: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "distro-decisions.md"
        if not path.exists():
            path.write_text(_front({"type": "project", "source": "surya-distro-log", "status": "active"},
                                   {"name": "distro-decisions", "description": "Surya Distro's decisions log: each playbook change with its before, after, evidence and date"})
                            + "# Playbook decisions\n\nEach approved playbook change, newest last. Rules are superseded, never deleted.\n", encoding="utf-8")
        lines = ["", f"## {date}: {repo} playbook v{after.get('version')} approved", ""]
        if not changed:
            lines.append("- No rule changed.")
        for c in changed:
            lines.append(f"- **{c['what'].capitalize()}** `{c['id']}`.")
            lines.append(f"  - Before: {c['before'] or 'none'}")
            lines.append(f"  - After: {c['after'] or 'none'}")
            lines.append(f"  - Evidence: {c['evidence'] or 'none given'}")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.write_index()
        return path

    def write_index(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        notes = self.notes()
        lines = ["# What worked in your launches", "",
                 "Written by Surya Distro. One note per finding, plus the decisions log. "
                 "A finding from one launch is early, a sample of one.", "", "- [Decisions log](distro-decisions.md)"]
        for n in sorted(notes, key=lambda n: n.get("date", ""), reverse=True):
            lines.append(f"- [{n.get('name')}]({Path(n['path']).name}) ({n.get('status')}, {n.get('confidence')}): {n.get('description', '')}")
        path = self.root / "surya-distro-learnings.md"
        path.write_text(_front({"type": "project", "source": "surya-distro-log", "status": "active"},
                               {"name": "surya-distro-learnings", "description": "Index of Surya Distro's launch findings: what worked, with evidence, and why the playbook changed"})
                        + "\n".join(lines) + "\n", encoding="utf-8")
        self.point()
        return path

    def point(self) -> bool:
        """Add one line to the memory directory's MEMORY.md, once."""
        if not self.memory:
            return False
        index = self.root.parent / "MEMORY.md"
        try:
            text = index.read_text(encoding="utf-8")
        except OSError:
            return False
        if POINTER in text:
            return False
        line = f"- [Surya Distro learnings]({POINTER}) — what worked in your launches, one note per finding, and the playbook decisions log"
        index.write_text(text.rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
        return True


# ------------------------------------------------------------------ findings --
def draft_tags(d, posted_at: str) -> Dict[str, str]:
    """The format of what was posted, tagged the way the study tags other posts."""
    from .engines import Heuristic
    from .playbook import FORM, MAIN_FIELD
    ch = d.channel
    main = d.fields.get(MAIN_FIELD.get(ch, ""), "")
    day, hour = formats.when(posted_at)
    media = d.fields.get("media", "")
    tags = {"length": formats.length_tag(main, FORM.get(ch, "long-form")) if main.strip() else "none",
            "link": "link-post" if ch == "hn" else formats.link_tag(main),
            "media": "video" if re.search(r"video|demo", media, re.I) else ("image" if media.strip() else "none"),
            "hook": Heuristic().one({"title": "", "text": main, "opening": formats.opening(main)})["hook"],
            "kind": "launch", "day": day, "hour": hour}
    if ch in ("hn", "reddit"):
        tags["title_shape"] = formats.title_shape(d.fields.get("title", ""))
    return tags


def followed(pb: Optional[dict], channel: str, tags: Dict[str, str]) -> List[str]:
    out = []
    for r in (pb or {}).get("rules", []):
        if r["status"] != "active" or r["channel"] != channel or r["dim"] not in tags:
            continue
        if (tags[r["dim"]] == r["value"]) == r["do"]:
            out.append(r["id"])
    return out


def _x(author: dict, posted_at: str) -> Dict[str, float]:
    x: Dict[str, float] = {}
    if author.get("size") is not None:
        x["size"] = float(author["size"])
    a, b = parse_time(author.get("created")), parse_time(posted_at)
    if a and b:
        x["age_days"] = (b - a).total_seconds() / 86400
    return x


def findings(base: Path, kit, study: Optional[dict], pb: Optional[dict], now: Optional[datetime] = None) -> List[dict]:
    now = now or datetime.now(timezone.utc)
    store = measure.Posts(base, kit.repo)
    out = []
    name = kit.repo.split("/")[1]
    for draft_id, rec in store.data.items():
        snap = measure.best(rec)
        if not snap:
            continue
        try:
            d = kit.draft(draft_id)
        except Exception:
            continue
        plat = rec.get("platform") or d.channel
        src = SOURCE_FOR.get(plat, plat)
        model = ((study or {}).get("sources", {}).get(src) or {}).get("model")
        x = _x(rec.get("author") or snap.get("author") or {}, rec.get("posted_at", ""))
        exp = stats.expected(model, x) if model and model.get("n") else None
        sc = stats.score(model, snap["engagement"], x) if exp is not None else None
        tags = draft_tags(d, rec.get("posted_at", ""))
        rules = followed(pb, d.channel, tags)
        g = measure.github_after(base, kit.repo, rec.get("posted_at", ""), plat)
        posted = rec.get("posted_at", "")[:10]
        when = snap.get("checkpoint") or f"{snap['age_hours']} hours"
        verdict = ("did about what an account this size gets" if sc is None or abs(sc) < 0.5 else
                   ("beat what an account this size gets" if sc > 0 else "did worse than an account this size usually gets"))
        desc = f"{NAMES.get(src, plat)} post for {kit.repo} {verdict}" + (f" ({sc:+.1f})" if sc is not None else "") + f", at {when}. Early: one launch"
        body = [f"# {d.id} on {NAMES.get(src, plat)}, posted {posted}", "", f"Post: {rec.get('url', '')}", "",
                f"- At {when}: {measure.describe(plat, snap['metrics'])}."]
        if exp is not None:
            size = x.get("size")
            body.append(f"- Expected for this account{f' ({size:,.0f} followers or karma)' if size is not None else ''}: "
                        f"{exp:,.0f}, from the study of {(study or {}).get('at', '')[:10]} (n={model['n']}). Score {sc:+.2f}.")
        else:
            body.append("- No study baseline for this channel, so there's no expected value to compare with.")
        if g:
            stars = g["stars_after"]
            body.append(f"- GitHub in the {g['days']} days after: {g['views_after']} views ({g['views_before']} in the {g['days']} days before), "
                        f"{g['clones_after']} clones, {('+' if (stars or 0) >= 0 else '') + str(stars) if stars is not None else 'unknown'} stars, "
                        f"{g['referrer_views']} views referred from {NAMES.get(src, plat)}." + ("" if g["complete"] else " Still counting."))
        body.append(f"- Format: " + ", ".join(f"{k} {v}" for k, v in tags.items() if v) + ".")
        body.append(f"- Playbook rules it followed: {', '.join(rules) or 'none'}.")
        body += ["", "Early: this rests on one launch. Treat it as a lead, not a rule.", ""]
        out.append({"name": f"distro-{name}-{draft_id}-{posted}".lower(), "description": desc, "repo": kit.repo,
                    "channel": d.channel, "draft": draft_id, "url": rec.get("url", ""), "date": iso(now)[:10],
                    "checkpoint": snap.get("checkpoint"), "score": sc, "expected": exp, "tags": tags, "rules": rules,
                    "github": g, "confidence": "early", "sample_size": 1, "status": "active", "body": "\n".join(body)})
    return out
