"""Suspension at its own rate: shaft velocity, ride heights, wheel load and their histograms.

``Susp Pos`` (100 Hz, m, per wheel) is the corner spring's compression: the outside wheels
read more in every corner, and a prototype's lightly loaded corner springs reach ~0 in
the air. Shaft velocity is its Savitzky–Golay derivative, + in bump, − in rebound, with a
window in seconds so it means the same at any logging rate.

Wheel load: DAM's ``Tyre Load`` when a log matches the recording; otherwise the spring's
share of it, proportional to the compression (the game logs spring settings as plain
indices, so no rate is known; std/mean, the load variation, doesn't need one). ARB,
damper and third-spring forces aren't in the proxy: it reads spring-load variation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.telemetry import Session
from autosetup.metrics.filters import savgol, window_for

WINDOW_S = 0.04
BIN_MM_S = 5.0
RANGE_MM_S = 1000.0  # histogram edges ±; faster samples land in the end bins
EDGES = np.arange(-RANGE_MM_S, RANGE_MM_S + BIN_MM_S / 2, BIN_MM_S)
KERB_SURFACE = 5  # rF2 surface code: rumble strip
MIN_LOAD_MM = 2.0  # spring proxy: a wheel compressed less than this on average is too light to judge
MIN_SPEED = 15.0


@dataclass
class Suspension:
    t: np.ndarray  # session time, s, at the channel's own rate
    rate: float
    x: np.ndarray  # (n, 4) compression, m
    v: np.ndarray  # (n, 4) shaft velocity, m/s (+ bump)
    rh: np.ndarray  # (n, 4) ride height, m
    load: np.ndarray  # (n, 4): N (DAM) or spring compression, m
    load_source: str  # "tyre_load" | "spring"
    kerb: np.ndarray  # (n, 4) bool, wheel on a kerb (surface code)
    has_surface: bool  # the recording logs surface types at all
    speed: np.ndarray = field(default_factory=lambda: np.zeros(0))  # m/s at t


def load_suspension(s: Session, speed_t: np.ndarray, speed: np.ndarray,
                    tyre_load: np.ndarray | None = None) -> Suspension | None:
    """``speed_t``/``speed``: the car's speed on any grid; ``tyre_load``: DAM tyre load
    already on this channel's own time base, or None."""
    if "Susp Pos" not in s.frequencies:
        return None
    t, x = s.channel("Susp Pos")
    if x.ndim != 2 or len(t) < 20:
        return None
    rate = float(s.frequencies["Susp Pos"])
    v = savgol(x, window_for(rate, WINDOW_S), 2, deriv=1, dt=1.0 / rate)
    if "RideHeights" in s.frequencies:
        tr, rh = s.channel("RideHeights")
        rh = np.column_stack([np.interp(t, tr, rh[:, i]) for i in range(4)])
    else:
        rh = np.full_like(x, np.nan)
    has_surface = "SurfaceTypes" in s.tables
    kerb = np.zeros_like(x, dtype=bool)
    if has_surface:
        try:
            surf = np.asarray(s.state_at("SurfaceTypes", t))
            if surf.ndim == 2:
                kerb = surf == KERB_SURFACE
        except KeyError:
            has_surface = False
    if tyre_load is not None and np.isfinite(tyre_load).any():
        load, source = tyre_load, "tyre_load"
    else:
        load, source = x, "spring"
    return Suspension(t, rate, x, v, rh, load, source, kerb, has_surface, np.interp(t, speed_t, speed))


def histogram(v_mm_s: np.ndarray) -> np.ndarray:
    """Counts on the fixed 5 mm/s grid (EDGES); the UI merges bins for wider ones."""
    v = np.clip(v_mm_s[np.isfinite(v_mm_s)], -RANGE_MM_S + 1e-6, RANGE_MM_S - 1e-6)
    return np.histogram(v, EDGES)[0]


def shape(v_mm_s: np.ndarray, knee_mm_s: float) -> dict[str, float]:
    """Bump share, share below the knee (slow circuit), skew."""
    v = v_mm_s[np.isfinite(v_mm_s)]
    if len(v) < 10:
        return {}
    sd = float(np.std(v))
    return {"bump_frac": float(np.mean(v > 0)), "ls_frac": float(np.mean(np.abs(v) < knee_mm_s)),
            "skew": float(np.mean(((v - v.mean()) / sd) ** 3)) if sd > 0 else 0.0}


def load_cv(load: np.ndarray, source: str) -> float | None:
    """std/mean of one wheel's load over some samples; None where it can't be judged."""
    x = load[np.isfinite(load)]
    if len(x) < 20:
        return None
    mean = float(np.mean(x))
    if mean <= 0 or (source == "spring" and mean * 1000.0 < MIN_LOAD_MM):
        return None
    return float(np.std(x) / mean)


@dataclass
class LapSuspension:
    seq: int
    wheel: str  # fl, fr, rl, rr
    kerb: bool  # kerb samples (True) or everything else
    hist: np.ndarray
    n: int
    stats: dict[str, float]  # bump_frac, ls_frac, skew, load_cv, load_cv_<phase>


def lap_suspension(susp: Suspension, seq: int, t0: float, t1: float, phase_at: np.ndarray,
                   knee_mm_s: float) -> list[LapSuspension]:
    """Per wheel, kerb and non-kerb samples of one lap while moving. ``phase_at``: the
    corner phase ("entry"/"mid"/"exit"/"") of every sample of ``susp.t``."""
    m = (susp.t >= t0) & (susp.t < t1) & (susp.speed > MIN_SPEED)
    if m.sum() < 20:
        return []
    out = []
    for i, w in enumerate(("fl", "fr", "rl", "rr")):
        for on_kerb in (False, True):
            sel = m & (susp.kerb[:, i] == on_kerb)
            if sel.sum() < (5 if on_kerb else 20):
                continue
            v = susp.v[sel, i] * 1000.0
            stats = shape(v, knee_mm_s)
            if not on_kerb:
                cv = load_cv(susp.load[sel, i], susp.load_source)
                if cv is not None:
                    stats["load_cv"] = cv
                for ph in ("entry", "mid", "exit"):
                    cv = load_cv(susp.load[sel & (phase_at == ph), i], susp.load_source)
                    if cv is not None:
                        stats[f"load_cv_{ph}"] = cv
            out.append(LapSuspension(seq, w, on_kerb, histogram(v), int(sel.sum()), stats))
    return out


def pitch_heave(rh: np.ndarray, wheelbase: float) -> tuple[np.ndarray, np.ndarray]:
    """Pitch (deg, + nose down) and heave (mm, mean ride height) from the four ride heights."""
    front = np.nanmean(rh[:, :2], axis=1)
    rear = np.nanmean(rh[:, 2:], axis=1)
    return np.degrees(np.arctan2(rear - front, wheelbase)), np.nanmean(rh, axis=1) * 1000.0
