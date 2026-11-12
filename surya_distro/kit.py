"""A launch kit on disk: drafts, the brief, the targets, and one review sheet.

    <home>/<owner>/<repo>/kit/
        review.md          everything in one page, for the person deciding
        brief.md           facts and channel rules, for whoever rewrites drafts
        facts.json         what was read from GitHub
        targets.json       the last live check
        drafts/<id>.md     one draft per channel

A draft's status is draft, approved or posted. Approval records a hash of the
draft. Editing an approved draft makes it a draft again, so what gets posted is
what was approved.

Each draft records the playbook it followed. `check` holds it to the approved
playbook, and refuses a draft written from a playbook change nobody approved.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import playbook as pbmod
from .channels import CHANNELS, Issue, lint
from .traffic import repo_dir, write_json

MARKER = re.compile(r"^::: (.+?)\s*$")


class KitError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Draft:
    id: str
    path: Path
    meta: Dict[str, str] = field(default_factory=dict)
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def channel(self) -> str:
        return self.meta.get("channel", "")

    def digest(self) -> str:
        raw = json.dumps(self.fields, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def state(self) -> str:
        s = self.meta.get("status", "draft")
        if s in ("approved", "posted") and self.meta.get("approved_hash") != self.digest():
            return "changed"
        return s

    def issues(self, pb: Optional[dict] = None, proposed: Optional[dict] = None) -> List[Issue]:
        """Channel rules, then the approved playbook `pb`. A preview draft, written from the
        waiting change `proposed`, is held to that change's rules so it can be fixed before approval."""
        out = lint(self.channel, self.fields)
        followed = self.meta.get("playbook", "")
        against = pb
        if followed.startswith("proposal") and followed.split()[-1] != pbmod.digest(pb):
            out.append(Issue("error", "playbook", "written from a playbook change that isn't approved yet. "
                                                  "Approve it with `surya-distro approve <repo> playbook`, or redraft."))
            if proposed and followed.split()[-1] == pbmod.digest(proposed):
                against = proposed
        out += pbmod.check(against, self.channel, self.fields)
        return out

    def errors(self, pb: Optional[dict] = None, proposed: Optional[dict] = None) -> List[Issue]:
        return [i for i in self.issues(pb, proposed) if i.level == "error"]

    def render(self) -> str:
        head = ["---"] + [f"{k}: {v}" for k, v in self.meta.items()] + ["---", ""]
        body = []
        for k, v in self.fields.items():
            body += [f"::: {k}", v.strip(), ""]
        return "\n".join(head + body)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Draft":
        text = path.read_text(encoding="utf-8")
        meta: Dict[str, str] = {}
        lines = text.split("\n")
        i = 0
        if lines and lines[0].strip() == "---":
            i = 1
            while i < len(lines) and lines[i].strip() != "---":
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    meta[k.strip()] = v.strip()
                i += 1
            i += 1
        fields: Dict[str, str] = {}
        current: Optional[str] = None
        buf: List[str] = []
        for line in lines[i:]:
            m = MARKER.match(line)
            if m:
                if current is not None:
                    fields[current] = "\n".join(buf).strip()
                current, buf = m.group(1), []
            elif current is not None:
                buf.append(line)
        if current is not None:
            fields[current] = "\n".join(buf).strip()
        return cls(id=path.stem, path=path, meta=meta, fields=fields)


class Kit:
    def __init__(self, base: Path, repo: str):
        self.repo = repo
        self.dir = repo_dir(base, repo) / "kit"
        self.drafts_dir = self.dir / "drafts"
        self.playbooks = pbmod.Playbooks(base, repo)

    def issues(self, d: "Draft") -> List[Issue]:
        return d.issues(self.playbooks.approved(), self.playbooks.proposed())

    def errors(self, d: "Draft") -> List[Issue]:
        return [i for i in self.issues(d) if i.level == "error"]

    def drafts(self) -> List[Draft]:
        if not self.drafts_dir.exists():
            return []
        return [Draft.load(p) for p in sorted(self.drafts_dir.glob("*.md"))]

    def draft(self, draft_id: str) -> Draft:
        p = self.drafts_dir / f"{draft_id}.md"
        if not p.exists():
            have = ", ".join(d.id for d in self.drafts()) or "none"
            raise KitError(f"no draft {draft_id!r} for {self.repo}. Drafts: {have}")
        return Draft.load(p)

    def write_drafts(self, drafts: Dict[str, tuple], force: bool = False, playbook: str = "none") -> Dict[str, str]:
        """Write first drafts. Existing drafts are kept unless `force`, and approved
        or posted ones are always kept."""
        done: Dict[str, str] = {}
        for draft_id, (channel, fields) in drafts.items():
            path = self.drafts_dir / f"{draft_id}.md"
            fields = dict(fields)
            note = fields.pop("note", "")
            if path.exists():
                old = Draft.load(path)
                if old.meta.get("status") in ("approved", "posted"):
                    done[draft_id] = f"kept ({old.meta['status']})"
                    continue
                if not force:
                    done[draft_id] = "kept (already drafted)"
                    continue
            meta = {"draft": draft_id, "channel": channel, "status": "draft", "created": now_iso(), "playbook": playbook}
            if note:
                meta["note"] = note
            Draft(draft_id, path, meta, fields).save()
            done[draft_id] = "written"
        return done

    # -- decisions, made by a person -----------------------------------------
    def approve(self, draft_id: str, confirm: Callable[[Draft], bool]) -> Draft:
        d = self.draft(draft_id)
        errs = self.errors(d)
        if errs:
            raise KitError(f"{draft_id} has {len(errs)} problem(s) to fix first:\n" + "\n".join(f"  {e}" for e in errs))
        if d.state == "approved":
            return d
        if not confirm(d):
            raise KitError("Not approved.")
        d.meta.update(status="approved", approved_at=now_iso(), approved_hash=d.digest())
        d.meta.pop("posted_at", None)
        d.meta.pop("post_url", None)
        d.save()
        return d

    def posted(self, draft_id: str, url: str = "") -> Draft:
        d = self.draft(draft_id)
        if d.state == "changed":
            raise KitError(f"{draft_id} changed after it was approved. Approve it again first.")
        if d.state != "approved" and d.state != "posted":
            raise KitError(f"{draft_id} isn't approved. Approve it before you post it.")
        d.meta.update(status="posted", posted_at=now_iso())
        if url:
            d.meta["post_url"] = url
        d.save()
        return d

    def outbox(self) -> List[dict]:
        """Approved emails as [{to, subject, body}], the shape a simple sender reads."""
        out = []
        for d in self.drafts():
            if d.channel == "email" and d.state == "approved":
                out.append({"to": d.fields.get("to", ""), "subject": d.fields.get("subject", ""), "body": d.fields.get("body", "")})
        return out

    # -- files for people and agents -----------------------------------------
    def save_facts(self, facts_json: dict) -> None:
        write_json(self.dir / "facts.json", facts_json)

    def facts(self) -> dict:
        p = self.dir / "facts.json"
        if not p.exists():
            raise KitError(f"no kit for {self.repo} yet. Run: surya-distro draft {self.repo}")
        return json.loads(p.read_text(encoding="utf-8"))

    def save_targets(self, results: List[dict], checked_at: str) -> None:
        write_json(self.dir / "targets.json", {"checked_at": checked_at, "results": results})

    def targets(self) -> Optional[dict]:
        p = self.dir / "targets.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ------------------------------------------------------------------ readiness --
def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3}


def readiness(f: dict) -> List[tuple]:
    """(ok, text) pairs: what to fix on the repo before posting anywhere."""
    out: List[tuple] = []
    rel = f.get("release")
    if not rel:
        out.append((False, "No release or tag yet. A release gives posts a version and notes to point at."))
    elif rel.get("is_tag_only"):
        out.append((False, f"Tagged {rel['tag']}, but there's no GitHub release with notes."))
    else:
        out.append((True, f"Latest release {rel['tag']}."))
    out.append((bool(f.get("topics")), "Repo topics set." if f.get("topics") else "No topics on the repo. GitHub search and some directories use them."))
    desc, summ = f.get("description", ""), f.get("summary", "")
    a, b = _words(desc), _words(summ)
    close = bool(a and b) and len(a & b) / len(a | b) >= 0.25
    if not desc:
        out.append((False, "No GitHub description. It shows in search results and link previews."))
    elif not close:
        out.append((False, f"The GitHub description doesn't match the README's opening. Description: \"{desc}\""))
    else:
        out.append((True, "GitHub description matches the README."))
    rd = f.get("readme", {})
    out.append((rd.get("images", 0) > 0, "README has an image." if rd.get("images", 0) else "No screenshot or GIF in the README."))
    out.append((bool(rd.get("has_install")), "README has an install section." if rd.get("has_install") else "No install section in the README."))
    out.append((bool(f.get("license")), f"License: {f['license']}." if f.get("license") else "No license. Most lists and directories need one."))
    out.append((bool(rd.get("has_tests")), "README says how to run the tests." if rd.get("has_tests") else "README doesn't say how to run tests."))
    return out


# --------------------------------------------------------------------- render --
def _status_line(d: Draft, issues: List[Issue]) -> str:
    errs = [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    state = {"changed": "changed after approval"}.get(d.state, d.state)
    return f"{state}, {len(errs)} error(s), {len(warns)} warning(s)"


def _playbook_brief(pb: Optional[dict], state: str) -> List[str]:
    if not pb:
        return ["## Playbook", "", "No playbook yet. Follow the channel rules below. `surya-distro strategy <repo>` proposes one from the study.", ""]
    head = f"v{pb.get('version')}, approved" if state == "approved" else "a proposal nobody has approved yet. Drafts written from it can't be approved until it is"
    lines = ["## Playbook (follow it)", "", f"These drafts follow the playbook: {head}. Every rule cites the posts behind it in `playbook.md`.",
             "`check` enforces the rules it can count. It warns on the rules about how a post opens.", ""]
    for ch in list(CHANNELS) + ["readme"]:
        rules = pbmod.active(pb, ch)
        if not rules:
            continue
        name = CHANNELS[ch].name if ch in CHANNELS else "The README"
        lines.append(f"### {name}")
        lines.append("")
        for r in rules:
            e = r.get("evidence") or {}
            ev = f"{e.get('n')} posts, gap {e.get('effect', 0):+.2f}" if e else f"{len(r.get('learnings', []))} of your launches"
            lines.append(f"- {r['text']} ({r['confidence']} confidence{', early' if r.get('early') else ''}, {ev}){' ' + r['fit'] if r.get('fit') else ''}")
        lines.append("")
    return lines


def render_brief(f: dict, pb: Optional[dict] = None, state: str = "approved") -> str:
    rd = f.get("readme", {})
    rel = f.get("release") or {}
    lines = [
        f"# Brief: {f['name']}", "",
        "Use these facts and nothing else. If a draft needs something that isn't here, like why it was built, ask the person and keep their words.",
        "", "## Facts", "",
        f"- Repo: {f['url']}",
        f"- One-line claim: {f.get('summary', '')}",
        f"- License: {f.get('license') or 'none'}",
        f"- Release: {rel.get('tag') or 'none'}",
        f"- Kinds: {', '.join(f.get('kinds', []))}",
        f"- Stars: {f.get('stars', 0)}, created {f.get('created', '')[:10]}",
        "", "Features, from the README:", "",
    ]
    lines += [f"- {b['text']}" for b in rd.get("bullets", [])] or ["- (none listed)"]
    if rd.get("install"):
        lines += ["", "Install:", "", "```", rd["install"], "```"]
    if rd.get("limits"):
        lines += ["", "Limits, from the README:", ""] + [f"- {l}" for l in rd["limits"]]
    if rel.get("body"):
        lines += ["", "Release notes:", "", rel["body"].strip()]
    lines += [""] + _playbook_brief(pb, state)
    lines += ["## Writing rules", "",
              "- Plain sentences. Say what it does and what it doesn't.",
              "- No hype words, no superlatives, no em dashes. `surya-distro check` flags them.",
              "- Replace every `[[fill: ...]]` with the person's own words. Don't invent a motive, a user count or a result.",
              "- One idea per post. Link the repo, not a landing page.",
              "", "## Channel rules", ""]
    for ch in CHANNELS.values():
        limits = ", ".join(f"{r.field} {r.max}" for r in ch.rules if r.max)
        lines.append(f"### {ch.name}")
        lines.append("")
        if limits:
            lines.append(f"- Limits: {limits} characters.")
        lines += [f"- {n}" for n in ch.notes]
        lines.append("")
    return "\n".join(lines)


def render_review(kit: Kit) -> str:
    f = kit.facts()
    t = kit.targets()
    lines = [
        f"# Launch kit: {f['name']}", "",
        f"Repo: {f['url']}. Generated by surya-distro.", "",
        "**Nothing here has been posted, submitted or sent.** Each draft waits for a person to approve it and post it by hand.",
        "",
        f"Approve a draft from your own terminal: `surya-distro approve {kit.repo} <draft>`. After posting it, record it: `surya-distro posted {kit.repo} <draft> --url <link>`.",
        "", "## Before you post", "",
    ]
    lines += [f"- [{'x' if ok else ' '}] {text}" for ok, text in readiness(f)]
    from .study import load as load_study
    st = load_study(kit.dir.parent.parent.parent, kit.repo)
    lines += ["", "## Study", ""]
    if st:
        from .sources import NAMES
        counts = ", ".join(f"{s['relevant']} from {NAMES.get(src, src)}" for src, s in st["sources"].items() if s.get("relevant"))
        lines.append(f"Studied {st['at'][:10]}, labels by {st['engine']}. Relevant posts: {counts or 'none'}. "
                     "The outliers and what beat its baseline are in `study/study.md`.")
        gaps = [(NAMES.get(src, src), s["note"]) for src, s in st["sources"].items() if s.get("note") and not s.get("posts")]
        if gaps:
            lines += ["", "Not read:"] + [f"- {name}: {note.rstrip('.')}." for name, note in gaps]
    else:
        lines.append(f"Not run. `surya-distro study {kit.repo}` reads what works in the repo's space first.")
    approved, proposed = kit.playbooks.approved(), kit.playbooks.proposed()
    lines += ["", "## Playbook", ""]
    if approved:
        lines.append(f"Approved: v{approved.get('version')}, from the study of {approved.get('study_at', '')[:10]}. See `playbook.md`.")
    if proposed:
        lines.append(f"**A playbook change is waiting for you.** Read `playbook.proposed.md`, then approve it from your own terminal: "
                     f"`surya-distro approve {kit.repo} playbook`. Drafts written from it can't be approved until you do.")
        strong = [r for r in proposed.get("rules", []) if r["status"] == "active" and r["confidence"] in ("medium", "high")]
        if strong:
            lines += ["", "Its medium and high confidence rules:"]
            lines += [f"- {pbmod.CHANNEL_NAMES.get(r['channel'], CHANNELS[r['channel']].name if r['channel'] in CHANNELS else r['channel'])}: "
                      f"{r['text']} ({r['confidence']}, {r['evidence']['n'] if r.get('evidence') else 'own launches'} posts)" for r in strong]
    if not approved and not proposed:
        lines.append("None yet. Drafts follow the channel rules only.")
    lines += ["", "## Drafts", ""]
    order = list(CHANNELS)
    for d in sorted(kit.drafts(), key=lambda d: (order.index(d.channel) if d.channel in order else len(order), d.id)):
        ch = CHANNELS.get(d.channel)
        lines.append(f"### {d.id} ({ch.name if ch else d.channel})")
        lines.append("")
        issues = kit.issues(d)
        lines.append(f"Status: {_status_line(d, issues)}. Playbook: {d.meta.get('playbook', 'none')}.")
        if d.meta.get("note"):
            lines.append(f"Note: {d.meta['note']}")
        if ch and ch.submit and d.channel != "email":
            lines.append(f"Post it here: {ch.submit.format(subreddit=d.fields.get('subreddit', ''))}")
        for i in issues:
            lines.append(f"- {i}")
        lines.append("")
        for k, v in d.fields.items():
            lines.append(f"**{k}**")
            lines.append("")
            lines.append("````text")   # four backticks, so a code block inside a draft survives
            lines.append(v)
            lines.append("````")
            lines.append("")
    lines += ["## Where to submit", ""]
    if t:
        from .targets import Result, render
        lines.append(render([Result(**r) for r in t["results"]], t["checked_at"]))
    else:
        lines.append("Not checked yet. Run `surya-distro targets " + kit.repo + "`.")
    lines.append("")
    return "\n".join(lines)


def write_docs(kit: Kit) -> Path:
    f = kit.facts()
    pb, state = kit.playbooks.for_drafting(preview=any(d.meta.get("playbook", "").startswith("proposal") for d in kit.drafts()))
    (kit.dir / "brief.md").write_text(render_brief(f, pb, state), encoding="utf-8")
    p = kit.dir / "review.md"
    p.write_text(render_review(kit), encoding="utf-8")
    return p
