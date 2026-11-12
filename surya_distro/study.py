"""Study what is working now in a repo's space, before anything is drafted.

    <home>/<owner>/<repo>/study/
        study.json    every post read, with its numbers, tags, labels and score
        study.md      the outliers, and which formats beat their baseline
        config.json   the queries and the description of the space, which you can edit
        manual.csv    X and LinkedIn posts you paste in, since neither has a free read API

A post's score is how far its engagement lands above what its author's size and
account age predict (stats.py). Young or small accounts with high scores are the
ones worth learning from, because their reach didn't come from an audience they
already had. The study copies formats, never anyone's words.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import formats, sources, stats
from .formats import HOOK_TEXT, KIND_TEXT, TITLE_SHAPES, iso, parse_time
from .traffic import repo_dir, write_json

STALE_DAYS = 7
RELEVANT = 0.5
SMALL = {"hn": 1000, "reddit": 5000, "bluesky": 1000, "github": 100, "x": 1000, "linkedin": 1000}
YOUNG_DAYS = 365
DIMENSIONS = ("title_shape", "hook", "kind", "length", "media", "link", "day", "hour")
DIM_NAMES = {"title_shape": "title", "hook": "opening", "kind": "kind of post", "length": "length",
             "media": "media", "link": "link", "day": "day posted", "hour": "time posted"}

KIND_TERMS = (("claude-plugin", "Claude Code"), ("claude-code", "Claude Code"), ("mcp-server", "MCP server"))
GENERIC = set("""a an and are as at be by can for from has have in into is it its of on or our that the their them they
this to was we with you your yours own across through using use used uses make makes made one any other every each
more most less just only also than then when where what which who how why all out about over under via per not no
tool tools open source free plugin plugins server servers code claude github repo repos file files plain python
cli app apps project projects version release new ai agent agents model models data way work works like see talk
undo anything wrote write writes keep keeps step mcp codex machine machines private public
local simple small fast easy better first""".split())


def _paths(base: Path, repo: str) -> Dict[str, Path]:
    d = repo_dir(base, repo) / "study"
    return {"dir": d, "json": d / "study.json", "md": d / "study.md", "config": d / "config.json"}


def key_terms(facts: dict, n: int = 1) -> List[str]:
    """The nouns that name the repo's space, ranked by how often the README uses them."""
    name = [w.lower() for w in re.findall(r"[A-Za-z]+", facts.get("name", ""))]
    # A one-word name is a brand. In a longer name, only the first word is: "Acme Memory" is about memory.
    name_words = set(name if len(name) == 1 else name[:1])
    blob = " ".join([facts.get("description", ""), facts.get("summary", "")])
    cands = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", blob)]
    cands = [w for w in dict.fromkeys(cands) if w not in GENERIC and w not in name_words]
    readme = " ".join([facts.get("summary", "")] + [b.get("text", "") for b in facts.get("readme", {}).get("bullets", [])]).lower()
    counts = Counter({w: len(re.findall(r"\b" + re.escape(w) + r"s?\b", readme + " " + blob.lower())) for w in cands})
    return [w for w, c in counts.most_common() if c >= 2][:n]


def plan(facts: dict, queries: Optional[List[str]] = None, about: Optional[str] = None) -> Dict[str, object]:
    """Queries for the search APIs, and a sentence describing the space for the labeller."""
    kinds = facts.get("kinds", [])
    base = next((term for kind, term in KIND_TERMS if kind in kinds), "")
    nouns = key_terms(facts)
    if not queries:
        queries = []
        if base:
            queries.append(base)
        for noun in nouns:
            queries.append(f"{base} {noun}".strip())
            if base:
                queries.append(f"AI agent {noun}")
        if not queries:
            queries = [t for t in facts.get("topics", [])[:2]] or [facts.get("name", "")]
        queries = list(dict.fromkeys(q for q in queries if q))[:4]
    if not about:
        if base:
            about = f"{base}, AI coding agents and the tools around them" + (f", especially {' and '.join(nouns)}" if nouns else "")
        else:
            about = ", ".join(queries)
    terms = sorted({q.lower() for q in queries} | {n.lower() for n in nouns})
    return {"queries": queries, "about": about, "terms": terms}


def load(base: Path, repo: str) -> Optional[dict]:
    p = _paths(base, repo)["json"]
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(base: Path, repo: str) -> dict:
    try:
        return json.loads(_paths(base, repo)["config"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def age_days(study: Optional[dict], now: Optional[datetime] = None) -> Optional[float]:
    at = parse_time((study or {}).get("at"))
    if not at:
        return None
    return ((now or datetime.now(timezone.utc)) - at).total_seconds() / 86400


def is_stale(study: Optional[dict], now: Optional[datetime] = None, max_days: int = STALE_DAYS) -> bool:
    a = age_days(study, now)
    return a is None or a > max_days


def _subreddits(facts: dict) -> List[str]:
    from .channels import SUBREDDITS
    kinds = set(facts.get("kinds", []))
    return [s[0] for s in SUBREDDITS if s[1] & kinds][:4]


def score_posts(posts: List[dict]) -> Dict[str, dict]:
    """Fit a baseline per source on the relevant posts, and score every post against it."""
    models: Dict[str, dict] = {}
    by_src: Dict[str, List[dict]] = {}
    for p in posts:
        by_src.setdefault(p["source"], []).append(p)
    for src, group in by_src.items():
        feats = sources.FEATURES.get(src, [])
        rel = [p for p in group if p.get("relevant", 1) >= RELEVANT] or group
        model = stats.fit([p["x"] for p in rel], [p["engagement"] for p in rel], feats)
        models[src] = model
        for p in group:
            p["expected"] = round(stats.expected(model, p["x"]), 1)
            p["score"] = stats.score(model, p["engagement"], p["x"])
            size, age = p["x"].get("size"), p["x"].get("age_days")
            p["small"] = size is not None and size < SMALL.get(src, 1000)
            p["young"] = age is not None and age < YOUNG_DAYS
    return models


def run(facts: dict, net, gh, engine, base: Path, wanted: Optional[List[str]] = None, days: int = 90,
        queries: Optional[List[str]] = None, about: Optional[str] = None, now: Optional[datetime] = None,
        progress: Callable[[str], None] = lambda s: None) -> dict:
    now = now or datetime.now(timezone.utc)
    repo = facts["repo"]
    paths = _paths(base, repo)
    saved = load_config(base, repo)
    cfg = plan(facts, queries or saved.get("queries"), about or saved.get("about"))
    write_json(paths["config"], {"queries": cfg["queries"], "about": cfg["about"],
                                 "note": "Edit the queries or the description of the space, then run study again."})
    if hasattr(engine, "fallback"):
        engine.fallback.terms = cfg["terms"]
    elif hasattr(engine, "terms"):
        engine.terms = cfg["terms"]
    cache_path = base / "cache" / "authors.json"
    ctx = sources.Ctx(net=net, gh=gh, queries=cfg["queries"], now=now, days=days, subreddits=_subreddits(facts),
                      skip_repo=repo, study_dir=paths["dir"], cache=sources.load_cache(cache_path), progress=progress)
    posts: List[dict] = []
    notes: Dict[str, str] = {}
    for name in wanted or sources.ALL:
        progress(f"reading {name}")
        try:
            got, note = sources.COLLECTORS[name](ctx)
        except Exception as e:  # one source failing must not stop the others
            got, note = [], f"failed: {type(e).__name__}: {str(e)[:120]}"
        posts += got
        if note:
            notes[name] = note
        if name != "manual" and not got and not note:
            notes[name] = "no posts found"
    write_json(cache_path, ctx.cache)
    for p in posts:
        formats.tag(p)
    # Labels: reuse the ones this engine gave last time, so a refresh is cheap.
    old = {(p["source"], p["id"]): p for p in (load(base, repo) or {}).get("posts", [])}
    todo, idx = [], []
    for i, p in enumerate(posts):
        prev = old.get((p["source"], p["id"]))
        if prev and prev.get("labels", {}).get("engine") == engine.name and prev.get("about") == cfg["about"]:
            p["labels"] = prev["labels"]
        else:
            todo.append(p)
            idx.append(i)
    if todo:
        labels = engine.label(todo, cfg["about"], progress)
        for i, lab in zip(idx, labels):
            posts[i]["labels"] = lab
    for p in posts:
        lab = p.get("labels") or {}
        p["about"] = cfg["about"]
        p["relevant"] = lab.get("relevant", 0.5)
        p["tags"]["kind"] = lab.get("kind", "")
        p["tags"]["hook"] = lab.get("hook", "")
    models = score_posts(posts)
    engines_used = Counter((p.get("labels") or {}).get("engine", "") for p in posts)
    study = {
        "repo": repo, "at": iso(now), "days": days, "queries": cfg["queries"], "about": cfg["about"],
        "engine": engine.name, "engines_used": dict(engines_used),
        "sources": {s: {"posts": sum(1 for p in posts if p["source"] == s),
                        "relevant": sum(1 for p in posts if p["source"] == s and p["relevant"] >= RELEVANT),
                        "model": models.get(s), "note": notes.get(s, "")}
                    for s in sorted({p["source"] for p in posts} | set(notes))},
        "posts": posts,
    }
    write_json(paths["json"], study)
    paths["md"].write_text(render(study), encoding="utf-8")
    return study


# -------------------------------------------------------------------- render --
def relevant(study: dict, source: Optional[str] = None) -> List[dict]:
    return [p for p in study.get("posts", []) if p.get("relevant", 0) >= RELEVANT and (source is None or p["source"] == source)]


def group_rows(posts: List[dict], dim: str, min_n: int = 3) -> List[dict]:
    """For each tag in a dimension, how posts with it score against the rest."""
    rows = []
    values = Counter(p["tags"].get(dim, "") for p in posts)
    for value, n in values.most_common():
        if not value or value == "none" and dim in ("title_shape",) or n < min_n:
            continue
        group = [p["score"] for p in posts if p["tags"].get(dim) == value]
        rest = [p["score"] for p in posts if p["tags"].get(dim) != value]
        if not rest:
            continue
        row = stats.compare(group, rest)
        row.update(dim=dim, value=value, posts=sorted((p for p in posts if p["tags"].get(dim) == value),
                                                      key=lambda p: -p["score"]))
        rows.append(row)
    return sorted(rows, key=lambda r: -r["effect"])


def _num(n: float) -> str:
    return f"{n:,.0f}" if n >= 10 or n == int(n) else f"{n:.1f}"


def describe_engagement(p: dict) -> str:
    m = p.get("metrics", {})
    if p["source"] == "hn":
        s = f"{_num(m.get('points', 0))} points, {_num(m.get('comments', 0))} comments"
    elif p["source"] == "reddit":
        s = f"{_num(m.get('score', 0))} upvotes, {_num(m.get('comments', 0))} comments"
    elif p["source"] == "github":
        return f"{_num(m.get('stars', 0))} stars in {_num(p['x'].get('days') or 0)} days"
    elif p["source"] == "producthunt":
        s = f"{_num(m.get('votes', 0))} votes, {_num(m.get('comments', 0))} comments"
    else:
        s = f"{_num(m.get('likes', 0))} likes, {_num(m.get('reposts', 0))} reposts, {_num(m.get('replies', 0))} replies"
        if m.get("views"):
            s += f", {_num(m['views'])} views"
    if p.get("discussion") is not None:
        s += f" ({p['discussion']:.2f} comments per like)" if p["source"] not in ("hn", "reddit", "producthunt") \
            else f" ({p['discussion']:.2f} per point)"
    return s


def describe_author(p: dict) -> str:
    bits = []
    size = p["x"].get("size")
    unit = sources.SIZE_UNITS.get(p["source"], "")
    if size is not None and unit:
        bits.append(f"{_num(size)} {unit}")
    age = p["x"].get("age_days")
    if age is not None:
        bits.append(f"account {age / 365:.1f} years old" if age >= 365 else f"account {int(age // 30)} months old" if age >= 60 else f"account {int(age)} days old")
    flags = [f for f, on in (("small", p.get("small")), ("young", p.get("young"))) if on]
    if flags:
        bits.append(" and ".join(flags))
    return ", ".join(bits) or "unknown"


def describe_format(p: dict) -> str:
    t = p["tags"]
    out = []
    if t.get("title_shape") not in ("", "none", None):
        out.append(f"title: {t['title_shape']}")
    for k in ("kind", "hook", "length", "media", "link"):
        if t.get(k) and t[k] != "none":
            out.append(f"{DIM_NAMES[k]}: {t[k]}")
    return ", ".join(out)


def _title(p: dict, limit: int = 70) -> str:
    t = re.sub(r"\s+", " ", (p.get("title") or p.get("opening") or p.get("text") or "")).strip()
    t = t.replace("|", "/").replace("[", "(").replace("]", ")")
    return (t[:limit - 3] + "...") if len(t) > limit else t


def render(study: dict) -> str:
    rel = relevant(study)
    lines = [
        f"# Trend study: {study['about']}", "",
        f"For {study['repo']}, studied {study['at'][:10]}. Posts from the last {study['days']} days, at least a day old. "
        f"Queries: {', '.join(study['queries'])}.", "",
        f"Labels for relevance, kind of post and opening come from **{study['engine']}**. "
        "Scores are arithmetic, not a model: how far a post lands above what its author's size and account age predict, in standard deviations.",
        "", "Public data and official APIs only. Nothing was posted. Copy a format, never the words.", "",
        "## Sources", "", "| Source | Posts | Relevant | Baseline | Note |", "|---|---|---|---|---|",
    ]
    for src, s in study["sources"].items():
        m = s.get("model") or {}
        base = f"{m.get('kind')}, n={m.get('n')}" if m.get("n") else ""
        if m.get("kind") == "regression":
            base += " (" + ", ".join(m.get("features", [])) + ")"
        lines.append(f"| {sources.NAMES.get(src, src)} | {s['posts']} | {s['relevant']} | {base} | {s.get('note', '')} |")
    used = study.get("engines_used") or {}
    if len(used) > 1:
        lines += ["", "Labels by engine: " + ", ".join(f"{k or 'none'} {v}" for k, v in used.items()) + "."]

    def table(rows: List[dict]) -> List[str]:
        out = ["| Score | Source | Post | Engagement | Expected | Author | Format |", "|---|---|---|---|---|---|---|"]
        for p in rows:
            out.append(f"| {p['score']:+.1f} | {sources.NAMES.get(p['source'], p['source'])} | [{_title(p)}]({p['url']}) | "
                       f"{describe_engagement(p)} | {_num(p['expected'])} | {describe_author(p)} | {describe_format(p)} |")
        return out

    top = sorted(rel, key=lambda p: -p["score"])[:12]
    lines += ["", "## Outliers", "", "The relevant posts that beat their baseline by the most.", ""]
    lines += table(top) if top else ["None yet."]
    small = [p for p in sorted(rel, key=lambda p: -p["score"]) if p["score"] >= 1.0 and (p.get("small") or p.get("young"))][:10]
    lines += ["", "## Small or young accounts that won", "",
              "Small means under " + ", ".join(f"{v:,} {sources.SIZE_UNITS[k]} on {sources.NAMES[k]}" for k, v in SMALL.items() if k in ("hn", "bluesky", "github"))
              + f". Young means an account under a year old. These posts scored +1 or more.", ""]
    lines += table(small) if small else ["None in this sample."]
    lines += ["", "## What works, by source", "",
              "Each row compares posts with a tag to the other relevant posts from the same source. "
              "Only tags with three or more posts are shown.", ""]
    for src in study["sources"]:
        posts = relevant(study, src)
        if len(posts) < 6:
            continue
        lines += [f"### {sources.NAMES.get(src, src)} ({len(posts)} relevant posts)", "",
                  "| What | Tag | Posts | Mean score | The rest | Gap | Confidence |", "|---|---|---|---|---|---|---|"]
        for dim in DIMENSIONS:
            for r in group_rows(posts, dim):
                lines.append(f"| {DIM_NAMES[dim]} | {r['value']} | {r['n']} | {r['mean']:+.2f} | {r['mean_rest']:+.2f} | "
                             f"{r['effect']:+.2f} | {stats.confidence(r['n'], r['t'])} |")
        disc = [p["discussion"] for p in posts if p.get("discussion") is not None]
        if disc:
            lines += ["", f"Comments per like or point, the discussion signal: median {stats.median(disc):.2f}."]
        lines.append("")
    lines += ["## Tags", "",
              "- Title: " + ", ".join(f"{k} ({v})" for k, v in TITLE_SHAPES.items() if k != "none") + ".",
              "- Opening: " + ", ".join(f"{k} ({v})" for k, v in HOOK_TEXT.items()) + ".",
              "- Kind: " + ", ".join(f"{k} ({v})" for k, v in KIND_TEXT.items()) + ".",
              "- Times are UTC. Karma and follower counts are today's, not the day of the post.", ""]
    return "\n".join(lines)
