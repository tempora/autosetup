"""Read LMU's built-in telemetry recordings (UserData/Telemetry/*.duckdb).

File layout (as written by the game):
  * one table per channel; ``channelsList`` gives each fixed-rate channel's frequency;
  * fixed-rate tables have no timestamp column, only ``value`` or ``value1..value4``
    (four-wheel channels, ordered FL, FR, RL, RR);
  * event tables (Lap, TC, ABS, Gear, In Pits, ...) carry ``ts`` in session seconds and
    hold one row per change, the first row being the state when recording started;
  * ``GPS Time`` is the 100 Hz session clock that event ``ts`` values share;
  * ``metadata`` holds session info plus ``CarSetup``: JSON of every setup parameter.

Channel frequencies vary between game versions (older files log Lap Dist at 10 Hz), so
they are always read from the file, never assumed.

While an outing is running, LMU writes it through a DuckDB write-ahead log next to the
file (``<name>.duckdb.wal``) and keeps both locked. When you return to the garage it
merges the log into the file, deletes it and lets go. So a ``.wal`` beside a recording
means "still being driven".
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np

CLOCK_CHANNEL = "GPS Time"
WHEELS = ("FL", "FR", "RL", "RR")
COPY_RETRIES = 5
FRESH_S = 120.0  # a file modified this recently may be mid-write


class RecordingBusy(OSError):
    """LMU is still writing this recording: it's locked, half-written, or the stint is
    still running. Not an error; try again later."""


def wal_of(path: Path) -> Path:
    return path.with_name(path.name + ".wal")


def in_progress(path: Path) -> bool:
    """LMU is still writing this outing (its write-ahead log is still there)."""
    return wal_of(Path(path)).exists()


def _copy_when_free(path: Path, dst: Path) -> None:
    """Copy a recording, retrying while LMU holds its write lock (it locks the file in
    short bursts while flushing, so a copy a moment later usually works)."""
    delay = 0.2
    for attempt in range(COPY_RETRIES):
        try:
            shutil.copyfile(path, dst)
            return
        except PermissionError:
            if attempt == COPY_RETRIES - 1:
                raise RecordingBusy(f"{path.name} is locked by LMU (being written)") from None
            time.sleep(delay)
            delay *= 2


@dataclass(frozen=True)
class Lap:
    number: int
    t_start: float
    t_end: float
    complete: bool  # both ends are real start/finish crossings
    in_pits: bool  # car was in the pit lane at some point during the lap

    @property
    def time(self) -> float:
        return self.t_end - self.t_start


@dataclass
class Session:
    """An open telemetry recording. Use as a context manager, or call ``close()``."""

    path: Path
    metadata: dict[str, str]
    setup_json: dict
    frequencies: dict[str, int]
    units: dict[str, str]
    _con: duckdb.DuckDBPyConnection = field(repr=False)
    _tmp: Path | None = field(default=None, repr=False)
    _clock: np.ndarray | None = field(default=None, repr=False)

    # -- lifecycle ---------------------------------------------------------------
    @classmethod
    def open(cls, path: str | Path, copy: bool = True) -> "Session":
        """Open a recording. ``copy`` (default) reads from a temp copy so LMU's file is
        never locked or touched; a leftover write-ahead log (the game stopped mid-outing)
        is copied too and replayed into the copy, so its laps aren't lost."""
        path = Path(path)
        tmp = None
        src = path
        if copy:
            tmp = Path(tempfile.mkdtemp(prefix="autosetup_"))
            src = tmp / "session.duckdb"
            try:
                _copy_when_free(path, src)
                if wal_of(path).exists():
                    _copy_when_free(wal_of(path), wal_of(src))
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        con = None
        try:
            # The copy is ours, so it may be opened writable: that lets DuckDB replay a
            # copied log. LMU's own file is only ever opened read-only.
            con = duckdb.connect(str(src), read_only=not copy)
            metadata = dict(con.execute("select key, value from metadata").fetchall())
            rows = con.execute("select * from channelsList").fetchall()
            freqs = {r[0]: r[1] for r in rows}
            units = {r[0]: (r[2] if len(r) > 2 and r[2] is not None else "") for r in rows}
        except Exception as e:
            if con is not None:
                con.close()
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            if isinstance(e, PermissionError) or (path.exists() and time.time() - path.stat().st_mtime < FRESH_S):
                # A copy taken mid-write can be torn; a moment later it won't be.
                raise RecordingBusy(f"{path.name} is being written by LMU") from e
            raise
        setup_json = json.loads(metadata.pop("CarSetup", "{}") or "{}")
        return cls(path, metadata, setup_json, freqs, units, con, tmp)

    def close(self) -> None:
        self._con.close()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- metadata ----------------------------------------------------------------
    @property
    def track(self) -> str:
        return self.metadata.get("TrackName", "")

    @property
    def car_name(self) -> str:
        """Livery-level name, e.g. 'Iron Lynx 2024 #60' (the ':LM' suffix is dropped)."""
        return self.metadata.get("CarName", "").split(":")[0]

    @property
    def car_class(self) -> str:
        return self.metadata.get("CarClass", "")

    @property
    def session_type(self) -> str:
        return self.metadata.get("SessionType", "")

    @property
    def tables(self) -> set[str]:
        return {r[0] for r in self._con.execute("select table_name from duckdb_tables()").fetchall()}

    # -- time base ---------------------------------------------------------------
    @property
    def clock(self) -> np.ndarray:
        """Session time (s) of every sample of the clock channel."""
        if self._clock is None:
            self._clock = self._values(CLOCK_CHANNEL).astype(np.float64)
        return self._clock

    @property
    def t_start(self) -> float:
        return float(self.clock[0]) if len(self.clock) else 0.0

    @property
    def t_end(self) -> float:
        return float(self.clock[-1]) if len(self.clock) else 0.0

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start

    def _sample_times(self, n: int, freq: int) -> np.ndarray:
        """Map sample i of an n-sample channel at ``freq`` onto the session clock.

        Fixed-rate channels share the clock's start and are sampled in lockstep with it,
        so sample i sits at clock index i * clock_freq / freq. Interpolating along the
        clock (not t0 + i/f) keeps channels aligned across the rare clock gaps.
        """
        clock = self.clock
        if not len(clock):
            return np.zeros(n)
        clock_freq = self.frequencies.get(CLOCK_CHANNEL, 100)
        idx = np.arange(n, dtype=np.float64) * (clock_freq / freq)
        return np.interp(idx, np.arange(len(clock)), clock)

    # -- data access -------------------------------------------------------------
    def _columns(self, table: str) -> list[str]:
        rows = self._con.execute(
            "select column_name from information_schema.columns where table_name = ? order by ordinal_position",
            [table],
        ).fetchall()
        return [r[0] for r in rows]

    def _values(self, table: str) -> np.ndarray:
        cols = [c for c in self._columns(table) if c != "ts"]
        if not cols:
            raise KeyError(table)
        sel = ", ".join(f'"{c}"' for c in cols)
        rows = self._con.execute(f'select {sel} from "{table}"').fetchnumpy()
        arrays = [np.asarray(rows[c], dtype=np.float64) for c in cols]
        return arrays[0] if len(arrays) == 1 else np.column_stack(arrays)

    def channel(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """Fixed-rate channel → (t, values). Four-wheel channels return shape (n, 4)."""
        if name not in self.frequencies:
            raise KeyError(f"{name!r} is not a fixed-rate channel in {self.path.name}")
        values = self._values(name)
        return self._sample_times(len(values), self.frequencies[name]), values

    def events(self, name: str) -> tuple[np.ndarray, np.ndarray]:
        """Event channel → (ts, values), one row per change; row 0 is the initial state."""
        cols = self._columns(name)
        if "ts" not in cols:
            raise KeyError(f"{name!r} is not an event channel in {self.path.name}")
        val_cols = [c for c in cols if c != "ts"]
        sel = ", ".join(f'"{c}"' for c in ["ts", *val_cols])
        rows = self._con.execute(f'select {sel} from "{name}" order by ts').fetchnumpy()
        ts = np.asarray(rows["ts"], dtype=np.float64)
        arrays = [np.asarray(rows[c], dtype=np.float64) for c in val_cols]
        return ts, arrays[0] if len(arrays) == 1 else np.column_stack(arrays)

    def state_at(self, name: str, t: np.ndarray | float) -> np.ndarray:
        """Value of an event channel at time(s) t (last change at or before t)."""
        ts, values = self.events(name)
        i = np.searchsorted(ts, np.asarray(t, dtype=np.float64), side="right") - 1
        return values[np.clip(i, 0, len(values) - 1)]

    # -- laps --------------------------------------------------------------------
    def laps(self) -> list[Lap]:
        """Laps from the ``Lap`` event table.

        Row 0 is the lap counter's value at recording start, not a crossing, so the first
        segment (recording start → first crossing) is a partial lap, as is the last one
        (last crossing → recording end). Partial segments are returned with
        ``complete=False`` so callers can still use their data (e.g. for tyre wear).
        """
        if "Lap" not in self.tables or not len(self.clock):
            return []
        ts, nums = self.events("Lap")
        crossings = [(t, int(n)) for t, n in zip(ts[1:], nums[1:])]
        bounds = [(self.t_start, int(nums[0]), False)] + [(t, n, True) for t, n in crossings]
        pit_ts, pit_vals = self.events("In Pits") if "In Pits" in self.tables else (np.array([]), np.array([]))
        laps = []
        for i, (t0, number, real_start) in enumerate(bounds):
            if i + 1 < len(bounds):
                t1, real_end = bounds[i + 1][0], True
            else:
                t1, real_end = self.t_end, False
            if t1 <= t0:
                continue
            laps.append(Lap(number, t0, t1, real_start and real_end, _any_true(pit_ts, pit_vals, t0, t1)))
        return laps


def _any_true(ts: np.ndarray, values: np.ndarray, t0: float, t1: float) -> bool:
    """Whether an event channel is non-zero anywhere in [t0, t1)."""
    if not len(ts):
        return False
    i = np.searchsorted(ts, t0, side="right") - 1
    if i >= 0 and values[i]:
        return True
    inside = (ts >= t0) & (ts < t1)
    return bool(np.any(values[inside]))


def list_recordings(telemetry_dir: str | Path) -> list[Path]:
    return sorted(Path(telemetry_dir).glob("*.duckdb"), key=lambda p: p.stat().st_mtime)
