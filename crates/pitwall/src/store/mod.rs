/* pitwall's own database (DuckDB, %LOCALAPPDATA%\autosetup\pitwall.duckdb):
   imported stints with their analysed metrics, what the driver said about
   each stint (rating, feel sliders), parameter locks per car and layout,
   locked balance fingerprints, setups written to the garage, app settings,
   and every game build seen.
   A stint is keyed by its LMU file name (unique per outing; it embeds the
   UTC start).  A file that grew is re-imported and replaces its row; what
   the driver said about it is keyed by the same name and survives.  An
   unusable recording is stored with its reason, so it is not analysed again
   at the same size.  Metrics are cached as JSON with METRICS_VERSION; a
   different version is stale and re-analysed from the recording.  Times are
   Unix seconds, UTC.  LMU's files are only ever read through a private copy
   (DuckDbRecording::open_copy).
   plan_for assembles the planner's input for a stint from the store.  A
   stint without a flying lap is asked about: unless the driver said it was
   fine, the proposal is built from the latest earlier stint of the car and
   layout with one, and does not repeat the short stint's moves.  */

pub mod watch;

#[cfg(test)]
mod tests;

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use ::duckdb::{params, Connection, OptionalExt};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::chassis::CarClass;
use crate::lmu::builds::{BuildHistory, GameBuild};
use crate::lmu::writer::Written;
use crate::metrics::balance::CellStats;
use crate::metrics::{analyse_stint, StintMetrics, METRICS_VERSION};
use crate::opt::dampers::DamperStint;
use crate::opt::solve::{AeroTrim, DriverFlags, Toggles};
use crate::opt::target::Sliders;
use crate::opt::{plan, PlanInput, Proposal};
use crate::physics::calibration::model_for_car;
use crate::setup::CarSetup;
use crate::telemetry::duckdb::DuckDbRecording;
use crate::telemetry::{SessionMeta, TelemetryError, TelemetrySource};

pub const STORE_FILE: &str = "pitwall.duckdb";

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("store: {0}")]
    Db(#[from] ::duckdb::Error),
    #[error("store: {0}")]
    Json(#[from] serde_json::Error),
    #[error("store: {0}")]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Telemetry(#[from] TelemetryError),
}

const SCHEMA: &str = "
create table if not exists recordings (
    file_name       varchar primary key,
    file_size       bigint not null,
    file_mtime      bigint not null,
    recorded_at     bigint,
    track           varchar not null,
    layout          varchar not null,
    car_name        varchar not null,
    car_class       varchar not null,
    session_type    varchar not null,
    signature       varchar not null,
    setup           varchar not null,   -- CarSetup JSON
    metrics_version integer not null,
    metrics         varchar,            -- StintMetrics JSON; null when unusable
    error           varchar,            -- why it is unusable
    imported_at     bigint not null
);
create table if not exists game_builds (
    id              varchar primary key,
    installed_at    bigint not null
);
create table if not exists ratings (
    file_name       varchar primary key,
    note            varchar,
    lockups         boolean not null,
    undrivable      boolean,            -- null: not answered
    kerbs_unstable  boolean not null,
    aero_trim       varchar not null,   -- AeroTrim JSON
    rated_at        bigint not null
);
create table if not exists sliders (
    file_name       varchar primary key,
    front           double not null,
    rear            double not null,
    fast            double not null
);
create table if not exists locks (
    signature       varchar not null,
    layout          varchar not null,
    key             varchar not null,
    primary key (signature, layout, key)
);
create sequence if not exists fingerprint_id;
create table if not exists fingerprints (
    id              integer primary key default nextval('fingerprint_id'),
    signature       varchar not null,
    car_name        varchar not null,
    layout          varchar not null,
    file_name       varchar not null,
    cells           varchar not null,   -- off-kerb CellStats JSON as measured when locked
    locked_at       bigint not null,
    active          boolean not null
);
create sequence if not exists proposal_id;
create table if not exists proposals (
    id              integer primary key default nextval('proposal_id'),
    file_name       varchar not null,   -- the stint it was built from
    signature       varchar not null,
    layout          varchar not null,
    path            varchar not null,
    changes         varchar not null,   -- [(key, from, to)] JSON
    setup           varchar not null,   -- the written CarSetup JSON
    written_at      bigint not null
);
create table if not exists settings (
    key             varchar primary key,
    value           varchar not null    -- JSON
);
";

pub fn default_path() -> PathBuf {
    let base = std::env::var_os("LOCALAPPDATA").or_else(|| std::env::var_os("HOME")).map_or_else(|| PathBuf::from("."), PathBuf::from);
    base.join("autosetup").join(STORE_FILE)
}

pub fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64)
}

// One recording as stored: what it is and, when usable, its metrics.
#[derive(Debug, Clone)]
pub struct StoredStint {
    pub file_name: String,
    pub file_size: u64,
    pub file_mtime: i64,
    pub meta: SessionMeta,
    pub signature: String,
    // Err: why the recording can't be used.
    pub metrics: Result<StintMetrics, String>,
}

impl StoredStint {
    pub fn class(&self) -> CarClass {
        CarClass::parse(&self.meta.car_class)
    }

    pub fn usable(&self) -> Option<&StintMetrics> {
        self.metrics.as_ref().ok()
    }
}

/* Reads and analyses one recording from a private copy; the slow part of an
   import, done without the store.  Err only for a recording that may still
   be readable later (locked by LMU, gone, unreadable file system); a
   recording that reads but can't be analysed comes back unusable.  */
pub fn analyse_file(path: &Path) -> Result<StoredStint, StoreError> {
    let stat = std::fs::metadata(path)?;
    let file_mtime = stat.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0, |d| d.as_secs() as i64);
    let file_name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let unusable = |meta: SessionMeta, why: String| StoredStint {
        file_name: file_name.clone(),
        file_size: stat.len(),
        file_mtime,
        signature: meta.setup.signature(),
        meta,
        metrics: Err(why),
    };
    let rec = match DuckDbRecording::open_copy(path) {
        Ok(r) => r,
        Err(e @ TelemetryError::Busy(_)) => return Err(e.into()),
        Err(e) => return Ok(unusable(empty_meta(), e.to_string())),
    };
    let meta = match rec.metadata() {
        Ok(m) => m,
        Err(e) => return Ok(unusable(empty_meta(), e.to_string())),
    };
    Ok(match analyse_stint(&rec) {
        Ok(m) => StoredStint { file_name, file_size: stat.len(), file_mtime, signature: meta.setup.signature(), meta, metrics: Ok(m) },
        Err(e) => unusable(meta, e.to_string()),
    })
}

fn empty_meta() -> SessionMeta {
    SessionMeta {
        track: String::new(),
        layout: String::new(),
        car_name: String::new(),
        car_class: String::new(),
        session_type: String::new(),
        recorded_at: None,
        setup: CarSetup::default(),
    }
}

// What the driver said about a stint.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rating {
    pub note: Option<String>,
    pub lockups: bool,
    // About a stint without a flying lap: Some(true) couldn't drive it, Some(false) it was fine.
    pub undrivable: Option<bool>,
    pub kerbs_unstable: bool,
    pub aero_trim: AeroTrim,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Fingerprint {
    pub id: i64,
    pub signature: String,
    pub car_name: String,
    pub layout: String,
    pub file_name: String,
    pub cells: Vec<CellStats>,
    pub locked_at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WrittenSetup {
    pub id: i64,
    pub file_name: String,
    pub path: PathBuf,
    pub changes: Vec<(String, i32, i32)>,
    pub setup_json: String,
    pub written_at: i64,
}

#[derive(Debug, Clone)]
pub struct Planned {
    pub base: StoredStint,
    // A short stint the proposal steps back from.
    pub undone: Option<StoredStint>,
    pub proposal: Proposal,
}

const STINT_COLUMNS: &str =
    "file_name, file_size, file_mtime, recorded_at, track, layout, car_name, car_class, session_type, signature, setup, metrics, error";

pub struct Store {
    conn: Connection,
}

impl Store {
    pub fn open(path: &Path) -> Result<Self, StoreError> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let conn = Connection::open(path)?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self { conn })
    }

    // -- stints --------------------------------------------------------------

    // Every stored recording's size by file name, usable or not.
    pub fn imported(&self) -> Result<HashMap<String, u64>, StoreError> {
        let mut stmt = self.conn.prepare("select file_name, file_size from recordings")?;
        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)? as u64)))?;
        Ok(rows.collect::<Result<_, _>>()?)
    }

    // Inserts or replaces the stint by file name.
    pub fn save(&self, s: &StoredStint) -> Result<(), StoreError> {
        let (metrics, error) = match &s.metrics {
            Ok(m) => (Some(serde_json::to_string(m)?), None),
            Err(e) => (None, Some(e.as_str())),
        };
        self.conn.execute(
            &format!("insert or replace into recordings ({STINT_COLUMNS}, metrics_version, imported_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"),
            params![
                s.file_name,
                s.file_size as i64,
                s.file_mtime,
                s.meta.recorded_at,
                s.meta.track,
                s.meta.layout,
                s.meta.car_name,
                s.meta.car_class,
                s.meta.session_type,
                s.signature,
                serde_json::to_string(&s.meta.setup)?,
                metrics,
                error,
                METRICS_VERSION,
                now(),
            ],
        )?;
        Ok(())
    }

    pub fn import(&self, path: &Path) -> Result<StoredStint, StoreError> {
        let s = analyse_file(path)?;
        self.save(&s)?;
        Ok(s)
    }

    pub fn stint(&self, file_name: &str) -> Result<Option<StoredStint>, StoreError> {
        self.query_stints("where file_name = ?", &[file_name]).map(|v| v.into_iter().next())
    }

    // Usable stints of one car model and layout, oldest first.
    pub fn stints_of(&self, signature: &str, layout: &str) -> Result<Vec<StoredStint>, StoreError> {
        self.query_stints("where metrics is not null and signature = ? and layout = ?", &[signature, layout])
    }

    // Every usable stint, oldest first.
    pub fn usable_stints(&self) -> Result<Vec<StoredStint>, StoreError> {
        self.query_stints("where metrics is not null", &[])
    }

    // Recordings analysed by another METRICS_VERSION, newest first.
    pub fn stale(&self) -> Result<Vec<String>, StoreError> {
        let mut stmt = self.conn.prepare("select file_name from recordings where metrics_version <> ? order by recorded_at desc nulls last, file_name desc")?;
        let rows = stmt.query_map([METRICS_VERSION], |r| r.get::<_, String>(0))?;
        Ok(rows.collect::<Result<_, _>>()?)
    }

    /* Re-analyses every stale recording still in `telemetry_dir` and not
       being written (no log beside it); returns those refreshed.  A gone
       recording keeps its old metrics.  */
    pub fn refresh_stale(&self, telemetry_dir: &Path) -> Result<Vec<String>, StoreError> {
        let mut done = Vec::new();
        for name in self.stale()? {
            let path = telemetry_dir.join(&name);
            if !path.is_file() || crate::telemetry::duckdb::wal_of(&path).exists() {
                continue;
            }
            match self.import(&path) {
                Ok(_) => done.push(name),
                Err(StoreError::Telemetry(TelemetryError::Busy(_))) => {}
                Err(e) => return Err(e),
            }
        }
        Ok(done)
    }

    fn query_stints(&self, filter: &str, args: &[&str]) -> Result<Vec<StoredStint>, StoreError> {
        let mut stmt = self.conn.prepare(&format!(
            "select {STINT_COLUMNS} from recordings {filter} order by recorded_at nulls last, file_name"
        ))?;
        let rows = stmt.query_map(::duckdb::params_from_iter(args), |r| {
            Ok((
                (r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, i64>(2)?, r.get::<_, Option<i64>>(3)?),
                (r.get::<_, String>(4)?, r.get::<_, String>(5)?, r.get::<_, String>(6)?, r.get::<_, String>(7)?, r.get::<_, String>(8)?),
                (r.get::<_, String>(9)?, r.get::<_, String>(10)?, r.get::<_, Option<String>>(11)?, r.get::<_, Option<String>>(12)?),
            ))
        })?;
        rows.map(|row| {
            let ((file_name, size, file_mtime, recorded_at), (track, layout, car_name, car_class, session_type), (signature, setup, metrics, error)) = row?;
            let metrics = match metrics {
                Some(json) => Ok(serde_json::from_str(&json)?),
                None => Err(error.unwrap_or_default()),
            };
            Ok(StoredStint {
                file_name,
                file_size: size as u64,
                file_mtime,
                meta: SessionMeta { track, layout, car_name, car_class, session_type, recorded_at, setup: serde_json::from_str(&setup)? },
                signature,
                metrics,
            })
        })
        .collect()
    }

    // -- game builds ---------------------------------------------------------

    pub fn note_build(&self, b: &GameBuild) -> Result<(), StoreError> {
        self.conn.execute("insert or ignore into game_builds values (?, ?)", params![b.id, b.installed_at])?;
        Ok(())
    }

    pub fn builds(&self) -> Result<BuildHistory, StoreError> {
        let mut stmt = self.conn.prepare("select id, installed_at from game_builds")?;
        let rows = stmt.query_map([], |r| Ok(GameBuild { id: r.get(0)?, installed_at: r.get(1)? }))?;
        Ok(BuildHistory::new(rows.collect::<Result<Vec<_>, _>>()?))
    }

    // -- what the driver said ------------------------------------------------

    pub fn set_rating(&self, file_name: &str, r: &Rating) -> Result<(), StoreError> {
        self.conn.execute(
            "insert or replace into ratings values (?, ?, ?, ?, ?, ?, ?)",
            params![file_name, r.note, r.lockups, r.undrivable, r.kerbs_unstable, serde_json::to_string(&r.aero_trim)?, now()],
        )?;
        Ok(())
    }

    pub fn rating(&self, file_name: &str) -> Result<Option<Rating>, StoreError> {
        let row = self
            .conn
            .query_row(
                "select note, lockups, undrivable, kerbs_unstable, aero_trim from ratings where file_name = ?",
                [file_name],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get::<_, String>(4)?)),
            )
            .optional()?;
        row.map(|(note, lockups, undrivable, kerbs_unstable, trim)| {
            Ok(Rating { note, lockups, undrivable, kerbs_unstable, aero_trim: serde_json::from_str(&trim)? })
        })
        .transpose()
    }

    pub fn set_sliders(&self, file_name: &str, s: Sliders) -> Result<(), StoreError> {
        self.conn.execute(
            "insert or replace into sliders values (?, ?, ?, ?)",
            params![file_name, s.front.raw(), s.rear.raw(), s.fast.raw()],
        )?;
        Ok(())
    }

    // The sliders set on a stint; centred when none are.
    pub fn sliders(&self, file_name: &str) -> Result<Sliders, StoreError> {
        use crate::units::Degrees;
        let row = self
            .conn
            .query_row("select front, rear, fast from sliders where file_name = ?", [file_name], |r| {
                Ok((r.get::<_, f64>(0)?, r.get::<_, f64>(1)?, r.get::<_, f64>(2)?))
            })
            .optional()?;
        Ok(row.map_or_else(Sliders::default, |(f, r, s)| Sliders { front: Degrees::new(f), rear: Degrees::new(r), fast: Degrees::new(s) }))
    }

    // -- locks ---------------------------------------------------------------

    pub fn locks(&self, signature: &str, layout: &str) -> Result<BTreeSet<String>, StoreError> {
        let mut stmt = self.conn.prepare("select key from locks where signature = ? and layout = ?")?;
        let rows = stmt.query_map([signature, layout], |r| r.get::<_, String>(0))?;
        Ok(rows.collect::<Result<_, _>>()?)
    }

    pub fn set_lock(&self, signature: &str, layout: &str, key: &str, locked: bool) -> Result<(), StoreError> {
        let sql = if locked {
            "insert or ignore into locks values (?, ?, ?)"
        } else {
            "delete from locks where signature = ? and layout = ? and key = ?"
        };
        self.conn.execute(sql, [signature, layout, key])?;
        Ok(())
    }

    // -- fingerprints --------------------------------------------------------

    /* Locks the stint's off-kerb balance as its car model's fingerprint, as
       measured now; any earlier one of the car model is retired.  */
    pub fn lock_fingerprint(&self, stint: &StoredStint) -> Result<Option<i64>, StoreError> {
        let Some(m) = stint.usable() else { return Ok(None) };
        self.conn.execute("update fingerprints set active = false where signature = ?", [&stint.signature])?;
        let id = self.conn.query_row(
            "insert into fingerprints (signature, car_name, layout, file_name, cells, locked_at, active)
             values (?, ?, ?, ?, ?, ?, true) returning id",
            params![stint.signature, stint.meta.car_name, stint.meta.layout, stint.file_name, serde_json::to_string(&m.cells_off_kerb())?, now()],
            |r| r.get::<_, i64>(0),
        )?;
        Ok(Some(id))
    }

    pub fn fingerprint(&self, signature: &str) -> Result<Option<Fingerprint>, StoreError> {
        let row = self
            .conn
            .query_row(
                "select id, signature, car_name, layout, file_name, cells, locked_at from fingerprints
                 where signature = ? and active order by id desc limit 1",
                [signature],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get::<_, String>(5)?, r.get(6)?)),
            )
            .optional()?;
        row.map(|(id, signature, car_name, layout, file_name, cells, locked_at)| {
            Ok(Fingerprint { id, signature, car_name, layout, file_name, cells: serde_json::from_str(&cells)?, locked_at })
        })
        .transpose()
    }

    // Retires the car model's fingerprint; false when it had none.
    pub fn forget_fingerprint(&self, signature: &str) -> Result<bool, StoreError> {
        Ok(self.conn.execute("update fingerprints set active = false where signature = ? and active", [signature])? > 0)
    }

    // -- planning ------------------------------------------------------------

    /* (the stint a proposal is built from, the short stint it must not
       repeat) for `stint`, given the car and layout's stints up to it.  */
    fn base_of(&self, stint: StoredStint, earlier: &[StoredStint]) -> Result<(StoredStint, Option<StoredStint>), StoreError> {
        let flying = |s: &StoredStint| s.usable().is_some_and(|m| m.laps.iter().any(|l| l.is_flying()));
        if flying(&stint) || self.rating(&stint.file_name)?.and_then(|r| r.undrivable) == Some(false) {
            return Ok((stint, None));
        }
        Ok(match earlier.iter().rev().find(|s| flying(s)) {
            Some(good) => (good.clone(), Some(stint)),
            None => (stint, None),
        })
    }

    /* The proposal for `file_name` with everything the store knows: the
       driver's rating and sliders on that stint, the car and layout's locks
       and damper history, the car model's fingerprint, and its calibration.
       None for an unknown or unusable stint.  */
    pub fn plan_for(&self, file_name: &str, toggles: Toggles) -> Result<Option<Planned>, StoreError> {
        let Some(stint) = self.stint(file_name)?.filter(|s| s.usable().is_some()) else { return Ok(None) };
        let history = self.stints_of(&stint.signature, &stint.meta.layout)?;
        let upto = history.iter().position(|s| s.file_name == stint.file_name).unwrap_or(history.len());
        let (base, undone) = self.base_of(stint, &history[..upto])?;
        let until = history.iter().position(|s| s.file_name == base.file_name).map_or(history.len(), |i| i + 1);
        let damper_history: Vec<DamperStint> = history[..until]
            .iter()
            .filter_map(|s| s.usable().map(|m| DamperStint::new(s.meta.setup.clone(), m)))
            .collect();
        let rating = self.rating(file_name)?.unwrap_or_default();
        let flags = DriverFlags {
            kerbs_unstable: rating.kerbs_unstable,
            lockups: rating.lockups,
            aero_trim: rating.aero_trim,
            undrivable: undone.as_ref().map(|s| s.meta.setup.clone()),
        };
        let fingerprint = self.fingerprint(&base.signature)?;
        let m = base.usable().expect("base stints are usable");
        let cells = m.cells_off_kerb();
        let (model, verification) = model_for_car(m.class, &base.signature);
        let proposal = plan(&model, &PlanInput {
            class: m.class,
            setup: &base.meta.setup,
            cells: &cells,
            summary: &m.summary,
            damper_history: &damper_history,
            fingerprint: fingerprint.as_ref().map(|f| f.cells.as_slice()),
            sliders: self.sliders(file_name)?,
            flags,
            toggles,
            locks: self.locks(&base.signature, &base.meta.layout)?,
            verification,
        });
        Ok(Some(Planned { base, undone, proposal }))
    }

    // -- written setups ------------------------------------------------------

    pub fn record_written(&self, stint: &StoredStint, written: &Written, setup: &CarSetup) -> Result<i64, StoreError> {
        Ok(self.conn.query_row(
            "insert into proposals (file_name, signature, layout, path, changes, setup, written_at)
             values (?, ?, ?, ?, ?, ?, ?) returning id",
            params![
                stint.file_name,
                stint.signature,
                stint.meta.layout,
                written.path.to_string_lossy(),
                serde_json::to_string(&written.changes)?,
                serde_json::to_string(setup)?,
                now()
            ],
            |r| r.get(0),
        )?)
    }

    // Setups written from a stint, oldest first.
    pub fn written_from(&self, file_name: &str) -> Result<Vec<WrittenSetup>, StoreError> {
        let mut stmt = self.conn.prepare("select id, file_name, path, changes, setup, written_at from proposals where file_name = ? order by id")?;
        let rows = stmt.query_map([file_name], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?, r.get(4)?, r.get(5)?))
        })?;
        rows.map(|row| {
            let (id, file_name, path, changes, setup_json, written_at) = row?;
            Ok(WrittenSetup { id, file_name, path: PathBuf::from(path), changes: serde_json::from_str(&changes)?, setup_json, written_at })
        })
        .collect()
    }

    // -- settings ------------------------------------------------------------

    pub fn setting<T: DeserializeOwned>(&self, key: &str) -> Result<Option<T>, StoreError> {
        let row = self.conn.query_row("select value from settings where key = ?", [key], |r| r.get::<_, String>(0)).optional()?;
        row.map(|v| serde_json::from_str(&v).map_err(StoreError::from)).transpose()
    }

    pub fn put_setting<T: Serialize>(&self, key: &str, value: &T) -> Result<(), StoreError> {
        self.conn.execute("insert or replace into settings values (?, ?)", params![key, serde_json::to_string(value)?])?;
        Ok(())
    }
}
