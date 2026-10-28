"""Whole-recording signals on one common time grid, plus derived accelerations.

Findings from real recordings that shape this module:
  * The game's ``G Force Lat/Long`` channels (10 Hz) don't correlate with anything,
    probably because they sample vibration, so accelerations are derived instead:
    longitudinal from the 100 Hz speed, lateral from speed × yaw rate, and yaw rate from
    the GPS heading.
  * ``TyresTempLeft/Right`` are car-frame sides: the inner edge is Right on the left-hand
    wheels and Left on the right-hand ones (they agree with negative camber).
  * ``Wheel Speed`` is each wheel's contact-patch speed over the ground, not its
    rotation: it stays within ±3 % of car speed even with ABS active, so it carries no
    wheelspin or lock-up information and isn't loaded.
  * ``Steering Pos`` changed unit in an LMU update: older files log -1..1 of full lock
    (unit ''), October 2026 files log -100..100 (unit '%'). Normalised to -1..1 here.
  * Units: Ground Speed km/h, GPS Speed m/s, pedals 0-100 %, Steering Pos
    -1..1 of full lock (positive = right), ride heights m, tyre temps °C, pressure kPa,
    wear % remaining.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from autosetup.lmu.telemetry import Session

G = 9.81
GRID_HZ = 50

# Rough wheelbases (m) by class; understeer angles are compared within one car, so a
# wrong guess shifts every lap equally instead of corrupting comparisons.
WHEELBASE = {"GT3": 2.65, "GTE": 2.70, "LMP2": 3.00, "LMP3": 2.85, "Hyper": 3.15}


def smooth(x: np.ndarray, n: int) -> np.ndarray:
    """Centred moving average over n samples (edges use what's available)."""
    if n <= 1 or len(x) < n:
        return x
    k = np.ones(n) / n
    num = np.convolve(x, k, "same")
    den = np.convolve(np.ones_like(x), k, "same")
    return num / den


@dataclass
class Signals:
    """Every channel the metrics need, resampled onto one grid (``t``)."""

    t: np.ndarray
    dist: np.ndarray  # lap distance, m
    speed: np.ndarray  # m/s
    steer: np.ndarray  # -1..1, positive = right
    throttle: np.ndarray  # 0..1
    brake: np.ndarray  # 0..1
    yaw_rate: np.ndarray  # rad/s, sign aligned with steer (positive = turning right)
    a_lat: np.ndarray  # g, positive = turning right
    a_lon: np.ndarray  # g, positive = accelerating
    ride_height: np.ndarray  # (n, 4) m
    off_line: np.ndarray  # m beyond the track edge (negative = inside)
    surface: np.ndarray  # (n, 4) surface type codes
    tc: np.ndarray  # 0/1
    abs: np.ndarray  # 0/1
    tyre_inner: np.ndarray  # (n, 4) °C
    tyre_centre: np.ndarray
    tyre_outer: np.ndarray
    tyre_pressure: np.ndarray  # (n, 4) kPa
    tyre_carcass: np.ndarray  # (n, 4) °C
    tyre_wear: np.ndarray  # (n, 4) % remaining
    brake_temp: np.ndarray  # (n, 4) °C
    overheating: np.ndarray  # 0/1
    fuel: np.ndarray  # litres
    track_temp: np.ndarray  # °C
    ambient_temp: np.ndarray  # °C
    third: np.ndarray  # (n, 2) front / rear third-spring deflection, m (NaN on cars without one)


def _fixed(s: Session, name: str, t: np.ndarray, cols: int = 1) -> np.ndarray:
    if name not in s.frequencies:
        return np.full((len(t), cols), np.nan) if cols > 1 else np.full(len(t), np.nan)
    tt, v = s.channel(name)
    if len(tt) < 2:
        return np.full((len(t), cols), np.nan) if cols > 1 else np.full(len(t), np.nan)
    if v.ndim == 1:
        return np.interp(t, tt, v)
    return np.column_stack([np.interp(t, tt, v[:, i]) for i in range(v.shape[1])])


def _lap_distance(s: Session, t: np.ndarray) -> np.ndarray:
    """Lap Dist resampled without blending across the start/finish wrap: interpolating
    4266 m → 1 m linearly (10 Hz files) would invent values like 1700 m just after the
    line. Unwrap, interpolate, wrap again."""
    if "Lap Dist" not in s.frequencies:
        return np.full(len(t), np.nan)
    tt, d = s.channel("Lap Dist")
    if len(tt) < 2:
        return np.full(len(t), np.nan)
    lap_len = float(np.nanmax(d))
    wraps = np.concatenate([[0.0], np.cumsum(np.diff(d) < -lap_len / 2)])
    unwrapped = np.interp(t, tt, d + wraps * lap_len)
    return np.mod(unwrapped, lap_len) if lap_len > 0 else unwrapped


def _event(s: Session, name: str, t: np.ndarray) -> np.ndarray:
    if name not in s.tables:
        return np.zeros(len(t))
    try:
        return np.asarray(s.state_at(name, t), dtype=np.float64)
    except KeyError:  # some versions store it as a fixed-rate channel
        return _fixed(s, name, t)


def load_signals(s: Session) -> Signals:
    t = np.arange(s.t_start, s.t_end, 1.0 / GRID_HZ)
    speed = _fixed(s, "Ground Speed", t) / 3.6
    steer = _fixed(s, "Steering Pos", t)
    if s.units.get("Steering Pos") == "%" or np.nanmax(np.abs(steer), initial=0.0) > 1.5:
        steer = steer / 100.0
    left, centre, right = (_fixed(s, n, t, 4) for n in ("TyresTempLeft", "TyresTempCentre", "TyresTempRight"))
    inner = np.column_stack([right[:, 0], left[:, 1], right[:, 2], left[:, 3]])
    outer = np.column_stack([left[:, 0], right[:, 1], left[:, 2], right[:, 3]])
    yaw = _yaw_rate(s, t, speed, steer)
    surface = _event(s, "SurfaceTypes", t)
    if surface.ndim == 1:
        surface = np.zeros((len(t), 4))
    return Signals(
        t=t,
        dist=_lap_distance(s, t),
        speed=speed,
        steer=steer,
        throttle=_fixed(s, "Throttle Pos", t) / 100.0,
        brake=_fixed(s, "Brake Pos", t) / 100.0,
        yaw_rate=yaw,
        a_lat=speed * yaw / G,
        a_lon=np.gradient(smooth(speed, 5), t) / G if len(t) > 1 else np.zeros(len(t)),
        ride_height=_fixed(s, "RideHeights", t, 4),
        off_line=np.abs(_fixed(s, "Path Lateral", t)) - np.abs(_fixed(s, "Track Edge", t)),
        surface=surface,
        tc=_event(s, "TC", t),
        abs=_event(s, "ABS", t),
        tyre_inner=inner,
        tyre_centre=centre,
        tyre_outer=outer,
        tyre_pressure=_fixed(s, "TyresPressure", t, 4),
        tyre_carcass=_fixed(s, "TyresCarcassTemp", t, 4),
        tyre_wear=_fixed(s, "Tyres Wear", t, 4),
        brake_temp=_fixed(s, "Brakes Temp", t, 4),
        overheating=_fixed(s, "OverheatingState", t),
        fuel=_fixed(s, "Fuel Level", t),
        track_temp=_fixed(s, "Track Temperature", t),
        ambient_temp=_fixed(s, "Ambient Temperature", t),
        third=np.column_stack([_fixed(s, "Front3rdDeflection", t), _fixed(s, "Rear3rdDeflection", t)]),
    )


def _yaw_rate(s: Session, t: np.ndarray, speed: np.ndarray, steer: np.ndarray) -> np.ndarray:
    """Yaw rate from the GPS track's heading change, signed like the steering."""
    if "GPS Latitude" not in s.frequencies:
        return np.zeros(len(t))
    tg, lat = s.channel("GPS Latitude")
    _, lon = s.channel("GPS Longitude")
    if len(tg) < 5:
        return np.zeros(len(t))
    r = 6371000.0
    x = np.radians(lon) * r * np.cos(np.radians(np.nanmean(lat)))
    y = np.radians(lat) * r
    vx, vy = np.gradient(smooth(x, 3), tg), np.gradient(smooth(y, 3), tg)
    heading = np.unwrap(np.arctan2(vy, vx))
    yaw = smooth(np.gradient(heading, tg), 3)
    yaw[np.hypot(vx, vy) < 5.0] = 0.0  # heading is noise when (nearly) stopped
    yaw = np.interp(t, tg, yaw)
    moving = speed > 15
    if moving.sum() < 50:
        return yaw
    # Map heading (counter-clockwise positive) onto the steering convention.
    if np.corrcoef(steer[moving], yaw[moving])[0, 1] < 0:
        yaw = -yaw
    # GPS-derived yaw trails the steering (vehicle response + 10 Hz smoothing, ~0.2 s
    # on real data). Shift it back by the best-correlating lag so moments where the
    # wheel points against the rotation are real countersteer, not transition lag.
    best, best_lag = -1.0, 0
    for lag in range(0, int(0.4 * GRID_HZ) + 1, 2):
        c = np.corrcoef(steer[moving][: len(steer[moving]) - lag], yaw[moving][lag:])[0, 1] if lag else \
            np.corrcoef(steer[moving], yaw[moving])[0, 1]
        if c > best:
            best, best_lag = c, lag
    if best_lag:
        yaw = np.concatenate([yaw[best_lag:], np.repeat(yaw[-1], best_lag)])
    return yaw
