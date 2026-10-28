"""Calibrating the lap model from your laps, and the per-car aero map.

The car (lapsim.Car) is your measured envelope: per 5 m/s of speed, net acceleration at
full throttle (median), braking (95th percentile in a straight line) and lateral grip
(95th percentile in corners, as μ·g·(1 + c·v²)). It already holds the engine, gearing,
drag and downforce of the setup you drove.

What a setup change does is measured between stints at the same layout, where track,
slopes and gearing are the same:
  drag        a_x = f(v) + s_b − Δk_r · v² on straights at full throttle, away from gear
              changes; f is shared, s_b a per-25 m slope term shared by every stint (a hill
              would otherwise read as drag), Δk_r the stint's drag against the others;
  downforce   lateral limit per speed bin = f(v) + μ·g·Δc_r·v², f shared (lift_deltas);
  balance     high- minus low-speed understeer (deg, stage 1 cells).

The aero map, per car model and game build, is the slope of each against rear wing
(share of its range), front ride height and rake at speed (mm, measured). Every layout's
stints are compared among themselves (values and platform taken relative to the layout's
mean), so the slopes carry to other tracks. Bayesian with weak priors from classes.toml:
a few stints already give a direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.sim.lapsim import G, V_CAP, V_GRID, Car

SLOPE_SD = 0.5  # m/s²: prior sd of a 25 m stretch's slope term
DRAG_SD = 5e-4  # 1/m: prior sd of one stint's drag against the layout's others
MIN_LAT_BINS = 3
BRAKE_PER_LAT = 1.1  # braking against lateral grip, with no braking zone measured
AT_LIMIT = 0.85  # a speed bin under this share of the running-max limit says nothing about downforce
FEATURES = ("wing", "rh_front", "rake")
SPAN_MM = 3.0  # mm beyond the driven ride heights / rake the aero map is still trusted


def _fit_poly(v: np.ndarray, a: np.ndarray, w: np.ndarray, powers: list[int]) -> np.ndarray:
    X = np.column_stack([v ** p for p in powers])
    A = X.T @ (X * w[:, None]) + 1e-9 * np.eye(len(powers))
    return np.linalg.solve(A, X.T @ (w * a))


def _lateral(lat: np.ndarray, lift_typ: float) -> tuple[np.ndarray, float]:
    """Lateral grip limit against V_GRID, as μ·g·(1 + c·v²).

    Downforce only adds grip, so the limit never falls with speed: a lower top percentile
    at high speed is a track whose fast corners are taken flat out, not a car with less
    grip. So the curve is fitted to the running maximum of the bins (non-decreasing), and
    in the smooth physical form: a staircase of noisy bins can run along v²·κ for a few
    m/s and make a corner's speed jump with the smallest change. Too few bins for a slope:
    the class's typical downforce."""
    order = np.argsort(lat[:, 0])
    vb, ab, n = lat[order, 0], np.maximum.accumulate(lat[order, 2]), lat[order, 1]
    if np.ptp(vb) >= 15.0:
        a0, a2 = _fit_poly(vb ** 2, ab, n, [0, 1])
    else:
        a0, a2 = -1.0, 0.0
    if a0 <= 0 or a2 < 0:  # no usable slope: hold the top bin's grip with the class's downforce
        a0 = float(ab[-1] / (1.0 + lift_typ * vb[-1] ** 2))
        a2 = a0 * lift_typ
    return a0 + a2 * V_GRID ** 2, float(a0)


def envelope_car(envelope: list[tuple[str, float, int, float]], lift_typ: float = 6e-5,
                 drag_typ: float = 7e-4) -> Car | None:
    """``envelope``: (kind, v, samples, value m/s²) bins, from extract.py; one stint's or
    several stints' pooled. ``lift_typ`` / ``drag_typ``: the class's downforce and drag,
    for what a lap or two doesn't show: speeds above the measured ones, a braking curve
    from a single braking zone (shaped like the grip: both are tyres plus downforce), a
    drive curve from one or two straights (falling off towards top speed with the drag)."""
    by = {k: np.array([(v, n, a) for kind, v, n, a in envelope if kind == k]).reshape(-1, 3)
          for k in ("lat", "drive", "brake")}
    lat, drive, brake = by["lat"], by["drive"], by["brake"]
    if len(lat) < MIN_LAT_BINS or len(drive) < 1:
        return None
    v = V_GRID
    lat_curve, mu_g = _lateral(lat, lift_typ)
    # Drive: a smooth fit over the measured speeds; below them traction-limited (held),
    # above them extended along its end slope until it runs out (top speed).
    lo, hi = drive[:, 0].min(), drive[:, 0].max()
    if len(drive) >= 3:
        coef = _fit_poly(drive[:, 0], drive[:, 2], drive[:, 1], [0, 1, 2])
    else:
        a1 = float(np.average(drive[:, 2], weights=drive[:, 1]))
        v1 = float(np.average(drive[:, 0], weights=drive[:, 1]))
        coef = np.array([a1 + drag_typ * v1 * v1, 0.0, -drag_typ])
    f = lambda x: coef[0] + coef[1] * x + coef[2] * x * x  # noqa: E731
    end_slope = min(coef[1] + 2 * coef[2] * hi, -0.02)
    drive_curve = np.where(v < lo, f(lo), np.where(v > hi, f(hi) + end_slope * (v - hi), f(v)))
    # Braking: b0 + b2·v² (downforce and drag both grow with v²), b2 ≥ 0.
    if len(brake) >= 2:
        b0, b2 = _fit_poly(brake[:, 0] ** 2, brake[:, 2], brake[:, 1], [0, 1])
        if b2 < 0:
            b0, b2 = float(np.average(brake[:, 2], weights=brake[:, 1])), 0.0
        brake_curve = b0 + b2 * v ** 2
    elif len(brake) == 1:
        vb, _, ab = brake[0]
        brake_curve = ab * lat_curve / float(np.interp(vb, v, lat_curve))
        b0 = float(brake_curve[0])
    else:
        brake_curve = BRAKE_PER_LAT * lat_curve
        b0 = float(brake_curve[0])
    return Car(lat=np.maximum(lat_curve, 1.0), drive=drive_curve, brake=np.maximum(brake_curve, 1.0),
               mu=mu_g / G, mu_brake=b0 / G)


def pooled(envelopes: list[list[tuple[str, float, int, float]]]) -> list[tuple[str, float, int, float]]:
    """Several stints' envelope bins as one: per kind and speed bin, the sample-weighted
    mean. Engine, gearing and track are the same for a car's stints at one layout; what a
    setup changes is put back as drag and downforce deltas (``stint_car``)."""
    acc: dict[tuple[str, float], list[tuple[int, float]]] = {}
    for env in envelopes:
        for kind, v, n, a in env:
            acc.setdefault((kind, v), []).append((n, a))
    return [(kind, v, sum(n for n, _ in rows), float(np.average([a for _, a in rows], weights=[n for n, _ in rows])))
            for (kind, v), rows in sorted(acc.items())]


def layout_car(envelopes: list[list[tuple[str, float, int, float]]], lift_typ: float,
               drag_typ: float = 7e-4) -> Car | None:
    """The car at a layout from all its stints there (pooled envelope)."""
    return envelope_car(pooled(envelopes), lift_typ, drag_typ)


def shrink(values: dict[int, tuple[float, float]], prior_sd: float) -> dict[int, float]:
    """Per stint, its value against the stints' precision-weighted mean, shrunk towards 0
    by its noise (prior sd ``prior_sd``): a noisy stint barely moves from the pool."""
    if not values:
        return {}
    w = {k: 1.0 / max(se, 1e-12) ** 2 for k, (_, se) in values.items()}
    mean = sum(w[k] * v for k, (v, _) in values.items()) / sum(w.values())
    out = {}
    for k, (v, se) in values.items():
        gain = prior_sd ** 2 / (prior_sd ** 2 + se ** 2)
        out[k] = gain * (v - mean)
    return out


def lift_of(envelope: list[tuple[str, float, int, float]]) -> tuple[float, float] | None:
    """(c, se) from one stint's lateral envelope."""
    lat = [(v, n, a) for kind, v, n, a in envelope if kind == "lat"]
    if len(lat) < MIN_LAT_BINS or max(r[0] for r in lat) - min(r[0] for r in lat) < 15.0:
        return None
    v = np.array([r[0] for r in lat])
    w = np.array([r[1] for r in lat], dtype=float)
    a = np.array([r[2] for r in lat])
    X = np.column_stack([np.ones_like(v), v * v])
    W = w / w.mean()
    A = X.T @ (X * W[:, None])
    beta = np.linalg.solve(A, X.T @ (W * a))
    s2 = float(np.sum(W * (a - X @ beta) ** 2) / max(len(a) - 2, 1))
    cov = np.linalg.inv(A) * s2
    if beta[0] <= 0:
        return None
    return float(beta[1] / beta[0]), float(np.sqrt(max(cov[1, 1], 0.0)) / beta[0])


def lift_deltas(envelopes: dict[int, list[tuple[str, float, int, float]]], mu: float
                ) -> dict[int, tuple[float, float]]:
    """Per stint at one layout, its downforce term against the others (Δc, se), summing to
    zero: lateral limit per speed bin = the layout's curve + μ·g·Δc·v². Compared bin by
    bin like this, a fast corner the track has you take flat out reads the same for every
    stint and cancels, where an absolute fit of each stint reads it as less downforce.
    Bins are weighted by how close the pooled envelope is to the limit there; what bins
    below it still dilute makes the difference come out smaller than it is (towards zero,
    never the wrong way)."""
    rows = [(rid, v, n, a) for rid, env in envelopes.items() for kind, v, n, a in env if kind == "lat"]
    rids = sorted({r[0] for r in rows})
    bins = sorted({r[1] for r in rows})
    shared = [b for b in bins if len({r[0] for r in rows if r[1] == b}) >= 2]
    rows = [r for r in rows if r[1] in shared]
    # Weight each speed bin by how close the pooled envelope is to the limit there (its
    # running maximum): bins where the track has you below the limit carry little.
    pool = {b: float(np.average([r[3] for r in rows if r[1] == b], weights=[r[2] for r in rows if r[1] == b]))
            for b in shared}
    limit = dict(zip(shared, np.maximum.accumulate([pool[b] for b in shared])))
    at_limit = {b: float(np.clip((pool[b] / limit[b] - AT_LIMIT) / (1 - AT_LIMIT), 0.0, 1.0)) ** 2 for b in shared}
    rows = [r for r in rows if at_limit[r[1]] > 0]
    shared = [b for b in shared if at_limit[b] > 0]
    if len(rids) < 2 or len({r[0] for r in rows}) < 2 or len(shared) < 3:
        return {}
    free = [rid for rid in rids if any(r[0] == rid for r in rows)]
    last = free[-1]
    free = free[:-1]
    col_r = {rid: len(shared) + i for i, rid in enumerate(free)}
    col_b = {b: i for i, b in enumerate(shared)}
    X = np.zeros((len(rows), len(shared) + len(free)))
    y, w = np.zeros(len(rows)), np.zeros(len(rows))
    for i, (rid, v, n, a) in enumerate(rows):
        X[i, col_b[v]] = 1.0
        g = mu * G * v * v
        if rid in col_r:
            X[i, col_r[rid]] = g
        else:
            X[i, len(shared):] = -g
        y[i], w[i] = a, n * at_limit[v]
    W = w / w.mean()
    A = X.T @ (X * W[:, None]) + 1e-9 * np.eye(X.shape[1])
    beta = np.linalg.solve(A, X.T @ (W * y))
    s2 = float(np.sum(W * (y - X @ beta) ** 2) / max(len(y) - X.shape[1], 1))
    cov = np.linalg.inv(A) * max(s2, 0.05 ** 2)
    idx = list(col_r.values())
    out = {rid: (float(beta[j]), float(np.sqrt(max(cov[j, j], 0.0)))) for rid, j in col_r.items()}
    out[last] = (-float(beta[idx].sum()), float(np.sqrt(max(cov[np.ix_(idx, idx)].sum(), 0.0))))
    return out


def drag_deltas(rows: list[tuple[int, int, int, float, float]]) -> dict[int, tuple[float, float]]:
    """``rows``: (recording id, distance bin, samples, v, a_x) of one car's stints at one
    layout → per stint (Δk, se), summing to zero over the stints."""
    rows = [r for r in rows if r[3] >= 30.0 and np.isfinite(r[4])]
    rids = sorted({r[0] for r in rows})
    if len(rids) < 2 or len(rows) < 8:
        return {}
    bins = sorted({r[1] for r in rows})
    shared = [b for b in bins if len({r[0] for r in rows if r[1] == b}) >= 2]
    powers = [0, 1, 2, 3]
    scale = float(max(r[3] for r in rows))
    # Δk sums to zero (the shared curve holds the layout's mean drag): the last stint's
    # Δk is minus the others', so only the others get a column.
    free = rids[:-1]
    col_r = {rid: len(powers) + i for i, rid in enumerate(free)}
    col_b = {b: len(powers) + len(free) + i for i, b in enumerate(shared)}
    X = np.zeros((len(rows), len(powers) + len(free) + len(shared)))
    y, w = np.zeros(len(rows)), np.zeros(len(rows))
    for i, (rid, b, n, v, a) in enumerate(rows):
        X[i, :len(powers)] = [(v / scale) ** p for p in powers]
        if rid in col_r:
            X[i, col_r[rid]] = -v * v
        else:
            X[i, len(powers):len(powers) + len(free)] = v * v
        if b in col_b:
            X[i, col_b[b]] = 1.0
        y[i], w[i] = a, n
    prec = np.full(X.shape[1], 1e-8)
    prec[len(powers):len(powers) + len(free)] = 1.0 / DRAG_SD ** 2
    prec[len(powers) + len(free):] = 1.0 / SLOPE_SD ** 2
    sigma2 = 1.0
    for _ in range(2):
        W = w / sigma2
        A = X.T @ (X * W[:, None]) + np.diag(prec)
        beta = np.linalg.solve(A, X.T @ (W * y))
        r = y - X @ beta
        sigma2 = max(float(np.sum(w * r * r)) / max(len(y) - X.shape[1], 1), 0.05 ** 2)
    cov = np.linalg.inv(A)
    idx = list(col_r.values())
    out = {rid: (float(beta[j]), float(np.sqrt(max(cov[j, j], 0.0)))) for rid, j in col_r.items()}
    out[rids[-1]] = (-float(beta[idx].sum()), float(np.sqrt(max(cov[np.ix_(idx, idx)].sum(), 0.0))))
    return out


# -- aero map -------------------------------------------------------------------------------
@dataclass
class Platform:
    """Aero platform state: rear wing as a share of its range, ride heights at speed (mm)."""

    wing: float
    rh_front: float
    rh_rear: float

    @property
    def rake(self) -> float:
        return self.rh_rear - self.rh_front

    def x(self) -> np.ndarray:
        return np.array([self.wing, self.rh_front, self.rake])


@dataclass
class Stint:
    """One stint's aero evidence at a layout."""

    layout: str
    platform: Platform
    drag: tuple[float, float] | None = None  # Δk against the layout's stints, se
    lift: tuple[float, float] | None = None  # Δc against the layout's stints, se
    balance: tuple[float, float] | None = None  # deg (high − low speed understeer), se


@dataclass
class Slopes:
    """Bayesian slopes (no intercept) of one quantity against FEATURES."""

    mean: np.ndarray
    cov: np.ndarray

    @classmethod
    def fit(cls, X: np.ndarray, y: np.ndarray, var: np.ndarray, prior_mean: np.ndarray,
            prior_sd: np.ndarray) -> "Slopes":
        P0 = np.diag(1.0 / prior_sd ** 2)
        A = P0 + X.T @ (X / var[:, None])
        cov = np.linalg.inv(A)
        return cls(cov @ (P0 @ prior_mean + X.T @ (y / var)), cov)

    def effect(self, dx: np.ndarray) -> tuple[float, float]:
        return float(dx @ self.mean), float(np.sqrt(max(dx @ self.cov @ dx, 0.0)))

    def to_json(self) -> dict:
        return {"mean": self.mean.tolist(), "cov": self.cov.tolist()}

    @classmethod
    def from_json(cls, d: dict) -> "Slopes":
        return cls(np.array(d["mean"]), np.array(d["cov"]))


def priors(k: dict) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Per quantity: prior slopes (wing range, mm front ride height, mm rake) and sds.
    The knowledge file gives drag and downforce changes as shares of the class's typical
    k and c; aero_prior_sd × each is its sd, plus a floor so a 0 prior can still move."""
    sd = float(k["aero_prior_sd"])
    out = {}
    for name, typ in (("drag", float(k["drag_k_typ"])), ("lift", float(k["lift_c_typ"])), ("balance", 1.0)):
        m = np.array([k[f"aero_{name}_per_wing"], k[f"aero_{name}_per_mm_rh"], k[f"aero_{name}_per_mm_rake"]]) * typ
        floor = np.array([0.05, 0.005, 0.005]) * typ if name != "balance" else np.array([0.5, 0.05, 0.05])
        out[name] = (m, sd * np.abs(m) + floor)
    return out


@dataclass
class AeroMap:
    """Per car model: drag Δk, downforce c and aero balance against the platform."""

    drag: Slopes
    lift: Slopes
    balance: Slopes
    n: dict[str, int] = field(default_factory=dict)
    span: dict[str, list[float]] = field(default_factory=dict)  # feature → [min, max] driven

    @classmethod
    def fit(cls, stints: list[Stint], knowledge: dict) -> "AeroMap":
        pri = priors(knowledge)
        out, n = {}, {}
        for name in ("drag", "lift", "balance"):
            rows = [(s.layout, s.platform.x(), getattr(s, name)) for s in stints if getattr(s, name) is not None]
            X, y, var = [], [], []
            for layout in sorted({r[0] for r in rows}):
                mine = [r for r in rows if r[0] == layout]
                if len(mine) < 2:
                    continue  # one stint at a layout says nothing about a change
                xs = np.array([r[1] for r in mine])
                ys = np.array([r[2][0] for r in mine])
                X += list(xs - xs.mean(0))
                y += list(ys - ys.mean())
                var += [max(r[2][1], 1e-12) ** 2 for r in mine]
            X = np.array(X).reshape(len(X), len(FEATURES))
            fit = Slopes.fit(X, np.array(y), np.array(var), *pri[name])
            # Known directions are kept: a slope the data puts on the wrong side of its
            # prior's direction (confounded stints, a narrow range) counts as zero.
            prior_mean = pri[name][0]
            wrong = (prior_mean != 0) & (np.sign(fit.mean) != np.sign(prior_mean))
            fit.mean = np.where(wrong, 0.0, fit.mean)
            out[name] = fit
            n[name] = len(y)
        xs = np.array([s.platform.x() for s in stints]).reshape(len(stints), len(FEATURES))
        span = {f: [float(xs[:, i].min()), float(xs[:, i].max())] for i, f in enumerate(FEATURES)} if len(stints) else {}
        return cls(out["drag"], out["lift"], out["balance"], n, span)

    def within(self, platform: "Platform", wing_step: float, mm: float = SPAN_MM) -> bool:
        """The platform is inside what was driven (plus a click of wing / ``mm``), where
        the map is evidence and not a guess."""
        if not self.span:
            return True
        x = platform.x()
        margin = (wing_step, mm, mm)
        return all(self.span[f][0] - m <= v <= self.span[f][1] + m for f, v, m in zip(FEATURES, x, margin))

    def car(self, base: Car, base_platform: Platform, platform: Platform) -> Car:
        """``base`` (measured on ``base_platform``) moved to ``platform``."""
        dx = platform.x() - base_platform.x()
        return base.perturb(d_drag=self.drag.effect(dx)[0], d_lift=self.lift.effect(dx)[0])

    def to_json(self) -> dict:
        return {"drag": self.drag.to_json(), "lift": self.lift.to_json(), "balance": self.balance.to_json(),
                "n": self.n, "span": self.span}

    @classmethod
    def from_json(cls, d: dict) -> "AeroMap":
        return cls(Slopes.from_json(d["drag"]), Slopes.from_json(d["lift"]), Slopes.from_json(d["balance"]),
                   d.get("n", {}), d.get("span", {}))
