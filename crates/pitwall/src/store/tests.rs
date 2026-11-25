/* Store and watcher tests.  Synthetic recordings are complete LMU files
   (metrics::tests fixture) in temporary folders; the ignored test imports
   real recordings from copies and drives the planner through the store.
   Nothing is written to the LMU install.  */

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use ::duckdb::Connection;

use crate::metrics::analyse_stint;
use crate::metrics::tests::write_recording;
use crate::store::watch::{sync, Entry, Watcher, SETTLE_S};
use crate::store::Store;
use crate::telemetry::duckdb::{wal_of, DuckDbRecording};

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> Self {
        let d = std::env::temp_dir().join(format!("pitwall_store_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("Telemetry")).unwrap();
        Self(d)
    }

    fn telemetry(&self) -> PathBuf {
        self.0.join("Telemetry")
    }

    fn store(&self) -> Store {
        Store::open(&self.0.join("store").join("pitwall.duckdb")).unwrap()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

// Every file in `dir` with its bytes.
fn snapshot(dir: &Path) -> Vec<(String, Vec<u8>)> {
    let mut v: Vec<(String, Vec<u8>)> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(Result::ok)
        .map(|e| (e.file_name().to_string_lossy().into_owned(), std::fs::read(e.path()).unwrap()))
        .collect();
    v.sort();
    v
}

const NAME: &str = "Slalom_P_2026-11-13T16_58_22Z.duckdb";

#[test]
fn a_stored_stint_reads_back_exactly_after_reopening_and_lmus_folder_is_untouched() {
    let tmp = TempDir::new("roundtrip");
    let path = tmp.telemetry().join(NAME);
    write_recording(&path, 1.25);
    let before = snapshot(&tmp.telemetry());
    let first = tmp.store().import(&path).unwrap().id;
    assert_eq!(tmp.store().import(&path).unwrap().id, first, "re-importing changed the stint's id");
    let back = tmp.store().stint(NAME).unwrap().expect("stored");
    assert_eq!(back.id, first);
    let fresh = analyse_stint(&DuckDbRecording::open(&path).unwrap()).unwrap();
    assert_eq!(back.usable(), Some(&fresh), "metrics change through the store");
    assert_eq!((back.meta.track.as_str(), back.meta.car_name.as_str()), ("Slalom", "Fixture GT3 #1"));
    assert_eq!(snapshot(&tmp.telemetry()), before, "the telemetry folder changed");
}

#[test]
fn a_leftover_wal_is_replayed_from_a_copy_without_touching_lmus_files() {
    let tmp = TempDir::new("wal");
    let path = tmp.telemetry().join(NAME);
    write_recording(&path, 1.0);
    // The game stops mid-outing: the last write is only in the log.
    let conn = Connection::open(&path).unwrap();
    conn.execute_batch(
        "pragma disable_checkpoint_on_shutdown;
         insert into metadata values ('RecordingTime', '2026-11-13T16_58_22Z');",
    )
    .unwrap();
    drop(conn);
    assert!(wal_of(&path).exists(), "fixture left no write-ahead log");
    let before = snapshot(&tmp.telemetry());

    let s = tmp.store().import(&path).unwrap();
    assert_eq!(s.meta.recorded_at, Some(1_794_589_102), "the log was not replayed");
    assert!(s.usable().is_some());
    assert_eq!(snapshot(&tmp.telemetry()), before, "LMU's recording or its log changed");
}

#[test]
fn stints_analysed_by_another_metrics_version_are_reanalysed_from_their_recording() {
    let tmp = TempDir::new("stale");
    let path = tmp.telemetry().join(NAME);
    write_recording(&path, 1.25);
    let store = tmp.store();
    let fresh = store.import(&path).unwrap();
    store.conn.execute("update recordings set metrics_version = metrics_version - 1, metrics = '{}'", []).unwrap();
    assert!(store.stint(NAME).is_err(), "an old metrics layout no longer parses");
    assert_eq!(store.stale().unwrap(), [NAME]);
    assert_eq!(store.refresh_stale(&tmp.telemetry()).unwrap(), [NAME]);
    assert!(store.stale().unwrap().is_empty());
    assert_eq!(store.stint(NAME).unwrap().unwrap().usable(), fresh.usable());
}

fn entry(name: &str, size: u64, wal: bool) -> Entry {
    Entry { name: name.into(), size, wal }
}

#[test]
fn an_outing_is_ready_the_moment_its_log_goes_and_never_while_driving() {
    let mut w = Watcher::default();
    let stored = HashMap::new();
    for t in 0..10 {
        let size = 1000 + 100 * t;
        assert!(w.poll(&[entry("a", size, true)], &stored, true, f64::from(t as u32)).is_empty(), "ready while driving");
    }
    assert_eq!(w.poll(&[entry("a", 2000, false)], &stored, true, 10.0), ["a"], "log gone: finished at once");
}

#[test]
fn new_files_wait_for_their_size_to_hold_and_stored_ones_are_skipped() {
    let mut w = Watcher::default();
    let mut stored = HashMap::new();
    assert!(w.poll(&[entry("b", 500, false)], &stored, false, 0.0).is_empty());
    assert!(w.poll(&[entry("b", 800, false)], &stored, false, 2.0).is_empty(), "still growing");
    assert!(w.poll(&[entry("b", 800, false)], &stored, false, 2.0 + SETTLE_S - 0.1).is_empty());
    assert_eq!(w.poll(&[entry("b", 800, false)], &stored, false, 2.0 + SETTLE_S), ["b"]);
    assert_eq!(w.poll(&[entry("b", 800, false)], &stored, false, 9.0), ["b"], "not stored yet (LMU held it): still ready");
    stored.insert("b".to_string(), 800);
    assert!(w.poll(&[entry("b", 800, false)], &stored, false, 10.0).is_empty());
    assert!(w.poll(&[entry("b", 900, false)], &stored, false, 11.0).is_empty(), "grew: wait again");
    assert_eq!(w.poll(&[entry("b", 900, false)], &stored, false, 11.0 + SETTLE_S), ["b"]);
}

#[test]
fn sync_imports_each_finished_recording_once_and_keeps_unusable_ones_with_their_reason() {
    let tmp = TempDir::new("sync");
    let dir = tmp.telemetry();
    write_recording(&dir.join(NAME), 1.0);
    std::fs::write(dir.join("Broken_P_2026-11-13T17_00_00Z.duckdb"), b"not a database").unwrap();
    let driving = dir.join("Slalom_P_2026-11-13T17_30_00Z.duckdb");
    write_recording(&driving, 1.1);
    std::fs::write(wal_of(&driving), b"").unwrap();
    let store = tmp.store();
    let mut w = Watcher::default();

    assert!(sync(&store, &mut w, &dir, true, 0.0).unwrap().imported.is_empty(), "nothing has held its size yet");
    let r = sync(&store, &mut w, &dir, true, SETTLE_S).unwrap();
    let mut got = r.imported.clone();
    got.sort();
    assert_eq!(got, ["Broken_P_2026-11-13T17_00_00Z.duckdb", NAME]);
    assert_eq!(r.driving, 1);
    let broken = store.stint("Broken_P_2026-11-13T17_00_00Z.duckdb").unwrap().unwrap();
    assert!(broken.metrics.as_ref().is_err_and(|e| !e.is_empty()), "{:?}", broken.metrics.err());
    assert!(sync(&store, &mut w, &dir, true, 2.0 * SETTLE_S).unwrap().imported.is_empty(), "imported twice");

    std::fs::remove_file(wal_of(&driving)).unwrap();
    let r = sync(&store, &mut w, &dir, true, 2.0 * SETTLE_S + 1.0).unwrap();
    assert_eq!(r.imported, ["Slalom_P_2026-11-13T17_30_00Z.duckdb"]);
    assert_eq!(store.usable_stints().unwrap().len(), 2);
}

#[cfg(test)]
mod local {
    use super::*;
    use crate::opt::rules::Rule;
    use crate::opt::solve::Toggles;
    use crate::opt::target::Anchor;
    use crate::opt::Proposal;
    use crate::store::Rating;
    use crate::telemetry::TelemetrySource;
    use crate::testdata::local_recordings;
    use std::collections::BTreeMap;

    const MAX_STINTS: usize = 6;

    // Every setting key a proposal moves.
    fn moved(p: &Proposal) -> Vec<String> {
        let main = p.solution.changes.iter().flat_map(|c| c.kind.keys().iter().map(|k| k.to_string()));
        let rules = p.rules.iter().flat_map(|r| r.keys.clone());
        let dampers = p.dampers.changes.iter().flat_map(|d| d.keys.clone());
        main.chain(rules).chain(dampers).collect()
    }

    /* Imports the latest stints of the busiest car and layout without ABS
       (LMU's files copied first, so the watcher sees a quiet folder), checks
       the stored metrics against a fresh analysis, then drives the planner
       through the store: a lock keeps its setting still, lock-ups bring brake
       pressure down, a locked fingerprint anchors the target, and a short
       stint steps back to the last good one without repeating its moves.  */
    #[test]
    #[ignore = "requires local LMU recordings (PITWALL_TELEMETRY_DIR)"]
    fn real_store_drives_the_planner() {
        let mut groups: BTreeMap<(String, String), Vec<(PathBuf, bool)>> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            let abs = meta.setup.params.get("VM_ANTILOCKBRAKESYSTEMMAP").is_some_and(|p| p.adjustable());
            let pressure = meta.setup.params.get("VM_BRAKE_PRESSURE").is_some_and(|p| p.adjustable());
            groups.entry((meta.setup.signature(), meta.layout)).or_default().push((path, pressure && !abs));
        }
        let ((sig, layout), mut files) = groups
            .into_iter()
            .filter(|(_, f)| f.iter().all(|(_, no_abs)| *no_abs))
            .max_by_key(|(_, f)| f.len())
            .expect("a car without ABS");
        files.sort();
        let files: Vec<PathBuf> = files.into_iter().rev().take(MAX_STINTS).map(|(p, _)| p).collect();

        let tmp = TempDir::new("real");
        for f in &files {
            std::fs::copy(f, tmp.telemetry().join(f.file_name().unwrap())).unwrap();
        }
        let store = tmp.store();
        let mut w = Watcher::default();
        sync(&store, &mut w, &tmp.telemetry(), false, 0.0).unwrap();
        let r = sync(&store, &mut w, &tmp.telemetry(), false, SETTLE_S).unwrap();
        assert_eq!(r.imported.len(), files.len());
        assert!(sync(&store, &mut w, &tmp.telemetry(), false, 2.0 * SETTLE_S).unwrap().imported.is_empty());

        for f in &files {
            let name = f.file_name().unwrap().to_string_lossy();
            let stored = store.stint(&name).unwrap().unwrap();
            let fresh = analyse_stint(&DuckDbRecording::open(f).unwrap()).ok();
            assert_eq!(stored.usable(), fresh.as_ref(), "{name}: stored metrics differ");
        }
        let stints = store.stints_of(&sig, &layout).unwrap();
        println!("{} @ {layout}: {} usable of {} stints", stints[0].meta.car_name, stints.len(), files.len());
        let flying = |s: &crate::store::StoredStint| s.usable().unwrap().laps.iter().any(|l| l.is_flying());
        let latest = stints.iter().rev().find(|s| flying(s)).expect("a stint with a flying lap");
        let plan = |name: &str| store.plan_for(name, Toggles::default()).unwrap().unwrap();

        let base = plan(&latest.file_name);
        let keys = moved(&base.proposal);
        println!("as driven: moves {keys:?}");
        assert!(!keys.is_empty(), "nothing proposed: no lock to test");
        let key = &keys[0];
        store.set_lock(&sig, &layout, key, true).unwrap();
        assert!(!moved(&plan(&latest.file_name).proposal).contains(key), "{key} moved while locked");
        store.set_lock(&sig, &layout, key, false).unwrap();
        assert_eq!(plan(&latest.file_name).proposal, base.proposal, "unlocking did not restore the proposal");

        store.set_rating(&latest.file_name, &Rating { lockups: true, ..Rating::default() }).unwrap();
        let lockups = plan(&latest.file_name).proposal;
        let bp = lockups.rules.iter().find(|r| r.rule == Rule::BrakePressure).expect("lock-ups moved no brake pressure");
        assert!(bp.to < bp.from, "lock-ups raised brake pressure");
        store.set_rating(&latest.file_name, &Rating::default()).unwrap();

        assert_eq!(base.proposal.target.anchor, Anchor::FastNeutral);
        store.lock_fingerprint(latest).unwrap();
        assert_eq!(plan(&latest.file_name).proposal.target.anchor, Anchor::Fingerprint);
        assert!(store.forget_fingerprint(&sig).unwrap());
        assert_eq!(plan(&latest.file_name).proposal, base.proposal, "forgetting the fingerprint did not restore the target");
    }

    /* A stint without a flying lap after a good one, from real recordings:
       unanswered, the proposal is built from the good stint and does not
       repeat the short stint's moves; called fine, the short stint is its own
       base.  */
    #[test]
    #[ignore = "requires local LMU recordings (PITWALL_TELEMETRY_DIR)"]
    fn real_short_stint_steps_back_to_the_last_good_one() {
        use crate::metrics::laps::segment_laps;

        let mut groups: BTreeMap<(String, String), Vec<(PathBuf, bool)>> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(rec) = DuckDbRecording::open(&path) else { continue };
            let (Ok(meta), Ok(laps)) = (rec.metadata(), segment_laps(&rec)) else { continue };
            groups.entry((meta.setup.signature(), meta.layout)).or_default().push((path, laps.iter().any(|l| l.is_flying())));
        }
        let candidates: Vec<(PathBuf, PathBuf)> = groups
            .into_values()
            .flat_map(|mut g| {
                g.sort();
                (1..g.len())
                    .filter(|&i| !g[i].1)
                    .filter_map(|i| g[..i].iter().rev().find(|(_, f)| *f).map(|(good, _)| (good.clone(), g[i].0.clone())))
                    .collect::<Vec<_>>()
            })
            .collect();
        let tmp = TempDir::new("short");
        let store = tmp.store();
        let (good, short) = candidates
            .iter()
            .find_map(|(good, short)| {
                let copy = |f: &PathBuf| {
                    let to = tmp.telemetry().join(f.file_name().unwrap());
                    std::fs::copy(f, &to).unwrap();
                    store.import(&to).unwrap()
                };
                let (g, s) = (copy(good), copy(short));
                (g.usable().is_some() && s.usable().is_some()).then_some((g, s))
            })
            .expect("no usable short stint after a good one");
        let differ = good.meta.setup.params.iter().filter(|(k, q)| short.meta.setup.params.get(*k).is_some_and(|r| r.value != q.value)).count();
        println!("{} @ {}: short {} after {}, {differ} settings differ", short.meta.car_name, short.meta.layout, short.file_name, good.file_name);

        let p = store.plan_for(&short.file_name, Toggles::default()).unwrap().unwrap();
        assert_eq!(p.base.file_name, good.file_name, "not built from the last good stint");
        assert_eq!(p.undone.as_ref().map(|u| u.file_name.as_str()), Some(short.file_name.as_str()));
        for c in &p.proposal.solution.changes {
            let vals: Vec<f64> = c.kind.keys().iter().filter_map(|k| short.meta.setup.params.get(*k)).map(|q| f64::from(q.value)).collect();
            let step = (vals.iter().sum::<f64>() / vals.len().max(1) as f64).round() as i32 - c.from;
            assert!(step == 0 || (c.to - c.from) * step.signum() < step.abs(), "repeated the short stint's {:?}", c.kind);
        }
        println!("moves {:?}", moved(&p.proposal));
        store.set_rating(&short.file_name, &Rating { undrivable: Some(false), ..Rating::default() }).unwrap();
        let fine = store.plan_for(&short.file_name, Toggles::default()).unwrap().unwrap();
        assert_eq!((fine.base.file_name.as_str(), fine.undone.is_none()), (short.file_name.as_str(), true));
    }
}
