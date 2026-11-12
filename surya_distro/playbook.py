"""Strategy: turn the study and your own results into a playbook per channel.

    <home>/<owner>/<repo>/
        playbook.json            the approved playbook, which drafting follows
        playbook.md              the same, to read
        playbook.proposed.json   a change waiting for your yes
        playbook.proposed.md

Every rule cites its evidence: the posts, the numbers, the sample size and a
confidence. A change is shown as a diff and approved from your own terminal,
the same as a draft. Rules that stop being supported are marked superseded and
kept. Rules that rest only on your own launches are early, because one launch
is a sample of one.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import formats, stats
from .channels import CHANNELS, Issue
from .formats import HOOK_TEXT, KIND_TEXT, TITLE_SHAPES, iso
from .sources import NAMES
from .study import DIM_NAMES, RELEVANT, group_rows, relevant
from .traffic import repo_dir, write_json

# Channel -> the sources whose evidence it uses, in order of preference.
EVIDENCE = {
    "hn": ["hn"], "reddit": ["reddit"], "bluesky": ["bluesky"], "x": ["x", "bluesky"],
    "linkedin": ["linkedin"], "producthunt": ["producthunt"], "readme": ["github"],
}
CHANNEL_NAMES = {"readme": "The README"}
LONG = ("title_shape", "hook", "kind", "length", "media", "link", "day", "hour")
SHORT = ("hook", "kind", "length", "media", "link", "day", "hour")
DIMS = {"hn": LONG, "reddit": LONG, "linkedin": SHORT, "bluesky": SHORT, "x": SHORT,
        "producthunt": ("kind", "media", "day"), "readme": ("media", "hook")}
FORM = {"hn": "long-form", "reddit": "long-form", "linkedin": "long-form", "x": "short-form", "bluesky": "short-form",
        "producthunt": "long-form", "readme": "long-form"}
MAIN_FIELD = {"hn": "text", "reddit": "body", "x": "post", "bluesky": "post", "linkedin": "post",
              "producthunt": "first comment"}
FORMAT_DIMS = ("title_shape", "hook", "length", "media", "link")
CHECKED = ("title_shape", "length", "link", "media")   # by counting, so a failure is an error
LABELLED = ("hook",)                                    # by a label, so a failure is a warning
MIN_SOURCE, MIN_GROUP, MIN_EFFECT = 8, 3, 0.3
LENGTH_WORDS = {"long-form": {"short": "under 60 words", "medium": "60 to 250 words", "long": "over 250 words"},
                "short-form": {"short": "under 15 words", "medium": "15 to 40 words", "long": "over 40 words"}}
MEDIA_DO = {"video": "Show a short demo video.", "image": "Attach a screenshot or an image.",
            "demo-link": "Link to a demo.", "none": "Text alone does well here. Media didn't help."}
LINK_DO = {"link-post": "Submit it as a link to the repo, with the text alongside.",
           "link-card": "Add the link so it shows as a card.",
           "link-first": "Put the link in the first line.", "link-middle": "Put the link after the opening, mid-post.",
           "link-end": "Put the link at the end.", "no-link": "Leave the link out of the post, and add it in a reply."}
LINK_AVOID = {"link-first": "Don't put the link in the first line.", "link-middle": "Don't put the link in the middle of the post.",
              "link-end": "Don't leave the link to the end.", "no-link": "Don't leave the link out of the post."}
LEVELS = ("low", "medium", "high")
# Where a channel has no source of its own, or its source is pasted by hand.
SHORT_NAMES = {"x": "X", "linkedin": "LinkedIn"}
DAYS = dict(zip(formats.WEEKDAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")))


class PlaybookError(RuntimeError):
    pass


# ------------------------------------------------------------------- text -----
def _hours(value: str) -> str:
    """'18-24 UTC' -> '18:00 and 24:00 UTC'."""
    span = value.replace(" UTC", "").split("-")
    if len(span) == 2 and all(x.isdigit() for x in span):
        return f"{int(span[0]):02d}:00 and {int(span[1]):02d}:00 UTC"
    return value


def rule_id(channel: str, dim: str, value: str, do: bool) -> str:
    v = value.replace(" UTC", "").replace(" ", "-")
    return f"{channel}.{dim}.{v}" if do else f"{channel}.{dim}.not-{v}"


def rule_text(dim: str, value: str, do: bool, form: str) -> str:
    if dim == "title_shape":
        return f"Shape the title as {TITLE_SHAPES[value]}." if do else f"Avoid a title that is {TITLE_SHAPES[value]}."
    if dim == "hook":
        rest = HOOK_TEXT[value][len("opens "):]
        return f"Open {rest}." if do else f"Don't open {rest}."
    if dim == "kind":
        return f"Frame it as a post that {KIND_TEXT[value]}." if do else f"Don't frame it as a post that {KIND_TEXT[value]}."
    if dim == "length":
        w = LENGTH_WORDS[form][value]
        return f"Keep it {value}: {w}." if do else f"Don't go {value} ({w})."
    if dim == "media":
        return MEDIA_DO[value] if do else {"none": "Don't post without media."}.get(value, f"Media of this kind ({value}) did worse here.")
    if dim == "link":
        return LINK_DO[value] if do else LINK_AVOID.get(value, f"Avoid this link placement: {value}.")
    if dim == "day":
        day = DAYS.get(value, value)
        return f"Post on a {day}." if do else f"Avoid posting on a {day}."
    if dim == "hour":
        span = _hours(value)
        return f"Post between {span}." if do else f"Avoid posting between {span}."
    return f"{dim}: {value}"


def _audience(facts: dict) -> str:
    kinds = facts.get("kinds", [])
    who = []
    for k, label in (("claude-code", "Claude Code users"), ("mcp-server", "people who run MCP servers"),
                     ("python", "Python developers"), ("cli", "people who work in a terminal"), ("self-hosted", "people who self-host")):
        if k in kinds and label not in who:
            who.append(label)
    from .study import key_terms
    nouns = key_terms(facts)
    base = ", ".join(who[:2]) or "developers"
    return base + (f", who care about {' and '.join(nouns)}" if nouns else "")


def _assets(facts: dict) -> Dict[str, bool]:
    rd = facts.get("readme", {})
    rel = facts.get("release") or {}
    return {"screenshot or video": rd.get("images", 0) > 0, "install steps": bool(rd.get("has_install")),
            "comparison": bool(rd.get("comparison")), "release": bool(rel.get("tag")) and not rel.get("is_tag_only"),
            "license": bool(facts.get("license"))}


def _fit(dim: str, value: str, do: bool, facts: dict) -> str:
    a = _assets(facts)
    if do and dim == "media" and value in ("image", "video") and not a["screenshot or video"]:
        return "You have no screenshot or video yet. Make one before you post."
    if do and dim == "hook" and value == "result":
        return "Use a real number from your own use. Don't invent one."
    if do and dim == "kind" and value == "comparison":
        return "Your README has a comparison to start from." if a["comparison"] else "Your README has no comparison yet."
    if do and dim == "kind" and value == "release" and not a["release"]:
        return "There's no GitHub release yet."
    return ""


# ------------------------------------------------------------------ derive ----
def _evidence(study: dict, src: str, row: dict) -> dict:
    top = row["posts"][:3]
    return {
        "source": src, "study": study.get("at", "")[:10], "engine": study.get("engine", ""),
        "among": row.get("among", "relevant posts"), "n": row["n"], "n_rest": row["n_rest"], "mean": row["mean"], "mean_rest": row["mean_rest"],
        "effect": row["effect"], "t": row["t"], "confidence": stats.confidence(row["n"], row["t"]),
        "posts": [{"url": p["url"], "score": p["score"], "engagement": p["engagement"]} for p in top],
    }


README_MEDIA = {"image": "Put a screenshot near the top of the README.",
                "video": "Put a short demo GIF or video near the top of the README.",
                "none": "A README without images does fine here."}


def _rule(channel: str, dim: str, value: str, do: bool, facts: dict, date: str) -> dict:
    form = FORM[channel]
    rid = rule_id(channel, dim, value, do)
    checked = dim in CHECKED and channel in MAIN_FIELD and not (dim == "title_shape" and channel not in ("hn", "reddit")) \
        and not (dim == "link" and channel in ("hn", "producthunt")) and not (dim == "media" and (not do or value in ("none", "demo-link")))
    text = ""
    if channel == "readme" and dim == "media":
        text = README_MEDIA.get(value, "") if do else ("Don't ship the README without a screenshot or demo." if value == "none" else "")
    return {"id": rid, "channel": channel, "dim": dim, "value": value, "do": do, "text": text or rule_text(dim, value, do, form),
            "fit": _fit(dim, value, do, facts), "check": "count" if checked else ("label" if dim in LABELLED and channel in MAIN_FIELD else ""),
            "evidence": None, "learnings": [], "confidence": "low", "early": False, "status": "active", "since": date}


def derive(facts: dict, study: Optional[dict], findings: List[dict], date: str) -> Tuple[List[dict], Dict[str, dict]]:
    rules: Dict[str, dict] = {}
    channels: Dict[str, dict] = {}
    for channel, srcs in EVIDENCE.items():
        src, posts = None, []
        for s in srcs:
            got = relevant(study, s) if study else []
            if len(got) >= MIN_SOURCE:
                src, posts = s, got
                break
        info = {"source": src, "n": len(posts)}
        if src and src != srcs[0]:
            info["note"] = (f"{SHORT_NAMES.get(srcs[0], srcs[0])} has no free read API and too few posts were pasted, "
                            f"so the evidence is from {NAMES[src]}.")
        if not src and srcs[0] in SHORT_NAMES:
            info["note"] = (f"Too few {SHORT_NAMES[srcs[0]]} posts pasted into study/manual.csv ({MIN_SOURCE} needed). "
                            "The channel's base rules apply.")
        elif not src:
            names = " or ".join(NAMES.get(s, s) for s in srcs)
            info["note"] = f"Not enough relevant posts from {names} ({MIN_SOURCE} needed). The channel's base rules apply."
        channels[channel] = info
        if not src:
            continue
        # Formats are compared among launch posts when there are enough of them, since a launch is
        # what gets drafted. The kind of post and the timing are compared across everything relevant.
        launches = [p for p in posts if p["tags"].get("kind") in ("launch", "release") or p.get("show")]
        info["launches"] = len(launches)
        for dim in DIMS[channel]:
            pool = launches if dim in FORMAT_DIMS and len(launches) >= MIN_SOURCE else posts
            rows = [r for r in group_rows(pool, dim, MIN_GROUP) if r["value"] not in ("", "none") or dim in ("media", "link")]
            for r in rows:
                r["among"] = "launch posts" if pool is launches else "relevant posts"
            if not rows:
                continue
            best, worst = rows[0], rows[-1]
            if best["effect"] >= MIN_EFFECT and best["mean"] > 0:
                r = _rule(channel, dim, best["value"], True, facts, date)
                r["evidence"] = _evidence(study, src, best)
                r["confidence"] = r["evidence"]["confidence"]
                rules[r["id"]] = r
            if dim not in ("day", "hour") and worst is not best and worst["effect"] <= -MIN_EFFECT and worst["mean"] < 0 \
                    and not (dim == "kind" and worst["value"] in ("launch", "release")):
                r = _rule(channel, dim, worst["value"], False, facts, date)
                r["evidence"] = _evidence(study, src, worst)
                r["confidence"] = r["evidence"]["confidence"]
                rules[r["id"]] = r
    # Your own launches: evidence on the rules they followed, and early rules where the study has none.
    for f in findings:
        if f.get("status") != "active" or f.get("score") is None:
            continue
        ch = f.get("channel")
        entry = {"note": f.get("name"), "url": f.get("url"), "score": f["score"], "date": f.get("date"), "checkpoint": f.get("checkpoint")}
        for rid in f.get("rules", []):
            if rid in rules:
                rules[rid]["learnings"].append(entry)
        if f["score"] >= 1.0 and ch in DIMS:
            for dim, value in (f.get("tags") or {}).items():
                if dim not in DIMS[ch] or not value or value == "none":
                    continue
                if any(r["channel"] == ch and r["dim"] == dim and r["status"] == "active" and r["evidence"] for r in rules.values()):
                    continue
                rid = rule_id(ch, dim, value, True)
                r = rules.get(rid) or _rule(ch, dim, value, True, facts, date)
                r["early"] = True
                if entry not in r["learnings"]:
                    r["learnings"].append(entry)
                rules[rid] = r
    # An avoid-rule that only restates the do-rule for the same dimension adds nothing.
    # Keep it only when its own evidence is stronger.
    for rid in [k for k, r in rules.items() if not r["do"]]:
        r = rules[rid]
        dos = [d for d in rules.values() if d["do"] and d["channel"] == r["channel"] and d["dim"] == r["dim"]]
        if dos and all(LEVELS.index(r["confidence"]) <= LEVELS.index(d["confidence"]) for d in dos):
            del rules[rid]
    for r in rules.values():
        own = r["learnings"]
        if r["evidence"] is None:
            r["early"] = len(own) < 3
            r["confidence"] = "low"
        elif own and stats.mean([e["score"] for e in own]) < -0.5 and (r["do"]):
            i = LEVELS.index(r["confidence"])
            r["confidence"] = LEVELS[max(i - 1, 0)]
            r["disagree"] = True
        r["level"] = "error" if r["check"] == "count" and r["confidence"] in ("medium", "high") and not r["early"] else "warning"
    return sorted(rules.values(), key=lambda r: (list(EVIDENCE).index(r["channel"]), LONG.index(r["dim"]) if r["dim"] in LONG else 9, not r["do"])), channels


def _core(r: dict) -> tuple:
    return (r["id"], r["text"], r["status"], r.get("level"), r["confidence"], r["early"], r.get("fit", ""))


def digest(pb: Optional[dict]) -> str:
    if not pb:
        return ""
    raw = json.dumps(sorted(_core(r) for r in pb.get("rules", [])), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def propose(facts: dict, study: Optional[dict], findings: List[dict], current: Optional[dict], date: str) -> dict:
    new_rules, channels = derive(facts, study, findings, date)
    by_id = {r["id"]: r for r in new_rules}
    out: List[dict] = []
    for old in (current or {}).get("rules", []):
        if old["id"] in by_id:
            r = by_id.pop(old["id"])
            if old["status"] == "active":
                r["since"] = old.get("since", date)
            out.append(r)
        elif old["status"] == "active":
            gone = dict(old)
            gone.update(status="superseded", superseded_on=date,
                        reason=f"The study of {date} no longer supports it." if study else "No evidence supports it now.")
            out.append(gone)
        else:
            out.append(old)
    out += list(by_id.values())
    return {
        "repo": facts["repo"], "version": (current or {}).get("version", 0), "created": date,
        "study_at": (study or {}).get("at", ""), "engine": (study or {}).get("engine", ""), "about": (study or {}).get("about", ""),
        "learnings": len([f for f in findings if f.get("status") == "active"]),
        "product": {"name": facts.get("name", ""), "claim": facts.get("summary", ""), "audience": _audience(facts), "assets": _assets(facts)},
        "channels": channels, "rules": out,
    }


# ------------------------------------------------------------------- render ---
def _ev_text(r: dict) -> str:
    e = r.get("evidence")
    bits = []
    if e:
        posts = ", ".join(f"[{i}]({p['url']}) {p['score']:+.1f}" for i, p in enumerate(e["posts"], 1))
        bits.append(f"Among {NAMES.get(e['source'], e['source'])} {e.get('among', 'relevant posts')}, {e['n']} with this score "
                    f"{e['mean']:+.2f} on average, against {e['mean_rest']:+.2f} for the other {e['n_rest']} (gap {e['effect']:+.2f}). "
                    f"Confidence: {e['confidence']}. "
                    f"Top posts: {posts}. Study of {e['study']}, labels by {e['engine']}.")
    if r.get("learnings"):
        own = ", ".join(f"[{l.get('date', '')}]({l.get('url', '')}) {l['score']:+.1f}" for l in r["learnings"])
        bits.append(f"Your launches: {own}." + (" They did worse than the study suggests." if r.get("disagree") else ""))
    if r.get("early"):
        bits.append("Early: this rests on your own launches only, so on a sample of " + str(len(r.get("learnings", []))) + ".")
    return " ".join(bits)


def render(pb: dict, state: str) -> str:
    p = pb["product"]
    head = f"v{pb['version']}, approved {pb.get('approved_at', '')[:10]}" if state == "approved" else "proposed, not approved"
    lines = [f"# Playbook: {pb['repo']} ({head})", "",
             f"From the study of {pb.get('study_at', '')[:10] or 'no date'} ({pb.get('about') or 'no study'})"
             f" and {pb.get('learnings', 0)} finding(s) from your own launches.", "",
             "Drafting follows the approved playbook, and `check` holds drafts to it. Rules checked by counting are errors "
             "at medium or high confidence. Rules on how a post opens are warnings, because they rest on a label.", "",
             "Confidence: high means 15 or more posts and a clear gap. Medium means 6 or more and a likely gap. Low is the rest.", "",
             "## Product", "",
             f"- Claim: {p['claim']}",
             f"- Core audience (inferred from the repo, so confirm it): {p['audience']}.",
             "- Has: " + ", ".join(k for k, v in p["assets"].items() if v) + ". Missing: " + (", ".join(k for k, v in p["assets"].items() if not v) or "nothing") + ".", ""]
    for ch, info in pb["channels"].items():
        name = CHANNEL_NAMES.get(ch) or (CHANNELS[ch].name if ch in CHANNELS else ch)
        rules = [r for r in pb["rules"] if r["channel"] == ch and r["status"] == "active"]
        lines += [f"## {name}", ""]
        if info.get("note"):
            lines += [info["note"], ""]
        if info.get("n"):
            lines += [f"Evidence: {info['n']} relevant posts, {info.get('launches', 0)} of them launches.", ""]
        for r in rules:
            how = {"count": f"checked, {r['level']}", "label": "checked by label, warning"}.get(r["check"], "not checked")
            lines.append(f"- **{r['text']}** `{r['id']}` ({how}, {r['confidence']} confidence{', early' if r.get('early') else ''})")
            if r.get("fit"):
                lines.append(f"  - For this repo: {r['fit']}")
            lines.append(f"  - Evidence: {_ev_text(r)}")
        if not rules and info.get("n"):
            lines.append("No format stood out enough to make a rule.")
        lines.append("")
    old = [r for r in pb["rules"] if r["status"] == "superseded"]
    if old:
        lines += ["## Superseded", "", "Kept for the record. Drafting doesn't use them.", ""]
        lines += [f"- ~~{r['text']}~~ `{r['id']}`, superseded {r.get('superseded_on', '')}. {r.get('reason', '')}" for r in old]
        lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- store ---
class Playbooks:
    def __init__(self, base: Path, repo: str):
        self.repo = repo
        d = repo_dir(base, repo)
        self.json, self.md = d / "playbook.json", d / "playbook.md"
        self.pjson, self.pmd = d / "playbook.proposed.json", d / "playbook.proposed.md"

    @staticmethod
    def _load(p: Path) -> Optional[dict]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def approved(self) -> Optional[dict]:
        return self._load(self.json)

    def proposed(self) -> Optional[dict]:
        return self._load(self.pjson)

    def save_proposal(self, pb: dict) -> str:
        """Save a proposal if it changes a rule. If only the numbers moved, refresh the
        approved playbook's evidence in place, since no rule changed."""
        cur = self.approved()
        if cur and digest(pb) == digest(cur):
            pb.update(version=cur["version"], approved_at=cur.get("approved_at", ""))
            write_json(self.json, pb)
            self.md.write_text(render(pb, "approved"), encoding="utf-8")
            for p in (self.pjson, self.pmd):
                if p.exists():
                    p.unlink()
            return "unchanged"
        write_json(self.pjson, pb)
        self.pmd.write_text(render(pb, "proposed"), encoding="utf-8")
        return "proposed"

    def diff(self) -> str:
        pb = self.proposed()
        if not pb:
            return ""
        cur = self.approved()
        before = render(cur, "approved").splitlines() if cur else []
        after = render(pb, "proposed").splitlines()
        return "\n".join(difflib.unified_diff(before, after, "playbook (approved)" if cur else "no playbook yet",
                                              "playbook (proposed)", lineterm="", n=1))

    def for_drafting(self, preview: bool = False) -> Tuple[Optional[dict], str]:
        """The playbook drafts follow: the approved one, or the proposal with `preview`."""
        if preview and self.proposed():
            return self.proposed(), "proposal"
        return self.approved(), "approved"

    def approve(self, confirm: Callable[[str], bool], now: str, record: Optional[Callable[[dict, Optional[dict]], None]] = None) -> dict:
        pb = self.proposed()
        if not pb:
            raise PlaybookError(f"No playbook change is waiting for {self.repo}. Run: surya-distro strategy {self.repo}")
        if not confirm(self.diff()):
            raise PlaybookError("Not approved.")
        cur = self.approved()
        pb["version"] = (cur or {}).get("version", 0) + 1
        pb["approved_at"] = now
        write_json(self.json, pb)
        self.md.write_text(render(pb, "approved"), encoding="utf-8")
        for p in (self.pjson, self.pmd):
            if p.exists():
                p.unlink()
        if record:
            record(pb, cur)
        return pb


def changes(before: Optional[dict], after: dict) -> List[dict]:
    """What a playbook change did, rule by rule, for the decisions log."""
    old = {r["id"]: r for r in (before or {}).get("rules", [])}
    out = []
    for r in after.get("rules", []):
        o = old.get(r["id"])
        if o is None:
            if r["status"] == "active":
                out.append({"what": "added", "id": r["id"], "before": "", "after": r["text"], "evidence": _ev_text(r)})
        elif o["status"] == "active" and r["status"] == "superseded":
            out.append({"what": "superseded", "id": r["id"], "before": o["text"], "after": "", "evidence": r.get("reason", "")})
        elif _core(o) != _core(r):
            what = "restored" if o["status"] == "superseded" else "changed"
            out.append({"what": what, "id": r["id"], "before": f"{o['text']} ({o['confidence']}, {o.get('level')})",
                        "after": f"{r['text']} ({r['confidence']}, {r.get('level')})", "evidence": _ev_text(r)})
    return out


# -------------------------------------------------------------------- check ---
def _draft_value(channel: str, dim: str, fields: Dict[str, str]) -> Optional[str]:
    main = fields.get(MAIN_FIELD.get(channel, ""), "")
    if dim == "title_shape":
        return formats.title_shape(fields.get("title", ""))
    if dim == "length":
        return formats.length_tag(main, FORM[channel]) if main.strip() else None
    if dim == "link":
        return formats.link_tag(main)
    if dim == "hook":
        lead = formats.opening(main)
        if "[[fill" in lead:   # not written yet, and the placeholder check already says so
            return None
        from .engines import Heuristic
        return Heuristic().one({"title": "", "text": main, "opening": lead})["hook"]
    return None


def check(pb: Optional[dict], channel: str, fields: Dict[str, str]) -> List[Issue]:
    """Hold a draft to the approved playbook's rules that can be checked."""
    issues: List[Issue] = []
    if not pb:
        return issues
    for r in pb.get("rules", []):
        if r["status"] != "active" or r["channel"] != channel or not r.get("check"):
            continue
        level = r.get("level", "warning")
        field = "title" if r["dim"] == "title_shape" else MAIN_FIELD.get(channel, "")
        if r["dim"] == "media":
            if not (fields.get("media") or "").strip():
                issues.append(Issue(level, "media", f"playbook {r['id']}: {r['text']} Add a `media` field saying what you'll attach."))
            continue
        got = _draft_value(channel, r["dim"], fields)
        if got is None:
            continue
        broke = (got != r["value"]) if r["do"] else (got == r["value"])
        if broke:
            issues.append(Issue(level, field, f"playbook {r['id']}: {r['text']} This draft reads as {got}."))
    return issues


def active(pb: Optional[dict], channel: str) -> List[dict]:
    return [r for r in (pb or {}).get("rules", []) if r["status"] == "active" and r["channel"] == channel]
