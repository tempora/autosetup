"""Dampers: the contact patch, inside the same balance band, without making kerbs worse.

Per stint (racing laps outside the pits, current build), from metrics/suspension.py and
metrics/kerbs.py:

  contact (off-kerb samples, lower is better)
    cv_<axle>      wheel-load variation, std/mean (tyre load from DAM, else the spring proxy)
    sym_<axle>     |bump share − ½| of the shaft-velocity histogram (balanced bump/rebound)
    knee_<axle>    |share under the knee − damper_ls_target| (a sensible slow/fast split)
    shape          front vs rear histogram, L1 distance of the normalised shapes
  kerbs (kerb samples: constraints, a change mustn't make them worse)
    peak_load, airtime_s, pitch_deg, heave_mm, yaw_dist   (means over the kerb events)

What a click does is learned per car and layout (Bayesian linear in the settings' share
of range), from priors that say which settings serve what: fast bump and rebound mainly
the kerbs, slow bump and rebound mainly contact patch and platform (and their known
balance effects, priors.py). Springs are inputs too (they move the same numbers), so the
main block can use the contact score as its tiebreaker and the kerb numbers as a
constraint.

A move is scored on what it changes: the contact score, plus the kerb numbers at
KERB_WEIGHT (a fast damper that takes the kerbs better is worth proposing even if the
contact patch off them barely moves). The kerbs stay a constraint as well: no kerb number
may get worse by more than KERB_MARGIN.

Moves are whole clicks, at most MAX_MOVES settings per proposal; the balance band must
still hold with them (a damper change that moves balance out of it is turned down). Each
setting is explored on its own: until it has been driven at EXPLORE_VALUES different
values, what a move of it would teach counts too.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from autosetup.metrics.suspension import BIN_MM_S, EDGES
from autosetup.opt.space import DAMPERS, Dim

CONTACT = ("cv_front", "cv_rear", "sym_front", "sym_rear", "knee_front", "knee_rear", "shape")
KERB = ("peak_load", "airtime_s", "pitch_deg", "heave_mm", "yaw_dist")
SCALE = {"cv_front": 0.03, "cv_rear": 0.03, "sym_front": 0.03, "sym_rear": 0.03, "knee_front": 0.05,
         "knee_rear": 0.05, "shape": 0.1, "peak_load": 0.1, "airtime_s": 0.01, "pitch_deg": 0.05, "heave_mm": 1.0,
         "yaw_dist": 0.01}
KERB_MARGIN = 0.5  # in SCALE units: how much worse than the stint you drove a kerb number may get
MAX_MOVES = 2
MAX_CLICKS = 3  # per damper move
MAX_PACKER_CLICKS = 2
MIN_GAIN = 0.1  # score units (contact + weighted kerbs) a move must be expected to win
KERB_WEIGHT = 0.5  # a kerb number's SCALE unit against a contact one
EXPLORE = 0.3  # weight of what a move would teach, while its setting is little known
EXPLORE_VALUES = 3  # values of one setting driven before what its click does counts as known
DAMPER_NAMES = {name for name, _ in DAMPERS}
SHAPE_BIN = 20.0  # mm/s, histograms are compared at this width
WHEELS = {"front": ("fl", "fr"), "rear": ("rl", "rr")}


def _prior(name: str) -> dict[str, tuple[float, float]]:
    """Per metric: (prior mean, prior sd) of the effect of the setting's whole range, in
    SCALE units, going up in index (stiffer / more)."""
    kind, axle = (name.rsplit("_", 1) + [""])[:2]
    third = name.startswith("third_")
    kind = kind.removeprefix("third_")
    out: dict[str, tuple[float, float]] = {}
    same = [axle] if axle in WHEELS else ["front", "rear"]
    if kind in ("slow_bump", "slow_rebound"):
        for a in same:
            out[f"cv_{a}"] = (0.5, 1.5)  # stiffer: the wheel follows the road less
            out[f"sym_{a}"] = (0.0, 1.0)
            out[f"knee_{a}"] = (-0.5, 1.5)  # stiffer: more of the motion is slow
        out["pitch_deg"], out["heave_mm"] = (-0.5, 1.0), (-0.5, 1.0)  # platform: less body motion
        for k in ("peak_load", "airtime_s", "yaw_dist"):
            out[k] = (0.0, 0.5)
    elif kind in ("fast_bump", "fast_rebound"):
        for a in same:
            out[f"cv_{a}"], out[f"sym_{a}"], out[f"knee_{a}"] = (0.2, 0.5), (0.0, 0.5), (0.0, 0.5)
        if kind == "fast_bump":  # stiffer: the kerb goes into the car
            out.update(peak_load=(1.0, 2.0), pitch_deg=(0.5, 2.0), heave_mm=(0.5, 2.0), yaw_dist=(0.5, 2.0),
                       airtime_s=(0.5, 2.0))
        else:  # stiffer: the wheel comes back down slower after it
            out.update(airtime_s=(1.0, 2.0), peak_load=(0.0, 1.0), pitch_deg=(0.0, 1.0), heave_mm=(-0.5, 2.0),
                       yaw_dist=(0.5, 2.0))
    elif kind == "packers":
        for a in same:
            out[f"cv_{a}"] = (0.5, 1.5)
        out.update(pitch_deg=(-0.5, 1.5), heave_mm=(-1.0, 1.5), peak_load=(0.5, 1.5))
    elif kind == "spring":
        for a in same:
            out[f"cv_{a}"] = (0.5, 1.5)
        out.update(pitch_deg=(-0.5, 1.5), heave_mm=(-0.5, 1.5), peak_load=(0.5, 1.5), yaw_dist=(0.3, 1.5))
    if third:  # a third element works on the platform, less on one wheel
        out = {k: (m * 0.5, s) for k, (m, s) in out.items()}
    out.setdefault("shape", (0.0, 1.0))
    return out


@dataclass
class StintMetrics:
    rid: int
    x: dict[str, float]  # dim name → share of range
    values: dict[str, tuple[float, float]]  # metric → (mean, var)


def _hist(store, rid: int, wheels: tuple[str, ...], kerb: bool) -> np.ndarray:
    total = np.zeros(len(EDGES) - 1)
    for lo, hist in store.con.execute(
            """select s.hist_lo, s.hist from lap_suspension s join laps l on l.recording_id = s.recording_id
               and l.seq = s.seq and not l.in_pits
               where s.recording_id = ? and s.kerb = ? and s.wheel in (select unnest(?::VARCHAR[]))""",
            [rid, kerb, list(wheels)]).fetchall():
        h = np.array(json.loads(hist), dtype=float)
        total[lo:lo + len(h)] += h
    return total


def rebin(counts: np.ndarray, width: float) -> tuple[np.ndarray, np.ndarray]:
    """(edges mm/s, counts) at ``width`` (a multiple of the stored 5 mm/s)."""
    k = max(1, int(round(width / BIN_MM_S)))
    n = len(counts) // k * k
    return EDGES[: n + 1: k], counts[:n].reshape(-1, k).sum(1)


def stint_metrics(store, rid: int, knowledge: dict) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    rows = store.con.execute(
        """select s.wheel, s.stats from lap_suspension s join laps l on l.recording_id = s.recording_id
           and l.seq = s.seq and not l.in_pits where s.recording_id = ? and not s.kerb""", [rid]).fetchall()
    stats: dict[str, list[dict]] = {}
    for wheel, st in rows:
        stats.setdefault(wheel, []).append(json.loads(st))
    target_ls = float(knowledge["damper_ls_target"])
    for axle, wheels in WHEELS.items():
        laps = [s for w in wheels for s in stats.get(w, [])]
        for name, get in ((f"cv_{axle}", lambda s: s.get("load_cv")),
                          (f"sym_{axle}", lambda s: abs(s["bump_frac"] - 0.5) if "bump_frac" in s else None),
                          (f"knee_{axle}", lambda s: abs(s["ls_frac"] - target_ls) if "ls_frac" in s else None)):
            vals = [v for v in (get(s) for s in laps) if v is not None]
            if vals:
                out[name] = (float(np.mean(vals)), float(np.var(vals) / len(vals)) + (0.2 * SCALE[name]) ** 2)
    f, r = (_hist(store, rid, WHEELS[a], False) for a in ("front", "rear"))
    if f.sum() > 0 and r.sum() > 0:
        _, fb = rebin(f, SHAPE_BIN)
        _, rb = rebin(r, SHAPE_BIN)
        out["shape"] = (float(np.abs(fb / fb.sum() - rb / rb.sum()).sum()), (0.2 * SCALE["shape"]) ** 2)
    kerbs = store.con.execute(
        f"select {', '.join(KERB)} from kerb_events where recording_id = ?", [rid]).fetchall()
    for i, name in enumerate(KERB):
        vals = [r[i] for r in kerbs if r[i] is not None and math.isfinite(r[i])]
        if len(vals) >= 2:
            out[name] = (float(np.mean(vals)), float(np.var(vals) / len(vals)) + (0.2 * SCALE[name]) ** 2)
    return out


@dataclass
class Linear:
    mean: np.ndarray
    cov: np.ndarray
    names: list[str]

    def predict(self, x: dict[str, float]) -> tuple[float, float]:
        phi = np.array([1.0] + [x.get(n, 0.0) for n in self.names])
        return float(phi @ self.mean), float(max(phi @ self.cov @ phi, 0.0))


@dataclass
class DamperModel:
    dims: list[Dim]
    models: dict[str, Linear] = field(default_factory=dict)
    stints: int = 0
    settings: int = 0  # distinct damper settings among the stints (the springs aside)
    tried: dict[str, int] = field(default_factory=dict)  # per setting, distinct values driven

    @classmethod
    def fit(cls, dims: list[Dim], data: list[StintMetrics]) -> "DamperModel":
        names = [d.name for d in dims]
        priors = {d.name: _prior(d.name) for d in dims}
        models = {}
        for metric in CONTACT + KERB:
            rows = [(s.x, s.values[metric]) for s in data if metric in s.values]
            if not rows:
                continue
            scale = SCALE[metric]
            m0 = np.array([float(np.mean([v[0] for _, v in rows]))] +
                          [priors[n].get(metric, (0.0, 0.3))[0] * scale for n in names])
            s0 = np.array([10.0 * scale + abs(m0[0])] + [priors[n].get(metric, (0.0, 0.3))[1] * scale for n in names])
            Phi = np.array([[1.0] + [x.get(n, 0.0) for n in names] for x, _ in rows])
            y = np.array([v[0] for _, v in rows])
            var = np.array([max(v[1], 1e-12) for _, v in rows])
            A = np.diag(1 / s0 ** 2) + Phi.T @ (Phi / var[:, None])
            cov = np.linalg.inv(A)
            models[metric] = Linear(cov @ (m0 / s0 ** 2 + Phi.T @ (y / var)), cov, names)
        damper = [n for n in names if n in DAMPER_NAMES]
        settings = len({tuple(round(s.x.get(n, 0.0), 6) for n in damper) for s in data})
        tried = {n: len({round(s.x[n], 6) for s in data if n in s.x}) for n in names}
        return cls(dims, models, len(data), settings, tried)

    def contact(self, x: dict[str, float]) -> tuple[float, float]:
        """Contact score (sum of the contact metrics in SCALE units): mean, variance."""
        m = v = 0.0
        for metric in CONTACT:
            if metric in self.models:
                a, b = self.models[metric].predict(x)
                m += a / SCALE[metric]
                v += b / SCALE[metric] ** 2
        return m, v

    def _delta(self, metrics, x: dict[str, float], x_base: dict[str, float]) -> tuple[float, float]:
        """Predicted change of the sum of ``metrics`` (SCALE units) from ``x_base`` to ``x``:
        mean, variance. Higher is worse."""
        m = v = 0.0
        for metric in metrics:
            lin = self.models.get(metric)
            if lin is None:
                continue
            d = np.array([0.0] + [x.get(n, 0.0) - x_base.get(n, 0.0) for n in lin.names])
            m += float(d @ lin.mean) / SCALE[metric]
            v += float(max(d @ lin.cov @ d, 0.0)) / SCALE[metric] ** 2
        return m, v

    def change(self, x: dict[str, float], x_base: dict[str, float]) -> tuple[float, float, float]:
        """What a move does to the score: (contact change, kerb change, variance of the
        weighted sum). Negative is better."""
        cm, cv = self._delta(CONTACT, x, x_base)
        km, kv = self._delta(KERB, x, x_base)
        return cm, km, cv + KERB_WEIGHT ** 2 * kv

    def kerb_ok(self, x: dict[str, float], x_base: dict[str, float]) -> float:
        """Chance no kerb number gets worse than at ``x_base`` (the setup you drove) by more
        than KERB_MARGIN: the change is judged, under one model, not the model's level
        against one stint's measurement."""
        p = 1.0
        for metric in KERB:
            lin = self.models.get(metric)
            if lin is None:
                continue
            d = (np.array([0.0] + [x.get(n, 0.0) for n in lin.names])
                 - np.array([0.0] + [x_base.get(n, 0.0) for n in lin.names]))
            m, var = float(d @ lin.mean), float(max(d @ lin.cov @ d, 0.0))
            if var <= 0 and m <= 0:
                continue
            sd = math.sqrt(max(var, 1e-12))
            p *= 0.5 * (1.0 + math.erf((KERB_MARGIN * SCALE[metric] - m) / (sd * math.sqrt(2.0))))
        return p


@dataclass
class DamperChoice:
    moves: dict[str, tuple[int, int]]  # dim → (from, to)
    contact: tuple[float, float]  # predicted score, sd, after
    contact_base: tuple[float, float]
    exploring: bool
    reason: str
    kerbs: float = 0.0  # predicted change of the kerb numbers, SCALE units (negative: better)


def _reach(d: Dim) -> int:
    """Clicks a damper move may go: a few, packers fewer (they're platform too)."""
    return max(1, min(MAX_PACKER_CLICKS if "packers" in d.name else MAX_CLICKS, round(0.15 * (d.hi - d.lo))))


def choose(model: DamperModel, dims: list[Dim], base: dict[str, int], x_main: dict[str, float],
           balance_ok, explore: bool = True) -> DamperChoice:
    """Best damper moves (whole clicks): the most predicted gain in contact score and
    weighted kerb numbers, kerbs within margin, the balance band holding (``balance_ok``:
    indices of these dims → probability). A move of a setting driven at fewer than
    EXPLORE_VALUES values also counts what it would teach (EXPLORE), unless ``explore``
    is off (a stint that changes the balance isn't also a damper experiment: what changed
    on track couldn't be told apart)."""
    def x_of(idx):
        return {**x_main, **{d.name: d.x(idx[d.name]) for d in dims}}

    xb = x_of(base)

    def score(idx):
        """(value, gain, sd of the gain, new): value adds the teaching bonus to the gain."""
        cm, km, v = model.change(x_of(idx), xb)
        gain, sd = -(cm + KERB_WEIGHT * km), math.sqrt(v)
        new = explore and any(idx[n] != base[n] and model.tried.get(n, 0) < EXPLORE_VALUES for n in idx)
        return gain + (EXPLORE * sd if new else 0.0), gain, sd, new

    def allowed(idx):
        return model.kerb_ok(x_of(idx), xb) >= 0.5 and balance_ok(idx) >= 0.5

    singles = []
    for d in dims:
        r = _reach(d)
        for step in sorted({s for s in (1, 2, r, -1, -2, -r) if abs(s) <= r}):
            to = base[d.name] + step
            if d.lo <= to <= d.hi:
                idx = {**base, d.name: to}
                if allowed(idx):
                    singles.append((score(idx), idx, d.name))
    singles.sort(key=lambda s: -s[0][0])
    cands = [(s, idx) for s, idx, _ in singles]
    for i, (_, idx1, n1) in enumerate(singles[:6]):
        for _, idx2, n2 in singles[i + 1:6]:
            if n1 != n2:
                idx = {**idx1, n2: idx2[n2]}
                if allowed(idx):
                    cands.append((score(idx), idx))
    # A gain is claimed only when it clears its own doubt; a move that doesn't is for
    # learning, and only while one of the settings it moves is little known.
    ok = [(s, idx) for s, idx in cands
          if s[1] - 0.5 * s[2] >= MIN_GAIN or (s[3] and s[0] >= MIN_GAIN)]
    b_m, b_v = model.contact(xb)
    if not ok:
        return DamperChoice({}, (b_m, math.sqrt(b_v)), (b_m, math.sqrt(b_v)), False, "")
    (_, gain, sd, _), best = max(ok, key=lambda c: c[0][0])
    moves = {k: (base[k], best[k]) for k in best if best[k] != base[k]}
    exploring = gain - 0.5 * sd < MIN_GAIN
    cm, km, _ = model.change(x_of(best), xb)
    m, v = model.contact(x_of(best))
    if exploring:
        reason = "learning what it does to the tyre load and kerbs"
    else:
        reason = "takes the kerbs better" if -KERB_WEIGHT * km > -cm else "steadier tyre load"
    return DamperChoice(moves, (m, math.sqrt(v)), (b_m, math.sqrt(b_v)), exploring, reason, km)


def histograms(store, rid: int, kerbs: bool | None, width: float, knowledge: dict) -> dict:
    """Per wheel, the shaft-velocity histogram (rebound −, bump +) at ``width`` mm/s, with
    kerb samples (True), without (False) or both (None), and its stats."""
    knee = float(knowledge["damper_knee_mm_s"])
    out = {"knee": knee, "width": width, "wheels": {}}
    for wheel in ("fl", "fr", "rl", "rr"):
        counts = np.zeros(len(EDGES) - 1)
        for k in ((False, True) if kerbs is None else (kerbs,)):
            counts += _hist(store, rid, (wheel,), k)
        edges, c = rebin(counts, width)
        total = c.sum()
        centres = (edges[:-1] + edges[1:]) / 2
        st = {}
        if total > 0:
            p = c / total
            mean = float(np.sum(p * centres))
            sd = float(np.sqrt(np.sum(p * (centres - mean) ** 2)))
            st = {"bump": float(p[centres > 0].sum()), "rebound": float(p[centres < 0].sum()),
                  "slow": float(p[np.abs(centres) < knee].sum()), "fast": float(p[np.abs(centres) >= knee].sum()),
                  "skew": float(np.sum(p * ((centres - mean) / sd) ** 3)) if sd > 0 else 0.0}
        cv = [json.loads(s).get("load_cv") for (s,) in store.con.execute(
            "select stats from lap_suspension where recording_id = ? and wheel = ? and not kerb", [rid, wheel]).fetchall()]
        cv = [v for v in cv if v is not None]
        if cv:
            st["load_cv"] = float(np.mean(cv))
        out["wheels"][wheel] = {"edges": edges.tolist(), "counts": c.astype(int).tolist(), "stats": st}
    return out
