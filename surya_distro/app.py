"""The Distro app: a local page over the whole launch flow.

It serves one page on 127.0.0.1. A launch plan is private until you post it, and a
web page in another tab can reach a localhost port, so every API call needs a
per-launch token, the Host and Origin are checked, and the page's content
security policy blocks anything a post's text might try to load.

What the app will not do, on purpose:

- It never approves. Approving a draft or a playbook change stays in your own
  terminal. The page shows the exact command and why.
- It never posts, submits or sends. There is no route that does.
- It writes only inside the home folder: a draft saved in place, and the files
  the real commands write. Steps run as the real CLI in a subprocess.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, learn, measure, stages, stats, study, traffic
from . import playbook as pbmod
from .channels import CHANNELS, Issue, size_of, thread_posts
from .kit import Draft, Kit, KitError, write_docs
from .sources import NAMES

PAGE = Path(__file__).resolve().parent / "app.html"
PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
IDLE_LIMIT = 900            # the page polls, so this long with no calls means the tab closed
MAX_BODY = 600_000
MAX_FIELD = 60_000
MAX_LINES = 5000
MAX_LINE = 2000
CANCEL_GRACE = 3.0

# Steps the page may run. Each one is the real CLI command, and none of them can approve or post.
STEPS = (
    ("study", "Study", "Read public posts in the repo's space and score each one."),
    ("strategy", "Strategy", "Turn the study and your results into a playbook change."),
    ("draft", "Draft", "One draft per channel. Existing drafts are kept."),
    ("check", "Check", "Hold the drafts to their channel's rules and the playbook."),
    ("create", "Create", "Make the images and motion video for the drafts."),
    ("targets", "Targets", "Check the places to submit again, live."),
    ("measure", "Measure", "Read your posts and store GitHub traffic."),
    ("learn", "Learn", "One note per finding, and a playbook change for you."),
)
RUNNABLE = tuple(s[0] for s in STEPS)

# Neither may start with a dash, or the command line would read it as an option.
NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,99}$")
ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,79}$")
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
               ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
SKIP_DIRS = {"cache", "learnings"}
GET_ROUTES = ("state", "launches", "launch", "study", "playbook", "drafts", "targets", "media", "results", "jobs", "job", "job/stream")
POST_ROUTES = ("check", "draft/save", "run", "cancel")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ----------------------------------------------------------------- safe paths --
def inside(path: Path, root: Path) -> bool:
    try:
        resolved, base = path.resolve(), root.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == base or base in resolved.parents


def valid_repo(repo: Any) -> str:
    """owner/name, each part plain, so nothing can step out of the home folder."""
    if not isinstance(repo, str) or repo.count("/") != 1:
        raise ApiError("A repo looks like owner/name.")
    for part in repo.split("/"):
        if not NAME_RE.match(part) or part in (".", ".."):
            raise ApiError("That repo name has characters Distro doesn't use.")
    return repo


def safe_url(url: Any) -> str:
    return url if isinstance(url, str) and re.match(r"^https?://[^\s]+$", url) else ""


# ---------------------------------------------------------------- media manifest --
def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def read_manifest(repo_dir: Path) -> Dict[str, Any]:
    """The media manifest, whatever state it is in. A missing file is empty, not an error."""
    kit_dir = repo_dir / "kit"
    path = kit_dir / "media" / "media.json"
    out: Dict[str, Any] = {"exists": path.is_file(), "error": "", "assets": []}
    if not out["exists"]:
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out["error"] = f"media.json can't be read: {e}"
        return out
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list):
        out["error"] = "media.json has no list of assets."
        return out
    skipped = 0
    for raw in assets:
        if not isinstance(raw, dict) or not _text(raw.get("id")):
            skipped += 1
            continue
        kind = raw.get("kind") if raw.get("kind") in ("image", "video") else "other"
        follows = raw.get("follows")
        follows = [str(f) for f in follows] if isinstance(follows, list) else ([follows] if isinstance(follows, str) and follows else [])
        asset = {"id": _text(raw["id"]), "channel": _text(raw.get("channel")), "kind": kind, "width": _num(raw.get("width")),
                 "height": _num(raw.get("height")), "seconds": _num(raw.get("seconds")), "template": _text(raw.get("template")),
                 "follows": follows, "status": _text(raw.get("status")), "sha256": _text(raw.get("sha256")),
                 "created": _text(raw.get("created")), "path": _text(raw.get("path")), "html": _text(raw.get("html")),
                 "src": "", "problem": ""}
        found = _resolve_asset(repo_dir, asset["path"])
        if kind == "other":
            asset["problem"] = "Distro doesn't know this kind of asset."
        elif not asset["path"]:
            asset["problem"] = "No file path in the manifest."
        elif not found:
            asset["problem"] = "The file is missing or sits outside the kit."
        else:
            asset["src"] = found.relative_to(repo_dir.resolve()).as_posix()
        out["assets"].append(asset)
    if skipped:
        out["error"] = f"{skipped} entr{'y' if skipped == 1 else 'ies'} in media.json had no id and {'was' if skipped == 1 else 'were'} skipped."
    return out


def _resolve_asset(repo_dir: Path, rel: str) -> Optional[Path]:
    """The file for a manifest path, which must be a media type and inside the kit."""
    if not rel or "\0" in rel or Path(rel).suffix.lower() not in MEDIA_TYPES:
        return None
    kit_dir = repo_dir / "kit"
    given = Path(rel)
    candidates = [given] if given.is_absolute() else [kit_dir / "media" / given, kit_dir / given, repo_dir / given]
    for c in candidates:
        if c.is_file() and inside(c, kit_dir):
            return c.resolve()
    return None


def parse_range(header: Optional[str], size: int):
    """None to serve the whole file, 'bad' when no byte is in range, else (start, end)."""
    m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):
        n = int(m.group(2))
        if n == 0 or size == 0:
            return "bad"
        return max(size - n, 0), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if m.group(2) and end < start:
        return None
    if start >= size:
        return "bad"
    return start, min(end, size - 1)


# ------------------------------------------------------------------------ jobs --
class Job:
    def __init__(self, job_id: str, repo: str, step: str, argv: List[str]):
        self.id, self.repo, self.step, self.argv = job_id, repo, step, argv
        self.lines: List[str] = []
        self.state = "running"          # running, done, failed, cancelled
        self.code: Optional[int] = None
        self.started = time.time()
        self.ended: Optional[float] = None
        self.cancelled = False
        self.proc: Optional[subprocess.Popen] = None
        self.cond = threading.Condition()

    def add(self, line: str) -> None:
        with self.cond:
            if len(self.lines) < MAX_LINES:
                self.lines.append(line[:MAX_LINE])
                if len(self.lines) == MAX_LINES:
                    self.lines.append("(output cut here. The command is still running.)")
            self.cond.notify_all()

    def command(self) -> str:
        """The command as you would type it, without this machine's Python path."""
        if "surya_distro" in self.argv:
            return "surya-distro " + " ".join(self.argv[self.argv.index("surya_distro") + 1:])
        return " ".join(self.argv)

    def summary(self) -> dict:
        return {"id": self.id, "repo": self.repo, "step": self.step, "state": self.state, "code": self.code,
                "started": self.started, "ended": self.ended, "lines": len(self.lines), "command": self.command()}


class Jobs:
    """Runs the real CLI one job per repo, streams its output, and cancels what it started."""

    def __init__(self, argv_for: Callable[[str, str, List[str]], List[str]], env: Optional[Dict[str, str]] = None):
        self.argv_for = argv_for
        self.env = env
        self.items: Dict[str, Job] = {}
        self.lock = threading.Lock()

    def running(self, repo: str) -> Optional[Job]:
        for j in self.items.values():
            if j.repo == repo and j.state == "running":
                return j
        return None

    def start(self, repo: str, step: str, extra: Optional[List[str]] = None) -> Job:
        if step not in RUNNABLE:
            raise ApiError(f"'{step}' isn't a step the app runs.")
        with self.lock:
            busy = self.running(repo)
            if busy:
                raise ApiError(f"{busy.step} is still running for {repo}. Wait for it or cancel it.", 409)
            job = Job(secrets.token_hex(6), repo, step, self.argv_for(step, repo, extra or []))
            self.items[job.id] = job
            old = sorted(self.items.values(), key=lambda j: j.started)[:-30]
            for j in old:
                if j.state != "running":
                    self.items.pop(j.id, None)
            try:
                kwargs: Dict[str, Any] = {"start_new_session": True} if hasattr(os, "killpg") else {}
                job.proc = subprocess.Popen(job.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            env=self.env, **kwargs)
            except OSError as e:
                job.add(f"Couldn't start the command: {e}")
                self._finish(job, 127)
                return job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    def _pump(self, job: Job) -> None:
        proc = job.proc
        assert proc and proc.stdout
        try:
            for raw in proc.stdout:
                job.add(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        finally:
            proc.stdout.close()
            self._finish(job, proc.wait())

    @staticmethod
    def _finish(job: Job, code: int) -> None:
        with job.cond:
            job.code = code
            job.ended = time.time()
            job.state = "cancelled" if job.cancelled else ("done" if code == 0 else "failed")
            job.cond.notify_all()

    def cancel(self, job_id: str) -> Job:
        job = self.items.get(job_id)
        if not job:
            raise ApiError("No such job. The app can only cancel a job it started.", 404)
        if job.state != "running" or not job.proc:
            return job
        job.cancelled = True
        job.add("Cancelling...")
        self._signal(job.proc, signal.SIGTERM)
        timer = threading.Timer(CANCEL_GRACE, lambda: job.proc and job.proc.poll() is None and self._signal(job.proc, signal.SIGKILL))
        timer.daemon = True
        timer.start()
        return job

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)     # the group this app started, never anyone else's
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            pass

    def stop_all(self) -> None:
        for j in list(self.items.values()):
            if j.state == "running" and j.proc:
                j.cancelled = True
                self._signal(j.proc, signal.SIGKILL)


# ------------------------------------------------------------------------- app --
class Distro:
    """What the page talks to. One per running app."""

    def __init__(self, base: Path, engine: Optional[str] = None, memory: Optional[str] = None,
                 argv_for: Optional[Callable[[str, str, List[str]], List[str]]] = None):
        self.base = Path(base).expanduser()
        self.engine, self.memory = engine, memory
        self.token = secrets.token_urlsafe(24)
        self.nonce = secrets.token_urlsafe(16)
        self.port = 0
        self.last_seen = time.monotonic()
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        env["PYTHONPATH"] = PACKAGE_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.jobs = Jobs(argv_for or self.argv_for, env)

    # ----- the CLI, as a subprocess, so a new command works without coupling -----
    def argv_for(self, step: str, repo: str, extra: List[str]) -> List[str]:
        argv = [sys.executable, "-m", "surya_distro", "--home", str(self.base)]
        if self.engine:
            argv += ["--engine", self.engine]
        if self.memory:
            argv += ["--memory", self.memory]
        return argv + [step, repo] + extra

    def cli(self, command: str, repo: str = "") -> str:
        """What to type in your own terminal."""
        home = "" if str(self.base) == str(traffic.home(None)) else f"--home {shell_quote(str(self.base))} "
        return f"surya-distro {home}{command} {repo}".strip()

    def learnings(self) -> learn.Learnings:
        root, memory, why = learn.locate(self.memory, self.base)
        return learn.Learnings(root, memory, why)

    def kit(self, repo: str) -> Kit:
        repo = valid_repo(repo)
        if not inside(traffic.repo_dir(self.base, repo), self.base):
            raise ApiError("That repo is outside the home folder.", 403)
        return Kit(self.base, repo)

    def repo_dir(self, repo: str) -> Path:
        return traffic.repo_dir(self.base, valid_repo(repo))

    # ----- launches -----
    def repos(self) -> List[str]:
        out: List[str] = []
        if not self.base.is_dir():
            return out
        for owner in sorted(self.base.iterdir()):
            if not owner.is_dir() or owner.name in SKIP_DIRS or not NAME_RE.match(owner.name) or owner.name.startswith("."):
                continue
            for name in sorted(owner.iterdir()):
                if name.is_dir() and NAME_RE.match(name.name) and any((name / m).exists() for m in ("kit", "study", "playbook.json", "playbook.proposed.json", "posts.json")):
                    out.append(f"{owner.name}/{name.name}")
        return out

    def state(self) -> dict:
        return {"version": __version__, "home": str(self.base), "engine": self.engine or "auto", "memory": self.learnings().root.as_posix(),
                "steps": [{"key": k, "label": label, "does": does} for k, label, does in STEPS]}

    def launch(self, repo: str) -> dict:
        kit = self.kit(repo)
        root = self.learnings().root
        cells = stages.cells(self.base, kit.repo, root)
        nxt = stages.next_step(self.base, kit.repo, root)
        drafts = kit.drafts()
        approved, proposed = kit.playbooks.approved(), kit.playbooks.proposed()
        st = study.load(self.base, kit.repo)
        posts = measure.Posts(self.base, kit.repo).data
        notes = [n for n in self.learnings().notes() if n.get("repo") == kit.repo]
        done = {"study": bool(st), "strategy": bool(approved), "draft": bool(drafts),
                "check": bool(drafts) and not any(kit.errors(d) for d in drafts), "create": bool(kit.manifest().assets), "approve": any(d.state in ("approved", "posted") for d in drafts),
                "post": any(d.state == "posted" for d in drafts), "posted": any(d.state == "posted" for d in drafts),
                "measure": any(r.get("snapshots") for r in posts.values()), "learn": bool(notes)}
        steps = [{"key": s.key, "automation": s.automation, "does": s.does, "text": cells[s.key], "done": done[s.key], "current": s.key == nxt["key"]}
                 for s in stages.FLOW]
        facts: dict = {}
        try:
            facts = json.loads((kit.dir / "facts.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        job = self.jobs.running(kit.repo)
        nxt = dict(nxt, command=self._with_home(nxt["command"]))
        return {"repo": kit.repo, "description": facts.get("description") or facts.get("summary") or "", "url": safe_url(facts.get("url")),
                "stars": facts.get("stars"), "steps": steps, "next": nxt, "job": job.summary() if job else None,
                "counts": {"drafts": len(drafts), "approved": sum(d.state == "approved" for d in drafts),
                           "posted": sum(d.state == "posted" for d in drafts), "errors": sum(1 for d in drafts if kit.errors(d)),
                           "posts": len(posts), "findings": len(notes)},
                "playbook": "proposed" if proposed else ("approved" if approved else "none")}

    def _with_home(self, command: str) -> str:
        return self.cli(command.replace("surya-distro ", "", 1)) if command.startswith("surya-distro ") else command

    def launches(self) -> dict:
        out = []
        for repo in self.repos():
            try:
                out.append(self.launch(repo))
            except Exception as e:  # one broken repo must not hide the others
                out.append({"repo": repo, "error": f"Couldn't read this launch: {e}"})
        return {"home": str(self.base), "launches": out}

    # ----- study -----
    def study(self, repo: str) -> dict:
        kit = self.kit(repo)
        st = study.load(self.base, kit.repo)
        if not st:
            return {"study": None, "command": self.cli("study", kit.repo)}
        rel = study.relevant(st)

        def row(p: dict) -> dict:
            return {"score": p["score"], "source": p["source"], "source_name": NAMES.get(p["source"], p["source"]),
                    "title": study._title(p, 110), "url": safe_url(p.get("url")), "engagement": study.describe_engagement(p),
                    "expected": study._num(p.get("expected", 0)), "author": study.describe_author(p), "format": study.describe_format(p),
                    "small": bool(p.get("small")), "young": bool(p.get("young"))}

        ordered = sorted(rel, key=lambda p: -p["score"])
        small = [p for p in ordered if p["score"] >= 1.0 and (p.get("small") or p.get("young"))][:10]
        by_source = []
        for src in st["sources"]:
            posts = study.relevant(st, src)
            if len(posts) < 6:
                continue
            rows = []
            for dim in study.DIMENSIONS:
                for r in study.group_rows(posts, dim):
                    rows.append({"what": study.DIM_NAMES[dim], "value": r["value"], "n": r["n"], "mean": round(r["mean"], 2),
                                 "mean_rest": round(r["mean_rest"], 2), "effect": round(r["effect"], 2),
                                 "confidence": stats.confidence(r["n"], r["t"]),
                                 "top": safe_url(r["posts"][0]["url"]) if r["posts"] else ""})
            rows.sort(key=lambda r: -r["effect"])
            by_source.append({"source": src, "name": NAMES.get(src, src), "posts": len(posts), "rows": rows})
        sources = []
        for src, s in st["sources"].items():
            m = s.get("model") or {}
            sources.append({"source": src, "name": NAMES.get(src, src), "posts": s["posts"], "relevant": s["relevant"], "note": s.get("note", ""),
                            "baseline": (f"{m.get('kind')}, n={m.get('n')}" if m.get("n") else "")})
        age = study.age_days(st)
        return {"study": {"at": st["at"], "days": st.get("days"), "about": st.get("about", ""), "queries": st.get("queries", []),
                          "engine": st.get("engine", ""), "engines_used": st.get("engines_used") or {}, "stale": study.is_stale(st),
                          "age_days": None if age is None else round(age, 1), "posts": len(st.get("posts", [])), "relevant": len(rel),
                          "sources": sources, "outliers": [row(p) for p in ordered[:12]], "small": [row(p) for p in small], "formats": by_source},
                "command": self.cli("study", kit.repo)}

    # ----- playbook -----
    @staticmethod
    def rule_out(r: dict) -> dict:
        e = r.get("evidence")
        ev = None
        if e:
            ev = {"source": NAMES.get(e.get("source"), e.get("source")), "study": e.get("study"), "engine": e.get("engine"), "among": e.get("among"),
                  "n": e.get("n"), "n_rest": e.get("n_rest"), "mean": e.get("mean"), "mean_rest": e.get("mean_rest"), "effect": e.get("effect"),
                  "confidence": e.get("confidence"), "posts": [{"url": safe_url(p.get("url")), "score": p.get("score"), "engagement": p.get("engagement")}
                                                                for p in e.get("posts", [])]}
        ch = r["channel"]
        return {"id": r["id"], "channel": ch, "channel_name": pbmod.CHANNEL_NAMES.get(ch) or (CHANNELS[ch].name if ch in CHANNELS else ch),
                "text": r["text"], "fit": r.get("fit", ""), "check": r.get("check", ""), "level": r.get("level", ""), "confidence": r["confidence"],
                "early": bool(r.get("early")), "status": r["status"], "since": r.get("since", ""), "superseded_on": r.get("superseded_on", ""),
                "reason": r.get("reason", ""), "evidence": ev, "disagree": bool(r.get("disagree")),
                "learnings": [{"date": l.get("date", ""), "url": safe_url(l.get("url")), "score": l.get("score")} for l in r.get("learnings", [])]}

    def playbook(self, repo: str) -> dict:
        kit = self.kit(repo)
        approved, proposed = kit.playbooks.approved(), kit.playbooks.proposed()

        def pb_out(pb: Optional[dict]) -> Optional[dict]:
            if not pb:
                return None
            return {"version": pb.get("version"), "approved_at": pb.get("approved_at", ""), "created": pb.get("created", ""),
                    "study_at": pb.get("study_at", ""), "engine": pb.get("engine", ""), "learnings": pb.get("learnings", 0),
                    "claim": (pb.get("product") or {}).get("claim", ""), "audience": (pb.get("product") or {}).get("audience", ""),
                    "channels": pb.get("channels", {}), "rules": [self.rule_out(r) for r in pb.get("rules", [])]}

        changes = pbmod.changes(approved, proposed) if proposed else []
        return {"approved": pb_out(approved), "proposed": pb_out(proposed), "changes": changes,
                "diff": kit.playbooks.diff() if proposed else "", "approve_command": self.cli("approve", f"{kit.repo} playbook"),
                "strategy_command": self.cli("strategy", kit.repo)}

    # ----- drafts -----
    def _draft(self, repo: str, draft_id: Any):
        kit = self.kit(repo)
        if not isinstance(draft_id, str) or not ID_RE.match(draft_id):
            raise ApiError("That isn't a draft name.")
        path = kit.drafts_dir / f"{draft_id}.md"
        if not path.is_file() or not inside(path, kit.drafts_dir):
            raise ApiError(f"No draft called {draft_id}.", 404)
        return kit, Draft.load(path)

    @staticmethod
    def counts(channel: str, fields: Dict[str, str]) -> List[dict]:
        ch = CHANNELS.get(channel)
        out: List[dict] = []
        for rule in (ch.rules if ch else ()):
            if not rule.max:
                continue
            value = (fields.get(rule.field) or "").strip()
            parts = thread_posts(value) if rule.field == "thread" else [value]
            for i, part in enumerate(parts or [""], 1):
                out.append({"field": rule.field, "label": f"{rule.field} {i}" if rule.field == "thread" else rule.field,
                            "size": size_of(rule, part), "max": rule.max})
        return out

    @staticmethod
    def issues_out(issues: List[Issue]) -> List[dict]:
        return [{"level": i.level, "field": i.field, "message": i.message} for i in issues]

    def draft_out(self, kit: Kit, d: Draft) -> dict:
        ch = CHANNELS.get(d.channel)
        issues = kit.issues(d)
        limits = {r.field: {"max": r.max, "required": r.required} for r in (ch.rules if ch else ())}
        approve = self.cli("approve", f"{kit.repo} {d.id}")
        posted = self.cli("posted", f"{kit.repo} {d.id} --url LINK")
        return {"id": d.id, "channel": d.channel, "channel_name": ch.name if ch else d.channel, "state": d.state,
                "playbook": d.meta.get("playbook", ""), "note": d.meta.get("note", ""), "posted_at": d.meta.get("posted_at", ""),
                "post_url": safe_url(d.meta.get("post_url")), "approved_at": d.meta.get("approved_at", ""),
                "created": d.meta.get("created", ""),
                "submit": safe_url(ch.submit.format(subreddit=d.fields.get("subreddit", "")) if ch and ch.submit and d.channel != "email" else ""),
                "fields": [{"name": k, "value": v, **limits.get(k, {"max": None, "required": False})} for k, v in d.fields.items()],
                "issues": self.issues_out(issues), "counts": self.counts(d.channel, d.fields),
                "errors": sum(i.level == "error" for i in issues), "approve_command": approve, "posted_command": posted,
                "notes": list(ch.notes) if ch else []}

    def drafts(self, repo: str) -> dict:
        kit = self.kit(repo)
        order = list(CHANNELS)
        ds = sorted(kit.drafts(), key=lambda d: (order.index(d.channel) if d.channel in order else len(order), d.id))
        return {"drafts": [self.draft_out(kit, d) for d in ds], "draft_command": self.cli("draft", kit.repo)}

    @staticmethod
    def _clean_fields(d: Draft, fields: Any) -> Dict[str, str]:
        if not isinstance(fields, dict) or not fields:
            raise ApiError("Send the draft's fields.")
        merged = dict(d.fields)
        for k, v in fields.items():
            if k not in d.fields:
                raise ApiError(f"This draft has no field called '{k}'.")
            if not isinstance(v, str):
                raise ApiError("A field is text.")
            v = v.replace("\r\n", "\n").replace("\r", "\n")
            if len(v) > MAX_FIELD:
                raise ApiError(f"'{k}' is too long.", 413)
            if any(line.startswith("::: ") for line in v.split("\n")):
                raise ApiError("A line can't start with ':::' because that marks a field in the draft file.")
            merged[k] = v.strip()
        return merged

    def check(self, repo: str, draft_id: Any, fields: Any) -> dict:
        kit, d = self._draft(repo, draft_id)
        merged = self._clean_fields(d, fields)
        trial = Draft(d.id, d.path, dict(d.meta), merged)
        issues = kit.issues(trial)
        return {"issues": self.issues_out(issues), "counts": self.counts(d.channel, merged), "errors": sum(i.level == "error" for i in issues),
                "changed": trial.digest() != d.digest(), "resets": d.state == "approved" and trial.digest() != d.digest()}

    def save_draft(self, repo: str, draft_id: Any, fields: Any, reset_ok: bool) -> dict:
        kit, d = self._draft(repo, draft_id)
        if d.meta.get("status") == "posted":
            raise ApiError("This draft is recorded as posted, so what you posted stays as it was. Edit the file by hand if you must.", 409)
        merged = self._clean_fields(d, fields)
        changed = Draft(d.id, d.path, dict(d.meta), merged).digest() != d.digest()
        resets = d.state == "approved" and changed
        if resets and not reset_ok:
            raise ApiError("This draft is approved. Saving a change resets it to draft, and you approve it again in your terminal. "
                           "Confirm the reset to save.", 409)
        if changed:
            d.fields = merged
            if d.meta.get("status") in ("approved", "posted") or d.state == "changed":
                d.meta["status"] = "draft"
                for k in ("approved_at", "approved_hash", "posted_at", "post_url"):
                    d.meta.pop(k, None)
            d.save()
            try:
                write_docs(kit)
            except (KitError, OSError, ValueError):
                pass   # the review sheet needs the kit's facts, and the draft is saved either way
        return {"saved": changed, "reset": resets, "draft": self.draft_out(kit, Draft.load(d.path))}

    # ----- targets, media, results -----
    def targets(self, repo: str) -> dict:
        kit = self.kit(repo)
        t = kit.targets()
        if t:
            for r in t["results"]:
                r["submit"] = safe_url(r.get("submit"))
        return {"targets": t, "command": self.cli("targets", kit.repo)}

    def media(self, repo: str) -> dict:
        kit = self.kit(repo)
        m = read_manifest(self.repo_dir(kit.repo))
        return dict(m, command=self.cli("create", kit.repo), channels={k: c.name for k, c in CHANNELS.items()})

    def results(self, repo: str) -> dict:
        kit = self.kit(repo)
        store = measure.Posts(self.base, kit.repo).data
        rows = []
        for d in kit.drafts():
            if d.state != "posted" and d.id not in store:
                continue
            rec = store.get(d.id) or {}
            snaps = rec.get("snapshots") or []
            plat = rec.get("platform") or d.channel
            cps = {}
            for name in ("1h", "24h", "7d"):
                s = next((s for s in snaps if s.get("checkpoint") == name), None)
                cps[name] = {"text": measure.describe(plat, s["metrics"]), "engagement": s.get("engagement"), "age_hours": s.get("age_hours")} if s else None
            posted_at = rec.get("posted_at") or d.meta.get("posted_at", "")
            rows.append({"draft": d.id, "channel": d.channel, "channel_name": CHANNELS[d.channel].name if d.channel in CHANNELS else d.channel,
                         "platform": plat, "url": safe_url(rec.get("url") or d.meta.get("post_url")), "posted_at": posted_at, "checkpoints": cps,
                         "latest": ({"text": measure.describe(plat, snaps[-1]["metrics"]), "age_hours": snaps[-1].get("age_hours")} if snaps else None),
                         "github": measure.github_after(self.base, kit.repo, posted_at, plat) if posted_at else None})
        rows.sort(key=lambda r: r["posted_at"])
        tr = traffic.load(self.base, kit.repo)
        days = [{"date": k, **{f: v.get(f, 0) for f in ("views", "view_uniques", "clones", "clone_uniques")}} for k, v in sorted(tr.get("days", {}).items())]
        stars = [{"date": k, "stars": v.get("stars", 0)} for k, v in sorted(tr.get("stars", {}).items())]
        marks = [{"date": day, "drafts": ids} for day, ids in sorted(traffic.post_dates(self.base, kit.repo).items())]
        notes = []
        for n in self.learnings().notes():
            if n.get("repo") != kit.repo:
                continue
            try:
                text = Path(n["path"]).read_text(encoding="utf-8")
            except OSError:
                text = ""
            body = re.sub(r"^---\n.*?\n---\n?", "", text, count=1, flags=re.S).strip()
            notes.append({"name": n.get("name", ""), "description": n.get("description", ""), "channel": n.get("channel", ""), "draft": n.get("draft", ""),
                          "date": n.get("date", ""), "status": n.get("status", ""), "confidence": n.get("confidence", ""),
                          "sample_size": n.get("sample_size", ""), "score": n.get("score"), "evidence": safe_url(n.get("evidence")), "body": body[:4000]})
        return {"posts": rows, "traffic": {"days": days, "stars": stars, "marks": marks, "fetches": len(tr.get("fetches", []))},
                "notes": notes, "notes_dir": str(self.learnings().root), "measure_command": self.cli("measure", kit.repo)}

    # ----- jobs -----
    def run(self, repo: str, step: Any, draft: Any = None) -> dict:
        repo = valid_repo(repo)
        if not isinstance(step, str) or step not in RUNNABLE:
            raise ApiError("That isn't a step the app runs. Approving and posting happen in your own terminal.", 403)
        extra: List[str] = []
        if step == "check" and draft:
            if not isinstance(draft, str) or not ID_RE.match(draft):
                raise ApiError("That isn't a draft name.")
            extra = [draft]
        return self.jobs.start(repo, step, extra).summary()


def shell_quote(text: str) -> str:
    return text if re.match(r"^[\w@%+=:,./~-]+$", text) else "'" + text.replace("'", "'\\''") + "'"


# ------------------------------------------------------------------------ http --
class Handler(BaseHTTPRequestHandler):
    app: Distro
    server_version = "SuryaDistro"
    sys_version = ""

    def log_message(self, *_args: Any) -> None:
        pass

    def _json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self) -> bool:
        self.app.last_seen = time.monotonic()
        port = self.app.port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        return not origin or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _authorised(self, supplied: Optional[str] = None) -> bool:
        return hmac.compare_digest(supplied if supplied is not None else (self.headers.get("X-Token") or ""), self.app.token)

    def _page(self) -> None:
        app = self.app
        html = PAGE.read_text(encoding="utf-8").replace("__NONCE__", app.nonce).encode("utf-8")
        policy = (f"default-src 'none'; script-src 'nonce-{app.nonce}'; style-src 'nonce-{app.nonce}'; connect-src 'self'; "
                  "img-src 'self' data:; media-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html)

    def do_GET(self) -> None:
        if not self._allowed():
            return self._json({"error": "Not allowed."}, 403)
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self._page()
        if url.path.startswith("/media/"):
            # <img> and <video> can't send a header, so media takes the token in the address
            if not self._authorised(query.get("t", "")):
                return self._json({"error": "Not allowed."}, 403)
            return self._media(url.path[len("/media/"):])
        if not url.path.startswith("/api/") or not self._authorised():
            return self._json({"error": "Not allowed."}, 403)
        route = url.path[5:]
        if route == "job/stream":
            return self._stream(query)
        self._guarded(lambda: self._get(route, query))

    def do_POST(self) -> None:
        if not self._allowed() or not self._authorised():
            return self._json({"error": "Not allowed."}, 403)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._json({"error": "Send JSON."}, 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "Bad length."}, 400)
        if length < 0:
            return self._json({"error": "Bad length."}, 400)
        if length > MAX_BODY:
            return self._json({"error": "That is too large."}, 413)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except ValueError:
            return self._json({"error": "Bad JSON."}, 400)
        path = urlparse(self.path).path
        if not path.startswith("/api/") or path[5:] not in POST_ROUTES or not isinstance(body, dict):
            return self._json({"error": "Unknown request."}, 404)
        self._guarded(lambda: self._post(path[5:], body))

    def _guarded(self, action: Callable[[], Any]) -> None:
        try:
            result = action()
        except ApiError as e:
            return self._json({"error": str(e)}, e.status)
        except (KitError, pbmod.PlaybookError) as e:
            return self._json({"error": str(e)}, 400)
        except (KeyError, TypeError, ValueError) as e:
            return self._json({"error": f"Bad request: {e}"}, 400)
        except Exception as e:  # the app should show a failure, not die
            print(f"surya-distro app: {e!r}", flush=True)
            return self._json({"error": "That failed unexpectedly."}, 500)
        self._json(result)

    def _get(self, route: str, q: dict) -> Any:
        app = self.app
        repo = q.get("repo", "")
        if route == "state":
            return app.state()
        if route == "launches":
            return app.launches()
        if route == "launch":
            return app.launch(repo)
        if route in ("study", "playbook", "drafts", "targets", "media", "results"):
            return getattr(app, route)(repo)
        if route == "jobs":
            items = [j.summary() for j in sorted(app.jobs.items.values(), key=lambda j: -j.started) if not repo or j.repo == valid_repo(repo)]
            return {"jobs": items[:10]}
        if route == "job":
            job = app.jobs.items.get(q.get("id", ""))
            if not job:
                raise ApiError("No such job.", 404)
            return dict(job.summary(), output=list(job.lines))
        raise ApiError("Unknown request.", 404)

    def _post(self, route: str, b: dict) -> Any:
        app = self.app
        if route == "check":
            return app.check(b.get("repo"), b.get("id"), b.get("fields"))
        if route == "draft/save":
            return app.save_draft(b.get("repo"), b.get("id"), b.get("fields"), bool(b.get("reset_ok")))
        if route == "run":
            return app.run(b.get("repo"), b.get("step"), b.get("draft"))
        if route == "cancel":
            return app.jobs.cancel(str(b.get("id") or "")).summary()
        raise ApiError("Unknown request.", 404)

    def _stream(self, q: dict) -> None:
        job = self.app.jobs.items.get(q.get("id", ""))
        if not job:
            return self._json({"error": "No such job."}, 404)
        try:
            sent = max(int(q.get("from") or 0), 0)
        except ValueError:
            sent = 0
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            while True:
                with job.cond:
                    if len(job.lines) <= sent and job.state == "running":
                        job.cond.wait(timeout=10)
                    batch, state = job.lines[sent:], job.state
                self.app.last_seen = time.monotonic()
                for line in batch:
                    self.wfile.write((json.dumps({"type": "line", "n": sent, "text": line}, ensure_ascii=False) + "\n").encode("utf-8"))
                    sent += 1
                if state != "running" and not batch:
                    self.wfile.write((json.dumps({"type": "end", "state": state, "code": job.code}) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    return
                if not batch:
                    self.wfile.write(b'{"type":"ping"}\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _media(self, rest: str) -> None:
        parts = [unquote(p) for p in rest.split("/")]
        if len(parts) < 4 or any(p in ("", ".", "..") or "\0" in p or "\\" in p for p in parts):
            return self._json({"error": "Not found."}, 404)
        try:
            repo = valid_repo(f"{parts[0]}/{parts[1]}")
        except ApiError:
            return self._json({"error": "Not found."}, 404)
        repo_dir = traffic.repo_dir(self.app.base, repo)
        target = repo_dir.joinpath(*parts[2:])
        kit_dir = repo_dir / "kit"
        ctype = MEDIA_TYPES.get(target.suffix.lower())
        if not ctype or not target.is_file() or not inside(target, kit_dir):
            return self._json({"error": "Not found."}, 404)
        size = target.stat().st_size
        want = parse_range(self.headers.get("Range"), size)
        self.send_response(206 if isinstance(want, tuple) else (416 if want == "bad" else 200))
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        if want == "bad":
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        start, end = want if isinstance(want, tuple) else (0, size - 1)
        if isinstance(want, tuple):
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(max(end - start + 1, 0)))
        self.end_headers()
        try:
            with target.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


def make_server(app: Distro, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    app.port = httpd.server_address[1]
    return httpd


def _close_when_idle(httpd: ThreadingHTTPServer, app: Distro) -> None:
    """Started from a browser, nothing else would ever stop the server."""
    while time.monotonic() - app.last_seen <= IDLE_LIMIT or app.jobs.items and any(j.state == "running" for j in app.jobs.items.values()):
        time.sleep(30)
    httpd.shutdown()


def run(base: Path, port: int = 0, open_browser: bool = True, engine: Optional[str] = None, memory: Optional[str] = None) -> int:
    app = Distro(base, engine, memory)
    httpd = make_server(app, port)
    url = f"http://127.0.0.1:{app.port}/?t={app.token}"
    print(f"SURYA_DISTRO_URL={url}", flush=True)
    print("This page is for you alone: it listens on 127.0.0.1 and needs that link's token. Press Ctrl-C to stop it.", flush=True)
    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
        threading.Thread(target=_close_when_idle, args=(httpd, app), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.jobs.stop_all()
        httpd.server_close()
    return 0
