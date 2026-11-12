"""Scoring engines: they label posts for the study and for learning.

Each engine answers three questions about a post, each with a probability:

    relevant   is it about the space being studied?
    kind       launch, release, tutorial, comparison, story, opinion, question or news
    hook       how it opens: problem, what-it-is, first-person, result, question or announcement

Engines, picked with --engine or SURYA_DISTRO_ENGINE:

    auto       Ollama when it is running on this machine, otherwise the heuristic
    heuristic  rules only, no model. Always works, and is the weakest
    ollama     a local model through Ollama. Free
    jev        TypeSafe's Jev decision model. Opt-in, needs TYPESAFE_API_KEY, paid
    unite      Surya Unite's chore role, which routes to a local model, Claude or Codex

The outlier scores themselves are arithmetic (stats.py), not a model. Every
label records which engine gave it. The Jev key is read from the environment,
sent only in the Authorization header, and never logged or stored.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional

from .formats import HOOK_TEXT, KIND_TEXT, opening, strip_prefix
from .net import Net

KINDS = {
    "launch": "Shows a new tool or project that people can try",
    "release": "Announces a new version of an existing project",
    "tutorial": "Teaches how to do something",
    "comparison": "Compares tools or approaches",
    "story": "Tells what happened: a case study, an experience or lessons learned",
    "opinion": "Argues a view or starts a discussion",
    "question": "Asks readers for help, advice or views",
    "news": "Reports news about a company, product or event",
}
HOOKS = {
    "problem": "Opens with a problem or pain the reader knows",
    "what-it-is": "Opens by saying what the thing is or does",
    "first-person": "Opens with the author's own experience or action, such as 'I built' or 'I kept'",
    "result": "Opens with a number, a result or a demo",
    "question": "Opens with a question",
    "announcement": "Opens with an announcement or news",
}
assert set(KINDS) == set(KIND_TEXT) and set(HOOKS) == set(HOOK_TEXT)


class EngineError(RuntimeError):
    pass


def _item_text(p: dict, limit: int = 600) -> dict:
    return {"title": strip_prefix(p.get("title", ""))[:200], "opening": (p.get("opening") or opening(p.get("text", "")))[:220],
            "text": re.sub(r"\s+", " ", p.get("text", ""))[:limit]}


def _clip(x, lo=0.0, hi=1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return 0.5


# ----------------------------------------------------------------- heuristic --
KIND_RULES = (
    ("release", r"\bv?\d+\.\d+(?:\.\d+)?\b.*\b(released?|is out|now|adds?|update)|\b(released?|update)\b.*\bv?\d+\.\d+", 0.65),
    ("tutorial", r"^(how to|how i|guide|tutorial|step[- ]by[- ]step|tips?\b)|\bhow to\b", 0.65),
    ("comparison", r"\b(vs\.?|versus|compared (to|with)|alternatives? to|instead of)\b", 0.65),
    ("story", r"\b(lessons|what i learned|after \d+ (days|weeks|months|years)|my experience|post-?mortem|case study)\b", 0.6),
    ("question", r"^(ask hn|has anyone|does anyone|anyone else|what do you|how do you)\b|\?\s*$", 0.6),
    ("launch", r"^show hn|\b(i|we) (just )?(built|made|wrote|launched|shipped|open[- ]?sourced|released)\b|\bintroducing\b|\bmy (new )?(tool|app|project|plugin)\b", 0.7),
    ("opinion", r"\b(should|shouldn't|stop|the case for|unpopular|overrated|is dead|i think|hot take)\b", 0.55),
)
HOOK_RULES = (
    ("question", r"\?\s*$", 0.75),
    ("problem", r"^(ever|tired|every time|when you|if you|i kept|i keep|stop|the problem|it'?s hard|most |nobody|i was tired|frustrated)\b", 0.65),
    ("announcement", r"^(introducing|announcing|today|new:|just (released|shipped|launched)|meet |we'?re (excited|launching|releasing))", 0.7),
    ("result", r"^\W*\d|^\S+(\s+\S+){0,6}\s+\d+(\.\d+)?\s*(%|x\b|times|ms|stars|users|hours|minutes)", 0.6),
    ("first-person", r"^(i|we|my|our|i'm|i've|we've|we're)\b", 0.65),
)


class Heuristic:
    name = "heuristic"

    def __init__(self, terms: Optional[List[str]] = None):
        self.terms = [t.lower() for t in (terms or [])]

    def relevance(self, blob: str) -> float:
        low = blob.lower()
        if not self.terms:
            return 0.5
        if any(t in low for t in self.terms):
            return 0.8
        words = {w for t in self.terms for w in t.split() if len(w) > 2}
        hits = sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", low))
        return 0.55 if hits >= 2 else (0.4 if hits else 0.15)

    def one(self, p: dict) -> dict:
        it = _item_text(p, 2000)
        head = (p.get("title") or "")
        blob = f"{head}\n{p.get('text', '')}"
        kind, kp = "news" if p.get("link_post") else "opinion", 0.3
        for label, rx, prob in KIND_RULES:
            if re.search(rx, head.strip().lower()) or (label != "question" and re.search(rx, it["opening"].lower())):
                kind, kp = label, prob
                break
        op = it["opening"].strip() or strip_prefix(head)
        hook, hp = "what-it-is", 0.45
        for label, rx, prob in HOOK_RULES:
            if re.search(rx, op.lower()):
                hook, hp = label, prob
                break
        return {"relevant": self.relevance(blob), "kind": kind, "kind_p": kp, "hook": hook, "hook_p": hp, "engine": self.name}

    def label(self, posts: List[dict], about: str, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
        return [self.one(p) for p in posts]


# ---------------------------------------------------------- model prompt ------
def _prompt(batch: List[dict], about: str) -> str:
    kinds = "\n".join(f"  - {k}: {v}" for k, v in KINDS.items())
    hooks = "\n".join(f"  - {k}: {v}" for k, v in HOOKS.items())
    items = [{"id": str(i), **_item_text(p)} for i, p in enumerate(batch)]
    return (
        "You label public social media posts for a study of which post formats work. "
        f"The study is about: {about}.\n\n"
        "For each post give:\n"
        f"- relevant: the probability, 0 to 1, that the post is about {about}\n"
        f"- kind: one of\n{kinds}\n"
        f"- hook: how the opening line works, one of\n{hooks}\n"
        "- kind_p and hook_p: the probability, 0 to 1, that your label is right\n\n"
        "Judge the format, not whether you like the post. Answer with JSON only, in the shape "
        '{"labels": [{"id": "0", "relevant": 0.9, "kind": "launch", "kind_p": 0.8, "hook": "problem", "hook_p": 0.7}]}\n\n'
        "Posts:\n" + json.dumps(items, ensure_ascii=False)
    )


SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "relevant": {"type": "number"},
                       "kind": {"type": "string", "enum": list(KINDS)}, "kind_p": {"type": "number"},
                       "hook": {"type": "string", "enum": list(HOOKS)}, "hook_p": {"type": "number"}},
        "required": ["id", "relevant", "kind", "kind_p", "hook", "hook_p"]}}},
    "required": ["labels"],
}


def _read_labels(text: str, n: int, engine: str) -> Dict[int, dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    out: Dict[int, dict] = {}
    for row in (data.get("labels") or []) if isinstance(data, dict) else []:
        try:
            i = int(str(row.get("id")))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and row.get("kind") in KINDS and row.get("hook") in HOOKS:
            out[i] = {"relevant": _clip(row.get("relevant")), "kind": row["kind"], "kind_p": _clip(row.get("kind_p")),
                      "hook": row["hook"], "hook_p": _clip(row.get("hook_p")), "engine": engine}
    return out


class _Batched:
    """Label in batches. A post the model doesn't answer for gets the heuristic's
    label, and says so."""
    name = "model"
    batch = 12

    def __init__(self, terms: Optional[List[str]] = None):
        self.fallback = Heuristic(terms)

    def ask(self, prompt: str) -> str:  # pragma: no cover - replaced by each engine
        raise NotImplementedError

    def label(self, posts: List[dict], about: str, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
        out: List[dict] = []
        for start in range(0, len(posts), self.batch):
            chunk = posts[start:start + self.batch]
            if progress:
                progress(f"labelling {start + 1}-{start + len(chunk)} of {len(posts)} with {self.name}")
            try:
                got = _read_labels(self.ask(_prompt(chunk, about)), len(chunk), self.name)
            except EngineError as e:
                got, why = {}, str(e)
            else:
                why = "no answer for this post"
            for i, p in enumerate(chunk):
                if i in got:
                    out.append(got[i])
                else:
                    h = self.fallback.one(p)
                    h["engine"] = f"heuristic (fallback: {self.name}, {why[:80]})"
                    out.append(h)
        return out


class Ollama(_Batched):
    def __init__(self, net: Net, model: str, host: str = "http://127.0.0.1:11434", terms=None):
        super().__init__(terms)
        self.net, self.model, self.host = net, model, host.rstrip("/")
        self.name = f"ollama:{model}"

    def ask(self, prompt: str) -> str:
        r = self.net.send_json(f"{self.host}/api/chat", {
            "model": self.model, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}],
        }, timeout=300)
        if not r.ok:
            raise EngineError(f"Ollama returned HTTP {r.status}")
        data = r.json() or {}
        return (data.get("message") or {}).get("content", "")


def ollama_host() -> str:
    h = os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    if not h.startswith("http"):
        h = "http://" + h
    return h.replace("0.0.0.0", "127.0.0.1")


def find_ollama(net: Net) -> Optional[str]:
    """The model to use, if Ollama is running here: $SURYA_DISTRO_OLLAMA_MODEL, else the first one pulled."""
    r = net.get(ollama_host() + "/api/tags", timeout=3)
    if not r.ok:
        return None
    models = [m.get("name") for m in (r.json() or {}).get("models", []) if m.get("name")]
    want = os.environ.get("SURYA_DISTRO_OLLAMA_MODEL", "").strip()
    if want:
        return want if want in models or any(m.startswith(want + ":") for m in models) else None
    return models[0] if models else None


class Jev:
    """TypeSafe's System One API (docs.typesafe.ai/api/system-one). One request per
    post, with a Noul for relevance and a Choice each for kind and hook."""
    URL = "https://api.typesafe.ai/v1/systemone"

    def __init__(self, net: Net, key: str, model: str = "jev-latest", terms=None):
        self.net, self._key, self.model = net, key, model
        self.name = f"jev:{model}"
        self.fallback = Heuristic(terms)

    def __repr__(self) -> str:  # never show the key
        return f"Jev(model={self.model!r})"

    def one(self, p: dict, about: str) -> dict:
        it = _item_text(p, 1500)
        r = self.net.send_json(self.URL, {
            "model": self.model,
            "state": {"study": about, **it},
            "questions": {
                "relevant": {"type": "noul", "instructions": f"Is this post about {about}?"},
                "kind": {"type": "choice", "instructions": "What kind of post is this?", "criteria": KINDS},
                "hook": {"type": "choice", "instructions": "How does the post open? Judge its opening line.", "criteria": HOOKS},
            },
        }, headers={"Authorization": f"Bearer {self._key}"}, timeout=60)
        if r.status == 401:
            raise EngineError("Jev refused the key (HTTP 401). Check TYPESAFE_API_KEY.")
        if not r.ok:
            raise EngineError(f"Jev returned HTTP {r.status}")
        data = r.json() or {}
        a = data.get("answers") or {}
        kind, hook = a.get("kind") or {}, a.get("hook") or {}
        if kind.get("choice") not in KINDS or hook.get("choice") not in HOOKS or "noul" not in (a.get("relevant") or {}):
            raise EngineError("Jev's answer didn't have the expected shape")
        name = f"jev:{data.get('model') or self.model}"
        return {"relevant": _clip(a["relevant"]["noul"]),
                "kind": kind["choice"], "kind_p": _clip((kind.get("probabilities") or {}).get(kind["choice"], kind.get("confidence"))),
                "hook": hook["choice"], "hook_p": _clip((hook.get("probabilities") or {}).get(hook["choice"], hook.get("confidence"))),
                "engine": name}

    def label(self, posts: List[dict], about: str, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
        out = []
        for i, p in enumerate(posts):
            if progress and i % 20 == 0:
                progress(f"labelling {i + 1}-{min(i + 20, len(posts))} of {len(posts)} with {self.name}")
            try:
                out.append(self.one(p, about))
            except EngineError as e:
                if "401" in str(e):
                    raise
                h = self.fallback.one(p)
                h["engine"] = f"heuristic (fallback: {self.name}, {str(e)[:80]})"
                out.append(h)
        return out


Runner = Callable[[List[str], str, int], tuple]


def _run(args: List[str], stdin: str, timeout: int) -> tuple:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"{type(e).__name__}: {e}"
    return p.returncode, p.stdout, p.stderr


class Unite(_Batched):
    """`surya-unite ask chore`, which tries a local model first, then Claude or Codex."""
    batch = 20

    def __init__(self, exe: str, runner: Optional[Runner] = None, terms=None):
        super().__init__(terms)
        self.exe, self.runner = exe, runner or _run
        self.name = "unite"

    def ask(self, prompt: str) -> str:
        code, out, err = self.runner([self.exe, "ask", "chore", "--quiet"], prompt, 600)
        if code != 0:
            raise EngineError(f"surya-unite failed: {(err or out).strip()[:120]}")
        answer, _, who = out.rpartition("\n\n---\n")
        if who and answer:
            self.name = "unite:" + who.strip().splitlines()[0].rstrip(".")[:60]
            return answer
        return out


def find_unite() -> Optional[str]:
    return os.environ.get("SURYA_UNITE") or shutil.which("surya-unite")


ENGINES = ("auto", "heuristic", "ollama", "jev", "unite")


def choose(name: Optional[str], net: Net, terms: Optional[List[str]] = None) -> object:
    name = (name or os.environ.get("SURYA_DISTRO_ENGINE") or "auto").strip().lower()
    if name not in ENGINES:
        raise EngineError(f"unknown engine {name!r}. Pick one of: {', '.join(ENGINES)}")
    if name == "heuristic":
        return Heuristic(terms)
    if name in ("auto", "ollama"):
        model = find_ollama(net)
        if model:
            return Ollama(net, model, ollama_host(), terms)
        if name == "ollama":
            raise EngineError("Ollama isn't running here, or has no model. Install it from ollama.com and run `ollama pull qwen3:8b`.")
        return Heuristic(terms)
    if name == "jev":
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise EngineError("Jev needs TYPESAFE_API_KEY in the environment. It's a paid API with no free tier.")
        return Jev(net, key, os.environ.get("SURYA_DISTRO_JEV_MODEL", "jev-latest"), terms)
    exe = find_unite()
    if not exe:
        raise EngineError("surya-unite isn't installed. Get it from github.com/KrishantM/surya-unite, or set SURYA_UNITE to its path.")
    return Unite(exe, terms=terms)
