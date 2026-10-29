"""Choosing the next setup: the fastest one predicted to feel the way you asked.

Objective: predicted lap time (the lap model plus its residual GP, laptime.py).
Constraints:
  balance    every cell that counts at this layout predicted inside its target band
             (target.py); how surely it is (balance model) ranks setups that are
  platform   no bottoming, no ride height under the class minimum (platform.py)
  extra      any further ones (kerb drivability, from the damper module)
Tiebreaker: contact-patch quality (the damper module's score).

The search is a trust region around the setup you drove, in click indices: single and
double moves of each setting, random points changing up to MAX_CHANGES settings, and
any extra candidates (the fingerprint's own setup on a new track). The drag level comes
first: the rear wing goes where the lap model is fastest within the region, then the
balance is solved with the rest of the settings around it. Candidates are scored on the
models; only the chosen one is sent to the car.

If the fastest point needs a balance outside the band, the band wins; the gap says what
it costs ("0.3 s available with more rotation").
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from autosetup.feel.target import shifted
from autosetup.opt.balance_model import BalanceModel
from autosetup.opt.platform import Platform
from autosetup.opt.space import Dim, aero_wing

P_EXTRA = 0.5  # chance the other constraints (platform, kerbs) need to hold
BAND_COST_S = 0.2  # of two setups in the band, the surer one, at this much per unit of doubt
MIN_WEIGHT = 0.1  # cells of a speed band with less of the layout than this don't constrain
MIN_GAIN_S = 0.03  # predicted gain worth changing the car for
CHANGE_COST_S = 0.01  # per setting changed, unless Problem.cost says otherwise: of two equal setups, the one closer to yours
TIE_S = 0.02  # within this, contact patch decides
GAP_MIN_S = 0.05
MAX_CHANGES = 4
RISK = 1.0  # sds of lap-time doubt a candidate is charged
POOL = 300
RADIUS_DEFAULT, RADIUS_MIN, RADIUS_MAX = 0.25, 0.08, 0.5


@dataclass
class TargetBand:
    cells: dict[str, float]
    tolerance: dict[str, float]
    weights: dict[str, float]
    step: float  # deg per button press


@dataclass
class Problem:
    dims: list[Dim]
    base: dict[str, int]
    lap: Callable[[dict[str, int]], tuple[float, float]]  # indices → (mean s, sd s)
    balance: BalanceModel
    target: TargetBand | None
    platform: Platform | None = None
    extra: list[Callable[[dict[str, int]], float]] = field(default_factory=list)  # → probability ok
    contact: Callable[[dict[str, int]], float] | None = None  # → lower is better
    radius: float = RADIUS_DEFAULT
    candidates: list[dict[str, int]] = field(default_factory=list)
    seed: int = 0
    reach: dict[str, int] = field(default_factory=dict)  # clicks a dim may move at most (e.g. ride heights)
    cost: dict[str, float] = field(default_factory=dict)  # s per move of a dim, instead of CHANGE_COST_S
    penalty: Callable[[dict[str, int]], float] | None = None  # s a setup pays for where it is (e.g. rear toe-out)
    avoid: dict[str, tuple[int, int]] = field(default_factory=dict)  # dim → (direction, clicks) not to go again


@dataclass
class Evaluated:
    indices: dict[str, int]
    lap: tuple[float, float]
    balance: dict[str, tuple[float, float]]
    p_ok: float  # chance it's in the band (cells' geometric mean), times the extra constraints'
    changes: int
    off: float = 0.0  # predicted distance outside the band, deg (weighted)
    p_extra: float = 1.0  # platform and extra constraints alone
    change_s: float = 0.0  # what changing the car this way costs, s (CHANGE_COST_S, per-dim costs, penalty)

    @property
    def feasible(self) -> bool:
        return self.off <= 1e-9 and self.p_extra >= P_EXTRA


@dataclass
class Choice:
    best: Evaluated
    base: Evaluated
    reason: str  # faster | balance | stay
    wing: dict | None = None  # {"index", "by_index": {index: lap}}
    gap: list[dict] = field(default_factory=list)  # [{"axis", "steps", "seconds"}]
    feasible: bool = True

    @property
    def moves(self) -> dict[str, tuple[int, int]]:
        return {k: (self.base.indices[k], v) for k, v in self.best.indices.items() if v != self.base.indices[k]}


def x_of(dims: list[Dim], idx: dict[str, int]) -> dict[str, float]:
    return {d.name: d.x(idx[d.name]) for d in dims if d.name in idx}


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def cell_probs(pred: dict[str, tuple[float, float]], target: TargetBand | None) -> dict[str, tuple[float, float]]:
    """Per counted cell: (chance it's inside its band, its weight)."""
    out = {}
    if target is None:
        return out
    for c, t in target.cells.items():
        w = target.weights.get(c, 0.5)
        if w < MIN_WEIGHT or c not in pred:
            continue
        m, var = pred[c]
        sd = math.sqrt(max(var, 1e-9))
        tol = target.tolerance[c]
        out[c] = (max(_phi((t + tol - m) / sd) - _phi((t - tol - m) / sd), 1e-12), w)
    return out


def p_in_band(pred: dict[str, tuple[float, float]], target: TargetBand | None) -> float:
    """How surely the setup is in the band: the weighted geometric mean over the cells of
    each one's chance of being inside (a typical cell's chance; a product over six cells
    would call even a perfect prediction doubtful)."""
    probs = cell_probs(pred, target)
    if not probs:
        return 1.0
    weights = sum(w for _, w in probs.values())
    return math.exp(sum(w * math.log(p) for p, w in probs.values()) / weights)


class _Scorer:
    def __init__(self, prob: Problem):
        self.prob = prob
        self.cache: dict[tuple, Evaluated] = {}

    def __call__(self, idx: dict[str, int], target: TargetBand | None = None) -> Evaluated:
        prob = self.prob
        target = target if target is not None else prob.target
        key = tuple(sorted(idx.items()))
        ev = self.cache.get(key)
        if ev is None:
            pred = prob.balance.predict(x_of(prob.dims, idx)) if prob.balance.offset else {}
            p = 1.0
            for f in prob.extra:
                p *= f(idx)
            if prob.platform is not None and not prob.platform.ok(prob.base, idx):
                p = 0.0
            moved = [k for k, v in idx.items() if v != prob.base[k]]
            ev = Evaluated(dict(idx), prob.lap(idx), pred, p, len(moved), change_s=change_cost(prob, idx, moved))
            self.cache[key] = ev  # p_ok here: platform and extra constraints, without the band
        out = Evaluated(ev.indices, ev.lap, ev.balance, ev.p_ok * p_in_band(ev.balance, target), ev.changes,
                        p_extra=ev.p_ok, change_s=ev.change_s)
        out.off = violation(out, target)
        return out


def change_cost(prob: Problem, idx: dict[str, int], moved: list[str] | None = None) -> float:
    """Seconds a setup is charged for being a change: each setting moved at its own cost
    (CHANGE_COST_S if it has none), and the penalty for where it ends up (rear toe-out)."""
    if moved is None:
        moved = [k for k, v in idx.items() if v != prob.base[k]]
    s = sum(prob.cost.get(k, CHANGE_COST_S) for k in moved)
    return s + (prob.penalty(idx) if prob.penalty is not None else 0.0)


def _region(prob: Problem, d: Dim) -> int:
    r = max(1, round(prob.radius * (d.hi - d.lo)))
    return min(r, prob.reach[d.name]) if d.name in prob.reach else r


def _candidates(prob: Problem, start: dict[str, int], free: list[Dim], rng) -> list[dict[str, int]]:
    dims = {d.name: d for d in prob.dims}
    drag = aero_wing(prob.dims)
    out = [dict(start)]
    for d in free:
        r = _region(prob, d)
        for step in sorted({s for s in (1, 2, r, -1, -2, -r) if abs(s) <= r}):
            v = start[d.name] + step
            if d.lo <= v <= d.hi and v != start[d.name]:
                out.append({**start, d.name: v})
    for _ in range(POOL if free else 0):
        k = int(rng.integers(1, min(MAX_CHANGES, len(free)) + 1))
        pick = rng.choice(len(free), size=k, replace=False)
        cand = dict(start)
        for i in pick:
            d = free[i]
            r = _region(prob, d)
            cand[d.name] = d.clamp(start[d.name] + int(rng.integers(-r, r + 1)))
        out.append(cand)
    for c in prob.candidates:  # e.g. the fingerprint's setup: allowed past the region, a jump you asked for
        out.append({**start, **{k: (dims[k].clamp(v) if k in dims else v) for k, v in c.items()
                                if k in start and (drag is None or k != drag.name)}})
    return [c for c in out if not avoided(prob, c)]


def avoided(prob: Problem, idx: dict[str, int]) -> bool:
    """A setup that moves a setting the way an undrivable stint did, as far or further."""
    for name, (direction, clicks) in prob.avoid.items():
        if name in idx:
            step = (idx[name] - prob.base[name]) * direction
            if step >= clicks > 0:
                return True
    return False


def _cost(ev: Evaluated) -> float:
    """Predicted lap time, made to answer for its doubt (RISK sd: a gain the models aren't
    sure of doesn't send you out on a guess), plus a little per setting changed, plus the
    doubt about the band (with little to go on about lap time, the surest in the band)."""
    return ev.lap[0] + RISK * ev.lap[1] + ev.change_s + BAND_COST_S * (1.0 - ev.p_ok)


def solve(prob: Problem) -> Choice:
    rng = np.random.default_rng(prob.seed)
    score = _Scorer(prob)
    base = score(prob.base)
    dims = {d.name: d for d in prob.dims}
    start = dict(prob.base)
    wing_info = None
    # The drag level first: the aero wing (rear, or front on a car without a rear one) where
    # the lap is fastest within the region.
    drag = aero_wing(prob.dims)
    w0 = drag.name if drag is not None else None
    if drag is not None:
        r = _region(prob, drag)
        laps = {w: prob.lap({**start, w0: w})[0] for w in range(drag.clamp(start[w0] - r), drag.clamp(start[w0] + r) + 1)
                if not avoided(prob, {**start, w0: w})}
        best_w = min(laps, key=lambda w: (laps[w] + change_cost(prob, {**start, w0: w}), abs(w - start[w0])))
        start[w0] = best_w
        wing_info = {"index": best_w, "by_index": laps}
    free = [d for d in prob.dims if d.name != w0]
    pool = [score(c) for c in _candidates(prob, start, free, rng)]
    # Without a feasible point at the chosen wing, look back towards the wing you had.
    if drag is not None and start[w0] != prob.base[w0] and not any(e.feasible for e in pool):
        start = {**start, w0: prob.base[w0]}
        pool += [score(c) for c in _candidates(prob, start, free, rng)]
    choice = _pick(pool, base, prob)
    choice.wing = wing_info
    if prob.target is not None and choice.feasible:
        choice.gap = _gaps(prob, score, pool, choice)
    return choice


def violation(ev: Evaluated, target: TargetBand | None) -> float:
    """How far outside the band the predicted balance is, deg, weighted by the cells' weights."""
    if target is None:
        return 0.0
    total = 0.0
    for c, t in target.cells.items():
        w = target.weights.get(c, 0.5)
        if w >= MIN_WEIGHT and c in ev.balance:
            total += w * max(0.0, abs(ev.balance[c][0] - t) - target.tolerance[c])
    return total


def _pick(pool: list[Evaluated], base: Evaluated, prob: Problem) -> Choice:
    feasible = [e for e in pool if e.feasible]
    if not feasible:
        # Nothing in reach is in the band: the closest to it, among the safe ones.
        safe = [e for e in pool if e.p_extra >= P_EXTRA] or pool
        best = min(safe, key=lambda e: (e.off + e.change_s, e.lap[0]))
        return Choice(best, base, "balance" if best.changes else "stay", feasible=False)
    best = min(feasible, key=_cost)
    if prob.contact is not None:
        close = [e for e in feasible if _cost(e) <= _cost(best) + TIE_S]
        best = min(close, key=lambda e: (prob.contact(e.indices), _cost(e)))
    base_ok = base.feasible
    if base_ok and _cost(base) - _cost(best) < MIN_GAIN_S:
        return Choice(base, base, "stay")
    return Choice(best, base, "faster" if base_ok else "balance")


def _gaps(prob: Problem, score: _Scorer, pool: list[Evaluated], choice: Choice) -> list[dict]:
    """Time the band costs: the best lap if the target allowed one or two presses more
    rotation on an axis."""
    out = []
    t = prob.target
    for axis in ("rear", "front"):
        for n in (1, 2):
            front, rear = (0, -n) if axis == "rear" else (-n, 0)
            moved = TargetBand(shifted(t.cells, front, rear, t.step), t.tolerance, t.weights, t.step)
            alt = [score(e.indices, moved) for e in pool]
            ok = [e for e in alt if e.feasible]
            if not ok:
                continue
            best = min(ok, key=_cost)
            gain = _cost(choice.best) - _cost(best)
            if gain >= GAP_MIN_S:
                out.append({"axis": axis, "steps": n, "seconds": round(gain, 2)})
                break
    return out


def next_radius(radius: float, improved: bool | None) -> float:
    """Trust region: grow after a proposal that paid off, shrink after one that didn't."""
    if improved is None:
        return radius
    return float(np.clip(radius * (1.5 if improved else 0.6), RADIUS_MIN, RADIUS_MAX))
