"""What each recording gives the lap-time model, computed at import from its signals.

  profile      the best complete lap per 5 m: curvature (from lateral acceleration and
               speed, κ = a_y / v²), speed, and the game's sector boundaries
  straights    full-throttle samples on straights, away from gear changes, in 25 m bins
               per lap: speed and acceleration (drag and power)
  envelope     per 5 m/s of speed, the 99th percentile of lateral acceleration in
               corners, the 95th of braking deceleration in a straight line, and the
               median acceleration at full throttle (the usable g-g envelope at each speed)
  platform     ride heights at speed (aero platform), front and rear
  traps        top speed on each straight per lap
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from autosetup.lmu.telemetry import Lap, Session
from autosetup.metrics.signals import G, GRID_HZ, Signals, smooth

STEP_M = 5.0
BIN_M = 25.0
V_BIN = 5.0  # m/s
SHIFT_S = 0.3  # samples this close to a gear change are left out of the straights
FULL_THROTTLE = 0.98
STRAIGHT_G = 0.25  # |a_lat| under this is a straight
CORNER_G = 0.6
MIN_V = 20.0
STRAIGHT_MIN_M = 200.0  # a straight for a speed trap: curvature this low for at least this long
STRAIGHT_KAPPA = 1 / 1500.0
FAST_MS = 45.0  # ride heights "at speed"
Q = 95
Q_LAT = 99  # most cornering at a given speed isn't at the limit; the top of it is


@dataclass
class Profile:
    seq: int
    lap_time: float
    kappa: np.ndarray  # 1/m, signed (+ right), per STEP_M
    speed: np.ndarray  # m/s
    sectors: list[float]  # sector boundary distances, m


@dataclass
class SimData:
    profile: Profile | None = None
    straights: list[tuple[int, int, int, float, float]] = field(default_factory=list)  # (seq, bin, n, v, a_x)
    envelope: list[tuple[str, float, int, float]] = field(default_factory=list)  # (kind, v, n, q95 m/s²)
    rh_front: float | None = None  # mm, at speed
    rh_rear: float | None = None
    traps: list[tuple[int, float, float]] = field(default_factory=list)  # (seq, trap distance m, v m/s)


def _grid_lap(sig: Signals, m: np.ndarray, lap_len: float) -> tuple[np.ndarray, np.ndarray] | None:
    d = np.nan_to_num(sig.dist[m], nan=0.0).copy()
    d[:GRID_HZ] = np.where(d[:GRID_HZ] > lap_len / 2, 0.0, d[:GRID_HZ])
    d = np.maximum.accumulate(d)
    keep = np.concatenate([[True], np.diff(d) > 0])
    if keep.sum() < 50:
        return None
    grid = np.arange(0.0, lap_len, STEP_M)
    return grid, np.flatnonzero(m)[keep], d[keep]


def _sector_bounds(s: Session, sig: Signals, lap: Lap) -> list[float]:
    if "Current Sector" not in s.tables:
        return []
    ts, sec = s.events("Current Sector")
    out = []
    for target in (2, 0):  # entering sector 2, then sector 3 (logged as 0)
        hits = ts[(ts > lap.t_start) & (ts < lap.t_end) & (sec == target)]
        if len(hits):
            out.append(float(np.interp(hits[0], sig.t, sig.dist)))
    return out


def _profile(s: Session, sig: Signals, laps: list[Lap], clean: set[int]) -> Profile | None:
    lap_len = float(np.nanmax(sig.dist)) if np.isfinite(sig.dist).any() else 0.0
    cands = [(lap.time, i) for i, lap in enumerate(laps) if i in clean]
    if not cands or lap_len <= 0:
        return None
    _, seq = min(cands)
    lap = laps[seq]
    m = (sig.t >= lap.t_start) & (sig.t < lap.t_end)
    g = _grid_lap(sig, m, lap_len)
    if g is None:
        return None
    grid, idx, d = g
    v = np.interp(grid, d, sig.speed[idx])
    a_y = np.interp(grid, d, smooth(sig.a_lat[idx], GRID_HZ // 5)) * G
    kappa = a_y / np.maximum(v, 5.0) ** 2
    kappa = np.convolve(kappa, np.ones(3) / 3, "same")
    return Profile(seq, lap.time, kappa, v, _sector_bounds(s, sig, lap))


def _gear_quiet(s: Session, t: np.ndarray) -> np.ndarray:
    if "Gear" not in s.tables:
        return np.ones(len(t), dtype=bool)
    ts, _ = s.events("Gear")
    ts = ts[1:]  # row 0 is the state at start, not a change
    if not len(ts):
        return np.ones(len(t), dtype=bool)
    i = np.searchsorted(ts, t)
    before = np.abs(t - ts[np.clip(i - 1, 0, len(ts) - 1)])
    after = np.abs(ts[np.clip(i, 0, len(ts) - 1)] - t)
    return np.minimum(before, after) > SHIFT_S


def extract(s: Session, sig: Signals, laps: list[Lap], clean: set[int]) -> SimData:
    """``clean``: seqs of complete laps outside the pits with no real off."""
    out = SimData(profile=_profile(s, sig, laps, clean))
    moving = sig.speed > MIN_V
    quiet = _gear_quiet(s, sig.t)
    racing = np.zeros(len(sig.t), dtype=bool)
    lap_of = np.full(len(sig.t), -1)
    for i, lap in enumerate(laps):
        if lap.in_pits:
            continue
        m = (sig.t >= lap.t_start) & (sig.t < lap.t_end)
        racing |= m
        lap_of[m] = i
    a_x = sig.a_lon * G
    a_y = np.abs(sig.a_lat) * G
    # Straights: full throttle, no cornering, no shift.
    st = racing & moving & quiet & (sig.throttle >= FULL_THROTTLE) & (np.abs(sig.a_lat) < STRAIGHT_G) & np.isfinite(sig.dist)
    if st.any():
        bins = (sig.dist[st] // BIN_M).astype(int)
        seqs = lap_of[st]
        for key in sorted(set(zip(seqs.tolist(), bins.tolist()))):
            sel = (seqs == key[0]) & (bins == key[1])
            if sel.sum() >= 3:
                out.straights.append((key[0], key[1], int(sel.sum()), float(np.mean(sig.speed[st][sel])),
                                      float(np.median(a_x[st][sel]))))
    # Envelope per speed bin.
    lat = racing & moving & (np.abs(sig.a_lat) > CORNER_G)
    brake = racing & moving & (sig.brake > 0.3) & (np.abs(sig.a_lat) < STRAIGHT_G)
    drive = racing & moving & quiet & (sig.throttle >= FULL_THROTTLE) & (np.abs(sig.a_lat) < STRAIGHT_G)
    # Limits are high percentiles (you're at them only part of the time); the drive curve
    # is the median (flat out is flat out, and a high percentile would pick out the hills).
    for kind, mask, value, q in (("lat", lat, a_y, Q_LAT), ("brake", brake, -a_x, Q), ("drive", drive, a_x, 50)):
        vb = np.floor(sig.speed[mask] / V_BIN)
        for b in np.unique(vb):
            sel = vb == b
            if sel.sum() >= GRID_HZ // 2:
                out.envelope.append((kind, float((b + 0.5) * V_BIN), int(sel.sum()),
                                     float(np.percentile(value[mask][sel], q))))
    # Platform at speed, cruising (no big pitch from braking or power).
    fast = racing & (sig.speed > FAST_MS) & (np.abs(sig.a_lon) < 0.3) & (np.abs(sig.a_lat) < 0.5)
    rh = sig.ride_height * 1000.0
    if fast.sum() > GRID_HZ and np.isfinite(rh[fast]).any():
        out.rh_front = float(np.nanmean(rh[fast, :2]))
        out.rh_rear = float(np.nanmean(rh[fast, 2:]))
    # Speed traps: the top speed on each straight of the profile, per lap.
    if out.profile is not None:
        for d0, d1 in straights_of(out.profile.kappa):
            for i, lap in enumerate(laps):
                if lap.in_pits:
                    continue
                m = (lap_of == i) & (sig.dist >= d0) & (sig.dist <= d1 + 100.0)
                if m.sum() > GRID_HZ // 2:
                    out.traps.append((i, float(d1), float(np.nanmax(sig.speed[m]))))
    return out


def straights_of(kappa: np.ndarray) -> list[tuple[float, float]]:
    """(start, end) distances of runs at least STRAIGHT_MIN_M long with |κ| < STRAIGHT_KAPPA."""
    low = np.abs(kappa) < STRAIGHT_KAPPA
    edges = np.diff(np.concatenate([[0], low.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [(float(a * STEP_M), float((b - 1) * STEP_M)) for a, b in zip(starts, ends)
            if (b - a) * STEP_M >= STRAIGHT_MIN_M]
