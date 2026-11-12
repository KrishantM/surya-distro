"""The arithmetic behind "outsized": no model, the same answer every run.

Each source gets a baseline. Its formula is log(1 + engagement) = a + b*log(1 + author
size) + c*log(1 + account age in days), plus extra terms some sources need. It is an
ordinary least-squares fit with a little ridge, and it needs at least 12 posts. Below
that, the baseline is the sample median. A post's score is how far it lands above
its expected value, in residual standard deviations. A score of +1 beats about five
posts in six from authors of the same size and age.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

MIN_FIT = 12


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def solve(a: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular")
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r != col:
                f = m[r][col] / m[col][col]
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(rows: List[List[float]], y: List[float], ridge: float = 1e-2) -> List[float]:
    k = len(rows[0])
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    for i in range(1, k):
        xtx[i][i] += ridge * len(rows)
    xty = [sum(r[i] * yy for r, yy in zip(rows, y)) for i in range(k)]
    return solve(xtx, xty)


def log1p(x) -> float:
    try:
        return math.log1p(max(float(x), 0.0))
    except (TypeError, ValueError):
        return 0.0


def fit(xs: List[Dict[str, float]], engagement: List[float], features: List[str]) -> dict:
    """A baseline for one source. `xs` holds each post's raw feature values."""
    y = [log1p(e) for e in engagement]
    n = len(y)
    if n == 0:
        return {"kind": "none", "n": 0, "features": [], "coef": [], "sd": 1.0, "fill": {}}
    fill = {f: median([x[f] for x in xs if x.get(f) is not None]) for f in features}
    usable = [f for f in features if sum(1 for x in xs if x.get(f) is not None) >= max(n // 2, 1)]
    if n >= MIN_FIT and usable:
        rows = [[1.0] + [log1p(x.get(f) if x.get(f) is not None else fill[f]) for f in usable] for x in xs]
        try:
            coef = ols(rows, y)
            kind = "regression"
        except ValueError:
            usable, coef, kind = [], [median(y)], "median"
    else:
        usable, coef, kind = [], [median(y)], "median"
    model = {"kind": kind, "n": n, "features": usable, "coef": coef, "fill": {f: fill[f] for f in usable}}
    res = [yy - predict_log(model, x) for yy, x in zip(y, xs)]
    dof = max(n - len(coef), 1)
    model["sd"] = max(math.sqrt(sum(r * r for r in res) / dof), 0.25)
    return model


def predict_log(model: dict, x: Dict[str, float]) -> float:
    coef = model.get("coef") or [0.0]
    out = coef[0]
    for c, f in zip(coef[1:], model.get("features", [])):
        v = x.get(f)
        out += c * log1p(v if v is not None else model.get("fill", {}).get(f, 0.0))
    return out


def expected(model: dict, x: Dict[str, float]) -> float:
    return math.expm1(predict_log(model, x))


def score(model: dict, engagement: float, x: Dict[str, float]) -> float:
    return round((log1p(engagement) - predict_log(model, x)) / (model.get("sd") or 1.0), 2)


def compare(group: List[float], rest: List[float]) -> dict:
    """The gap in mean score between posts with a tag and posts without it."""
    g, r = mean(group), mean(rest)
    se = math.sqrt((stdev(group) ** 2 / max(len(group), 1)) + (stdev(rest) ** 2 / max(len(rest), 1))) or 1.0
    return {"n": len(group), "n_rest": len(rest), "mean": round(g, 2), "mean_rest": round(r, 2),
            "effect": round(g - r, 2), "t": round((g - r) / se, 2)}


def confidence(n: int, t: float) -> str:
    """high: 15 or more posts and a clear gap. medium: 6 or more and a likely gap. low: the rest."""
    t = abs(t)
    if n >= 15 and t >= 2.5:
        return "high"
    if n >= 6 and t >= 1.5:
        return "medium"
    return "low"


def ratio(engagement: float, exp: float) -> Optional[float]:
    return round((engagement + 1) / (exp + 1), 1) if exp is not None else None
