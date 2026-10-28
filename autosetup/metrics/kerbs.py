"""Kerb events: when a wheel rides a kerb, and what it does to the car.

A wheel is on a kerb when the game logs its surface as a rumble strip (code 5). Recordings
without surface types fall back to shaft-velocity spikes while the car is at the track
edge. Contacts of any wheel less than MERGE_S apart are one event. Per event:

  peak_load      highest wheel load over the wheel's usual (median, off-kerb) load
  airtime_s      longest time a wheel carried under UNLOADED of its usual load
  pitch_deg      largest pitch change from just before the kerb
  heave_mm       largest ride-height (heave) change from just before the kerb
  yaw_dist       RMS yaw-rate error (rad/s) in the YAW_S after the kerb, minus before it

The samples inside events are what the damper module calls kerb samples; the rest
are the contact-patch samples.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from autosetup.metrics.signals import Signals
from autosetup.metrics.suspension import Suspension, pitch_heave

MERGE_S = 0.25
AFTER_S = 0.3  # the car's response is counted this long after the last contact
BEFORE_S = 0.5
YAW_S = 0.5
UNLOADED = 0.1
SPIKE_MM_S = 300.0
EDGE_M = -1.0  # off_line above this (m beyond the edge; negative = inside) is "at the edge"
SPIKE_PAD_S = 0.1


@dataclass(frozen=True)
class KerbEvent:
    t0: float
    t1: float
    wheels: str  # e.g. "fl,rl"
    source: str  # "surface" | "spike"
    peak_load: float | None
    airtime_s: float
    pitch_deg: float | None
    heave_mm: float | None
    yaw_dist: float | None
    dist: float | None = None  # lap distance at t0, m


def kerb_mask(susp: Suspension, sig: Signals) -> tuple[np.ndarray, str]:
    """(n, 4) per-wheel kerb flag at the suspension's rate, and where it came from."""
    if susp.has_surface and susp.kerb.any():
        return susp.kerb, "surface"
    edge = np.interp(susp.t, sig.t, np.nan_to_num(sig.off_line, nan=-99.0)) > EDGE_M
    spike = (np.abs(susp.v) * 1000.0 > SPIKE_MM_S) & edge[:, None]
    pad = int(SPIKE_PAD_S * susp.rate)
    if pad and spike.any():
        k = np.ones(2 * pad + 1)
        spike = np.column_stack([np.convolve(spike[:, i], k, "same") > 0 for i in range(4)])
    return spike, "spike"


def events(susp: Suspension, sig: Signals, yaw_error: np.ndarray, wheelbase: float) -> list[KerbEvent]:
    """``yaw_error``: r_ref − r (rad/s) on the Signals grid."""
    mask, source = kerb_mask(susp, sig)
    any_wheel = mask.any(axis=1)
    if not any_wheel.any():
        return []
    edges = np.diff(np.concatenate([[0], any_wheel.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
    runs: list[list[int]] = []
    gap = int(MERGE_S * susp.rate)
    for a, b in zip(starts, ends):
        if runs and a - runs[-1][1] <= gap:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    usual = np.array([np.nanmedian(susp.load[~mask[:, i] & (susp.speed > 15.0), i])
                      if (~mask[:, i] & (susp.speed > 15.0)).any() else np.nan for i in range(4)])
    pitch, heave = pitch_heave(susp.rh, wheelbase)
    out = []
    for a, b in runs:
        t0, t1 = float(susp.t[a]), float(susp.t[b])
        win = (susp.t >= t0) & (susp.t <= t1 + AFTER_S)
        pre = (susp.t >= t0 - BEFORE_S) & (susp.t < t0)
        wheels = ",".join(w for i, w in enumerate(("fl", "fr", "rl", "rr")) if mask[a:b + 1, i].any())
        peak, air = None, 0.0
        with np.errstate(invalid="ignore", divide="ignore"):
            ratios = [np.nanmax(susp.load[win, i]) / usual[i] for i in range(4)
                      if np.isfinite(usual[i]) and usual[i] > 0 and np.isfinite(susp.load[win, i]).any()]
        if ratios:
            peak = float(max(ratios))
        for i in range(4):
            if np.isfinite(usual[i]) and usual[i] > 0:
                low = susp.load[win, i] < UNLOADED * usual[i]
                air = max(air, _longest(low) / susp.rate)
        p_dev = h_dev = None
        if pre.any() and np.isfinite(pitch[pre]).any() and np.isfinite(pitch[win]).any():
            p_dev = float(np.nanmax(np.abs(pitch[win] - np.nanmedian(pitch[pre]))))
            h_dev = float(np.nanmax(np.abs(heave[win] - np.nanmedian(heave[pre]))))
        yd = _yaw_disturbance(sig.t, yaw_error, t0, t1)
        d = float(np.interp(t0, sig.t, sig.dist)) if np.isfinite(sig.dist).any() else None
        out.append(KerbEvent(t0, t1, wheels, source, peak, air, p_dev, h_dev, yd, d))
    return out


def _longest(mask: np.ndarray) -> int:
    if not mask.any():
        return 0
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return int(np.max(np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)))


def _yaw_disturbance(t: np.ndarray, err: np.ndarray, t0: float, t1: float) -> float | None:
    after = (t > t1) & (t <= t1 + YAW_S) & np.isfinite(err)
    before = (t >= t0 - YAW_S) & (t < t0) & np.isfinite(err)
    if after.sum() < 3 or before.sum() < 3:
        return None
    rms = lambda x: float(np.sqrt(np.mean(x ** 2)))  # noqa: E731
    return rms(err[after]) - rms(err[before])


def grid_mask(susp: Suspension, mask: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Any-wheel kerb flag of the suspension's samples, on another time grid."""
    if not len(susp.t):
        return np.zeros(len(t), dtype=bool)
    i = np.clip(np.searchsorted(susp.t, t), 0, len(susp.t) - 1)
    return mask.any(axis=1)[i]
