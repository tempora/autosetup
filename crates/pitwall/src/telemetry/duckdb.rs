/* LMU .duckdb recordings.  `open` reads a file in place, read-only;
   `open_copy` reads a private copy in a temp folder so LMU's own file is
   never opened or locked, retrying while LMU holds its write lock.  A
   leftover write-ahead log (the game stopped mid-outing) is copied too and
   replayed into the copy, so its laps are kept.  */

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use ::duckdb::{AccessMode, Config, Connection};

use crate::chassis::Wheel;
use crate::setup::CarSetup;
use crate::telemetry::{
    ChannelKind, SessionMeta, TelemetryError, TelemetrySource, TimeSeries, CLOCK_CHANNEL,
};
use crate::units::Seconds;

pub struct DuckDbRecording {
    conn: Connection,
    frequencies: HashMap<String, f64>,
    units: HashMap<String, String>,
    event_tables: HashMap<String, bool>,
    clock: Vec<Seconds>,
    clock_hz: f64,
    // Declared last: the connection closes before the copy is deleted.
    _copy: Option<TempCopy>,
}

const COPY_RETRIES: u32 = 5;

// Write-ahead log LMU keeps beside a recording while the outing runs.
pub fn wal_of(path: &Path) -> PathBuf {
    let mut s = path.as_os_str().to_owned();
    s.push(".wal");
    PathBuf::from(s)
}

struct TempCopy(PathBuf);

impl Drop for TempCopy {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

// Copies `from`, retrying with backoff while another process holds it.
fn copy_when_free(from: &Path, to: &Path) -> Result<(), TelemetryError> {
    let mut delay = Duration::from_millis(200);
    for attempt in 1..=COPY_RETRIES {
        match std::fs::copy(from, to) {
            Ok(_) => return Ok(()),
            // Windows: 32 sharing violation, 33 lock violation.
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied || matches!(e.raw_os_error(), Some(32 | 33)) => {
                if attempt == COPY_RETRIES {
                    return Err(TelemetryError::Busy(from.display().to_string()));
                }
                std::thread::sleep(delay);
                delay *= 2;
            }
            Err(e) => return Err(TelemetryError::Backend(format!("{}: {e}", from.display()))),
        }
    }
    unreachable!("the last attempt returns")
}

impl DuckDbRecording {
    pub fn open(path: impl AsRef<Path>) -> Result<Self, TelemetryError> {
        let cfg = Config::default()
            .access_mode(AccessMode::ReadOnly)
            .map_err(|e| TelemetryError::Backend(e.to_string()))?;
        let conn = Connection::open_with_flags(path.as_ref(), cfg)
            .map_err(|e| TelemetryError::Backend(e.to_string()))?;
        Self::from_connection(conn, None)
    }

    pub fn open_copy(path: impl AsRef<Path>) -> Result<Self, TelemetryError> {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let path = path.as_ref();
        let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!("pitwall_{}_{n}", std::process::id()));
        std::fs::create_dir_all(&dir).map_err(|e| TelemetryError::Backend(e.to_string()))?;
        let copy = TempCopy(dir);
        let local = copy.0.join("session.duckdb");
        copy_when_free(path, &local)?;
        let wal = wal_of(path);
        let conn = if wal.exists() {
            copy_when_free(&wal, &wal_of(&local))?;
            // Read-write so the log is replayed into the copy.
            Connection::open(&local)
        } else {
            Config::default().access_mode(AccessMode::ReadOnly).and_then(|cfg| Connection::open_with_flags(&local, cfg))
        }
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
        Self::from_connection(conn, Some(copy))
    }

    fn from_connection(conn: Connection, copy: Option<TempCopy>) -> Result<Self, TelemetryError> {

        let (frequencies, units) = read_channel_list(&conn)?;
        let event_tables = read_event_table_flags(&conn)?;
        let clock_hz = frequencies.get(CLOCK_CHANNEL).copied().unwrap_or(100.0);
        let clock = read_clock(&conn)?;

        Ok(Self {
            conn,
            frequencies,
            units,
            event_tables,
            clock,
            clock_hz,
            _copy: copy,
        })
    }

    fn sample_times(&self, n: usize, hz: f64) -> Vec<Seconds> {
        if self.clock.is_empty() || hz <= 0.0 {
            return vec![Seconds::ZERO; n];
        }
        let max_idx = (self.clock.len() - 1) as f64;
        let ratio = self.clock_hz / hz;
        (0..n)
            .map(|i| {
                let pos = (i as f64 * ratio).clamp(0.0, max_idx);
                let lo = pos.floor() as usize;
                let hi = (lo + 1).min(self.clock.len() - 1);
                let frac = pos - lo as f64;
                let t0 = self.clock[lo].raw();
                let t1 = self.clock[hi].raw();
                Seconds::new(t0 + frac * (t1 - t0))
            })
            .collect()
    }
}

impl TelemetrySource for DuckDbRecording {
    fn metadata(&self) -> Result<SessionMeta, TelemetryError> {
        let mut stmt = self
            .conn
            .prepare("select key, value from metadata")
            .map_err(|e| TelemetryError::Backend(e.to_string()))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| TelemetryError::Backend(e.to_string()))?;

        let mut kv = HashMap::new();
        for pair in rows {
            let (k, v) = pair.map_err(|e| TelemetryError::Backend(e.to_string()))?;
            kv.insert(k, v);
        }

        let setup = kv
            .get("CarSetup")
            .filter(|s| !s.is_empty())
            .map(|raw| CarSetup::from_lmu_json(raw))
            .transpose()
            .map_err(|e| TelemetryError::Backend(e.to_string()))?
            .unwrap_or_default();

        let car_name = kv
            .get("CarName")
            .map(|s| s.split(':').next().unwrap_or("").to_string())
            .unwrap_or_default();

        Ok(SessionMeta {
            track: kv.get("TrackName").cloned().unwrap_or_default(),
            layout: kv
                .get("TrackLayout")
                .filter(|s| !s.is_empty())
                .or_else(|| kv.get("TrackName"))
                .cloned()
                .unwrap_or_default(),
            car_name,
            car_class: kv.get("CarClass").cloned().unwrap_or_default(),
            session_type: kv.get("SessionType").cloned().unwrap_or_default(),
            recorded_at: kv.get("RecordingTime").and_then(|t| crate::lmu::builds::recording_time(t)),
            setup,
        })
    }

    fn channel_kind(&self, name: &str) -> Option<ChannelKind> {
        if let Some(&hz) = self.frequencies.get(name) {
            Some(ChannelKind::FixedRate { hz })
        } else if self.event_tables.get(name).copied().unwrap_or(false) {
            Some(ChannelKind::EventTable)
        } else {
            None
        }
    }

    fn channel_unit(&self, name: &str) -> Option<&str> {
        self.units.get(name).map(String::as_str)
    }

    fn scalar_channel(&self, name: &str) -> Result<TimeSeries<f64>, TelemetryError> {
        match self.channel_kind(name) {
            Some(ChannelKind::FixedRate { hz }) => {
                let sql = format!("select cast(value as double) from \"{}\"", escape_ident(name));
                let mut stmt = self
                    .conn
                    .prepare(&sql)
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let values: Vec<f64> = stmt
                    .query_map([], |row| row.get::<_, f64>(0))
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?
                    .collect::<Result<_, _>>()
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let times = self.sample_times(values.len(), hz);
                Ok(TimeSeries { times, values })
            }
            Some(ChannelKind::EventTable) => {
                let sql = format!(
                    "select cast(ts as double), cast(value as double) from \"{}\" order by ts",
                    escape_ident(name)
                );
                let mut stmt = self
                    .conn
                    .prepare(&sql)
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let mut times = Vec::new();
                let mut values = Vec::new();
                let rows = stmt
                    .query_map([], |row| Ok((row.get::<_, f64>(0)?, row.get::<_, f64>(1)?)))
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                for r in rows {
                    let (t, v) = r.map_err(|e| TelemetryError::Backend(e.to_string()))?;
                    times.push(Seconds::new(t));
                    values.push(v);
                }
                Ok(TimeSeries { times, values })
            }
            None => Err(TelemetryError::MissingChannel(name.to_string())),
        }
    }

    fn wheel_channel(&self, name: &str) -> Result<TimeSeries<Wheel<f64>>, TelemetryError> {
        match self.channel_kind(name) {
            Some(ChannelKind::FixedRate { hz }) => {
                let sql = format!(
                    "select cast(value1 as double), cast(value2 as double), \
                            cast(value3 as double), cast(value4 as double) \
                     from \"{}\"",
                    escape_ident(name)
                );
                let mut stmt = self
                    .conn
                    .prepare(&sql)
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let values: Vec<Wheel<f64>> = stmt
                    .query_map([], |row| {
                        Ok(Wheel::new(
                            row.get(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                        ))
                    })
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?
                    .collect::<Result<_, _>>()
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let times = self.sample_times(values.len(), hz);
                Ok(TimeSeries { times, values })
            }
            Some(ChannelKind::EventTable) => {
                let sql = format!(
                    "select cast(ts as double), cast(value1 as double), cast(value2 as double), \
                            cast(value3 as double), cast(value4 as double) \
                     from \"{}\" order by ts",
                    escape_ident(name)
                );
                let mut stmt = self
                    .conn
                    .prepare(&sql)
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                let mut times = Vec::new();
                let mut values = Vec::new();
                let rows = stmt
                    .query_map([], |row| {
                        Ok((
                            row.get::<_, f64>(0)?,
                            Wheel::new(row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?),
                        ))
                    })
                    .map_err(|e| TelemetryError::Backend(e.to_string()))?;
                for r in rows {
                    let (t, w) = r.map_err(|e| TelemetryError::Backend(e.to_string()))?;
                    times.push(Seconds::new(t));
                    values.push(w);
                }
                Ok(TimeSeries { times, values })
            }
            None => Err(TelemetryError::MissingChannel(name.to_string())),
        }
    }

    fn wheel_surface(&self) -> Result<TimeSeries<Wheel<u8>>, TelemetryError> {
        let raw = self.wheel_channel("SurfaceTypes")?;
        let values = raw
            .values
            .into_iter()
            .map(|w| w.map(|v| v.round().clamp(0.0, 255.0) as u8))
            .collect();
        Ok(TimeSeries {
            times: raw.times,
            values,
        })
    }
}

fn escape_ident(name: &str) -> String {
    name.replace('"', "\"\"")
}

type ChannelList = (HashMap<String, f64>, HashMap<String, String>);

fn read_channel_list(conn: &Connection) -> Result<ChannelList, TelemetryError> {
    let mut stmt = conn
        .prepare(
            "select channelName, cast(frequency as double), coalesce(cast(unit as varchar), '')              from channelsList",
        )
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?, row.get::<_, String>(2)?))
        })
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    let mut freqs = HashMap::new();
    let mut units = HashMap::new();
    for r in rows {
        let (name, hz, unit) = r.map_err(|e| TelemetryError::Backend(e.to_string()))?;
        freqs.insert(name.clone(), hz);
        units.insert(name, unit);
    }
    Ok((freqs, units))
}

fn read_event_table_flags(conn: &Connection) -> Result<HashMap<String, bool>, TelemetryError> {
    let mut stmt = conn
        .prepare(
            "select table_name, bool_or(column_name = 'ts') \
             from information_schema.columns group by table_name",
        )
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, bool>(1)?))
        })
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    let mut out = HashMap::new();
    for r in rows {
        let (table, has_ts) = r.map_err(|e| TelemetryError::Backend(e.to_string()))?;
        out.insert(table, has_ts);
    }
    Ok(out)
}

fn read_clock(conn: &Connection) -> Result<Vec<Seconds>, TelemetryError> {
    let sql = format!("select cast(value as double) from \"{}\"", CLOCK_CHANNEL);
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    let rows = stmt
        .query_map([], |row| row.get::<_, f64>(0))
        .map_err(|e| TelemetryError::Backend(e.to_string()))?;
    rows.map(|r| {
        r.map(Seconds::new)
            .map_err(|e| TelemetryError::Backend(e.to_string()))
    })
    .collect()
}
