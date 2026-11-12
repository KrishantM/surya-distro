"""The flow, every step named, and how much of each is automated.

    study → strategy → draft → check → approve → post → posted → measure → learn

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
    Step("check", "automatic", "holds each draft to its channel's rules and the playbook"),
    Step("approve", "manual", "you approve drafts and playbook changes, in your own terminal"),
    Step("post", "manual", "you post each draft by hand"),
    Step("posted", "manual", "you record the link"),
    Step("measure", "automatic", "reads each post at 1 hour, 24 hours and 7 days, and stores GitHub traffic"),
    Step("learn", "assisted", "writes one note per finding and proposes playbook changes"),
)


def status(base: Path, repo: str, learn_root: Path) -> List[str]:
    """One line per step: where this repo is."""
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
    cells = {
        "study": (f"{st['at'][:10]}, {sum(s['posts'] for s in st['sources'].values())} posts, "
                  f"{sum(s['relevant'] for s in st['sources'].values())} relevant, labels by {st['engine']}") if st else "not run",
        "strategy": ("change waiting: approve it with `approve " + repo + " playbook`" if proposed else
                     (f"v{approved['version']} approved {approved.get('approved_at', '')[:10]}" if approved else "no playbook")),
        "draft": f"{len(drafts)} drafts" + (f", {sum(1 for d in drafts if d.meta.get('playbook', '').startswith('proposal'))} from the waiting change"
                                            if proposed else "") if drafts else "none",
        "check": f"{errs} of {len(drafts)} with errors" if drafts else "",
        "approve": f"{sum(1 for d in drafts if d.state == 'approved')} approved" + (", 1 playbook change waiting" if proposed else ""),
        "post": "by hand",
        "posted": f"{sum(1 for d in drafts if d.state == 'posted')} recorded",
        "measure": f"{len(posts)} post(s) measured, traffic on {len(tr.get('fetches', []))} day(s)",
        "learn": f"{len(notes)} finding(s)",
    }
    out = [f"{repo}"]
    for i, s in enumerate(FLOW, 1):
        out.append(f"  {i} {s.key:<9} {cells[s.key]:<70} [{s.automation}]")
    return out


def render() -> str:
    lines = ["The flow, and what is automated:", ""]
    for i, s in enumerate(FLOW, 1):
        lines.append(f"  {i} {s.key:<9} [{s.automation}]  {s.does}")
    lines += ["", "Nothing is posted, submitted or sent. Approvals happen in your own terminal."]
    return "\n".join(lines)
