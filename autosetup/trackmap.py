"""Track outlines and per-lap traces from your own recordings.

The outline comes from the GPS trace of the fastest clean lap recorded on a layout,
projected to metres around a fixed origin and resampled every 10 m of lap distance.
Lap traces (time, speed, pedals, steering, gear and the driven line) are resampled every
5 m of lap distance in the same frame, so two laps — from different stints and setups —
line up point by point, on the map and in the traces.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.telemetry import Lap, RecordingBusy, Session, in_progress
from autosetup.store import Store

STEP_M = 10.0
TRACE_STEP_M = 5.0
EARTH_R = 6371000.0
SCHEMA = """
create table if not exists track_maps (
    layout     varchar primary key,
    file_name  varchar,
    lap_time   double,
    data       json
);
"""


def build_track_map(s: Session) -> dict | None:
    """Outline of the fastest complete lap in a recording, or None without GPS."""
    laps = [lap for lap in s.laps() if lap.complete and not lap.in_pits]
    if not laps or "GPS Latitude" not in s.frequencies or "Lap Dist" not in s.frequencies:
        return None
    lap = min(laps, key=lambda l: l.time)
    tg, lat = s.channel("GPS Latitude")
    _, lon = s.channel("GPS Longitude")
    td, dist = s.channel("Lap Dist")
    m = (tg >= lap.t_start) & (tg < lap.t_end)
    if m.sum() < 20:
        return None
    lat, lon, t = lat[m], lon[m], tg[m]
    length = float(np.nanmax(dist))
    d = _lap_distance(t, td, dist, lap, length)
    lat0, lon0 = float(np.mean(lat)), float(np.mean(lon))
    x, y = _project(lat, lon, (lat0, lon0))
    grid = np.arange(0.0, length, STEP_M)
    keep = np.concatenate([[True], np.diff(d) > 0])  # strictly increasing for interp
    gx = np.interp(grid, d[keep], x[keep])
    gy = np.interp(grid, d[keep], y[keep])
    sectors = []
    if "Current Sector" in s.tables:
        ts, sec = s.events("Current Sector")
        for target in (2, 0):  # entering sector 2, then sector 3 (logged as 0)
            hits = ts[(ts > lap.t_start) & (ts < lap.t_end) & (sec == target)]
            if len(hits):
                sectors.append(round(float(np.interp(hits[0], t, d)), 1))
    return {
        "length": round(length, 1),
        "step_m": STEP_M,
        "points": [[round(float(a), 1), round(float(b), 1), round(float(c), 1)] for a, b, c in zip(grid, gx, gy)],
        "sectors": sectors,
        "lap_time": lap.time,
        "origin": [lat0, lon0],
        "version": MAP_VERSION,
    }


def _project(lat: np.ndarray, lon: np.ndarray, origin: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    lat0, lon0 = origin
    x = np.radians(lon - lon0) * EARTH_R * np.cos(np.radians(lat0))
    y = np.radians(lat - lat0) * EARTH_R
    return x, y


def _lap_distance(t: np.ndarray, td: np.ndarray, dist: np.ndarray, lap: Lap, length: float) -> np.ndarray:
    """Monotonic lap distance at times t within one lap.

    Interpolation runs on unwrapped distance: blending the 4652 m → 0 m step at the
    line would invent ~2300 m just after it (and flatten the trace up to there). The
    wrap can also trail the lap event, so the first second may still read ~length."""
    wraps = np.concatenate([[0.0], np.cumsum(np.diff(dist) < -length / 2)])
    d = np.interp(t, td, dist + wraps * length)
    d = np.mod(d, length) if length > 0 else d
    head = t < lap.t_start + 1.0
    d[head & (d > length / 2)] = 0.0
    return np.maximum.accumulate(d)


def lap_trace(s: Session, lap: Lap, origin: tuple[float, float] | None, length: float | None = None,
              sectors: list[float] | None = None) -> dict:
    """One lap resampled every TRACE_STEP_M metres of lap distance. Without a map
    ``origin`` (no GPS on this layout yet) the driven line is left out."""
    from autosetup.metrics.compute import _steering_geometry

    t0, t1 = lap.t_start, lap.t_end
    tc = s.clock
    tc = tc[(tc >= t0) & (tc < t1)]
    td, dist = s.channel("Lap Dist")
    length = length or float(np.nanmax(dist))
    d = _lap_distance(tc, td, dist, lap, length)
    keep = np.concatenate([[True], np.diff(d) > 0])
    tc, d = tc[keep], d[keep]
    end = float(d[-1]) if len(d) else 0.0
    grid = np.arange(0.0, max(end, TRACE_STEP_M), TRACE_STEP_M)
    t_at = np.interp(grid, d, tc)

    def chan(name: str, scale: float = 1.0) -> list[float | None]:
        if name not in s.frequencies:
            return [None] * len(grid)
        tt, v = s.channel(name)
        return [round(float(x), 3) for x in np.interp(t_at, tt, v) * scale]

    setup = CarSetup.from_json(s.setup_json)
    half_lock, _, _ = _steering_geometry(setup, s.car_class)
    tt, steer = s.channel("Steering Pos") if "Steering Pos" in s.frequencies else (None, None)
    if steer is not None:
        if s.units.get("Steering Pos") == "%" or np.nanmax(np.abs(steer), initial=0) > 1.5:
            steer = steer / 100.0
        steer_deg = [round(float(x), 1) for x in np.interp(t_at, tt, steer) * half_lock]
    else:
        steer_deg = [None] * len(grid)
    gear = [int(g) for g in s.state_at("Gear", t_at)] if "Gear" in s.tables else [None] * len(grid)
    xs = ys = [None] * len(grid)
    if origin is not None and "GPS Latitude" in s.frequencies:
        tg, lat = s.channel("GPS Latitude")
        _, lon = s.channel("GPS Longitude")
        x, y = _project(lat, lon, origin)
        xs = [round(float(v), 2) for v in np.interp(t_at, tg, x)]
        ys = [round(float(v), 2) for v in np.interp(t_at, tg, y)]
    times = [round(float(v), 3) for v in t_at - t0]
    split = []
    if sectors:
        marks = [float(np.interp(b, grid, t_at - t0)) for b in sectors if b < end]
        bounds = [0.0, *marks, float(lap.time)]
        split = [round(float(b - a), 3) for a, b in zip(bounds, bounds[1:])]
    return {
        "step_m": TRACE_STEP_M,
        "distance": [round(float(v), 1) for v in grid],
        "time": times,
        "speed": chan("Ground Speed"),
        "throttle": chan("Throttle Pos", 0.01),
        "brake": chan("Brake Pos", 0.01),
        "steer": steer_deg,
        "gear": gear,
        "x": xs,
        "y": ys,
        "sectors": split,
    }


MAP_VERSION = 2  # 2: lap distance unwrapped before interpolating


def track_map(store: Store, layout: str, telemetry_dir: Path | None) -> dict | None:
    """Cached outline for a layout, built on first use from the recording holding its
    fastest clean lap (any car)."""
    store.con.execute(SCHEMA)
    row = store.con.execute("select data from track_maps where layout = ?", [layout]).fetchone()
    if row:
        data = json.loads(row[0])
        if data.get("version") == MAP_VERSION:
            return data
        store.con.execute("delete from track_maps where layout = ?", [layout])  # built by older code
    if telemetry_dir is None:
        return None
    candidates = store.con.execute(
        """select r.file_name, min(l.lap_time) as best from recordings r join laps l on l.recording_id = r.id
           where coalesce(r.track_layout, r.track) = ? and l.complete and not l.in_pits
           group by r.file_name order by best""", [layout]).fetchall()
    for file_name, best in candidates:
        f = telemetry_dir / file_name
        if not f.is_file() or in_progress(f):
            continue
        try:
            with Session.open(f) as s:
                data = build_track_map(s)
        except RecordingBusy:
            continue
        if data:
            data["layout"] = layout
            store.con.execute("insert or replace into track_maps values (?, ?, ?, ?)",
                              [layout, file_name, data["lap_time"], json.dumps(data)])
            return data
    return None


def best_clean_lap(store: Store, car_name: str, layout: str) -> dict | None:
    """The fastest clean lap of a car on a layout, with its minisector times."""
    from autosetup.analysis import _laps

    rows = store.con.execute(
        """select id from recordings where car_name = ? and coalesce(track_layout, track) = ? and duration > 30""",
        [car_name, layout]).fetchall()
    best = None
    for (rid,) in rows:
        for lap in _laps(store, rid):
            if lap["clean"] and (best is None or lap["time"] < best[2]):
                best = (rid, lap["seq"], lap["time"], lap["lap"])
    if best is None:
        return None
    rid, seq, time, number = best
    return {"stint": rid, "seq": seq, "lap": number, "time": time, "sectors": _sectors(store, rid, seq)}


def _sectors(store: Store, rid: int, seq: int) -> list[float]:
    rows = store.con.execute(
        "select time from lap_sectors where recording_id = ? and seq = ? order by sector", [rid, seq]).fetchall()
    return [r[0] for r in rows]
