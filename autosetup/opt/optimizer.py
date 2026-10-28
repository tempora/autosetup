"""Choose the next balance setting(s) from a fitted model of your stints.

One GP per objective (pace, tyres, drivability) over the balance settings plus two
context inputs (your experience on this car + layout, track temperature). Candidates are
small moves around the setup you're on — single settings by 1–2 steps or by the trust
radius, and pairs of the best single moves — scored by the expected improvement of an
augmented Chebyshev scalarisation of the three objectives, weighted by the trade-off
learned from you (preference.py). Every candidate is evaluated "as of now" (current experience and track
temperature), so getting faster yourself isn't mistaken for a better setup.

Balance: with the angle you like per zone (each corner phase within each corner speed;
balance.py), a linear model per zone predicts the understeer angle from
where the car runs now plus which way each setting is known to push it, at a fixed size
(not learned: what one track's stints say a setting does doesn't carry to another's
corners). Drivability carries how far the balance sits outside the range you like
(S_PER_POINT s a rating point beyond BAND either side of the target).

While learning, candidates are compared as moves of the same share of their range: a
setting with few positions (springs: 3-7) would otherwise always look the most
informative, because one click of it is a big move, and dampers (40+ clicks) would never
be tried.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.opt.balance import BAND, PRIOR_POINTS, S_PER_POINT, Balance, Linear
from autosetup.opt.dataset import OBJECTIVES, SPEEDS, ZONES, Dataset, Dim
from autosetup.opt.gp import GP

N_SAMPLES = 256
CHEBYSHEV_RHO = 0.05
MIN_EI = 0.001  # standardised units; the toy-car benchmark is best near zero (0.02 stalled the loop)
RADIUS_DEFAULT, RADIUS_MIN, RADIUS_MAX = 0.2, 0.05, 0.4
LEARN_SHARE = 0.08  # least share of a range a learning move is scored as
FIXED = 1e-3  # prior sd (share of the full effect) that keeps a balance slope where it starts


def prior_spread(name: str, ds: Dataset) -> float:
    """How much an objective plausibly moves with the setup, for when data can't say."""
    if name == "pace":
        laps = [o.y["pace"] for o in (ds.obs or ds.extra)]
        return 0.004 * (float(np.mean(laps)) if laps else 100.0)  # ~0.25 s on a 64 s lap
    return {"tyres": 0.05, "drivability": 0.08}[name]


@dataclass
class Move:
    dim: Dim
    before: int
    after: int

    @property
    def steps(self) -> int:
        return self.after - self.before


@dataclass
class ModelResult:
    n_obs: int
    n_setups: int
    exploring: bool = False  # the move is chosen to learn, not because it's expected to win
    moves: list[Move] = field(default_factory=list)
    predicted: dict[str, tuple[float, float]] = field(default_factory=dict)  # delta mean, sd vs base
    per_move: list[dict[str, tuple[float, float]]] = field(default_factory=list)
    balance: dict[str, tuple[float, float]] = field(default_factory=dict)  # phase: angle delta mean, sd
    per_move_balance: list[dict[str, tuple[float, float]]] = field(default_factory=list)
    ei: float = 0.0
    confidence: float = 0.0  # chance the chosen moves beat the base setup, as of now
    effects: list[dict] = field(default_factory=list)  # per dim: effect of +1 step
    experience_effect: tuple[float, float] | None = None  # pace now vs first stint, same setup
    lengthscales: dict[str, float] = field(default_factory=dict)
    rho: dict[str, float] = field(default_factory=dict)  # cross-track correlation, when transferring


def weights_vector(weights: dict) -> np.ndarray:
    w = np.array([max(0.0, float(weights.get(k, 0.0))) for k in ("pace", "tyres", "drivability")])
    return w / w.sum() if w.sum() > 0 else np.full(3, 1 / 3)


class Model:
    def __init__(self, ds: Dataset, balance: Balance | None = None,
                 understeer: dict[str, dict[str, int]] | None = None,
                 pace_prior: dict[str, dict[str, float]] | None = None):
        self.ds = ds
        self.pace_prior = pace_prior or {}
        X = ds.X_all()
        groups = ds.groups() if ds.extra else None
        self.gps: dict[str, GP] = {}
        self.scale: dict[str, tuple[float, float]] = {}
        for name in OBJECTIVES:
            y, noise = ds.Y_all(name)
            # Pace: the GP learns what's left after the known trend (drag.py).
            trend = self._pace_trend(X, [o.group for o in ds.all_obs]) if name == "pace" else 0.0
            self.gps[name] = GP.fit(X, y - trend, noise, prior_spread(name, ds), groups)
            # Standardise by the plausible setup effect, not the observed spread: the
            # spread of pace is mostly you getting faster, which would make tyres and
            # drivability dominate the trade-off.
            own = y[: len(ds.obs)]
            self.scale[name] = (float(np.mean(own if len(own) else y)), prior_spread(name, ds))
        self.balance: dict[str, tuple[Linear, object]] = {}  # zone: (model of the angle, Target)
        understeer = understeer or {}
        for ph, t in (balance.targets.items() if balance else ()):
            rows, y, noise = ds.B_all(ph)
            if not len(rows):
                continue
            # What each setting does is fixed (its known direction, PRIOR_POINTS across its
            # range), not learned: one track's stints don't say what it does at the next.
            # Only the level the car runs at is measured, per track.
            full = PRIOR_POINTS * t.per_point
            signs = np.array([understeer.get(d.keys[0], {}).get(ph, 0) for d in ds.dims] + [0, 0], dtype=float)
            self.balance[ph] = (Linear(X[rows], y, noise, groups[rows] if groups is not None else None,
                                       signs * full, np.full(len(signs), FIXED * full), 0.5 * t.per_point), t)

    def _pace_trend(self, X: np.ndarray, layouts: list[str]) -> np.ndarray:
        """Lap time each row's settings are expected to add at its layout, from
        ``pace_prior`` (per layout: seconds across a setting's whole range, index up)."""
        out = np.zeros(len(X))
        for i, d in enumerate(self.ds.dims):
            slope = np.array([self.pace_prior.get(lay, {}).get(d.name, 0.0) for lay in layouts])
            out += slope * X[:, i]
        return out

    def predict(self, X: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        out = {n: self.gps[n].predict(X) for n in OBJECTIVES}
        if self.pace_prior:
            m, v = out["pace"]
            out["pace"] = (m + self._pace_trend(X, [self.ds.target] * len(X)), v)
        return out

    def predict_balance(self, X: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        return {ph: gp.predict(X) for ph, (gp, _) in self.balance.items()}

    def z(self, X: np.ndarray, eps: np.ndarray, sample: bool) -> np.ndarray:
        """Standardised costs: (n, S, 3) sampled with ``eps`` (S, 3 + zones), or (n, 3)
        at the posterior means. Drivability includes how far each zone's balance sits
        outside the range you like (BAND either side of the target; always averaged over
        samples: the distance has no "mean" shortcut)."""
        p = self.predict(X)
        mu = np.stack([(p[n][0] - self.scale[n][0]) / self.scale[n][1] for n in OBJECTIVES], -1)
        off = np.zeros((len(X), len(eps)))
        for ph, (m, v) in self.predict_balance(X).items():
            angle = m[:, None] + np.sqrt(v)[:, None] * eps[None, :, 3 + ZONES.index(ph)]
            off += np.maximum(0.0, np.abs(self.balance[ph][1].points(angle)) - BAND)
        # Each corner phase is judged at every corner speed: average over the speeds, so
        # a phase weighs what it did as a whole.
        cost = S_PER_POINT * off / len(SPEEDS) / self.scale["drivability"][1]
        if not sample:
            mu[:, 2] += cost.mean(1)
            return mu
        sd = np.stack([np.sqrt(p[n][1]) / self.scale[n][1] for n in OBJECTIVES], -1)
        z = mu[:, None, :] + sd[:, None, :] * eps[None, :, :3]
        z[..., 2] += cost
        return z

    def info(self, X: np.ndarray, w: np.ndarray) -> np.ndarray:
        """Weighted predictive sd: how much driving X would teach, balance included."""
        p = self.predict(X)
        var = sum(w[i] ** 2 * p[n][1] / self.scale[n][1] ** 2 for i, n in enumerate(OBJECTIVES))
        for ph, (_, v) in self.predict_balance(X).items():
            var = var + (w[2] * S_PER_POINT / self.scale["drivability"][1] / self.balance[ph][1].per_point) ** 2 * v
        return np.sqrt(var)

    def x_of(self, setup: CarSetup, context: tuple[float, float | None]) -> np.ndarray:
        return self.ds.X([setup], [context])[0]


def _scalarise(z: np.ndarray, w: np.ndarray) -> np.ndarray:
    """z: (..., 3) standardised costs → augmented Chebyshev cost (lower is better)."""
    return (w * z).max(-1) + CHEBYSHEV_RHO * (w * z).sum(-1)


@dataclass(frozen=True)
class ChangeSize:
    """How much one proposal may change: settings per stint, step size as a share of each
    setting's range (at least one click), and the trust region it starts from."""

    name: str
    moves: int
    step_frac: float
    radius: float


CHANGE_SIZES = {
    "small": ChangeSize("small", 1, 0.0, 0.12),
    "medium": ChangeSize("medium", 3, 0.08, 0.2),
    "large": ChangeSize("large", 5, 0.15, 0.3),
}
DEFAULT_SIZE = "medium"
POOL = 400  # random multi-setting candidates per proposal


def group_of(dim: Dim) -> str:
    """Settings that do related jobs; one proposal changes at most one per group."""
    n = dim.name
    if n == "VM_TRACTIONCONTROLMAP":
        return "tc:map"
    if n == "VM_TRACTIONCONTROLPOWERCUTMAP":
        return "tc:cut"
    if n == "VM_TRACTIONCONTROLSLIPANGLEMAP":
        return "tc:slip"
    if n == "VM_FRONT_TOEIN":
        return "toe:F"
    if n == "VM_REAR_TOEIN":
        return "toe:R"
    if n.startswith("WM_CAMBER"):
        return "camber" + n[-2:]  # ':F' / ':R'
    if n == "VM_CASTER":
        return "caster"
    if n == "VM_ENGINE_BRAKEMAP":
        return "engine_brake"
    if n == "VM_BRAKE_BALANCE":
        return "brake_bias"
    if n == "VM_STEER_LOCK":
        return "steering"
    for prefix, group in (("VM_FRONT_3RD", "third:F"), ("VM_REAR_3RD", "third:R"), ("VM_TRACTIONCONTROL", "tc"), ("VM_ANTILOCK", "abs"), ("VM_DIFF", "diff"),
                          ("VM_BRAKE", "brakes"), ("VM_FRONT_ANTISWAY", "bar:F"), ("VM_REAR_ANTISWAY", "bar:R"),
                          ("VM_REAR_WING", "aero"), ("VM_FRONT_WING", "aero"), ("WM_SPRING", "spring")):
        if n.startswith(prefix):
            return group + (n[-2:] if group == "spring" else "")
    if n.startswith("WM_") and ("BUMP" in n or "REBOUND" in n):
        return "damper" + n[-2:]  # ':F' / ':R'
    return n
EXPLORE_BONUS = 0.5  # weight of "what we'd learn" against expected improvement while learning


def optimise(ds: Dataset, base: CarSetup, weights: dict, context_now: tuple[float, float | None],
             locks: set[str] = frozenset(), size: str | ChangeSize = DEFAULT_SIZE, radius: float | None = None,
             learning: bool = False, seed: int = 0, balance: Balance | None = None,
             understeer: dict[str, dict[str, int]] | None = None,
             pace_prior: dict[str, dict[str, float]] | None = None) -> ModelResult:
    """Pick the next setting changes.

    ``learning`` (too little data to trust the model): change exactly ``size.moves``
    settings, choosing the combination that best mixes expected improvement with what it
    would teach the model (its predictive uncertainty). Several settings changing at once
    is fine: the model separates their effects across stints, and one click per stint
    would take dozens of stints to cover ~20 settings.
    Otherwise: up to ``size.moves`` settings by expected improvement; no move when nothing
    is expected to improve.
    ``balance``: the angle you like per phase; without it balance isn't modelled.
    ``understeer``: per setting key and phase, +1 if a higher index understeers more
    (advisor.understeer_signs); where the balance model's slopes start.
    ``pace_prior``: per layout and setting (dim name), seconds of lap time across its whole
    range going up in index; where the pace model starts (drag.py: the rear wing).
    """
    size = CHANGE_SIZES[size] if isinstance(size, str) else size
    radius = size.radius if radius is None else radius
    res = ModelResult(len(ds.obs), ds.distinct_setups)
    if not ds.all_obs or not ds.dims:
        return res
    model = Model(ds, balance, understeer, pace_prior)
    res.lengthscales = {n: gp.lengthscale for n, gp in model.gps.items()}
    if ds.extra:
        res.rho = {n: gp.rho for n, gp in model.gps.items()}
    w = weights_vector(weights)
    x0 = model.x_of(base, context_now)
    dims = [d for d in ds.dims if not any(k in locks for k in d.keys)]
    rng = np.random.default_rng(seed)

    share = max(size.step_frac, LEARN_SHARE)

    def setup_x(moves: list[Move], fair: bool = False) -> np.ndarray:
        """Inputs after the moves; ``fair``: each move as the same share of its range."""
        x = x0.copy()
        for m in moves:
            i = ds.dims.index(m.dim)
            x[i] = (np.clip(x0[i] + np.sign(m.steps) * share, 0.0, 1.0) if fair
                    else (m.after - m.dim.lo) / max(m.dim.hi - m.dim.lo, 1))
        return x

    def step_of(d: Dim) -> int:
        return max(1, round(size.step_frac * (d.hi - d.lo)))

    # Single moves: the profile's step, double it, and the trust radius, both ways.
    options: dict[int, list[Move]] = {}
    for i, d in enumerate(dims):
        cur = d.index(d.x(base))
        s1 = step_of(d)
        r = max(1, round(radius * (d.hi - d.lo)))
        steps = {s1, -s1} if learning else {s1, -s1, 2 * s1, -2 * s1, r, -r}
        options[i] = [Move(d, cur, cur + st) for st in sorted(steps) if d.lo <= cur + st <= d.hi and st != 0]
    options = {i: o for i, o in options.items() if o}
    if not options:
        return res
    singles = [[m] for o in options.values() for m in o]

    eps = rng.standard_normal((N_SAMPLES, 3 + len(ZONES)))  # common random numbers across candidates
    driven = np.array([model.x_of(o.setup, context_now) for o in ds.obs] + [x0])
    s_best = float(_scalarise(model.z(driven, eps, sample=False), w).min())

    def score(cands: list[list[Move]]) -> tuple[np.ndarray, np.ndarray]:
        """(expected improvement, weighted predictive sd) per candidate."""
        X = np.array([setup_x(c, fair=learning) for c in cands])
        ei = np.maximum(s_best - _scalarise(model.z(X, eps, sample=True), w), 0.0).mean(1)
        return ei, model.info(X, w)

    # Multi-setting candidates: greedy combinations of the best singles, plus a random
    # pool so settings the model knows nothing about get picked too.
    single_ei, _ = score(singles)
    k = min(size.moves, len(options))
    order = np.argsort(-single_ei)
    combos: list[list[Move]] = []
    greedy: list[Move] = []
    for j in order:
        m = singles[j][0]
        if all(group_of(g.dim) != group_of(m.dim) for g in greedy):
            greedy.append(m)
            combos.append(list(greedy))
        if len(greedy) == k:
            break
    by_group: dict[str, list[int]] = {}
    for i in options:
        by_group.setdefault(group_of(dims[i]), []).append(i)
    groups = list(by_group)
    kg = min(k, len(groups))
    for _ in range(POOL if kg > 1 else 0):
        picked = [by_group[groups[g]][rng.integers(len(by_group[groups[g]]))]
                  for g in rng.choice(len(groups), size=kg, replace=False)]
        combos.append([options[i][rng.integers(len(options[i]))] for i in picked])
    style_intents = [
        {"toe:F": -1, "bar:F": -1, "bar:R": 1, "camber:F": 1, "diff": -1, "aero": -1},
        {"toe:R": 1, "bar:F": 1, "bar:R": -1, "diff": 1, "aero": 1},
        {"spring:F": -1, "spring:R": -1, "bar:F": -1, "bar:R": -1, "toe:F": -1},
        {"diff": -1, "tc:cut": -1, "tc:slip": -1, "engine_brake": -1},
    ]
    for style in style_intents:
        style_moves = []
        for grp, direction in style.items():
            if grp in by_group:
                dim_idx = by_group[grp][0]
                matching = [m for m in options[dim_idx] if np.sign(m.steps) == direction]
                if matching:
                    style_moves.append(matching[0])
            if len(style_moves) == kg:
                break
        if len(style_moves) == kg:
            combos.append(style_moves)
    k = kg if learning else k
    candidates = singles + combos if not learning else [c for c in combos + singles if len(c) == k]
    ei, info = score(candidates)
    total = ei + EXPLORE_BONUS * info if learning else ei
    best = int(np.argmax(total))
    res.ei = float(ei[best])

    base_pred = model.predict(x0[None, :])
    base_bal = model.predict_balance(x0[None, :])
    # EI rewards uncertainty: a model that knows nothing still finds "expected
    # improvement" everywhere. This is the plain chance the pick is better than staying.
    z_pick = model.z(setup_x(candidates[best])[None, :], eps, sample=True)[0]
    z_base = model.z(x0[None, :], eps, sample=False)[0]
    res.confidence = float((_scalarise(z_pick, w) < _scalarise(z_base, w)).mean())

    def delta(moves: list[Move]) -> dict[str, tuple[float, float]]:
        p = model.predict(setup_x(moves)[None, :])
        return {n: (float(p[n][0][0] - base_pred[n][0][0]), float(np.sqrt(p[n][1][0] + base_pred[n][1][0])))
                for n in OBJECTIVES}

    def balance_delta(moves: list[Move]) -> dict[str, tuple[float, float]]:
        p = model.predict_balance(setup_x(moves)[None, :])
        return {ph: (float(m[0] - base_bal[ph][0][0]), float(np.sqrt(v[0] + base_bal[ph][1][0])))
                for ph, (m, v) in p.items()}

    # Sensitivities: what one step up on each setting is expected to do, as of now.
    for d in ds.dims:
        cur = d.index(d.x(base))
        if cur + 1 <= d.hi:
            step = [Move(d, cur, cur + 1)]
            res.effects.append({"dim": d.name, "keys": d.keys, "delta": delta(step), "balance": balance_delta(step)})
    # Your own improvement: the base setup at your first stint's experience vs now.
    if ds.obs:
        first = (ds.obs[0].experience, context_now[1])
        p_then = model.predict(model.x_of(base, first)[None, :])["pace"]
        res.experience_effect = (float(base_pred["pace"][0][0] - p_then[0][0]),
                                 float(np.sqrt(base_pred["pace"][1][0] + p_then[1][0])))

    if not learning and res.ei < MIN_EI:
        return res
    res.exploring = learning
    res.moves = candidates[best]
    res.predicted = delta(res.moves)
    res.per_move = [delta([m]) for m in res.moves]
    res.balance = balance_delta(res.moves)
    res.per_move_balance = [balance_delta([m]) for m in res.moves]
    return res


def next_radius(radius: float, improved: bool | None) -> float:
    """Trust region: grow after a proposal that paid off, shrink after one that didn't."""
    if improved is None:
        return radius
    return float(np.clip(radius * (1.5 if improved else 0.7), RADIUS_MIN, RADIUS_MAX))
