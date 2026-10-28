"""Per-lap metrics: pace, handling balance, tyres, platform and driving style.

Every metric is a plain float keyed by name so the store can keep them in one long table
and later phases can add metrics without schema changes. Per-wheel metrics end in
``_fl/_fr/_rl/_rr``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.telemetry import Lap, Session
from autosetup.metrics.signals import GRID_HZ, WHEELBASE, Signals, load_signals, smooth

METRICS_VERSION = 9  # 2: Lap Dist unwrapped; 3: countersteer per phase; 4: steering unit, off_major; 5: third spring;
#                      6: understeer in mid-speed corners; 7: per corner speed × phase; 8: corner passes, suspension, kerbs;
#                      9: lap model data, the per speed × phase cells left to the corner passes
PACKER_FLAT = 0.25  # past contact, deflection gains under this share of the free spring's rate
WHEELS = ("fl", "fr", "rl", "rr")
SECTOR_M = 100.0  # minisector length; shorter ones bias the theoretical best low (min of noise)

# Thresholds (documented here so they're easy to tune against real data)
OFF_TRACK_M = 1.2  # beyond the edge line by this much = all four wheels off
OFF_TRACK_S = 0.3
# A real off (spin, gravel trip) rather than using the track: these laps are left out of
# setup comparisons; short track-limit excursions are kept.
OFF_MAJOR_M = 3.0
OFF_MAJOR_S = 1.0
OFF_SURFACES = (2, 3, 4)  # grass, dirt, gravel (rF2 surface codes); 5 = kerb
CORNER_G = 0.4  # |a_lat| above this counts as cornering
MIN_SPEED = 15.0  # m/s; below this, balance metrics are noise
SLOW_MS, FAST_MS = 30.0, 45.0  # the Setup table's slow (< 108 km/h) and fast (> 162 km/h) corners
REVERSAL_GAP_DEG = 2.0  # steering-wheel reversal hysteresis


@dataclass
class LapResult:
    seq: int
    metrics: dict[str, float]
    sectors: np.ndarray = field(default_factory=lambda: np.zeros(0))  # minisector times, s


def compute_recording(s: Session, laps: list[Lap] | None = None, setup: CarSetup | None = None,
                      sig: Signals | None = None) -> list[LapResult]:
    laps = laps if laps is not None else s.laps()
    if not laps:
        return []
    sig = sig if sig is not None else load_signals(s)
    setup = setup or CarSetup.from_json(s.setup_json)
    geometry = steering_geometry(setup, s.car_class)
    lap_len = float(np.nanmax(sig.dist)) if np.isfinite(sig.dist).any() else 0.0
    contact = _third_contacts(sig, setup)
    out = []
    for seq, lap in enumerate(laps):
        m = (sig.t >= lap.t_start) & (sig.t < lap.t_end)
        if m.sum() < GRID_HZ:  # under a second of data
            continue
        sl = _slice(sig, m)
        metrics = {"lap_time": lap.time, "complete": float(lap.complete), "in_pits": float(lap.in_pits)}
        metrics |= _pace(sl)
        metrics |= _handling(sl, geometry)
        metrics |= _tyres(sl)
        metrics |= _platform(sl)
        metrics |= _third_spring(sl, contact)
        metrics |= _driver(sl)
        sectors = _minisectors(sl, lap_len) if lap.complete else np.zeros(0)
        out.append(LapResult(seq, {k: float(v) for k, v in metrics.items() if v is not None and np.isfinite(v)}, sectors))
    return out


# -- helpers -----------------------------------------------------------------------
def _slice(sig: Signals, m: np.ndarray) -> Signals:
    return Signals(**{k: v[m] for k, v in vars(sig).items()})


def steering_geometry(setup: CarSetup, car_class: str) -> tuple[float, float, float]:
    """(steering-wheel degrees at full lock one way, steering ratio, wheelbase m)."""
    lock_total, ratio = 540.0, 15.0
    p = setup.params.get("VM_STEER_LOCK")
    if p and p.number:
        lock_total = p.number
    if setup.steering_ratio:
        ratio = setup.steering_ratio
    wheelbase = next((v for k, v in WHEELBASE.items() if car_class.lower().startswith(k.lower())), 2.8)
    return lock_total / 2.0, ratio, wheelbase


def _runs(mask: np.ndarray, min_len: int = 1) -> int:
    """Number of True runs at least ``min_len`` samples long."""
    if not mask.any():
        return 0
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return int(np.sum(ends - starts >= min_len))


def _rising(x: np.ndarray) -> int:
    return int(np.sum(np.diff((x > 0.5).astype(np.int8)) == 1))


def _finite(x: np.ndarray) -> bool:
    return bool(len(x)) and bool(np.isfinite(x).any())


def _nanmean(x: np.ndarray) -> float | None:
    return float(np.nanmean(x)) if _finite(x) else None


def _nanmax(x: np.ndarray) -> float | None:
    return float(np.nanmax(x)) if _finite(x) else None


def _nanmin(x: np.ndarray) -> float | None:
    return float(np.nanmin(x)) if _finite(x) else None


def _nanpct(x: np.ndarray, q: float) -> float | None:
    return float(np.nanpercentile(x, q)) if _finite(x) else None


def _per_wheel(name: str, values: np.ndarray) -> dict[str, float]:
    return {f"{name}_{w}": float(v) for w, v in zip(WHEELS, values) if np.isfinite(v)}


# -- metric groups ------------------------------------------------------------------
def _pace(s: Signals) -> dict[str, float]:
    dt = 1.0 / GRID_HZ
    off = s.off_line > OFF_TRACK_M
    off_surface = np.isin(s.surface, OFF_SURFACES).sum(axis=1) >= 3
    off_runs = _runs(off | off_surface, int(OFF_TRACK_S * GRID_HZ))
    major = (np.nanmax(s.off_line, initial=-np.inf) > OFF_MAJOR_M
             or _runs(off_surface, int(OFF_MAJOR_S * GRID_HZ)) > 0)
    return {
        "off_track": float(off_runs > 0),
        "off_track_events": off_runs,
        "off_major": float(major),
        "off_line_max": _nanmax(s.off_line),
        "kerb_s": float(np.sum((s.surface == 5).any(axis=1)) * dt),
        "fuel_start": float(s.fuel[0]) if np.isfinite(s.fuel[0]) else None,
        "fuel_used": float(s.fuel[0] - s.fuel[-1]) if np.isfinite(s.fuel[[0, -1]]).all() else None,
        "track_temp": _nanmean(s.track_temp),
        "ambient_temp": _nanmean(s.ambient_temp),
        "speed_max": _nanmax(s.speed * 3.6),
        "speed_min": _nanmin(s.speed * 3.6),
    }


def _handling(s: Signals, geometry: tuple[float, float, float]) -> dict[str, float]:
    half_lock, ratio, wheelbase = geometry
    sw_deg = s.steer * half_lock  # steering-wheel angle
    delta = np.radians(sw_deg / ratio)  # road-wheel angle
    v = np.maximum(s.speed, 1.0)
    curvature = s.yaw_rate / v
    # Understeer angle: steering beyond what the path's curvature needs (Ackermann),
    # signed so positive = understeer whichever way the corner goes.
    ua = np.degrees(np.sign(delta) * (delta - wheelbase * curvature))
    cornering = (np.abs(s.a_lat) > CORNER_G) & (s.speed > MIN_SPEED)
    entry = cornering & (s.brake > 0.05)
    exit_ = cornering & (s.brake <= 0.05) & (s.throttle >= 0.5)
    mid = cornering & (s.brake <= 0.05) & (s.throttle < 0.5)
    slow = cornering & (s.speed < SLOW_MS)
    fast = cornering & (s.speed > FAST_MS)

    def med(mask):
        return float(np.median(ua[mask])) if mask.sum() >= GRID_HZ // 5 else None

    # Countersteer: wheel held against the direction the car is rotating (yaw is
    # lag-aligned in signals.py, so steering transitions don't count).
    counter = (np.sign(s.steer) != np.sign(s.yaw_rate)) & (np.abs(sw_deg) > 5.0)         & (np.abs(s.yaw_rate) > 0.1) & (s.speed > MIN_SPEED)
    dt = 1.0 / GRID_HZ
    # Same split as the balance phases, by pedals only (a slide often drops lateral g).
    on_brake = s.brake > 0.05
    on_power = ~on_brake & (s.throttle >= 0.5)
    min_run = GRID_HZ // 5
    # Per corner phase and speed band from the corner passes: metrics/balance.py.
    return {
        "cs_entry": _runs(counter & on_brake, min_run),
        "cs_mid": _runs(counter & ~on_brake & ~on_power, min_run),
        "cs_exit": _runs(counter & on_power, min_run),
        "us_entry": med(entry),
        "us_mid": med(mid),
        "us_exit": med(exit_),
        "us_slow": med(slow),
        "us_fast": med(fast),
        "countersteer_events": _runs(counter, GRID_HZ // 5),
        "steer_reversals_per_min": _reversals(sw_deg, s.speed) / (len(s.t) * dt / 60.0),
        "tc_events": _rising(s.tc),
        "tc_s": float(np.sum(s.tc > 0.5) * dt),
        "abs_events": _rising(s.abs),
        "abs_s": float(np.sum(s.abs > 0.5) * dt),
        "lat_g_p98": _nanpct(np.abs(s.a_lat), 98),
        "brake_g_p98": _nanpct(-s.a_lon, 98),
    }


def _reversals(sw_deg: np.ndarray, speed: np.ndarray) -> int:
    """Steering reversals larger than REVERSAL_GAP_DEG: hysteresis on a 10 Hz-smoothed
    steering-wheel trace, counted only while moving."""
    x = smooth(sw_deg, GRID_HZ // 10)
    if not len(x):
        return 0
    count, direction, ref = 0, 0, x[0]
    for xi, vi in zip(x, speed):
        if vi < MIN_SPEED:
            continue
        if direction == 0:
            if abs(xi - ref) > REVERSAL_GAP_DEG:
                direction, ref = (1 if xi > ref else -1), xi
        elif direction * (xi - ref) > 0:
            ref = xi  # still moving the same way: extend the extreme
        elif abs(xi - ref) > REVERSAL_GAP_DEG:
            count += 1
            direction, ref = -direction, xi
    return count


def _tyres(s: Signals) -> dict[str, float]:
    out = {}
    # Wear and brake temperatures don't depend on the car moving.
    out |= _per_wheel("wear", s.tyre_wear[0] - s.tyre_wear[-1])
    if np.isfinite(s.brake_temp).any():
        out |= _per_wheel("brake_temp_max", np.nanmax(s.brake_temp, axis=0))
    if np.isfinite(s.overheating).any():
        out["overheating_s"] = float(np.nansum(s.overheating > 0.5) / GRID_HZ)
    moving = s.speed > 20.0
    if moving.sum() < GRID_HZ or not np.isfinite(s.tyre_centre[moving]).any():
        return out
    inner = np.nanmean(s.tyre_inner[moving], axis=0)
    centre = np.nanmean(s.tyre_centre[moving], axis=0)
    outer = np.nanmean(s.tyre_outer[moving], axis=0)
    out |= _per_wheel("t_inner", inner)
    out |= _per_wheel("t_centre", centre)
    out |= _per_wheel("t_outer", outer)
    out |= _per_wheel("t_spread", inner - outer)  # camber indicator
    out |= _per_wheel("t_crown", centre - (inner + outer) / 2)  # pressure indicator
    out |= _per_wheel("p_hot", np.nanmean(s.tyre_pressure[moving], axis=0))
    out |= _per_wheel("t_carcass", np.nanmean(s.tyre_carcass[moving], axis=0))
    return out


def _platform(s: Signals) -> dict[str, float]:
    if not np.isfinite(s.ride_height).any():
        return {}
    rh = s.ride_height * 1000.0  # mm
    fast = s.speed > 40.0
    out = _per_wheel("rh_min", np.nanpercentile(rh, 1, axis=0))
    out["bottoming_s"] = float((rh[:, :2] < 1.0).any(axis=1).sum() / GRID_HZ)
    if fast.sum() > GRID_HZ:
        out["rake_fast"] = float(np.nanmean(rh[fast, 2:]) - np.nanmean(rh[fast, :2]))
    return out


def _third_contacts(s: Signals, setup: CarSetup) -> dict[str, float | None]:
    """Where each axle's third spring meets its packers (mm), from the whole recording.

    Only for axles with an adjustable third element (GT3s log the channel with unrelated
    values). Aero load grows with speed², and on the free third spring the deflection
    grows with it. LMU's packers are a much stiffer rate, not a wall, so contact shows as
    a knee: past it the deflection hardly grows with speed. All laps are pooled, pit exit
    included: on a fast track a racing lap may never be slow enough to leave the packers,
    so a single lap can't show the knee."""
    out: dict[str, float | None] = {}
    kmh = s.speed * 3.6
    for i, (axle, name) in enumerate((("f", "FRONT"), ("r", "REAR"))):
        if not any(k.startswith(f"VM_{name}_3RD_") and p.adjustable for k, p in setup.params.items()):
            continue
        x = s.third[:, i] * 1000.0
        ok = np.isfinite(x) & np.isfinite(kmh)
        if ok.sum() < GRID_HZ or np.ptp(x[ok]) < 1.0:  # no data, or it never moves
            continue
        out[axle] = _packer_contact(x[ok], kmh[ok])
    return out


def _third_spring(s: Signals, contact: dict[str, float | None]) -> dict[str, float]:
    """Per axle with a third spring: travel used (98th percentile, mm), where the packers
    start (mm) and the share of the lap spent on them."""
    out = {}
    for i, axle in enumerate("fr"):
        if axle not in contact:
            continue
        x = s.third[:, i] * 1000.0
        x = x[np.isfinite(x)]
        if len(x) < GRID_HZ:
            continue
        out[f"third_travel_{axle}"] = float(np.percentile(x, 98))
        c = contact[axle]
        out[f"third_packers_{axle}"] = float(np.mean(x >= c)) if c is not None else 0.0
        if c is not None:
            out[f"third_contact_{axle}"] = c
    return out


def _packer_contact(x: np.ndarray, kmh: np.ndarray) -> float | None:
    """Deflection (mm) where the packers start, or None if the third spring never
    reaches them. Median deflection per 20 km/h bin against speed² is fitted with two
    straight lines joined at a knee (least squares over every knee position): contact
    when the second line gains under PACKER_FLAT of the first one's rate."""
    centres, meds = [], []
    for lo in np.arange(40.0, float(np.nanmax(kmh)), 20.0):
        m = (kmh >= lo) & (kmh < lo + 20.0)
        if m.sum() >= 0.3 * GRID_HZ:
            centres.append(float(np.mean(kmh[m])))
            meds.append(float(np.median(x[m])))
    if len(meds) < 5:
        return None
    load, y = (np.array(centres) / 100.0) ** 2, np.array(meds)
    best = None
    # Knee anywhere from the second bin to the third-last (two bins on each side).
    for k in np.linspace(load[1], load[-3], 200):
        A = np.column_stack([np.ones_like(load), np.minimum(load, k), np.maximum(load - k, 0.0)])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        sse = float(((A @ coef - y) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, k, coef)
    _, k, (a0, free, packed) = best
    if free <= 0 or packed >= PACKER_FLAT * free:
        return None
    return float(a0 + free * k)


def _driver(s: Signals) -> dict[str, float]:
    if not np.isfinite(s.speed).any():
        return {}
    moving = s.speed > MIN_SPEED
    braking = moving & (s.brake > 0.05)
    n = max(moving.sum(), 1)
    out = {
        "full_throttle_frac": float(np.sum(moving & (s.throttle > 0.98)) / n),
        "coast_frac": float(np.sum(moving & (s.throttle < 0.05) & (s.brake < 0.05)) / n),
        "brake_peak": _nanpct(s.brake, 99),
        "trail_brake_frac": float(np.sum(braking & (np.abs(s.a_lat) > CORNER_G)) / max(braking.sum(), 1)),
    }
    rate = np.gradient(s.throttle) * GRID_HZ
    applying = moving & (s.throttle > 0.2) & (s.throttle < 0.8) & (rate > 0.05)
    if applying.sum() > 5:
        out["throttle_apply_rate"] = float(np.median(rate[applying]))
    return out


def _minisectors(s: Signals, lap_len: float) -> np.ndarray:
    """Time spent in each SECTOR_M-long piece of the lap (needs a complete lap)."""
    if lap_len <= SECTOR_M or not np.isfinite(s.dist).any():
        return np.zeros(0)
    d = np.nan_to_num(s.dist, nan=0.0).copy()
    # The distance wrap can trail the Lap event by a sample or two: the lap's first
    # samples may still read ~lap_len.
    head = slice(0, GRID_HZ)
    d[head] = np.where(d[head] > lap_len / 2, 0.0, d[head])
    d = np.maximum.accumulate(d)  # monotonic within a lap
    edges = np.arange(0.0, lap_len, SECTOR_M)
    edges = np.append(edges, lap_len)
    t_at = np.interp(edges, d, s.t)
    return np.diff(t_at)
