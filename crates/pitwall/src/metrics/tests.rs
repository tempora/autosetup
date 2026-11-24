/* End-to-end metric tests.
   Fixture tests synthesise a complete LMU recording (real table layout:
   fixed-rate value/value1..4 tables, ts-indexed event tables, channelsList,
   metadata) from a known slalom trajectory, so expected laps, understeer
   angle and kerb splits follow from the trajectory, not from the code under
   test.  Ignored tests run on local LMU recordings.  */

use std::f64::consts::TAU;
use std::path::PathBuf;

use ::duckdb::{appender_params_from_iter, Connection};

use crate::chassis::{BalanceCell, CornerPhase, Direction, SpeedBand};
use crate::metrics::laps::{lap_distance, segment_laps};
use crate::metrics::{analyse_stint, StintMetrics};
use crate::telemetry::duckdb::DuckDbRecording;
use crate::telemetry::{TelemetryError, KERB_SURFACE_CODE};
use crate::testdata::local_recordings;

const CLOCK_HZ: f64 = 100.0;
const DURATION_S: f64 = 130.0;
const SPEED: f64 = 30.0;
const LAP_LEN: f64 = 1200.0;
const START_DIST: f64 = 100.0;
const PERIOD_M: f64 = 600.0;
const PEAK_CURVATURE: f64 = 1.0 / 60.0;
const WHEELBASE_GT3: f64 = 2.65;
const HALF_LOCK_DEG: f64 = 270.0;
const RATIO: f64 = 15.0;
const PIT_EXIT_S: f64 = 5.0;
const KERB_WINDOWS: [(f64, f64); 2] = [(50.0, 50.6), (80.0, 80.6)];
const SUSP_BASE_M: f64 = 0.05;

struct Fixture {
    path: PathBuf,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

// Right-hand curvature (1/m, + right) along the path.
fn curvature(s: f64) -> f64 {
    PEAK_CURVATURE * (TAU * s / PERIOD_M).sin()
}

fn in_kerb(t: f64) -> bool {
    KERB_WINDOWS.iter().any(|&(a, b)| t >= a && t < b)
}

struct Builder<'a> {
    conn: &'a Connection,
}

impl Builder<'_> {
    fn fixed(&self, name: &str, hz: f64, unit: &str, rows: &[Vec<f64>]) {
        let cols = rows.first().map_or(1, Vec::len);
        let decl = if cols == 1 {
            "value double".to_string()
        } else {
            (1..=cols).map(|c| format!("value{c} double")).collect::<Vec<_>>().join(", ")
        };
        self.conn
            .execute_batch(&format!(
                "create table \"{name}\" ({decl});
                 insert into channelsList values ('{name}', {}, '{unit}');",
                hz as i64
            ))
            .unwrap();
        let mut app = self.conn.appender(name).unwrap();
        for r in rows {
            app.append_row(appender_params_from_iter(r.iter())).unwrap();
        }
        app.flush().unwrap();
    }

    fn event(&self, name: &str, rows: &[(f64, f64)]) {
        self.conn
            .execute_batch(&format!("create table \"{name}\" (ts double, value double);"))
            .unwrap();
        let mut app = self.conn.appender(name).unwrap();
        for &(t, v) in rows {
            app.append_row([t, v]).unwrap();
        }
        app.flush().unwrap();
    }
}

fn sampled(hz: f64, f: impl Fn(f64) -> Vec<f64>) -> Vec<Vec<f64>> {
    let n = (DURATION_S * hz) as usize;
    (0..n).map(|i| f(i as f64 / hz)).collect()
}

#[derive(Clone, Copy)]
struct Rates {
    gps: f64,
    susp: f64,
    surface: f64,
}

impl Default for Rates {
    fn default() -> Self {
        Self { gps: 10.0, susp: 100.0, surface: 5.0 }
    }
}

/* Constant-speed slalom: curvature is a sine of distance, the road-wheel angle
   is `steer_gain` × the kinematic (neutral) angle L·κ, so the true understeer
   angle is (steer_gain − 1)·L·|κ| with the same sign through left and right
   turns.  GPS is integrated from the path at `rates.gps` Hz.  */
fn build_fixture(tag: &str, steer_gain: f64) -> Fixture {
    build_fixture_with_rates(tag, steer_gain, Rates::default())
}

fn build_fixture_with_rates(tag: &str, steer_gain: f64, rates: Rates) -> Fixture {
    let path = std::env::temp_dir().join(format!("pitwall_metrics_{tag}_{}.duckdb", std::process::id()));
    let _ = std::fs::remove_file(&path);
    let conn = Connection::open(&path).unwrap();
    let setup = r#"{"VM_STEER_LOCK": {"available": true, "minValue": 0, "maxValue": 1, "value": 0, "stringValue": "540 (18) deg"}}"#;
    conn.execute_batch(&format!(
        "create table metadata (key varchar, value varchar);
         insert into metadata values ('TrackName', 'Slalom'), ('CarName', 'Fixture GT3 #1:LM'),
           ('CarClass', 'GT3'), ('SessionType', 'Practice'), ('CarSetup', '{setup}');
         create table channelsList (channelName varchar, frequency integer, unit varchar);"
    ))
    .unwrap();
    let b = Builder { conn: &conn };

    let n = (DURATION_S * CLOCK_HZ) as usize;
    let dt = 1.0 / CLOCK_HZ;
    let (mut x, mut y, mut heading) = (0.0f64, 0.0f64, 0.0f64);
    let mut pos = Vec::with_capacity(n);
    for i in 0..n {
        pos.push((x, y));
        let s = START_DIST + SPEED * i as f64 * dt;
        // Heading is counter-clockwise positive; a right turn lowers it.
        heading -= SPEED * curvature(s) * dt;
        x += SPEED * heading.cos() * dt;
        y += SPEED * heading.sin() * dt;
    }
    let dist = |t: f64| START_DIST + SPEED * t;
    let lat0: f64 = 45.6;
    let r_earth = 6_371_000.0;

    b.fixed("GPS Time", CLOCK_HZ, "s", &sampled(CLOCK_HZ, |t| vec![t]));
    b.fixed("Ground Speed", 100.0, "km/h", &sampled(100.0, |_| vec![SPEED * 3.6]));
    b.fixed("Steering Pos", 100.0, "%", &sampled(100.0, |t| {
        let delta_deg = (steer_gain * WHEELBASE_GT3 * curvature(dist(t))).to_degrees();
        vec![delta_deg * RATIO / HALF_LOCK_DEG * 100.0]
    }));
    b.fixed("Throttle Pos", 50.0, "%", &sampled(50.0, |_| vec![60.0]));
    b.fixed("Brake Pos", 50.0, "%", &sampled(50.0, |_| vec![0.0]));
    b.fixed("Lap Dist", 10.0, "m", &sampled(10.0, |t| vec![dist(t) % LAP_LEN]));
    b.fixed("GPS Latitude", rates.gps, "deg", &sampled(rates.gps, |t| {
        let (_, py) = pos[((t * CLOCK_HZ).round() as usize).min(n - 1)];
        vec![lat0 + (py / r_earth).to_degrees()]
    }));
    b.fixed("GPS Longitude", rates.gps, "deg", &sampled(rates.gps, |t| {
        let (px, _) = pos[((t * CLOCK_HZ).round() as usize).min(n - 1)];
        vec![(px / (r_earth * lat0.to_radians().cos())).to_degrees()]
    }));
    b.fixed("RideHeights", 100.0, "m", &sampled(100.0, |_| vec![0.05, 0.05, 0.07, 0.07]));
    b.fixed("TC", 100.0, "", &sampled(100.0, |_| vec![0.0]));
    b.fixed("Brakes Temp", 50.0, "C", &sampled(50.0, |_| vec![600.0, 600.0, 500.0, 500.0]));
    for side in ["TyresTempLeft", "TyresTempCentre", "TyresTempRight"] {
        b.fixed(side, 100.0, "C", &sampled(100.0, |_| vec![85.0; 4]));
    }
    // Kerb windows: fast 20 Hz FL motion strictly inside the SurfaceTypes == 5 span.
    b.fixed("Susp Pos", rates.susp, "m", &sampled(rates.susp, |t| {
        let fast = KERB_WINDOWS.iter().any(|&(a, b)| t >= a + 0.05 && t < b - 0.05);
        let slow = 0.002 * (TAU * t).sin();
        let fl = if fast { 0.005 * (TAU * 20.0 * t).sin() } else { slow };
        vec![SUSP_BASE_M + fl, SUSP_BASE_M + slow, SUSP_BASE_M + slow, SUSP_BASE_M + slow]
    }));
    b.fixed("SurfaceTypes", rates.surface, "", &sampled(rates.surface, |t| {
        vec![if in_kerb(t) { f64::from(KERB_SURFACE_CODE) } else { 0.0 }, 0.0, 0.0, 0.0]
    }));

    let first_lap = 7.0;
    let crossings: Vec<(f64, f64)> = (1..)
        .map(|k| (k as f64 * LAP_LEN - START_DIST) / SPEED)
        .take_while(|&t| t < DURATION_S - dt)
        .enumerate()
        .map(|(k, t)| (t, first_lap + 1.0 + k as f64))
        .collect();
    let lap_rows: Vec<(f64, f64)> = std::iter::once((0.0, first_lap)).chain(crossings).collect();
    b.event("Lap", &lap_rows);
    b.event("In Pits", &[(0.0, 1.0), (PIT_EXIT_S, 0.0)]);
    b.event("ABS", &[(0.0, 0.0)]);
    drop(conn);
    Fixture { path }
}

#[test]
fn laps_mark_partial_and_pit_segments_and_distance_never_blends_across_the_line() {
    let fx = build_fixture("laps", 1.0);
    let rec = DuckDbRecording::open(&fx.path).unwrap();
    let laps = segment_laps(&rec).unwrap();

    // Crossings at 36.67 s, 76.67 s, 116.67 s: partial, two complete, partial.
    assert_eq!(laps.len(), 4);
    assert_eq!(laps.iter().map(|l| l.number).collect::<Vec<_>>(), vec![7, 8, 9, 10]);
    assert_eq!(laps.iter().map(|l| l.complete).collect::<Vec<_>>(), vec![false, true, true, false]);
    assert_eq!(laps.iter().map(|l| l.in_pits).collect::<Vec<_>>(), vec![true, false, false, false]);
    assert!((laps[1].time().raw() - LAP_LEN / SPEED).abs() < 0.02);

    let grid: Vec<f64> = (0..(DURATION_S * 50.0) as usize - 10).map(|i| i as f64 / 50.0).collect();
    let d = lap_distance(&rec, &grid).unwrap();
    let worst = grid
        .iter()
        .zip(&d)
        .map(|(&t, &di)| {
            let truth = (START_DIST + SPEED * t) % LAP_LEN;
            let e = (di - truth).abs();
            e.min(LAP_LEN - e)
        })
        .fold(0.0, f64::max);
    // The 10 Hz sample before the wrap reads ~LAP_LEN; its max sets the wrap length.
    assert!(worst < 3.5, "lap distance blended across the line: worst error {worst} m");
}

fn analyse(fx: &Fixture) -> StintMetrics {
    analyse_stint(&DuckDbRecording::open(&fx.path).unwrap()).unwrap()
}

fn cell_ua(m: &StintMetrics, phase: CornerPhase) -> f64 {
    m.cells
        .iter()
        .find(|c| c.cell == BalanceCell { phase, band: SpeedBand::Low })
        .unwrap_or_else(|| panic!("no {phase:?} cell"))
        .ua
        .raw()
}

#[test]
fn understeer_angle_sign_is_consistent_through_left_and_right_corners() {
    let under = analyse(&build_fixture("under", 1.25));
    let over = analyse(&build_fixture("over", 0.80));

    for m in [&under, &over] {
        assert!(m.corners.iter().any(|c| c.direction == Direction::Left));
        assert!(m.corners.iter().any(|c| c.direction == Direction::Right));
        assert!(m.observations.iter().all(|o| o.cell.band == SpeedBand::Low));
        assert!(m.observations.iter().all(|o| o.countersteer_s.raw() == 0.0));
    }

    // Mid-corner sits near peak curvature: |ua| ≈ |gain − 1| · L · κ_peak.
    let peak_deg = |gain: f64| ((gain - 1.0) * WHEELBASE_GT3 * PEAK_CURVATURE).to_degrees();
    for (m, gain) in [(&under, 1.25), (&over, 0.80)] {
        for phase in [CornerPhase::Entry, CornerPhase::Mid, CornerPhase::Exit] {
            let ua = cell_ua(m, phase);
            assert_eq!(ua.signum(), (gain - 1.0f64).signum(), "{phase:?} ua {ua} for gain {gain}");
        }
        let mid = cell_ua(m, CornerPhase::Mid);
        let expect = peak_deg(gain);
        assert!(
            (mid / expect) > 0.75 && (mid / expect) < 1.05,
            "mid ua {mid} vs peak {expect} for gain {gain}"
        );
    }

    // Lateral acceleration p95 tracks v²·κ_peak.
    let a_peak = SPEED * SPEED * PEAK_CURVATURE / crate::units::GRAVITY;
    let mid_lat = under
        .cells
        .iter()
        .find(|c| c.cell.phase == CornerPhase::Mid)
        .unwrap()
        .a_lat_p95
        .raw();
    assert!((mid_lat / a_peak - 1.0).abs() < 0.1, "mid a_lat p95 {mid_lat} vs {a_peak}");
}

#[test]
fn kerb_samples_are_split_strictly_by_surface_type() {
    let m = analyse(&build_fixture("kerbs", 1.0));
    let w = &m.dampers.wheels;
    // Two 0.6 s windows at 100 Hz, all on moving, non-pit laps.
    assert_eq!(w.fl.on.total(), 120);
    assert_eq!(w.fr.on.total() + w.rl.on.total() + w.rr.on.total(), 0);

    let knee = m.dampers.knee;
    // Off kerb the slow 1 Hz motion peaks at 12.6 mm/s: all below the knee.
    assert_eq!(w.fl.off.slow_share(knee), Some(1.0));
    // On kerb the 20 Hz motion peaks at 628 mm/s.
    assert!(w.fl.on.slow_share(knee).unwrap() < 0.3);

    assert_eq!(m.kerb_events.len(), 2);
    for (ev, &(a, _)) in m.kerb_events.iter().zip(&KERB_WINDOWS) {
        assert!((ev.t0.raw() - a).abs() < 0.011);
        assert!(ev.wheels.fl && !ev.wheels.fr && !ev.wheels.rl && !ev.wheels.rr);
        let peak = ev.peak_load.unwrap();
        // (base + 5 mm) / base, against a usual load that the slow motion leaves at base.
        assert!((peak - 1.1).abs() < 0.01, "peak_load {peak}");
    }
    assert_eq!(m.kerbs.events, 2);
    assert!(m.observations.iter().any(|o| o.kerb));
    assert!(m.observations.iter().any(|o| !o.kerb));
}

#[test]
fn slow_vital_channel_is_rejected_and_high_rate_gps_matches_default() {
    let slow_susp = build_fixture_with_rates("slow_susp", 1.25, Rates { susp: 20.0, ..Rates::default() });
    match analyse_stint(&DuckDbRecording::open(&slow_susp.path).unwrap()) {
        Err(TelemetryError::ChannelTooSlow { channel, min_hz, actual_hz }) => {
            assert_eq!(channel, "Susp Pos");
            assert_eq!(min_hz, 50.0);
            assert!((actual_hz - 20.0).abs() < 1e-6);
        }
        other => panic!("expected ChannelTooSlow for Susp Pos, got {other:?}"),
    }

    let slow_surf = build_fixture_with_rates("slow_surf", 1.25, Rates { surface: 1.0, ..Rates::default() });
    match analyse_stint(&DuckDbRecording::open(&slow_surf.path).unwrap()) {
        Err(TelemetryError::ChannelTooSlow { channel, .. }) => assert_eq!(channel, "SurfaceTypes"),
        other => panic!("expected ChannelTooSlow for SurfaceTypes, got {other:?}"),
    }

    let default_gps = analyse(&build_fixture("gps10", 1.25));
    let fast_gps = analyse(&build_fixture_with_rates("gps50", 1.25, Rates { gps: 50.0, ..Rates::default() }));
    let diff = (cell_ua(&default_gps, CornerPhase::Mid) - cell_ua(&fast_gps, CornerPhase::Mid)).abs();
    assert!(diff < 0.05, "50 Hz GPS diverged from 10 Hz GPS: diff {diff:.4} deg");
}

/* Plausibility on real LMU stints: every recording with a flying lap yields
   corners, balance cells inside physical bounds, off-kerb histograms and
   (where kerbs were touched) kerb events.  A missing channel is reported by
   name and fails the run.  */
#[test]
#[ignore = "requires local LMU .duckdb recordings (PITWALL_TELEMETRY_DIR or PITWALL_TEST_RECORDING)"]
fn real_recordings_produce_plausible_stint_metrics() {
    let mut failures = Vec::new();
    let mut analysed = 0;
    for path in local_recordings() {
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        let rec = match DuckDbRecording::open(&path) {
            Ok(r) => r,
            Err(e) => {
                println!("{name}: skipped (open: {e})");
                continue;
            }
        };
        let m = match analyse_stint(&rec) {
            Ok(m) => m,
            Err(TelemetryError::TooShort(secs)) => {
                println!("{name}: skipped ({secs:.2} s)");
                continue;
            }
            Err(e @ TelemetryError::MissingChannel(_)) => {
                failures.push(format!("{name}: {e}"));
                continue;
            }
            Err(e) => panic!("{name}: {e}"),
        };
        let flying = m.laps.iter().filter(|l| l.is_flying()).count();
        let off: u64 = m.dampers.wheels.iter().map(|w| w.off.total()).sum();
        let on: u64 = m.dampers.wheels.iter().map(|w| w.on.total()).sum();
        println!(
            "{name}: {:?} laps {} flying {} corners {} obs {} cells {} off-kerb {} on-kerb {} kerb events {} shape {:?}",
            m.class,
            m.laps.len(),
            flying,
            m.corners.len(),
            m.observations.len(),
            m.cells.len(),
            off,
            on,
            m.kerbs.events,
            m.dampers.front_rear_shape.map(|s| (s * 1000.0).round() / 1000.0),
        );
        for c in &m.cells {
            println!(
                "    {:?}/{:?}: n {:>3} ua {:+.2}±{:.2} deg a_lat95 {:.2} g v_min {:.0} km/h a_lon {:+.2} g cs {:.2} s tc {:.2} s",
                c.cell.phase,
                c.cell.band,
                c.passes,
                c.ua.raw(),
                c.ua_se.raw(),
                c.a_lat_p95.raw(),
                c.v_min.to_kmh(),
                c.a_lon.raw(),
                c.countersteer_s.raw(),
                c.tc_s.raw()
            );
        }
        if flying == 0 {
            continue;
        }
        analysed += 1;
        assert!(m.corners.len() >= 3, "{name}: only {} corners", m.corners.len());
        assert!(!m.cells.is_empty(), "{name}: no balance cells");
        for c in &m.cells {
            assert!(c.ua.raw().abs() < 5.0, "{name}: implausible ua {:?}", c);
            assert!(c.a_lat_p95.raw() < 4.5, "{name}: implausible a_lat {:?}", c);
            // Braking samples precede turn-in (|a_lat| < CORNER_G) and must decelerate.
            if c.cell.phase == CornerPhase::Braking {
                assert!(c.a_lon.raw() < -0.3, "{name}: braking cell not decelerating {:?}", c);
            }
        }
        assert!(off > 1000, "{name}: off-kerb histogram nearly empty");
        assert!(m.dampers.axles.front.is_some() && m.dampers.axles.rear.is_some());
    }
    assert!(failures.is_empty(), "missing channels:\n{}", failures.join("\n"));
    assert!(analysed > 0, "no recording with a flying lap");
}
