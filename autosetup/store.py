"""autosetup's own database: imported recordings, laps and their metrics, driver ratings,
and what we've learned about the install (which .svm template belongs to which car,
which folder belongs to which track).

LMU's recordings are never modified; each is copied, read, and summarised here.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np

from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import read_settings
from autosetup.lmu.telemetry import RecordingBusy, Session, in_progress, list_recordings
from autosetup.lmu.validate import match_recordings
from autosetup.metrics import METRICS_VERSION, compute_recording
from autosetup.metrics.corners import Corner, remap
from autosetup.metrics.observations import Observations, observe_recording
from autosetup.metrics.signals import load_signals

SCHEMA = """
create sequence if not exists recording_id;
create table if not exists recordings (
    id            integer primary key default nextval('recording_id'),
    file_name     varchar not null,
    file_size     bigint not null,
    file_mtime    double not null,
    recorded_at   varchar,
    track         varchar,
    track_layout  varchar,
    car_name      varchar,
    car_class     varchar,
    session_type  varchar,
    weather       varchar,
    car_signature varchar,
    setup_json    json,
    t_start       double,
    duration      double,
    imported_at   timestamp,
    unique (file_name, file_size)
);
create table if not exists laps (
    recording_id  integer not null,
    seq           integer not null,
    lap_number    integer,
    t_start       double,
    t_end         double,
    lap_time      double,
    complete      boolean,
    in_pits       boolean,
    primary key (recording_id, seq)
);
create table if not exists car_templates (
    car_signature varchar primary key,
    vehicle_class varchar,
    svm_path      varchar,
    score         double
);
create table if not exists car_classes (
    car_name      varchar primary key,
    vehicle_class varchar
);
create table if not exists track_folders (
    track         varchar primary key,
    folder        varchar,
    votes         integer
);
create table if not exists recording_setups (
    file_name     varchar primary key,  -- the saved .svm this recording was driven on
    svm_path      varchar,
    score         double
);
create table if not exists lap_metrics (
    recording_id  integer not null,
    seq           integer not null,
    name          varchar not null,
    value         double,
    primary key (recording_id, seq, name)
);
create table if not exists lap_sectors (
    recording_id  integer not null,
    seq           integer not null,
    sector        integer not null,
    time          double,
    primary key (recording_id, seq, sector)
);
-- Keyed by file name so a rating survives re-importing a recording that grew.
create table if not exists ratings (
    file_name     varchar primary key,
    rating        integer,              -- 1..5 overall confidence, null = not rated
    entry         integer,              -- -2 (oversteer) .. +2 (understeer)
    mid           integer,
    exit          integer,
    note          varchar,
    rated_at      timestamp
);
create table if not exists settings (
    key           varchar primary key,
    value         json
);
alter table recordings add column if not exists metrics_version integer;
alter table ratings add column if not exists lockups boolean;  -- you flagged too many lock-ups
alter table ratings add column if not exists slow integer;     -- corner-speed ratings, like the phases:
alter table ratings add column if not exists medium integer;   -- -2 .. +2 (- oversteer, + understeer)
alter table ratings add column if not exists fast integer;
create table if not exists zone_ratings (
    file_name     varchar,
    zone          varchar,               -- corner speed × phase, e.g. 'slow_exit'
    value         double,                -- rating points, -2 .. +2 in halves (- oversteer, + understeer)
    primary key (file_name, zone)
);
alter table recordings add column if not exists game_build varchar;   -- lmu/builds.py
alter table recordings add column if not exists dam_log varchar;      -- DAM .ld file merged in, if any
alter table recordings add column if not exists load_source varchar;  -- tyre_load (DAM) | spring
alter table recordings add column if not exists kerb_source varchar;  -- surface | spike
-- One corner map per layout, so passes from every recording share corner ids.
create table if not exists corners (
    layout        varchar,
    corner        integer,
    direction     integer,               -- +1 right, -1 left
    d_start       double,
    d_apex        double,
    d_end         double,
    v_apex        double,
    primary key (layout, corner)
);
-- metrics/balance.py: one row per lap, corner and phase.
create table if not exists corner_passes (
    recording_id  integer,
    seq           integer,
    corner        integer,
    phase         varchar,               -- entry | mid | exit
    band          varchar,               -- low | high
    n             integer,
    v_min         double,
    v_mean        double,
    a_lat         double,
    ua            double,                -- understeer angle, deg (+ understeer)
    ua_var        double,
    yaw_err       double,
    steer_rate    double,
    throttle_rate double,
    countersteer_s double,
    turnin_gain   double,
    slip_fr       double,
    kerb          boolean
);
-- metrics/suspension.py: per lap, wheel, kerb / off-kerb samples.
create table if not exists lap_suspension (
    recording_id  integer,
    seq           integer,
    wheel         varchar,
    kerb          boolean,
    n             integer,
    hist_lo       integer,               -- first stored bin of the fixed 5 mm/s grid
    hist          json,                  -- counts from hist_lo on
    stats         json
);
create table if not exists kerb_events (
    recording_id  integer,
    seq           integer,
    t0            double,
    t1            double,
    dist          double,
    wheels        varchar,
    source        varchar,
    peak_load     double,
    airtime_s     double,
    pitch_deg     double,
    heave_mm      double,
    yaw_dist      double
);
"""
OBSERVATION_TABLES = ("corner_passes", "lap_suspension", "kerb_events")


@dataclass
class ReadRecording:
    """A recording's laps and metrics, read and computed without touching the database."""

    path: Path
    size: int
    mtime: float
    columns: tuple  # recorded_at .. duration, in the recordings table's order
    laps: list
    results: list
    obs: Observations | None = None

    @property
    def layout(self) -> str:
        return self.columns[2] or self.columns[1] or ""


def read_recording(path: Path) -> ReadRecording | None:
    """Read and analyse a recording (the slow part of an import), or None when it holds no
    usable data. Doesn't need the store, so the server does it without holding its lock."""
    st = path.stat()
    with Session.open(path) as s:
        if not len(s.clock):
            return None
        setup = CarSetup.from_json(s.setup_json)
        laps = s.laps()
        results, obs = _analyse(s, laps, setup)
        md = s.metadata
        columns = (md.get("RecordingTime"), s.track, md.get("TrackLayout"), s.car_name, s.car_class, s.session_type,
                   md.get("WeatherConditions"), setup.signature if setup.params else None, json.dumps(s.setup_json),
                   s.t_start, s.duration)
    return ReadRecording(path, st.st_size, st.st_mtime, columns, laps, results, obs)


def _analyse(s: Session, laps: list, setup: CarSetup) -> tuple[list, Observations]:
    """Lap metrics and the models' observations, from one load of the signals."""
    if not laps:
        return [], Observations()
    sig = load_signals(s)
    return compute_recording(s, laps, setup, sig), observe_recording(s, sig, laps, setup)


def _num(x: float | None) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def default_db_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "autosetup"
    return base / "autosetup.duckdb"


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.path))
        self._owner = threading.get_ident()
        self._local = threading.local()
        self._con.execute(SCHEMA)

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """This thread's connection to the database. One DuckDB connection refuses a second
        call while one is running ("Connection is busy with another call"), and the server
        reads from request threads while the watcher imports, so every other thread gets
        its own cursor on the same database."""
        if threading.get_ident() == self._owner:
            return self._con
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = self._con.cursor()
        return con

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- import ------------------------------------------------------------------
    def has_recording(self, path: Path) -> bool:
        st = path.stat()
        row = self.con.execute(
            "select 1 from recordings where file_name = ? and file_size = ?", [path.name, st.st_size]
        ).fetchone()
        return row is not None

    def _delete_recording(self, rid: int) -> None:
        for table in ("lap_sectors", "lap_metrics", "laps", *OBSERVATION_TABLES):
            self.con.execute(f"delete from {table} where recording_id = ?", [rid])
        self.con.execute("delete from recordings where id = ?", [rid])

    def import_recording(self, path: str | Path, only_finished: bool = False,
                         game_running: bool = True) -> int | None:
        """Import one recording with its laps and metrics. Returns its id, or None if it was
        already imported or holds no usable data. A file that grew since its last import
        replaces the earlier import.

        ``only_finished``: raise RecordingBusy while LMU is still writing the outing (its
        write-ahead log exists and the game is running). A log left behind by a game that
        has exited is replayed instead."""
        path = Path(path)
        if only_finished and game_running and in_progress(path):
            raise RecordingBusy(f"{path.name}: outing still running")
        if self.has_recording(path):
            return None
        rec = read_recording(path)
        return self.save_recording(rec) if rec is not None else None

    def save_recording(self, rec: ReadRecording) -> int:
        """Store a recording read by ``read_recording``, replacing an earlier import of the
        same file. Only this part touches the database."""
        laps = rec.laps
        self.con.execute("begin")
        try:
            for (old,) in self.con.execute("select id from recordings where file_name = ?", [rec.path.name]).fetchall():
                self._delete_recording(old)
            rid = self.con.execute(
                """insert into recordings (file_name, file_size, file_mtime, recorded_at, track, track_layout,
                       car_name, car_class, session_type, weather, car_signature, setup_json, t_start,
                       duration, imported_at, metrics_version)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) returning id""",
                [rec.path.name, rec.size, rec.mtime, *rec.columns,
                 datetime.now(timezone.utc).replace(tzinfo=None), METRICS_VERSION],
            ).fetchone()[0]
            if laps:
                self.con.executemany(
                    "insert into laps values (?, ?, ?, ?, ?, ?, ?, ?)",
                    [[rid, i, lap.number, lap.t_start, lap.t_end, lap.time, lap.complete, lap.in_pits]
                     for i, lap in enumerate(laps)],
                )
            self._insert_metrics(rid, rec.results)
            if rec.obs is not None:
                self._insert_observations(rid, rec.layout, rec.obs)
            self.con.execute("commit")
        except Exception:
            self.con.execute("rollback")
            raise
        return rid

    def _insert_metrics(self, rid: int, results) -> None:
        # Bulk insert through unnest: executemany is row-by-row and slow in DuckDB.
        metric_rows = [(r.seq, k, v) for r in results for k, v in r.metrics.items()]
        sector_rows = [(r.seq, i, float(t)) for r in results for i, t in enumerate(r.sectors)]
        if metric_rows:
            seqs, names, values = map(list, zip(*metric_rows))
            self.con.execute(
                "insert into lap_metrics select $1, unnest($2::INTEGER[]), unnest($3::VARCHAR[]), "
                "unnest($4::DOUBLE[])", [rid, seqs, names, values])
        if sector_rows:
            seqs, sectors, times = map(list, zip(*sector_rows))
            self.con.execute(
                "insert into lap_sectors select $1, unnest($2::INTEGER[]), unnest($3::INTEGER[]), "
                "unnest($4::DOUBLE[])", [rid, seqs, sectors, times])

    def _insert(self, table: str, types: tuple[str, ...], rows: list[tuple]) -> None:
        """Many rows in one statement (executemany is row by row)."""
        if not rows:
            return
        cols = ", ".join(f"unnest(${i + 1}::{t}[])" for i, t in enumerate(types))
        self.con.execute(f"insert into {table} select {cols}", [list(c) for c in zip(*rows)])

    def corner_map(self, layout: str) -> list[Corner]:
        rows = self.con.execute(
            "select corner, direction, d_start, d_apex, d_end, v_apex from corners where layout = ? order by corner",
            [layout]).fetchall()
        return [Corner(*r) for r in rows]

    def canonical_corners(self, layout: str, found: list[Corner]) -> dict[int, int]:
        """This map's corner ids in the layout's map (the first recording's, if it's new)."""
        known = self.corner_map(layout)
        if not known:
            self._insert("corners", ("VARCHAR", "INTEGER", "INTEGER", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE"),
                         [(layout, c.id, c.direction, c.d_start, c.d_apex, c.d_end, c.v_apex) for c in found])
            return {c.id: c.id for c in found}
        return remap(found, known)

    def _insert_observations(self, rid: int, layout: str, obs: Observations) -> None:
        ids = self.canonical_corners(layout, obs.corners)
        self._insert("corner_passes", ("INTEGER", "INTEGER", "INTEGER", "VARCHAR", "VARCHAR", "INTEGER", "DOUBLE",
                                       "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE",
                                       "DOUBLE", "DOUBLE", "BOOLEAN"),
                     [(rid, seq, ids[o.corner], o.phase, o.band, o.n, o.v_min, o.v_mean, o.a_lat, o.ua, o.ua_var,
                       _num(o.yaw_err), o.steer_rate, o.throttle_rate, o.countersteer_s, o.turnin_gain, o.slip_fr,
                       o.kerb) for seq, o in obs.passes if o.corner in ids])
        rows = []
        for ls in obs.suspension:
            nz = np.flatnonzero(ls.hist)
            lo, hi = (int(nz[0]), int(nz[-1]) + 1) if len(nz) else (0, 0)
            rows.append((rid, ls.seq, ls.wheel, ls.kerb, ls.n, lo, json.dumps(ls.hist[lo:hi].tolist()),
                         json.dumps(ls.stats)))
        self._insert("lap_suspension", ("INTEGER", "INTEGER", "VARCHAR", "BOOLEAN", "INTEGER", "INTEGER", "JSON",
                                        "JSON"), rows)
        self._insert("kerb_events", ("INTEGER", "INTEGER", "DOUBLE", "DOUBLE", "DOUBLE", "VARCHAR", "VARCHAR",
                                     "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE"),
                     [(rid, seq, e.t0, e.t1, e.dist, e.wheels, e.source, e.peak_load, e.airtime_s, e.pitch_deg,
                       e.heave_mm, e.yaw_dist) for seq, e in obs.kerbs])
        self.con.execute("update recordings set dam_log = ?, load_source = ?, kerb_source = ? where id = ?",
                         [obs.dam, obs.load_source, obs.kerb_source, rid])

    def recompute_metrics(self, telemetry_dir: Path, force: bool = False,
                          lock: threading.RLock | None = None) -> tuple[int, list[str]]:
        """Recompute metrics for recordings summarised by an older METRICS_VERSION (or all,
        with ``force``). Recordings whose file is gone keep their old metrics. ``lock``:
        held only while writing each recording, so a server keeps answering meanwhile."""
        lock = lock or contextlib.nullcontext()
        with lock:
            rows = self.con.execute(
                "select id, file_name from recordings where ? or metrics_version is distinct from ?",
                [force, METRICS_VERSION],
            ).fetchall()
        done, missing = 0, []
        for rid, name in rows:
            f = telemetry_dir / name
            if not f.is_file():
                missing.append(name)
                continue
            if in_progress(f):
                continue  # LMU is writing it; it's re-imported when the outing ends
            try:
                with Session.open(f) as s:
                    results, obs = _analyse(s, s.laps(), CarSetup.from_json(s.setup_json))
                    layout = s.metadata.get("TrackLayout") or s.track
            except RecordingBusy:
                continue
            with lock:
                self.con.execute("begin")
                try:
                    for table in ("lap_metrics", "lap_sectors", *OBSERVATION_TABLES):
                        self.con.execute(f"delete from {table} where recording_id = ?", [rid])
                    self._insert_metrics(rid, results)
                    self._insert_observations(rid, layout, obs)
                    self.con.execute("update recordings set metrics_version = ? where id = ?", [METRICS_VERSION, rid])
                    self.con.execute("commit")
                except Exception:
                    self.con.execute("rollback")
                    raise
            done += 1
        return done, missing

    def import_all(self, telemetry_dir: Path, only_finished: bool = True, game_running: bool | None = None
                   ) -> tuple[list[int], list[tuple[Path, str]], list[Path]]:
        """Import every new recording; returns (new ids, [(file, error)], [files LMU is
        still writing])."""
        if game_running is None:
            from autosetup.lmu.paths import game_running as _running
            game_running = _running()
        added, errors, busy = [], [], []
        for f in list_recordings(telemetry_dir):
            try:
                rid = self.import_recording(f, only_finished=only_finished, game_running=game_running)
                if rid is not None:
                    added.append(rid)
            except RecordingBusy:
                busy.append(f)
            except Exception as e:  # one broken file must not stop the rest
                errors.append((f, f"{type(e).__name__}: {e}"))
        return added, errors, busy

    # -- learned install knowledge -------------------------------------------------
    def learn_links(self, telemetry_dir: Path, settings_dir: Path, min_score: float = 0.85,
                    only: list[str] | None = None) -> int:
        """Match recordings to saved setups; remember a template .svm per car model and the
        Settings folder per track. ``only``: match just these recordings (file names, e.g.
        new imports) and keep earlier matches; matching everything again after every
        import took longer with every stint. The templates and folders are rebuilt from
        every stored match either way. Returns the number of matches made."""
        svms = read_settings(settings_dir)
        # Setups come from the store (saved at import), not by reopening every recording.
        sql, params = "select file_name, setup_json, track, car_name from recordings", []
        if only is not None:
            if not only:
                return 0
            sql, params = sql + " where file_name in (select unnest(?::VARCHAR[]))", [list(only)]
        recordings = ((telemetry_dir / name, CarSetup.from_json(json.loads(sj)), track, car)
                      for name, sj, track, car in self.con.execute(sql, params).fetchall())
        matches = [m for m, _, _ in match_recordings(recordings, svms, min_score)]
        self.con.execute("begin")
        try:
            self._upsert("recording_setups", ("VARCHAR", "VARCHAR", "DOUBLE"),
                         [(m.recording.name, str(m.svm), m.score) for m in matches])
            best_per_car: dict[str, tuple[Path, float, str]] = {}
            classes: dict[str, str] = {}
            votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
            for svm_path, score, sig, car, track in self.con.execute(
                    """select s.svm_path, s.score, r.car_signature, r.car_name, r.track
                       from recording_setups s join recordings r on r.file_name = s.file_name
                       order by r.file_mtime""").fetchall():
                path = Path(svm_path)
                svm = svms.get(path)
                if svm is None:  # deleted since
                    continue
                cur = best_per_car.get(sig)
                if sig and (cur is None or (score, path.stat().st_mtime) > (cur[1], cur[0].stat().st_mtime)):
                    best_per_car[sig] = (path, score, svm.vehicle_class)
                classes[car] = svm.vehicle_class  # the latest recording of a livery wins
                votes[track][path.parent.name] += 1
            self._upsert("car_templates", ("VARCHAR", "VARCHAR", "VARCHAR", "DOUBLE"),
                         [(sig, cls, str(path), score) for sig, (path, score, cls) in best_per_car.items()])
            self._upsert("car_classes", ("VARCHAR", "VARCHAR"), list(classes.items()))
            self._upsert("track_folders", ("VARCHAR", "VARCHAR", "INTEGER"),
                         [(track, *c.most_common(1)[0]) for track, c in votes.items()])
            self.con.execute("commit")
        except Exception:
            self.con.execute("rollback")
            raise
        return len(matches)

    def _upsert(self, table: str, types: tuple[str, ...], rows: list[tuple]) -> None:
        """``insert or replace`` of many rows in one statement (executemany is row by row)."""
        if not rows:
            return
        cols = ", ".join(f"unnest(${i + 1}::{t}[])" for i, t in enumerate(types))
        self.con.execute(f"insert or replace into {table} select {cols}", [list(c) for c in zip(*rows)])

    def template_for(self, car_signature: str, car_name: str | None = None,
                     settings_dir: Path | None = None) -> Path | None:
        """A saved .svm to take the file header from.

        Prefer one matched to this exact car model. Game updates change the parameter
        ranges and so the signature; then fall back to the newest saved setup of the
        vehicle class this livery was last seen with (every setting is overwritten from
        the recording anyway, and the writer range-checks each one).
        """
        row = self.con.execute(
            "select svm_path from car_templates where car_signature = ?", [car_signature]
        ).fetchone()
        if row and Path(row[0]).is_file():
            return Path(row[0])
        if not (car_name and settings_dir):
            return None
        row = self.con.execute("select vehicle_class from car_classes where car_name = ?", [car_name]).fetchone()
        if not row:
            return None
        from autosetup.lmu.svm import SvmFile

        same_class = [f for f in settings_dir.glob("*/*.svm") if SvmFile.read(f).vehicle_class == row[0]]
        return max(same_class, key=lambda f: f.stat().st_mtime, default=None)

    def folder_for(self, track: str) -> str | None:
        row = self.con.execute("select folder from track_folders where track = ?", [track]).fetchone()
        return row[0] if row else None

    # -- ratings and settings --------------------------------------------------------
    def set_rating(self, file_name: str, rating: int | None = None, entry: int | None = None,
                   mid: int | None = None, exit: int | None = None, note: str | None = None,
                   lockups: bool | None = None, slow: int | None = None, medium: int | None = None,
                   fast: int | None = None) -> None:
        for name, v, lo, hi in (("rating", rating, 1, 5), ("entry", entry, -2, 2), ("mid", mid, -2, 2), ("exit", exit, -2, 2),
                                ("slow", slow, -2, 2), ("medium", medium, -2, 2), ("fast", fast, -2, 2)):
            if v is not None and not lo <= v <= hi:
                raise ValueError(f"{name} must be {lo}..{hi}")
        self.con.execute(
            """insert or replace into ratings (file_name, rating, entry, mid, "exit", note, rated_at, lockups,
                                               slow, medium, fast)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [file_name, rating, entry, mid, exit, note, datetime.now(timezone.utc).replace(tzinfo=None), lockups,
             slow, medium, fast],
        )

    def set_zone_rating(self, file_name: str, zone: str, value: float | None) -> None:
        """Your rating of one zone (corner speed × phase) of a stint: rating points, -2 .. +2
        in halves; None clears it."""
        if value is not None and not (-2 <= value <= 2 and float(value * 2).is_integer()):
            raise ValueError(f"{zone} must be -2 .. 2 in steps of 0.5")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if self.con.execute("select 1 from ratings where file_name = ?", [file_name]).fetchone():
            self.con.execute("update ratings set rated_at = ? where file_name = ?", [now, file_name])
        else:
            self.con.execute("insert into ratings (file_name, rated_at) values (?, ?)", [file_name, now])
        self.con.execute("delete from zone_ratings where file_name = ? and zone = ?", [file_name, zone])
        if value is not None:
            self.con.execute("insert into zone_ratings values (?, ?, ?)", [file_name, zone, float(value)])

    def rating(self, file_name: str) -> dict | None:
        """The stint's ratings, its zone ratings included (keyed by zone)."""
        cur = self.con.execute("select * from ratings where file_name = ?", [file_name])
        row = cur.fetchone()
        if not row:
            return None
        out = dict(zip([d[0] for d in cur.description], row))
        out |= dict(self.con.execute("select zone, value from zone_ratings where file_name = ?", [file_name]).fetchall())
        return out

    def get_setting(self, key: str, default=None):
        row = self.con.execute("select value from settings where key = ?", [key]).fetchone()
        return json.loads(row[0]) if row else default

    def put_setting(self, key: str, value) -> None:
        self.con.execute("insert or replace into settings values (?, ?)", [key, json.dumps(value)])

    # -- queries -------------------------------------------------------------------
    def recordings(self) -> list[dict]:
        cur = self.con.execute(
            """select r.id, r.file_name, r.track, r.car_name, r.car_class, r.session_type, r.car_signature,
                      r.duration, count(l.seq) filter (where l.complete and not l.in_pits) as clean_laps,
                      min(l.lap_time) filter (where l.complete and not l.in_pits) as best_lap
               from recordings r left join laps l on l.recording_id = r.id
               group by all order by any_value(r.file_mtime)"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def setup_of(self, recording_id: int) -> CarSetup:
        row = self.con.execute("select setup_json from recordings where id = ?", [recording_id]).fetchone()
        if row is None:
            raise KeyError(recording_id)
        return CarSetup.from_json(json.loads(row[0]))

    def recording(self, recording_id: int) -> dict:
        cur = self.con.execute("select * exclude (setup_json) from recordings where id = ?", [recording_id])
        row = cur.fetchone()
        if row is None:
            raise KeyError(recording_id)
        return dict(zip([d[0] for d in cur.description], row))
