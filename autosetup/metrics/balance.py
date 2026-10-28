"""Balance per corner pass: how the car rotates against what the steering asks for.

The kinematic reference is the yaw rate a neutral car would have at this speed and
road-wheel angle, r_ref = v·δ/L. The balance metric is the understeer angle

    ua = s · (δ − L·r/v) = s · L/v · (r_ref − r)    (deg, + understeer)

i.e. the yaw-rate error scaled to steering degrees, so it reads the same at any speed and
keeps the per-class calibrations in classes.toml. s is the way the car is turning (the
sign of the yaw rate), not of the steering: with the wheel held against a slide the car
is oversteering, and signing by the steering would read that as understeer. ``yaw_err`` is the same error as a
share of r_ref. With a DAM log, ``slip_fr`` adds front-minus-rear slip angle (deg).

Each pass gives one observation per phase, carrying its own noise (variance of the
median) and the driver inputs that move balance besides the setup: steering rate and
throttle application rate. ``normalise`` takes those out across passes of the same corner
and phase; ``cells`` pools passes into one estimate per phase × speed band, weighted by
count and variance (no gate on stints or laps: one pass is weak evidence, many are strong).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from autosetup.metrics.corners import BANDS, PHASES, Pass
from autosetup.metrics.signals import GRID_HZ, Signals

MIN_PHASE_SAMPLES = GRID_HZ // 5  # 0.2 s
MIN_REF_YAW = 0.05  # rad/s; below this r_ref is too small to divide by
TURN_IN_S = 0.4
NOISE_FLOOR = 0.02 ** 2  # deg², per pass
CORR_S = 0.2  # samples closer than this move together: one independent sample per CORR_S
TAU2_MIN = 0.1 ** 2  # deg²: passes of a cell differ at least this much (corners, laps), however few
RIDGE = 10.0  # passes' worth of evidence before a driver-input slope is trusted
MIN_NORMALISE = 8


@dataclass(frozen=True)
class PhaseObs:
    corner: int
    phase: str
    band: str
    n: int  # samples
    v_min: float  # m/s, the pass's apex speed
    v_mean: float
    a_lat: float  # g, mean |lateral|
    ua: float  # deg, median understeer angle (+ understeer)
    ua_var: float  # deg², variance of that median
    yaw_err: float  # share of r_ref (+ understeer)
    steer_rate: float  # deg/s of steering wheel, median |rate|
    throttle_rate: float | None  # 1/s, median while opening; None if it never opened
    countersteer_s: float
    turnin_gain: float | None = None  # entry: |r| / |r_ref| over the first TURN_IN_S of turn-in
    slip_fr: float | None = None  # deg, |front slip| − |rear slip| (DAM)
    kerb: bool = False
    ua_adj: float | None = None  # ua after normalise()

    @property
    def value(self) -> float:
        return self.ua if self.ua_adj is None else self.ua_adj


def sample_metrics(sig: Signals, geometry: tuple[float, float, float]) -> dict[str, np.ndarray]:
    """Per-sample understeer angle (deg), yaw-rate error share, steering-wheel angle (deg)
    and its rate (deg/s), the reference yaw rate and countersteer."""
    half_lock, ratio, wheelbase = geometry
    sw_deg = sig.steer * half_lock
    delta = np.radians(sw_deg / ratio)
    v = np.maximum(sig.speed, 1.0)
    r_ref = v * delta / wheelbase
    turning = np.abs(sig.yaw_rate) > MIN_REF_YAW / 2
    sign = np.where(turning, np.sign(sig.yaw_rate), np.sign(delta))
    ua = np.degrees(sign * (delta - wheelbase * sig.yaw_rate / v))
    with np.errstate(divide="ignore", invalid="ignore"):
        yaw_err = np.where(np.abs(r_ref) > MIN_REF_YAW, sign * (r_ref - sig.yaw_rate) / np.abs(r_ref), np.nan)
    dt = 1.0 / GRID_HZ
    steer_rate = np.gradient(sw_deg, dt) if len(sw_deg) > 1 else np.zeros_like(sw_deg)
    counter = ((np.sign(sig.steer) != np.sign(sig.yaw_rate)) & (np.abs(sw_deg) > 5.0)
               & (np.abs(sig.yaw_rate) > 0.1))
    return {"ua": ua, "yaw_err": yaw_err, "sw_deg": sw_deg, "steer_rate": steer_rate, "r_ref": r_ref,
            "counter": counter}


def _median_var(x: np.ndarray) -> float:
    """Variance of a median: (1.4826·MAD)² · π/2 / n_eff, floored. Neighbouring samples
    of a smooth signal aren't independent: n_eff counts one per CORR_S."""
    mad = float(np.median(np.abs(x - np.median(x)))) * 1.4826
    n_eff = max(1.0, len(x) / (CORR_S * GRID_HZ))
    return max(mad ** 2 * np.pi / 2 / n_eff, NOISE_FLOOR)


def observe(sig: Signals, per: dict[str, np.ndarray], p: Pass, kerb: np.ndarray | None = None,
            slip_fr: np.ndarray | None = None) -> list[PhaseObs]:
    """One observation per phase of a pass that has at least MIN_PHASE_SAMPLES of it.
    ``per``: sample_metrics of the same Signals; ``kerb``: per-sample kerb flag."""
    out = []
    dt = 1.0 / GRID_HZ
    throttle_rate = np.gradient(sig.throttle, dt) if len(sig.throttle) > 1 else np.zeros_like(sig.throttle)
    for ph in PHASES:
        sel = p.idx[p.phase == ph]
        sel = sel[np.isfinite(per["ua"][sel])]
        if len(sel) < MIN_PHASE_SAMPLES:
            continue
        ua = per["ua"][sel]
        opening = throttle_rate[sel][throttle_rate[sel] > 0.05]
        ye = per["yaw_err"][sel]
        ye = ye[np.isfinite(ye)]
        gain = None
        if ph == "entry":
            gain = _turnin_gain(sig, per, p)
        sf = None
        if slip_fr is not None:
            s = slip_fr[sel]
            s = s[np.isfinite(s)]
            sf = float(np.median(s)) if len(s) >= MIN_PHASE_SAMPLES else None
        out.append(PhaseObs(
            corner=p.corner.id, phase=ph, band=p.band, n=len(sel), v_min=p.v_min,
            v_mean=float(np.mean(sig.speed[sel])), a_lat=float(np.mean(np.abs(sig.a_lat[sel]))),
            ua=float(np.median(ua)), ua_var=_median_var(ua),
            yaw_err=float(np.median(ye)) if len(ye) else float("nan"),
            steer_rate=float(np.median(np.abs(per["steer_rate"][sel]))),
            throttle_rate=float(np.median(opening)) if len(opening) >= 3 else None,
            countersteer_s=float(per["counter"][sel].sum() * dt),
            turnin_gain=gain, slip_fr=sf,
            kerb=bool(kerb is not None and kerb[sel].any())))
    return out


def _turnin_gain(sig: Signals, per: dict[str, np.ndarray], p: Pass) -> float | None:
    """|yaw rate| / |r_ref| over the first TURN_IN_S after the steering passes 30 % of the
    pass's peak: how much of what the wheel asks for the car delivers on turn-in."""
    sw = np.abs(per["sw_deg"][p.idx])
    if not len(sw) or sw.max() < 10.0:
        return None
    start = int(np.argmax(sw >= 0.3 * sw.max()))
    sel = p.idx[start:start + int(TURN_IN_S * GRID_HZ)]
    rr = np.abs(per["r_ref"][sel])
    ok = rr > MIN_REF_YAW
    if ok.sum() < 5:
        return None
    return float(np.median(np.abs(sig.yaw_rate[sel][ok]) / rr[ok]))


def normalise(obs: list[PhaseObs]) -> list[PhaseObs]:
    """Take driver inputs out of the balance: within each corner × phase, passes where you
    turned in faster or opened the throttle harder than usual for that corner read more
    understeer / oversteer for reasons that aren't the setup. One slope per input and
    phase, pooled over corners and shrunk towards zero (RIDGE); each pass is moved to its
    corner's usual inputs. Fewer than MIN_NORMALISE passes in a phase: left as measured."""
    out = list(obs)
    for ph in PHASES:
        rows = [i for i, o in enumerate(obs) if o.phase == ph]
        if len(rows) < MIN_NORMALISE:
            continue
        groups: dict[int, list[int]] = {}
        for i in rows:
            groups.setdefault(obs[i].corner, []).append(i)
        X, y, w, idx = [], [], [], []
        for members in groups.values():
            if len(members) < 2:
                continue
            sr = np.array([obs[i].steer_rate for i in members])
            tr = np.array([obs[i].throttle_rate if obs[i].throttle_rate is not None else np.nan for i in members])
            tr = np.where(np.isfinite(tr), tr, np.nanmedian(tr) if np.isfinite(tr).any() else 0.0)
            ua = np.array([obs[i].ua for i in members])
            X.append(np.column_stack([sr - np.median(sr), tr - np.median(tr)]))
            y.append(ua - np.average(ua, weights=[1 / obs[i].ua_var for i in members]))
            w.append([1 / obs[i].ua_var for i in members])
            idx += members
        if len(idx) < MIN_NORMALISE:
            continue
        X, y, w = np.vstack(X), np.concatenate(y), np.concatenate(w)
        scale = np.maximum(X.std(0), 1e-9)
        Xs = X / scale
        W = w / w.mean()
        A = Xs.T @ (Xs * W[:, None]) + RIDGE * np.eye(2)
        beta = np.linalg.solve(A, Xs.T @ (W * y)) / scale
        for row, i in enumerate(idx):
            out[i] = replace(obs[i], ua_adj=float(obs[i].ua - X[row] @ beta))
    return out


@dataclass(frozen=True)
class Cell:
    mean: float  # deg
    se: float  # standard error, deg
    passes: int


def cells(obs: list[PhaseObs], include_kerbs: bool = True) -> dict[tuple[str, str], Cell]:
    """One estimate per (phase, band): a precision-weighted mean of the passes, with the
    spread between passes (corners differ, laps differ) added to each pass's own noise
    (DerSimonian–Laird, at least TAU2_MIN), so one long pass can't outweigh many short
    ones and two passes that happen to agree aren't taken as certain."""
    out = {}
    for ph in PHASES:
        for band in BANDS:
            sel = [o for o in obs if o.phase == ph and o.band == band and (include_kerbs or not o.kerb)]
            if not sel:
                continue
            y = np.array([o.value for o in sel])
            v = np.array([o.ua_var for o in sel])
            w = 1 / v
            mean_fixed = float(np.sum(w * y) / np.sum(w))
            tau2 = 0.0
            if len(sel) > 1:
                q = float(np.sum(w * (y - mean_fixed) ** 2))
                c = float(np.sum(w) - np.sum(w ** 2) / np.sum(w))
                tau2 = max(0.0, (q - (len(sel) - 1)) / c) if c > 0 else 0.0
            tau2 = max(tau2, TAU2_MIN)
            w2 = 1 / (v + tau2)
            mean = float(np.sum(w2 * y) / np.sum(w2))
            out[(ph, band)] = Cell(mean, float(np.sqrt(1 / np.sum(w2))), len(sel))
    return out


def corner_mix(obs: list[PhaseObs]) -> dict[str, float]:
    """Share of cornering time per speed band at this layout (weights the bands)."""
    t = {b: sum(o.n for o in obs if o.band == b) for b in BANDS}
    total = sum(t.values())
    return {b: (t[b] / total if total else 0.5) for b in BANDS}
