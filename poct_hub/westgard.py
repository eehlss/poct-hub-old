"""Copied from eehlss/medlabai-qc app/rules.py (MedLabAI-QC). Deterministic Westgard multi-rules engine. Pure functions, no I/O.

Research / decision-support only. Not a validated medical device.

Rules: 1_2s (warning), 1_3s, 2_2s, R_4s, 4_1s, 8_x, 10_x.
Conventions (documented in docs/architecture.md):
  * z = (value - mean) / sd against the test configuration target.
  * 1_3s, 2_2s (within run, across levels), R_4s are evaluated within the current run.
  * 2_2s across runs is evaluated per level (previous point of that level + current).
  * 4_1s, 8_x, 10_x are evaluated per level and on the pooled across-level sequence
    ordered by (run time, level). They only count sequences that END at a current point.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

DEFAULT_RULES = ["1_2s", "1_3s", "2_2s", "R_4s", "4_1s", "10_x"]
ALL_RULES = ["1_2s", "1_3s", "2_2s", "R_4s", "4_1s", "8_x", "10_x"]
RANDOM_RULES = {"1_3s", "R_4s"}
SYSTEMATIC_RULES = {"2_2s", "4_1s", "8_x", "10_x"}


@dataclass(frozen=True)
class Point:
    key: str            # caller id of the result (opaque)
    level: int
    value: float
    mean: float
    sd: float
    ts: str             # sortable ISO timestamp of the run

    @property
    def z(self) -> float:
        return (self.value - self.mean) / self.sd


@dataclass
class PointVerdict:
    status: str                 # accept | alert | reject
    violated: list[str]
    error_type: str | None      # random | systematic | None


def _same_side(zs: list[float], thr: float) -> bool:
    return bool(zs) and (all(z > thr for z in zs) or all(z < -thr for z in zs))


def evaluate_run(current: list[Point], history: list[Point], rules: Iterable[str]) -> dict[str, PointVerdict]:
    """Evaluate one run (all levels) against prior history.

    current: points of the run being evaluated (one per level normally).
    history: points from earlier runs, any order (sorted internally), all levels.
    Returns {point.key: PointVerdict}.
    """
    rules = set(rules)
    for p in current:
        if p.sd <= 0:
            raise ValueError("target SD must be > 0")
    hits: dict[str, set[str]] = {p.key: set() for p in current}
    zc = {p.key: p.z for p in current}

    if "1_3s" in rules:
        for p in current:
            if abs(zc[p.key]) > 3:
                hits[p.key].add("1_3s")
    if "2_2s" in rules and len(current) >= 2:
        for sign in (1, -1):
            idx = [p for p in current if sign * zc[p.key] > 2]
            if len(idx) >= 2:
                for p in idx:
                    hits[p.key].add("2_2s")
    if "R_4s" in rules and len(current) >= 2:
        hi = [p for p in current if zc[p.key] > 2]
        lo = [p for p in current if zc[p.key] < -2]
        if hi and lo:
            for p in hi + lo:
                hits[p.key].add("R_4s")

    hist = sorted(history, key=lambda h: (h.ts, h.level))
    # across runs, per level
    for p in current:
        lvl = [h for h in hist if h.level == p.level] + [p]
        if "2_2s" in rules and len(lvl) >= 2 and _same_side([x.z for x in lvl[-2:]], 2):
            hits[p.key].add("2_2s")
        if "4_1s" in rules and len(lvl) >= 4 and _same_side([x.z for x in lvl[-4:]], 1):
            hits[p.key].add("4_1s")
        for name, k in (("8_x", 8), ("10_x", 10)):
            if name in rules and len(lvl) >= k and _same_side([x.z for x in lvl[-k:]], 0):
                hits[p.key].add(name)
    # pooled across levels (sequence ends with the whole current run)
    if len(current) >= 2:
        pooled = hist + sorted(current, key=lambda c: c.level)
        for name, k, thr in (("4_1s", 4, 1), ("8_x", 8, 0), ("10_x", 10, 0)):
            if name in rules and len(pooled) >= k and _same_side([x.z for x in pooled[-k:]], thr):
                for p in current:
                    hits[p.key].add(name)

    out: dict[str, PointVerdict] = {}
    for p in current:
        rej = sorted(hits[p.key], key=ALL_RULES.index)
        if rej:
            sysm = any(r in SYSTEMATIC_RULES for r in rej)
            rnd = any(r in RANDOM_RULES for r in rej)
            et = "systematic" if sysm else ("random" if rnd else None)
            # 1-3s alone or R-4s alone is random; 2-2s etc systematic; mixed prefers systematic
            out[p.key] = PointVerdict("reject", rej, et)
        elif "1_2s" in rules and abs(zc[p.key]) > 2:
            out[p.key] = PointVerdict("alert", ["1_2s"], None)
        else:
            out[p.key] = PointVerdict("accept", [], None)
    return out


def worst(verdicts: Iterable[PointVerdict]) -> str:
    order = {"accept": 0, "alert": 1, "reject": 2}
    return max((v.status for v in verdicts), key=lambda s: order[s], default="accept")
