"""Command line for Surya Distro."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import __version__, engines, learn, measure, stages, study, traffic
from . import playbook as pbmod
from .channels import first_drafts, follow
from .github import GitHub, GitHubError, parse_repo
from .kit import Draft, Kit, KitError, now_iso, write_docs
from .net import Net, NetError
from .repo import gather
from .targets import Checker, fitting, render

HELP = """Surya Distro launches an open-source repo. It never posts, submits or sends.

  study → strategy → draft → check → approve → post (by hand) → posted → measure → learn

  study <repo>              read public posts in the repo's space, score each against its author's size and age
  strategy <repo>           turn the study and your results into a playbook, shown as a diff
  draft <repo>              study and strategy first if they're missing or stale, then draft one post per channel
  check <repo> [draft]      hold drafts to their channel's rules and the approved playbook
  show <repo>               where the repo is in the flow, and a fresh review sheet
  approve <repo> <draft>    approve a draft, from your own terminal. `approve <repo> playbook` approves a playbook change
  posted <repo> <draft>     record that you posted it (--url LINK)
  measure [repo] [draft]    read your posts at 1 hour, 24 hours and 7 days, and store GitHub traffic
  report <repo>             your posts beside the traffic that followed
  learn <repo>              one note per finding, and a playbook change for you to approve
  targets <repo>            check the places to submit again, live
  outbox <repo>             approved emails as JSON, for a sender you run yourself
  schedule                  print an hourly job for `measure`, to install yourself
  app                       open the app: every step in one page, on your machine only (--port, --no-open)
  flow                      the steps, and what is automated

<repo> is owner/name or a GitHub URL. Data lives in ~/.surya-distro, or $SURYA_DISTRO_HOME."""


def make_github() -> GitHub:
    return GitHub()


def make_net() -> Net:
    return Net()


def _base(args) -> Path:
    return traffic.home(getattr(args, "home", None))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _learnings(args) -> learn.Learnings:
    root, memory, why = learn.locate(getattr(args, "memory", None), _base(args))
    return learn.Learnings(root, memory, why)


def _say(msg: str) -> None:
    print(f"  .. {msg}", file=sys.stderr)


def _engine(args, net: Net):
    return engines.choose(getattr(args, "engine", None), net)


def _facts(args, gh: GitHub, repo: str, kit: Kit):
    """Fresh facts from GitHub, saved into the kit."""
    facts = gather(gh, repo)
    kit.save_facts(facts.to_json())
    return facts


def _run_study(args, facts_json: dict, gh: GitHub, net: Net) -> dict:
    eng = _engine(args, net)
    wanted = [s.strip() for s in args.sources.split(",")] if getattr(args, "sources", None) else None
    queries = getattr(args, "query", None) or None
    print(f"Studying {facts_json['repo']}'s space with {eng.name}. This reads public APIs and can take a few minutes.")
    st = study.run(facts_json, net, gh, eng, _base(args), wanted=wanted, days=getattr(args, "days", 90) or 90,
                   queries=queries, about=getattr(args, "about", None), progress=_say)
    for src, s in st["sources"].items():
        print(f"  {src:<12} {s['posts']:>4} posts, {s['relevant']:>4} relevant  {s.get('note', '')}")
    return st


def _propose(args, kit: Kit, facts_json: dict, st: Optional[dict]) -> str:
    notes = _learnings(args).notes()
    pb = pbmod.propose(facts_json, st, notes, kit.playbooks.approved(), now_iso()[:10])
    return kit.playbooks.save_proposal(pb)


def _print_proposal(kit: Kit, result: str) -> None:
    if result == "unchanged":
        print("No rule changed, so the approved playbook stands. Its evidence was refreshed.")
        return
    print(kit.playbooks.diff())
    print(f"\nPlaybook change proposed: {kit.playbooks.pmd}")
    print(f"Nothing uses it until you approve it, from your own terminal: surya-distro approve {kit.repo} playbook")


def cmd_study(args) -> int:
    repo = parse_repo(args.repo)
    gh, net = make_github(), make_net()
    kit = Kit(_base(args), repo)
    facts = _facts(args, gh, repo, kit)
    st = _run_study(args, facts.to_json(), gh, net)
    print(f"\nStudy: {study._paths(_base(args), repo)['md']}")
    print(f"Next: surya-distro strategy {repo}")
    return 0


def cmd_strategy(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    st = study.load(_base(args), kit.repo)
    if not st:
        raise KitError(f"no study for {kit.repo} yet. Run: surya-distro study {kit.repo}")
    _print_proposal(kit, _propose(args, kit, kit.facts(), st))
    return 0


def cmd_draft(args) -> int:
    repo = parse_repo(args.repo)
    gh = make_github()
    kit = Kit(_base(args), repo)
    facts = _facts(args, gh, repo, kit)
    fj = facts.to_json()
    if not args.offline:
        net = make_net()
        st = study.load(_base(args), repo)
        if args.restudy or study.is_stale(st):
            st = _run_study(args, fj, gh, net)
        approved, proposed = kit.playbooks.approved(), kit.playbooks.proposed()
        current = proposed or approved
        if st and (not current or current.get("study_at") != st.get("at")):
            result = _propose(args, kit, fj, st)
            if result == "proposed":
                print(f"\nThe study changes the playbook. Read {kit.playbooks.pmd}, then approve it from your own terminal: "
                      f"surya-distro approve {repo} playbook")
    pb, state = kit.playbooks.for_drafting(preview=args.preview)
    if kit.playbooks.proposed() and not args.preview:
        print("A playbook change is waiting. These drafts follow the approved playbook"
              + (" only." if pb else ", and there isn't one yet, so they follow the channel rules only.")
              + " Add --preview to draft from the change.")
    drafts = first_drafts(facts, [t for t in fitting(facts) if t.get("email")],
                          [s.strip() for s in args.subreddits.split(",") if s.strip()] if args.subreddits else None)
    drafts = {i: (ch, follow(ch, fields, pbmod.active(pb, ch), facts)) for i, (ch, fields) in drafts.items()}
    tag = "none" if not pb else (f"v{pb.get('version')} {pbmod.digest(pb)}" if state == "approved" else f"proposal {pbmod.digest(pb)}")
    print()
    for draft_id, what in kit.write_drafts(drafts, force=args.force, playbook=tag).items():
        print(f"  {draft_id:<28} {what}")
    if not args.offline:
        _check_targets(gh, facts, kit)
    path = write_docs(kit)
    print(f"\nKit for {repo}: {kit.dir}")
    print(f"Review sheet: {path}")
    print(f"Drafts follow: {'the playbook proposal (preview)' if state == 'proposal' and pb else ('playbook ' + tag if pb else 'the channel rules only')}.")
    print("Nothing has been posted. Fill each [[fill: ...]], run `check`, then approve the drafts you want.")
    return 0


def _check_targets(gh: GitHub, facts, kit: Kit) -> None:
    at = now_iso()
    net = make_net()
    results = Checker(gh, lambda url: net.get(url, {"Accept": "*/*"})).check_all(facts)
    kit.save_targets([r.to_json() for r in results], at)
    print()
    print(render(results, at))


def cmd_targets(args) -> int:
    repo = parse_repo(args.repo)
    gh = make_github()
    kit = Kit(_base(args), repo)
    facts = _facts(args, gh, repo, kit)
    _check_targets(gh, facts, kit)
    if kit.drafts_dir.exists():
        write_docs(kit)
    return 0


def cmd_check(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    drafts = [kit.draft(args.draft)] if args.draft else kit.drafts()
    if not drafts:
        raise KitError(f"no drafts for {kit.repo}. Run: surya-distro draft {kit.repo}")
    errors = 0
    for d in drafts:
        issues = kit.issues(d)
        errs = sum(1 for i in issues if i.level == "error")
        errors += errs
        print(f"{d.id}: {'ok' if not issues else f'{errs} error(s), {len(issues) - errs} warning(s)'}")
        for i in issues:
            print(f"  {i}")
    return 1 if errors else 0


def cmd_show(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    kit.facts()
    print("\n".join(stages.status(_base(args), kit.repo, _learnings(args).root)))
    print()
    for d in kit.drafts():
        errs = len(kit.errors(d))
        extra = f"  posted {d.meta['posted_at'][:10]}" if d.state == "posted" else ""
        print(f"  {d.id:<28} {d.state:<9} {errs} error(s){extra}")
    print(f"\nReview sheet: {write_docs(kit)}")
    return 0


def _tty() -> None:
    if not sys.stdin.isatty():
        raise KitError("Approve from your own terminal. An agent's shell can't approve for you.")


def _terminal_confirm(d: Draft) -> bool:
    _tty()
    print(d.render())
    try:
        answer = input(f'Type "{d.id}" to approve this draft: ')
    except EOFError:
        return False
    return answer.strip() == d.id


def _terminal_confirm_playbook(diff: str) -> bool:
    _tty()
    print(diff)
    try:
        answer = input('\nType "playbook" to approve this change: ')
    except EOFError:
        return False
    return answer.strip() == "playbook"


def cmd_approve(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    if args.draft == "playbook":
        store = _learnings(args)

        def record(after: dict, before: Optional[dict]) -> None:
            changed = pbmod.changes(before, after)
            date = now_iso()[:10]
            store.record_decision(kit.repo, after, before, changed, date)
            gone = [c["id"] for c in changed if c["what"] == "superseded"]
            if gone:
                store.supersede(gone, date)

        try:
            pb = kit.playbooks.approve(_terminal_confirm_playbook, now_iso(), record)
        except pbmod.PlaybookError as e:
            raise KitError(str(e))
        print(f"Approved playbook v{pb['version']}. The decision is logged in {store.root / 'distro-decisions.md'}.")
        print(f"Redraft from it: surya-distro draft {kit.repo} --force")
        write_docs(kit) if kit.drafts_dir.exists() else None
        return 0
    d = kit.approve(args.draft, _terminal_confirm)
    print(f"Approved {d.id}. Post it yourself, then run: surya-distro posted {kit.repo} {d.id} --url <link>")
    write_docs(kit)
    return 0


def cmd_posted(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    d = kit.posted(args.draft, args.url or "")
    traffic.watch(_base(args), kit.repo)
    print(f"Recorded {d.id} as posted at {d.meta['posted_at']}.")
    if args.url:
        print(f"`surya-distro measure` reads it at about 1 hour, 24 hours and 7 days. `schedule` prints a job that does it for you.")
    write_docs(kit)
    return 0


MANUAL_FLAGS = ("likes", "replies", "reposts", "views", "comments", "points")


def cmd_measure(args) -> int:
    base = _base(args)
    manual = {k: getattr(args, k) for k in MANUAL_FLAGS if getattr(args, k, None) is not None}
    if args.draft or manual:
        if not (args.repo and args.draft and manual):
            raise KitError("to type numbers in: surya-distro measure owner/name <draft> --likes N --replies N --reposts N --views N")
        kit = Kit(base, parse_repo(args.repo))
        try:
            s = measure.record(base, kit, args.draft, manual, args.followers)
        except measure.MeasureError as e:
            raise KitError(str(e))
        print(f"{args.draft}: stored {s['metrics']} at {s['age_hours']} hours" + (f" (the {s['checkpoint']} reading)" if s["checkpoint"] else ""))
        return 0
    repos = [parse_repo(args.repo)] if args.repo else traffic.watched(base)
    if not repos:
        raise KitError("no repos to measure. Run: surya-distro measure owner/name")
    net, gh = make_net(), make_github()
    failed = 0
    today = _now().strftime("%Y-%m-%d")
    for repo in repos:
        kit = Kit(base, repo)
        for line in measure.run(base, kit, net, due_only=args.due):
            print(f"{repo} {line}")
        if args.due and today in traffic.load(base, repo).get("fetches", []):
            continue
        try:
            store = traffic.fetch(gh, repo, base)
            print(f"{repo}: traffic, {len(store['days'])} days stored, fetched on {len(store['fetches'])} days")
        except GitHubError as e:
            failed += 1
            print(f"{repo}: {e}", file=sys.stderr)
    return 1 if failed else 0


def cmd_report(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    print(measure.report(_base(args), kit, study.load(_base(args), kit.repo), args.days))
    return 0


def cmd_learn(args) -> int:
    base = _base(args)
    kit = Kit(base, parse_repo(args.repo))
    st = study.load(base, kit.repo)
    store = _learnings(args)
    found = learn.findings(base, kit, st, kit.playbooks.approved())
    if not found:
        print(f"Nothing to learn yet: no posted draft has a measurement. Record a post with `posted`, then run `measure`.")
        return 0
    for f in found:
        path = store.write(f)
        print(f"  {f['draft']:<24} {f['description']}\n  {'':<24} {path}")
    store.write_index()
    print(f"\n{len(found)} finding(s) in {store.root} ({store.why}).")
    if st:
        _print_proposal(kit, _propose(args, kit, kit.facts(), st))
    return 0


def cmd_outbox(args) -> int:
    kit = Kit(_base(args), parse_repo(args.repo))
    out = kit.outbox()
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"{len(out)} approved email(s) written to {args.output}. Nothing was sent.")
    else:
        print(text)
    return 0


def cmd_app(args) -> int:
    from . import app
    return app.run(_base(args), port=args.port, open_browser=not args.no_open, engine=getattr(args, "engine", None),
                   memory=getattr(args, "memory", None))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="surya-distro", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"surya-distro {__version__}")
    p.add_argument("--home", help="where kits, studies and traffic are kept (default ~/.surya-distro)")
    p.add_argument("--engine", choices=engines.ENGINES, help="what labels posts: auto (default), heuristic, ollama, jev or unite")
    p.add_argument("--memory", help="a memory directory for the learnings, or 'off'")
    sub = p.add_subparsers(dest="cmd")

    def study_opts(sp):
        sp.add_argument("--sources", help="comma-separated: hn, bluesky, reddit, producthunt, github, manual (default: all)")
        sp.add_argument("--query", action="append", help="a search query, instead of the ones picked for you. Repeat it for more")
        sp.add_argument("--about", help="one line describing the space, for the labeller")
        sp.add_argument("--days", type=int, default=90, help="how far back to look (default 90)")

    s = sub.add_parser("study", help="study what works in the repo's space")
    s.add_argument("repo")
    study_opts(s)
    s.set_defaults(fn=cmd_study)

    s = sub.add_parser("strategy", help="propose a playbook from the study and your results")
    s.add_argument("repo")
    s.set_defaults(fn=cmd_strategy)

    d = sub.add_parser("draft", help="draft a launch kit")
    d.add_argument("repo")
    d.add_argument("--subreddits", help="comma-separated, instead of the ones picked for you")
    d.add_argument("--force", action="store_true", help="redraft drafts that exist (approved ones are kept)")
    d.add_argument("--offline", action="store_true", help="no study, strategy or live target checks")
    d.add_argument("--restudy", action="store_true", help="study again even if the last study is recent")
    d.add_argument("--preview", action="store_true", help="draft from a playbook change that isn't approved yet")
    study_opts(d)
    d.set_defaults(fn=cmd_draft)

    t = sub.add_parser("targets", help="check where to submit, live")
    t.add_argument("repo")
    t.set_defaults(fn=cmd_targets)

    c = sub.add_parser("check", help="hold drafts to their rules")
    c.add_argument("repo")
    c.add_argument("draft", nargs="?")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("show", help="where the repo is in the flow")
    s.add_argument("repo")
    s.set_defaults(fn=cmd_show)

    a = sub.add_parser("approve", help="approve a draft or a playbook change, interactively")
    a.add_argument("repo")
    a.add_argument("draft", help="a draft id, or `playbook`")
    a.set_defaults(fn=cmd_approve)

    po = sub.add_parser("posted", help="record that you posted a draft")
    po.add_argument("repo")
    po.add_argument("draft")
    po.add_argument("--url")
    po.set_defaults(fn=cmd_posted)

    m = sub.add_parser("measure", help="read your posts and store GitHub traffic")
    m.add_argument("repo", nargs="?")
    m.add_argument("draft", nargs="?")
    m.add_argument("--due", action="store_true", help="only posts at a 1 hour, 24 hour or 7 day mark, and traffic once a day")
    for k in MANUAL_FLAGS:
        m.add_argument(f"--{k}", type=float)
    m.add_argument("--followers", type=float, help="your follower count, for X or LinkedIn")
    m.set_defaults(fn=cmd_measure)

    r = sub.add_parser("report", help="posts beside the traffic that followed")
    r.add_argument("repo")
    r.add_argument("--days", type=int, default=30)
    r.set_defaults(fn=cmd_report)

    l = sub.add_parser("learn", help="write findings and propose playbook changes")
    l.add_argument("repo")
    l.set_defaults(fn=cmd_learn)

    o = sub.add_parser("outbox", help="approved emails as JSON")
    o.add_argument("repo")
    o.add_argument("-o", "--output")
    o.set_defaults(fn=cmd_outbox)

    sc = sub.add_parser("schedule", help="print an hourly job to install yourself")
    sc.set_defaults(fn=lambda args: print(traffic.schedule_text(_base(args))) or 0)

    ap = sub.add_parser("app", help="open the app: every step in one page, on your machine only")
    ap.add_argument("--home", default=argparse.SUPPRESS, help="where kits, studies and traffic are kept (default ~/.surya-distro)")
    ap.add_argument("--port", type=int, default=0, help="a port on 127.0.0.1 (default: any free one)")
    ap.add_argument("--no-open", action="store_true", help="print the link and don't open a browser")
    ap.set_defaults(fn=cmd_app)

    fl = sub.add_parser("flow", help="the steps, and what is automated")
    fl.set_defaults(fn=lambda args: print(stages.render()) or 0)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        print(HELP)
        return 0
    try:
        return args.fn(args)
    except (GitHubError, KitError, engines.EngineError, NetError, pbmod.PlaybookError) as e:
        print(str(e), file=sys.stderr)
        return 2


def console() -> int:
    return main(sys.argv[1:])
