"""The flow, every step named, and how much of each is automated.

    study → strategy → draft → create → check → approve → post → posted → measure → learn

`automation` is the honest field:

    automatic  runs without anyone in the loop
    assisted   drafted, checked or proposed here, and a person decides
    manual     a person does it

Posting stays manual on purpose. Every post is a draft until a person approves
it and posts it themselves. Playbook changes stop for a person's yes the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class Step:
    key: str
    automation: str
    does: str


FLOW = (
    Step("study", "automatic", "reads public posts in the repo's space and scores each against its author's size and age"),
    Step("strategy", "assisted", "turns the study and your results into a playbook, shown as a diff"),
    Step("draft", "assisted", "drafts one post per channel, following the approved playbook"),
    Step("create", "assisted", "makes the images and videos, in the media formats the study and the playbook favour"),
    Step("check", "automatic", "holds each draft and each media file to its channel's rules and the playbook"),
    Step("approve", "manual", "you approve drafts, media files and playbook changes, in your own terminal"),
    Step("post", "manual", "you post each draft by hand"),
    Step("posted", "manual", "you record the link"),
    Step("measure", "automatic", "reads each post at 1 hour, 24 hours and 7 days, and stores GitHub traffic"),
    Step("learn", "assisted", "writes one note per finding and proposes playbook changes"),
)


def cells(base: Path, repo: str, learn_root: Path) -> dict:
    """Where the repo is in each step, as one line of text per step key. `status` prints it."""
    from . import measure, study as study_mod, traffic
    from .kit import Kit
    kit = Kit(base, repo)
    st = study_mod.load(base, repo)
    pbs = kit.playbooks
    approved, proposed = pbs.approved(), pbs.proposed()
    drafts = kit.drafts()
    errs = sum(1 for d in drafts if kit.errors(d))
    posts = measure.Posts(base, repo).data
    tr = traffic.load(base, repo)
    from .learn import Learnings
    notes = [n for n in Learnings(learn_root, False).notes() if n.get("repo") == repo]
    return {
        "study": (f"{st['at'][:10]}, {sum(s['posts'] for s in st['sources'].values())} posts, "
                  f"{sum(s['relevant'] for s in st['sources'].values())} relevant, labels by {st['engine']}") if st else "not run",
        "strategy": ("change waiting: approve it with `approve " + repo + " playbook`" if proposed else
                     (f"v{approved['version']} approved {approved.get('approved_at', '')[:10]}" if approved else "no playbook")),
        "draft": f"{len(drafts)} drafts" + (f", {sum(1 for d in drafts if d.meta.get('playbook', '').startswith('proposal'))} from the waiting change"
                                            if proposed else "") if drafts else "none",
        "create": (lambda a: f"{len(a)} file(s), {sum(1 for x in a if x['status'] == 'approved')} approved" if a else "none")(kit.manifest().assets),
        "check": f"{errs} of {len(drafts)} with errors" if drafts else "",
        "approve": f"{sum(1 for d in drafts if d.state == 'approved')} approved" + (", 1 playbook change waiting" if proposed else ""),
        "post": "by hand",
        "posted": f"{sum(1 for d in drafts if d.state == 'posted')} recorded",
        "measure": f"{len(posts)} post(s) measured, traffic on {len(tr.get('fetches', []))} day(s)",
        "learn": f"{len(notes)} finding(s)",
    }


def status(base: Path, repo: str, learn_root: Path) -> List[str]:
    """One line per step: where this repo is."""
    c = cells(base, repo, learn_root)
    out = [f"{repo}"]
    for i, s in enumerate(FLOW, 1):
        out.append(f"  {i} {s.key:<9} {c[s.key]:<70} [{s.automation}]")
    return out


def next_step(base: Path, repo: str, learn_root: Path) -> dict:
    """The one thing to do next. `how` says who does it: `run` is a step Distro runs,
    `terminal` is an approval only a person can give, `hand` is posting by hand."""
    from . import measure, study as study_mod
    from .kit import Kit
    kit = Kit(base, repo)
    st = study_mod.load(base, repo)
    approved, proposed = kit.playbooks.approved(), kit.playbooks.proposed()
    drafts = kit.drafts()
    posts = measure.Posts(base, repo).data
    if not st:
        return {"key": "study", "how": "run", "text": "Study what works in the repo's space.", "command": f"surya-distro study {repo}"}
    if not approved and not proposed:
        return {"key": "strategy", "how": "run", "text": "Turn the study into a playbook.", "command": f"surya-distro strategy {repo}"}
    if proposed:
        return {"key": "approve", "how": "terminal", "text": "A playbook change is waiting. Read it, then approve it in your own terminal.",
                "command": f"surya-distro approve {repo} playbook"}
    if not drafts:
        return {"key": "draft", "how": "run", "text": "Draft one post per channel.", "command": f"surya-distro draft {repo}"}
    bad = [d for d in drafts if d.state in ("draft", "changed") and kit.errors(d)]
    if bad:
        return {"key": "check", "how": "edit", "text": f"{len(bad)} draft(s) have errors. Fill the markers and fix them.", "command": f"surya-distro check {repo}"}
    ready = [d for d in drafts if d.state in ("draft", "changed")]
    if ready:
        return {"key": "approve", "how": "terminal", "text": f"{len(ready)} draft(s) pass the checks. Approve the ones you want, in your own terminal.",
                "command": f"surya-distro approve {repo} {ready[0].id}"}
    approved_drafts = [d for d in drafts if d.state == "approved"]
    if approved_drafts:
        return {"key": "post", "how": "hand", "text": f"{len(approved_drafts)} approved. Post by hand, then record the link.",
                "command": f"surya-distro posted {repo} {approved_drafts[0].id} --url LINK"}
    if not any(r.get("snapshots") for r in posts.values()):
        return {"key": "measure", "how": "run", "text": "Read your posts and GitHub traffic.", "command": f"surya-distro measure {repo}"}
    return {"key": "learn", "how": "run", "text": "Write down what the numbers say.", "command": f"surya-distro learn {repo}"}


def render() -> str:
    lines = ["The flow, and what is automated:", ""]
    for i, s in enumerate(FLOW, 1):
        lines.append(f"  {i} {s.key:<9} [{s.automation}]  {s.does}")
    lines += ["", "`surya-distro app` shows all of it on one page, on your machine only. It runs these steps and shows the approve command, but can't approve or post.",
              "Nothing is posted, submitted or sent. Approvals happen in your own terminal."]
    return "\n".join(lines)
