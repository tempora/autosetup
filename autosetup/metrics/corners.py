"""Corners of a layout and each lap's pass through them.

The corner map comes from the median lap: lateral acceleration and speed per 5 m of lap
distance over the complete laps (or, in a run without one, the laps outside the pits that
cover at least half the lap, over the distance they cover). A corner is a run of distance where the median lap
corners harder than CORNER_G, in one direction (a chicane is two corners), with its
braking zone in front of it as entry. Its apex is the slowest point. Corners are
numbered by apex distance; the store keeps one map per layout so passes from every
recording share corner ids (``remap``).

Within a pass every cornering sample gets a phase, from the pedals and the apex:
  entry  on the brakes, or before the apex and off the throttle;
  exit   after the apex with the throttle at least half open;
  mid    the rest.
A pass is low or high speed by its apex speed (``band_split_kmh`` in classes.toml).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

from autosetup.lmu.telemetry import Lap
from autosetup.metrics.signals import Signals

STEP_M = 5.0
CORNER_G = 0.4
MERGE_GAP_M = 30.0  # cornering runs closer than this (same direction) are one corner
MIN_CORNER_M = 30.0
MAX_ENTRY_M = 250.0  # braking zone counted as entry, at most
MIN_SPEED = 15.0  # m/s
MATCH_M = 40.0  # apexes this close are the same corner in another recording's map
PHASES = ("entry", "mid", "exit")
BANDS = ("low", "high")


@dataclass(frozen=True)
class Corner:
    id: int
    direction: int  # +1 right, -1 left (the sign of the steering)
    d_start: float  # entry starts here (braking zone included), m
    d_apex: float
    d_end: float
    v_apex: float  # median apex speed, m/s


def _lap_grid(sig: Signals, lap: Lap, lap_len: float) -> tuple[np.ndarray, ...] | None:
    m = (sig.t >= lap.t_start) & (sig.t < lap.t_end) & np.isfinite(sig.dist)
    if m.sum() < 50:
        return None
    d = sig.dist[m].copy()
    if lap.complete:  # it starts at the line, but the distance wrap can trail the Lap event
        head = slice(0, 50)
        d[head] = np.where(d[head] > lap_len / 2, 0.0, d[head])
    d = np.maximum.accumulate(d)
    grid = np.arange(0.0, lap_len, STEP_M)
    keep = np.concatenate([[True], np.diff(d) > 0])
    d = d[keep]
    if len(d) < 10:
        return None
    return tuple(np.interp(grid, d, x[m][keep], left=np.nan, right=np.nan)
                 for x in (sig.a_lat, sig.speed, sig.brake, sig.steer))


def _covered(sig: Signals, lap: Lap) -> float:
    d = sig.dist[(sig.t >= lap.t_start) & (sig.t < lap.t_end)]
    d = d[np.isfinite(d)]
    return float(d.max() - d.min()) if len(d) else 0.0


def corner_map(sig: Signals, laps: list[Lap]) -> list[Corner]:
    """Corners from the median lap per 5 m (laps as in the module docstring)."""
    if not np.isfinite(sig.dist).any():
        return []
    lap_len = float(np.nanmax(sig.dist))
    good = [lap for lap in laps if lap.complete and not lap.in_pits]
    if not good:
        good = [lap for lap in laps if not lap.in_pits and _covered(sig, lap) >= 0.5 * lap_len]
    rows = [g for g in (_lap_grid(sig, lap, lap_len) for lap in good) if g is not None]
    if not rows:
        return []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # bins no lap covered stay NaN
        a_lat, speed, brake, steer = (np.nanmedian(np.stack([r[i] for r in rows]), axis=0) for i in range(4))
    a_lat = np.convolve(a_lat, np.ones(5) / 5, "same")  # 25 m
    grid = np.arange(0.0, lap_len, STEP_M)
    direction = np.sign(a_lat)
    cornering = (np.abs(a_lat) > CORNER_G) & (speed > MIN_SPEED)
    runs: list[list[int]] = []
    i = 0
    while i < len(grid):
        if not cornering[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(grid) and cornering[j + 1] and direction[j + 1] == direction[i]:
            j += 1
        if runs and direction[runs[-1][0]] == direction[i] and (i - runs[-1][1]) * STEP_M <= MERGE_GAP_M:
            runs[-1][1] = j
        else:
            runs.append([i, j])
        i = j + 1
    corners = []
    for i, j in runs:
        if (j - i + 1) * STEP_M < MIN_CORNER_M:
            continue
        apex = i + int(np.nanargmin(speed[i:j + 1]))
        start = i
        # Braking zone in front: back up while the brake is on, up to MAX_ENTRY_M.
        while start > 0 and (i - start) * STEP_M < MAX_ENTRY_M and np.nan_to_num(brake[start - 1]) > 0.05:
            start -= 1
        if corners and grid[start] < corners[-1][3]:
            start = int(round(corners[-1][3] / STEP_M))  # don't overlap the previous corner's exit
        corners.append((float(np.sign(np.median(steer[i:j + 1])) or direction[i]),
                        float(grid[start]), float(grid[apex]), float(grid[j]), float(speed[apex])))
    return [Corner(n, int(dirn), a, b, c, v) for n, (dirn, a, b, c, v) in enumerate(corners)]


def remap(corners: list[Corner], canonical: list[Corner]) -> dict[int, int]:
    """This map's corner id → the layout's canonical id (nearest apex within MATCH_M);
    corners with no match are left out."""
    out = {}
    for c in corners:
        best = min(canonical, key=lambda k: abs(k.d_apex - c.d_apex), default=None)
        if best is not None and abs(best.d_apex - c.d_apex) <= MATCH_M and best.direction == c.direction:
            out[c.id] = best.id
    return out


@dataclass
class Pass:
    """One lap's samples through one corner (indices into the lap's Signals slice)."""

    corner: Corner
    idx: np.ndarray  # sample indices of the pass
    phase: np.ndarray  # per sample of idx: "entry" | "mid" | "exit" | "" (not cornering)
    band: str
    v_min: float


def passes(sig: Signals, corners: list[Corner], band_split_ms: float) -> list[Pass]:
    """Passes of one lap's Signals through the corner map (the lap must not wrap mid-corner)."""
    out = []
    d = sig.dist
    for c in corners:
        if c.d_end <= c.d_start:
            continue
        idx = np.flatnonzero((d >= c.d_start) & (d <= c.d_end) & np.isfinite(sig.speed))
        if len(idx) < 10:
            continue
        breaks = np.flatnonzero(np.diff(idx) != 1)
        if len(breaks):  # e.g. the lap's first samples still reading last lap's distance
            pieces = np.split(idx, breaks + 1)
            idx = max(pieces, key=len)
        sp = sig.speed[idx]
        apex = int(np.argmin(sp))
        cornering = (np.abs(sig.a_lat[idx]) > CORNER_G) & (sp > MIN_SPEED)
        on_brake = sig.brake[idx] > 0.05
        on_power = sig.throttle[idx] >= 0.5
        before = np.arange(len(idx)) <= apex
        phase = np.where(on_brake | (before & (sig.throttle[idx] < 0.2)), "entry",
                         np.where(~before & on_power, "exit", "mid"))
        phase = np.where(cornering, phase, "")
        v_min = float(sp[apex])
        out.append(Pass(c, idx, phase, "low" if v_min < band_split_ms else "high", v_min))
    return out
