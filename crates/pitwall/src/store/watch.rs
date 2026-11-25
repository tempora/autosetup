/* Which recordings in LMU's telemetry folder are ready to import.
   LMU keeps <name>.duckdb.wal beside a recording while the outing runs,
   locks the file in short bursts while flushing, and deletes the log only
   once it has merged it and let go.  Per poll:
     driving    a log while the game runs: wait;
     finished   the log of a file seen driving is gone: ready at once;
     leftover   a log while the game is not running (it stopped mid-outing):
                ready once the size holds for SETTLE_S, once per size (the
                copy replays the log);
     new/grown  no log, not stored at this size: ready once the size holds
                for SETTLE_S.
   A ready file that fails to import (LMU holding it) stays ready on later
   polls.  `now` is any monotonic clock in seconds.  */

use std::collections::{HashMap, HashSet};
use std::path::Path;

use crate::store::{Store, StoreError};
use crate::telemetry::duckdb::wal_of;

pub const SETTLE_S: f64 = 3.0;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub name: String,
    pub size: u64,
    pub wal: bool,
}

// Every *.duckdb in `dir`, newest name first (names embed the UTC start).
pub fn list(dir: &Path) -> std::io::Result<Vec<Entry>> {
    let mut out: Vec<Entry> = std::fs::read_dir(dir)?
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().is_some_and(|x| x.eq_ignore_ascii_case("duckdb")))
        .filter_map(|e| {
            let size = e.metadata().ok()?.len();
            Some(Entry { name: e.file_name().into_string().ok()?, size, wal: wal_of(&e.path()).exists() })
        })
        .collect();
    out.sort_by(|a, b| b.name.cmp(&a.name));
    Ok(out)
}

#[derive(Debug, Default)]
pub struct Watcher {
    // Size first seen and when, for files waiting for their size to hold.
    seen: HashMap<String, (u64, f64)>,
    driving: HashSet<String>,
    replayed: HashSet<(String, u64)>,
}

impl Watcher {
    pub fn poll(&mut self, entries: &[Entry], stored: &HashMap<String, u64>, game_running: bool, now: f64) -> Vec<String> {
        let mut ready = Vec::new();
        for e in entries {
            if e.wal && game_running {
                self.driving.insert(e.name.clone());
                continue;
            }
            if !e.wal && self.driving.remove(&e.name) {
                self.seen.remove(&e.name);
                ready.push(e.name.clone());
                continue;
            }
            let pending = if e.wal { !self.replayed.contains(&(e.name.clone(), e.size)) } else { stored.get(&e.name) != Some(&e.size) };
            if !pending {
                self.seen.remove(&e.name);
                continue;
            }
            match self.seen.get(&e.name) {
                Some(&(size, since)) if size == e.size => {
                    if now - since >= SETTLE_S {
                        ready.push(e.name.clone());
                    }
                }
                _ => {
                    self.seen.insert(e.name.clone(), (e.size, now));
                }
            }
        }
        ready
    }

    // Marks a leftover-log file imported at this size.
    fn replayed(&mut self, e: &Entry) {
        if e.wal {
            self.replayed.insert((e.name.clone(), e.size));
        }
    }
}

#[derive(Debug, Default)]
pub struct SyncReport {
    pub imported: Vec<String>,
    // LMU held these; they are tried again on the next poll.
    pub busy: Vec<String>,
    pub driving: usize,
}

// One poll of `dir`: imports every ready recording into `store`.
pub fn sync(store: &Store, watcher: &mut Watcher, dir: &Path, game_running: bool, now: f64) -> Result<SyncReport, StoreError> {
    let entries = list(dir)?;
    let ready = watcher.poll(&entries, &store.imported()?, game_running, now);
    let mut report = SyncReport { driving: watcher.driving.len(), ..SyncReport::default() };
    for name in ready {
        match store.import(&dir.join(&name)) {
            Ok(_) => {
                if let Some(e) = entries.iter().find(|e| e.name == name) {
                    watcher.replayed(e);
                }
                report.imported.push(name);
            }
            Err(StoreError::Telemetry(crate::telemetry::TelemetryError::Busy(_))) => report.busy.push(name),
            Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e),
        }
    }
    Ok(report)
}
