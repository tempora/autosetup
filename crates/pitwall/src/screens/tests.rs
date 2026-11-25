/* Screen payload tests.  The synthetic recording (metrics::tests fixture)
   drives a known slalom at 30 m/s with laps of 1200 m, so the outline, the
   trace and the lap table follow from the trajectory.  The ignored test
   builds every payload from real recordings imported into a temp store.  */

use std::path::PathBuf;

use crate::metrics::tests::write_recording;
use crate::screens::history::{facts, lap_rows, sessions};
use crate::screens::names::{pretty_name, setup_diff};
use crate::screens::setup::{histograms, KerbFilter};
use crate::screens::trackmap::{lap_trace, track_map, STEP_M};
use crate::setup::CarSetup;
use crate::store::{Store, StoredStint};
use crate::telemetry::duckdb::DuckDbRecording;

const SPEED: f64 = 30.0;
const LAP_LEN: f64 = 1200.0;

struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> Self {
        let d = std::env::temp_dir().join(format!("pitwall_screens_{tag}_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("Telemetry")).unwrap();
        Self(d)
    }
    fn telemetry(&self) -> PathBuf {
        self.0.join("Telemetry")
    }
    fn store(&self) -> Store {
        Store::open(&self.0.join("pitwall.duckdb")).unwrap()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn setup(entries: &[(&str, i32, &str)]) -> CarSetup {
    let body: Vec<String> = entries
        .iter()
        .map(|(k, v, text)| format!(r#""{k}": {{"available": true, "minValue": 0, "maxValue": 20, "value": {v}, "stringValue": "{text}"}}"#))
        .collect();
    CarSetup::from_lmu_json(&format!("{{{}}}", body.join(", "))).unwrap()
}

#[test]
fn setting_names_read_like_the_garage_and_pairs_that_moved_together_collapse() {
    for (key, name) in [
        ("WM_CAMBER-W_FL", "Camber FL"),
        ("VM_REAR_ANTISWAY", "Rear anti-roll bar"),
        ("VM_FRONT_3RD_SPRING", "Front 3rd spring"),
        ("VM_BRAKE_BALANCE", "Brake bias"),
        ("VM_TRACTIONCONTROLMAP", "TC map"),
        ("WM_FASTBUMP-W_RR", "Fast bump RR"),
    ] {
        assert_eq!(pretty_name(key), name);
    }
    let a = setup(&[("WM_CAMBER-W_FL", 5, "-2.5 deg"), ("WM_CAMBER-W_FR", 5, "-2.5 deg"), ("WM_PRESSURE-W_RL", 3, "140 kPa"), ("WM_PRESSURE-W_RR", 3, "140 kPa"), ("WM_COMPOUND-W_FL", 1, "Medium")]);
    let b = setup(&[("WM_CAMBER-W_FL", 6, "-2.4 deg"), ("WM_CAMBER-W_FR", 6, "-2.4 deg"), ("WM_PRESSURE-W_RL", 2, "139 kPa"), ("WM_PRESSURE-W_RR", 4, "141 kPa"), ("WM_COMPOUND-W_FL", 2, "Medium")]);
    let d = setup_diff(&a, &b);
    let rows: Vec<(&str, &str, &str)> = d.iter().map(|r| (r.name.as_str(), r.before_text.as_str(), r.after_text.as_str())).collect();
    assert_eq!(rows, [("Camber FL/FR", "-2.5 deg", "-2.4 deg"), ("Pressure RL", "140 kPa", "139 kPa"), ("Pressure RR", "140 kPa", "141 kPa")]);
}

fn slalom(tmp: &TempDir, name: &str) -> (PathBuf, StoredStint) {
    let path = tmp.telemetry().join(name);
    write_recording(&path, 1.1);
    let s = tmp.store().import(&path).unwrap();
    (path, s)
}

#[test]
fn the_outline_and_traces_follow_the_driven_slalom() {
    let tmp = TempDir::new("map");
    let (path, s) = slalom(&tmp, "Slalom_P_2026-11-13T16_58_22Z.duckdb");
    let store = tmp.store();
    let map = track_map(&store, "Slalom", &tmp.telemetry()).unwrap().expect("an outline");
    assert!((map.length - LAP_LEN).abs() < 5.0, "length {}", map.length);
    let gaps: Vec<f64> = map.points.windows(2).map(|w| ((w[1][1] - w[0][1]).powi(2) + (w[1][2] - w[0][2]).powi(2)).sqrt()).collect();
    assert!(gaps.iter().all(|g| (g - STEP_M).abs() < 0.5), "points not {STEP_M} m apart along the path: {:?}", gaps.iter().fold((f64::MAX, 0.0f64), |(lo, hi), &g| (lo.min(g), hi.max(g))));
    assert_eq!(track_map(&store, "Slalom", &PathBuf::from("nowhere")).unwrap().as_ref(), Some(&map), "not cached");

    let m = s.usable().unwrap();
    let rec = DuckDbRecording::open(&path).unwrap();
    let flying: Vec<_> = m.laps.iter().filter(|l| l.is_flying()).collect();
    assert_eq!(flying.len(), 2);
    let traces: Vec<_> = flying.iter().map(|l| lap_trace(&rec, l, Some(&map), &m.geometry).unwrap()).collect();
    for tr in &traces {
        for (d, t) in tr.distance.iter().zip(&tr.time).skip(4) {
            assert!((t - d / SPEED).abs() < 0.1, "at {d} m: {t} s, not {}", d / SPEED);
        }
        assert!(tr.speed.iter().flatten().all(|v| (v - SPEED * 3.6).abs() < 0.01));
        assert!(tr.throttle.iter().flatten().all(|v| (v - 0.6).abs() < 1e-9), "throttle not 0..1");
    }
    /* The slalom repeats its curvature every lap without closing, so lap 2 is
       lap 1 moved by one fixed offset: equal lap distance is the same point of
       the road.  The lap the outline was drawn from lies on it.  */
    let (a, b) = (&traces[0], &traces[1]);
    let n = a.x.len().min(b.x.len());
    let off: Vec<(f64, f64)> = (0..n).map(|i| (b.x[i].unwrap() - a.x[i].unwrap(), b.y[i].unwrap() - a.y[i].unwrap())).collect();
    let (mx, my) = off.iter().fold((0.0, 0.0), |(sx, sy), (x, y)| (sx + x / n as f64, sy + y / n as f64));
    let worst = off.iter().map(|(x, y)| ((x - mx).powi(2) + (y - my).powi(2)).sqrt()).fold(0.0, f64::max);
    assert!(worst < 2.0, "lap 2 is not lap 1 moved: {worst:.2} m off at equal distance");
    let off_map = |tr: &crate::screens::trackmap::LapTrace| {
        (0..tr.x.len()).step_by(2).filter(|i| i / 2 < map.points.len()).map(|i| {
            let p = map.points[i / 2];
            ((tr.x[i].unwrap() - p[1]).powi(2) + (tr.y[i].unwrap() - p[2]).powi(2)).sqrt()
        }).fold(0.0, f64::max)
    };
    let nearest = traces.iter().map(off_map).fold(f64::INFINITY, f64::min);
    assert!(nearest < 1.0, "no lap lies on the outline: {nearest:.2} m");
}

#[test]
fn sessions_split_at_long_breaks_and_by_car_and_laps_count_once() {
    let tmp = TempDir::new("sessions");
    let (_, base) = slalom(&tmp, "Slalom_P_2026-11-13T16_58_22Z.duckdb");
    let at = |id: i64, t: i64, car: &str| {
        let mut s = base.clone();
        s.id = id;
        s.meta.recorded_at = Some(t);
        s.meta.car_name = car.into();
        s
    };
    let t0 = 1_794_589_102;
    let stints = vec![at(1, t0, "A"), at(2, t0 + 3600, "A"), at(3, t0 + 3700, "B"), at(4, t0 + 3600 + 4 * 3600, "A")];
    let s = sessions(&stints);
    let groups: Vec<(i64, Vec<i64>)> = s.iter().map(|x| (x.id, x.stint_ids.clone())).collect();
    assert_eq!(groups, [(4, vec![4]), (3, vec![3]), (1, vec![1, 2])], "newest first");
    let flying = lap_rows(base.usable().unwrap()).iter().filter(|l| l.complete && !l.in_pits).count();
    assert_eq!(s[2].laps, 2 * flying);
    assert_eq!(s[2].recorded_at.as_deref(), Some("2026-11-13T16_58_22Z"));
    let f = facts(&stints);
    assert!(f.contains(&format!("You've done {} laps at Slalom.", 4 * flying)), "{f:?}");
    assert!(f.iter().any(|x| x.starts_with("Your most driven car is the A: ")), "{f:?}");
}

#[test]
fn rebinned_histograms_keep_every_sample() {
    let tmp = TempDir::new("hist");
    let (_, s) = slalom(&tmp, "Slalom_P_2026-11-13T16_58_22Z.duckdb");
    let m = s.usable().unwrap();
    let totals = |k| histograms(m, k, 10.0).wheels.values().map(|h| h.counts.iter().sum::<u64>()).collect::<Vec<_>>();
    let (off, on, both) = (totals(KerbFilter::Off), totals(KerbFilter::On), totals(KerbFilter::Both));
    let raw: Vec<u64> = m.dampers.wheels.as_array().iter().map(|w| w.off.total()).collect();
    assert_eq!(off, raw);
    assert!(on[0] > 0, "the fixture's kerb strikes are on the front left");
    assert_eq!(both, off.iter().zip(&on).map(|(a, b)| a + b).collect::<Vec<_>>());
    let h = &histograms(m, KerbFilter::Off, 10.0).wheels["fl"];
    assert_eq!(h.edges.len(), h.counts.len() + 1);
    assert!((h.edges[1] - h.edges[0] - 10.0).abs() < 1e-9);
}

#[cfg(test)]
mod local {
    use super::*;
    use crate::lmu::svm::read_settings;
    use crate::opt::solve::Toggles;
    use crate::screens::driver::profile;
    use crate::screens::history::session_stints;
    use crate::screens::setup::proposal;
    use crate::screens::trackmap::lap;
    use crate::store::watch::{sync, Watcher, SETTLE_S};
    use crate::telemetry::TelemetrySource;
    use crate::testdata::local_recordings;
    use std::collections::BTreeMap;

    const MAX_STINTS: usize = 5;

    /* Every payload from real recordings: the busiest car and layout's
       latest stints, imported through the watcher into a temp store.  */
    #[test]
    #[ignore = "requires local LMU recordings and saved setups (PITWALL_TELEMETRY_DIR)"]
    fn real_payloads_hold_together() {
        let mut groups: BTreeMap<(String, String), Vec<PathBuf>> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            if !meta.setup.params.is_empty() {
                groups.entry((meta.setup.signature(), meta.layout)).or_default().push(path);
            }
        }
        let (_, mut files) = groups.into_iter().max_by_key(|(_, f)| f.len()).unwrap();
        files.sort();
        let files: Vec<PathBuf> = files.into_iter().rev().take(MAX_STINTS).collect();
        let tmp = TempDir::new("real");
        for f in &files {
            std::fs::copy(f, tmp.telemetry().join(f.file_name().unwrap())).unwrap();
        }
        let store = tmp.store();
        let mut w = Watcher::default();
        sync(&store, &mut w, &tmp.telemetry(), false, 0.0).unwrap();
        sync(&store, &mut w, &tmp.telemetry(), false, SETTLE_S).unwrap();
        let stints = store.usable_stints().unwrap();
        assert!(stints.len() >= 2, "{} usable stints", stints.len());
        let layout = stints[0].meta.layout.clone();

        let ss = sessions(&stints);
        let mut ids: Vec<i64> = ss.iter().flat_map(|s| s.stint_ids.clone()).collect();
        ids.sort_unstable();
        let mut want: Vec<i64> = stints.iter().map(|s| s.id).collect();
        want.sort_unstable();
        assert_eq!(ids, want, "every stint in exactly one session");
        let rows = session_stints(&store, ss[0].id).unwrap().unwrap();
        println!("{} @ {layout}: {} sessions, newest has {} stints", stints[0].meta.car_name, ss.len(), rows.stints.len());
        for r in &rows.stints {
            assert!(r.changes.iter().all(|c| !c.name.contains('_')), "raw key in {:?}", r.changes);
        }

        let map = track_map(&store, &layout, &tmp.telemetry()).unwrap().expect("an outline");
        let max_dist = {
            let rec = DuckDbRecording::open(&files[0]).unwrap();
            rec.scalar_channel("Lap Dist").unwrap().values.iter().copied().fold(0.0, f64::max)
        };
        assert!((map.length - max_dist).abs() < 1.0);
        let first_last = {
            let (a, b) = (map.points[0], map.points[map.points.len() - 1]);
            ((a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
        };
        assert!(first_last < 3.0 * STEP_M, "outline not closed: {first_last:.1} m");
        println!("map {:.0} m, {} points, sectors {:?}", map.length, map.points.len(), map.sectors);

        // Two laps from different stints share the road at equal distance.
        let mut flying: Vec<(i64, usize, f64)> = stints
            .iter()
            .flat_map(|s| lap_rows(s.usable().unwrap()).into_iter().filter(|l| l.clean).map(move |l| (s.id, l.seq, l.time)))
            .collect();
        flying.sort_by(|a, b| a.2.total_cmp(&b.2));
        let a = lap(&store, &tmp.telemetry(), flying[0].0, flying[0].1).unwrap().unwrap();
        let other = flying.iter().find(|f| f.0 != flying[0].0).expect("a clean lap in another stint");
        let b = lap(&store, &tmp.telemetry(), other.0, other.1).unwrap().unwrap();
        assert!((a.trace.time.last().unwrap() - a.time).abs() < 0.5, "trace ends {} s before the lap", a.time - a.trace.time.last().unwrap());
        let n = a.trace.x.len().min(b.trace.x.len());
        let mut apart: Vec<f64> = (0..n)
            .filter_map(|i| Some(((a.trace.x[i]? - b.trace.x[i]?).powi(2) + (a.trace.y[i]? - b.trace.y[i]?).powi(2)).sqrt()))
            .collect();
        apart.sort_by(f64::total_cmp);
        let p95 = apart[apart.len() * 95 / 100];
        println!("laps {:.3} and {:.3}: p95 {p95:.1} m apart at equal distance; sectors {:?}", a.time, b.time, a.trace.sectors);
        assert!(p95 < 15.0, "laps don't line up: p95 {p95:.1} m");
        if !a.trace.sectors.is_empty() {
            assert!((a.trace.sectors.iter().sum::<f64>() - a.time).abs() < 0.01);
        }

        let saved = std::env::var("PITWALL_TELEMETRY_DIR").ok().map(|t| read_settings(&PathBuf::from(t).parent().unwrap().join("player").join("Settings"))).unwrap_or_default();
        let latest = stints.iter().rev().find(|s| lap_rows(s.usable().unwrap()).iter().any(|l| l.clean)).unwrap();
        let p = proposal(&store, &saved, latest.id, Toggles::default()).unwrap().unwrap();
        assert_eq!(p.why.changes.len(), p.changes.len(), "Why? and the change list disagree");
        let labelled = p.changes.iter().filter(|c| c.after_text != c.after.to_string()).count();
        for c in &p.changes {
            println!("  {}: {} → {}{} ({})", c.name, c.before_text, if c.approx { "≈ " } else { "" }, c.after_text, c.reason);
        }
        println!("{}", p.why.headline);
        assert!(labelled * 2 >= p.changes.len(), "only {labelled} of {} changes labelled", p.changes.len());
        let plat = p.plan.platform.as_ref().expect("ride heights from labels");
        assert!((10.0..200.0).contains(&plat.current.rh_front) && (10.0..200.0).contains(&plat.current.rh_rear), "{plat:?}");
        assert_eq!(proposal(&store, &saved, latest.id, Toggles::default()).unwrap().unwrap().signature, p.signature, "signature not stable");
        if let Some(c) = p.changes.first() {
            store.set_lock(&latest.signature, &layout, &c.keys[0], true).unwrap();
            let locked = proposal(&store, &saved, latest.id, Toggles::default()).unwrap().unwrap();
            assert_ne!(locked.signature, p.signature, "a lock left the signature unchanged");
            assert_eq!(locked.locks.len(), 1);
            store.set_lock(&latest.signature, &layout, &c.keys[0], false).unwrap();
        }

        assert!(profile(&store).unwrap().classes[0].cars.is_empty());
        store.lock_fingerprint(&stints[0]).unwrap();
        let d = profile(&store).unwrap();
        let car = &d.classes[0].cars[0];
        println!("driver: {} {:?} {:?} {:?}: {}", car.car_name, car.front, car.rear, car.fast, car.line);
        assert!(car.front.is_some() && car.rear.is_some());
    }
}
