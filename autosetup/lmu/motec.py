"""Read MoTeC .ld logs written by the rF2 DAM plugin (``<LMU>/LOG/*.ld``).

DAM logs what the game's own telemetry lacks: tyre load, contact-patch and ground
velocities per wheel (slip angles, yaw rate), aero forces, body pitch/roll. It's optional:
everything works from the game's recordings, and a matching .ld only adds channels.

File layout (little-endian): the header holds a pointer to the first channel's metadata;
each channel's metadata is a linked list node with its sample count, rate, data pointer,
data type and the conversion ``(raw / scale * 10^-dec + shift) * mul``.

``Session Elapsed Time`` runs on the same session clock as the game recording's
``GPS Time``, so a log lines up with a recording by session time (``attach``).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

_HEADER = struct.Struct("<I4xII20xI24xHHHI8sHHI4x16s16x16s16x64s64s64x64s64x1024xI66x64s126x")
_CHANNEL = struct.Struct("<IIIIHHHHhhhh32s8s12s40x")
CLOCK = "Session Elapsed Time"
WHEELS = ("FL", "FR", "RL", "RR")
_FILE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}) - (\d{2}-\d{2}-\d{2}) - (.+) - [A-Z]+\d*\.ld$")


def _text(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("latin-1").strip()


@dataclass(frozen=True)
class Channel:
    name: str
    unit: str
    freq: int
    count: int
    data_ptr: int
    dtype_a: int
    dtype: int
    shift: int
    mul: int
    scale: int
    dec: int

    def numpy_type(self) -> str:
        if self.dtype_a == 0x07:
            return {2: "<f2", 4: "<f4"}[self.dtype]
        return {2: "<i2", 4: "<i4"}[self.dtype]


class MotecLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._raw = self.path.read_bytes()
        head = _HEADER.unpack_from(self._raw, 0)
        meta_ptr = head[1]
        self.date, self.time = _text(head[12]), _text(head[13])
        self.driver, self.vehicle, self.venue = _text(head[14]), _text(head[15]), _text(head[16])
        self.channels: dict[str, Channel] = {}
        seen, ptr = set(), meta_ptr
        while ptr and ptr not in seen:
            seen.add(ptr)
            (_, nxt, data_ptr, count, _, dta, dt, freq, shift, mul, scale, dec,
             name, _, unit) = _CHANNEL.unpack_from(self._raw, ptr)
            ch = Channel(_text(name), _text(unit), freq, count, data_ptr, dta, dt, shift, mul, scale, dec)
            self.channels.setdefault(ch.name, ch)
            ptr = nxt

    def values(self, name: str) -> np.ndarray:
        ch = self.channels[name]
        raw = np.frombuffer(self._raw, dtype=ch.numpy_type(), count=ch.count, offset=ch.data_ptr).astype(np.float64)
        return (raw / (ch.scale or 1) * 10.0 ** -ch.dec + ch.shift) * (ch.mul or 1)

    def channel(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """(session time, values). Every channel spans the log from its start; sample i of a
        channel at f Hz sits at clock_start + i / f."""
        t0, _ = self.clock_span()
        v = self.values(name)
        return t0 + np.arange(len(v)) / self.channels[name].freq, v

    def clock_span(self) -> tuple[float, float]:
        clock = self.values(CLOCK)
        return float(clock[0]), float(clock[-1])

    @property
    def started(self) -> datetime | None:
        """Local wall-clock start, from the file name (DAM names logs in local time)."""
        m = _FILE_NAME.match(self.path.name)
        if not m:
            return None
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S")

    @property
    def track(self) -> str:
        m = _FILE_NAME.match(self.path.name)
        return m.group(3) if m else self.venue


def list_logs(log_dir: str | Path) -> list[Path]:
    return sorted(Path(log_dir).glob("*.ld"), key=lambda p: p.stat().st_mtime)


def find_log(log_dir: str | Path, track: str, t_start: float, t_end: float, around: datetime | None = None,
             max_hours: float = 14.0) -> MotecLog | None:
    """The DAM log of the same outing: same track, its session-clock span covering most of
    [t_start, t_end], and (when ``around`` is given, the recording's start in local time)
    started within ``max_hours`` of it, since session clocks restart every session."""
    best, best_overlap = None, 0.0
    for path in list_logs(log_dir):
        m = _FILE_NAME.match(path.name)
        if m and m.group(3).strip().lower() != track.strip().lower():
            continue
        if around is not None and m:
            started = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H-%M-%S")
            if abs((started - around).total_seconds()) > max_hours * 3600:
                continue
        try:
            log = MotecLog(path)
            a, b = log.clock_span()
        except (struct.error, KeyError, ValueError):
            continue
        overlap = max(0.0, min(b, t_end) - max(a, t_start))
        if overlap > best_overlap:
            best, best_overlap = log, overlap
    if best is None or best_overlap < 0.5 * (t_end - t_start):
        return None
    return best


# What the pipeline takes from a log, per wheel where it has a wheel suffix.
WHEEL_CHANNELS = {
    "tyre_load": "Tyre Load",  # N
    "lat_ground_vel": "Lat Ground Vel",  # m/s, car frame
    "long_ground_vel": "Long Ground Vel",
    "lat_patch_vel": "Lat Patch Vel",
    "long_patch_vel": "Long Patch Vel",
}
CAR_CHANNELS = {
    "drag": "Drag",  # N
    "downforce_front": "Front Downforce",
    "downforce_rear": "Rear Downforce",
    "pitch": "Body Pitch",  # rad
    "roll": "Body Roll",
}


MAX_LAG_S = 1.5


def clock_offset(log: MotecLog, t: np.ndarray, speed_kmh: np.ndarray) -> float:
    """Seconds to add to the log's times so they match the recording's. DAM logs its
    session clock in whole seconds, so the start can be up to a second off; the lag that
    best lines up both logs' Ground Speed settles it."""
    if "Ground Speed" not in log.channels:
        return 0.0
    tl, vl = log.channel("Ground Speed")
    inside = (t > tl[0] + MAX_LAG_S) & (t < tl[-1] - MAX_LAG_S) & np.isfinite(speed_kmh)
    if inside.sum() < 50 or np.std(speed_kmh[inside]) < 1.0:
        return 0.0
    a = speed_kmh[inside]
    lags = np.arange(-MAX_LAG_S, MAX_LAG_S + 1e-9, 0.02)
    corr = [np.corrcoef(a, np.interp(t[inside] - lag, tl, vl))[0, 1] for lag in lags]
    return float(lags[int(np.nanargmax(corr))])


def resample(log: MotecLog, t: np.ndarray, offset: float = 0.0) -> dict[str, np.ndarray]:
    """Every channel the pipeline uses that the log has, interpolated onto ``t`` (session
    seconds; ``offset`` from ``clock_offset``). Per-wheel channels come back as (n, 4) in
    FL, FR, RL, RR order; NaN outside the log."""
    out: dict[str, np.ndarray] = {}
    for key, base in WHEEL_CHANNELS.items():
        names = [f"{base} {w}" for w in WHEELS]
        if all(n in log.channels for n in names):
            cols = []
            for n in names:
                tt, v = log.channel(n)
                cols.append(np.interp(t, tt + offset, v, left=np.nan, right=np.nan))
            out[key] = np.column_stack(cols)
    for key, name in CAR_CHANNELS.items():
        if name in log.channels:
            tt, v = log.channel(name)
            out[key] = np.interp(t, tt + offset, v, left=np.nan, right=np.nan)
    return out
