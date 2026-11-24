/* Planner tests: every toggle is run OFF vs ON through the full plan()
   (space → target → QSS search → gates → rules) and checked for the expected
   physical direction and for non-degenerate, counted gate behaviour.  The
   ignored release-gate test prints plain-English proposals for local LMU
   recordings with each toggle OFF and ON.  */

use std::collections::BTreeSet;

use crate::chassis::{BalanceCell, CarClass, CornerPhase, SpeedBand};
use crate::metrics::balance::CellStats;
use crate::opt::rules::Rule;
use crate::opt::solve::{DriverFlags, Gate, Outcome, Toggles};
use crate::opt::space::DimKind;
use crate::opt::target::{FeelAxis, Sliders};
use crate::opt::why::explain;
use crate::opt::{plan, PlanInput, Proposal};
use crate::physics::calibrate::Verification;
use crate::physics::{QssModel, VehicleParams};
use crate::setup::CarSetup;
use crate::units::{Degrees, Gs, MetersPerSec, Seconds};

fn param(min: i32, max: i32, value: i32) -> String {
    format!(r#"{{"available": true, "minValue": {min}, "maxValue": {max}, "value": {value}, "stringValue": "{value}"}}"#)
}

fn gt3_setup() -> CarSetup {
    let entries = [
        ("VM_REAR_WING", param(0, 12, 5)),
        ("WM_RIDEHEIGHT-W_FL", param(0, 20, 3)),
        ("WM_RIDEHEIGHT-W_FR", param(0, 20, 3)),
        ("WM_RIDEHEIGHT-W_RL", param(0, 20, 8)),
        ("WM_RIDEHEIGHT-W_RR", param(0, 20, 8)),
        ("VM_FRONT_ANTISWAY", param(0, 11, 5)),
        ("VM_REAR_ANTISWAY", param(0, 11, 5)),
        ("WM_SPRING-W_FL", param(0, 11, 5)),
        ("WM_SPRING-W_FR", param(0, 11, 5)),
        ("WM_SPRING-W_RL", param(0, 11, 5)),
        ("WM_SPRING-W_RR", param(0, 11, 5)),
        ("VM_DIFF_PRELOAD", param(0, 11, 5)),
        ("VM_DIFF_POWER", param(0, 11, 5)),
        ("VM_DIFF_COAST", param(0, 11, 5)),
        ("VM_BRAKE_BALANCE", param(0, 41, 20)),
        ("VM_BRAKE_PRESSURE", param(0, 21, 18)),
    ];
    let json = format!(
        "{{{}}}",
        entries.iter().map(|(k, v)| format!("\"{k}\": {v}")).collect::<Vec<_>>().join(", ")
    );
    CarSetup::from_lmu_json(&json).unwrap()
}

fn cells() -> Vec<CellStats> {
    let rows = [
        (CornerPhase::Braking, SpeedBand::Low, 0.05, 0.3, -1.5),
        (CornerPhase::Entry, SpeedBand::Low, 0.80, 1.6, -0.4),
        (CornerPhase::Mid, SpeedBand::Low, 1.20, 1.7, 0.0),
        (CornerPhase::Exit, SpeedBand::Low, 0.50, 1.5, 0.4),
        (CornerPhase::Braking, SpeedBand::High, 0.05, 0.3, -1.7),
        (CornerPhase::Entry, SpeedBand::High, 0.50, 1.9, -0.2),
        (CornerPhase::Mid, SpeedBand::High, 0.70, 2.0, 0.1),
        (CornerPhase::Exit, SpeedBand::High, 0.30, 1.9, 0.3),
    ];
    rows.iter()
        .map(|&(phase, band, ua, lat, lon)| CellStats {
            cell: BalanceCell { phase, band },
            passes: 20,
            ua: Degrees::new(ua),
            ua_se: Degrees::new(0.05),
            a_lat_p95: Gs::new(lat),
            v_min: MetersPerSec::from_kmh(if band == SpeedBand::Low { 90.0 } else { 180.0 }),
            a_lon: Gs::new(lon),
            countersteer_s: Seconds::ZERO,
            tc_s: Seconds::ZERO,
        })
        .collect()
}

struct Case {
    setup: CarSetup,
    cells: Vec<CellStats>,
    sliders: Sliders,
    flags: DriverFlags,
    toggles: Toggles,
    locks: BTreeSet<String>,
}

impl Case {
    fn new() -> Self {
        Self {
            setup: gt3_setup(),
            cells: cells(),
            sliders: Sliders::default(),
            flags: DriverFlags::default(),
            toggles: Toggles::default(),
            locks: BTreeSet::new(),
        }
    }

    fn slider(mut self, axis: FeelAxis, steps: f64) -> Self {
        self.sliders = self.sliders.with(axis, CarClass::Gt3.feel_step() * steps);
        self
    }

    fn run(&self) -> Proposal {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        plan(
            &model,
            &PlanInput {
                class: CarClass::Gt3,
                setup: &self.setup,
                cells: &self.cells,
                fingerprint: None,
                sliders: self.sliders,
                flags: self.flags.clone(),
                toggles: self.toggles,
                locks: self.locks.clone(),
                verification: Verification::default(),
            },
        )
    }
}

// Mean predicted ua over the phase's usable cells; None without one.
fn mean_predicted(p: &Proposal, phase: CornerPhase) -> Option<f64> {
    let sel: Vec<f64> = p.solution.chosen.cells.iter().filter(|c| c.cell.phase == phase).map(|c| c.predicted_ua.raw()).collect();
    (!sel.is_empty()).then(|| sel.iter().sum::<f64>() / sel.len() as f64)
}

fn moved_up(p: &Proposal, kind: DimKind) -> bool {
    p.solution.changes.iter().any(|c| c.kind == kind && c.to > c.from)
}

#[test]
fn pointier_slider_moves_turn_in_toward_rotation() {
    let off = Case::new().run();
    let on = Case::new().slider(FeelAxis::Front, -1.0).run();
    assert_eq!(on.solution.outcome, Outcome::Changed, "{}", explain(&on));
    let (e_off, e_on) = (mean_predicted(&off, CornerPhase::Entry).unwrap(), mean_predicted(&on, CornerPhase::Entry).unwrap());
    assert!(e_on < e_off - 0.02, "entry ua OFF {e_off:.3} ON {e_on:.3}\n{}", explain(&on));
    assert!(on.solution.chosen.band_excess < on.solution.base.band_excess);
}

#[test]
fn safer_slider_moves_exit_toward_understeer() {
    let off = Case::new().run();
    let on = Case::new().slider(FeelAxis::Rear, 1.0).run();
    assert_eq!(on.solution.outcome, Outcome::Changed, "{}", explain(&on));
    let (x_off, x_on) = (mean_predicted(&off, CornerPhase::Exit).unwrap(), mean_predicted(&on, CornerPhase::Exit).unwrap());
    assert!(x_on > x_off + 0.02, "exit ua OFF {x_off:.3} ON {x_on:.3}\n{}", explain(&on));
}

#[test]
fn kerbs_unstable_never_stiffens_bars_or_springs_and_counts_rejections() {
    let mut case = Case::new().slider(FeelAxis::Front, -1.0);
    let off = case.run();
    case.flags.kerbs_unstable = true;
    let on = case.run();
    assert!(!off.solution.rejected.contains_key(&Gate::KerbStiffening));
    assert!(on.solution.rejected.get(&Gate::KerbStiffening).copied().unwrap_or(0) > 0);
    for kind in [DimKind::ArbFront, DimKind::ArbRear, DimKind::SpringFront, DimKind::SpringRear] {
        assert!(!moved_up(&on, kind), "{kind:?} stiffened with kerbs unstable\n{}", explain(&on));
    }
    assert_ne!(on.solution.outcome, Outcome::AllRejected, "{}", explain(&on));
}

#[test]
fn gt3_front_ride_height_goes_to_minimum_and_is_never_searched() {
    let p = Case::new().slider(FeelAxis::Front, -1.0).run();
    assert!(p.solution.changes.iter().all(|c| c.kind != DimKind::RideHeightFront));
    let rule = p.rules.iter().find(|r| r.rule == Rule::FrontRideHeightAtMin).expect("front ride height rule");
    assert_eq!((rule.from, rule.to), (3, 0));
    let out = p.setup_from(&Case::new().setup);
    assert_eq!(out.params["WM_RIDEHEIGHT-W_FL"].value, 0);
    assert_eq!(out.params["WM_RIDEHEIGHT-W_FR"].value, 0);
}

#[test]
fn lockups_take_brake_pressure_down_one_click() {
    let mut case = Case::new();
    assert!(case.run().rules.iter().all(|r| r.rule != Rule::LockupsBrakePressure));
    case.flags.lockups = true;
    let on = case.run();
    let r = on.rules.iter().find(|r| r.rule == Rule::LockupsBrakePressure).expect("lockup rule");
    assert_eq!((r.from, r.to), (18, 17));
}

#[test]
fn undrivable_stint_moves_are_not_repeated() {
    let base = Case::new().slider(FeelAxis::Front, -1.0);
    let off = base.run();
    let pushed: Vec<_> = off.solution.changes.clone();
    assert!(!pushed.is_empty());
    // The driver could not drive a stint that made OFF's first move.
    let first = pushed[0];
    let mut bad = gt3_setup();
    for k in first.kind.keys() {
        bad.set_value(k, first.to);
    }
    let mut case = base;
    case.flags.undrivable = Some(bad);
    let on = case.run();
    assert!(on.solution.rejected.get(&Gate::Undrivable).copied().unwrap_or(0) > 0);
    let step = (first.to - first.from).signum();
    assert!(
        on.solution.changes.iter().all(|c| c.kind != first.kind || (c.to - c.from) * step <= 0),
        "repeated {first:?}\n{}",
        explain(&on)
    );
}

#[test]
fn every_move_blocked_is_reported_not_silent() {
    let mut case = Case::new().slider(FeelAxis::Front, -1.0);
    case.locks = gt3_setup().params.keys().filter(|k| *k != "VM_REAR_ANTISWAY").cloned().collect();
    case.flags.kerbs_unstable = true;
    let mut bad = gt3_setup();
    bad.set_value("VM_REAR_ANTISWAY", 4);
    case.flags.undrivable = Some(bad);
    let p = case.run();
    assert_eq!(p.solution.outcome, Outcome::AllRejected);
    assert!(p.solution.rejected.contains_key(&Gate::KerbStiffening));
    assert!(p.solution.rejected.contains_key(&Gate::Undrivable));
    let why = explain(&p);
    assert!(why.headline.contains("blocked"));
    assert!(why.notes.len() >= 2, "{why}");
}

#[test]
fn countersteer_guard_keeps_rotation_out_of_cells_that_already_slid() {
    let mut case = Case::new().slider(FeelAxis::Front, -2.0);
    for c in case.cells.iter_mut().filter(|c| c.cell.phase == CornerPhase::Mid) {
        c.countersteer_s = Seconds::new(0.2);
    }
    let on = case.run();
    for p in on.solution.chosen.cells.iter().filter(|c| c.cell.phase == CornerPhase::Mid) {
        let asked = p.target.is_some_and(|t| t.centre < p.measured_ua);
        if !asked {
            assert!(p.predicted_ua >= p.measured_ua - Degrees::new(1e-6), "{:?}\n{}", p.cell, explain(&on));
        }
    }
    case.toggles.countersteer_guard = false;
    let off = case.run();
    assert!(!off.solution.rejected.contains_key(&Gate::Countersteer));
}

#[cfg(feature = "duckdb-native")]
mod release_gate {
    use std::collections::BTreeMap;

    use super::*;
    use crate::metrics::analyse_stint;
    use crate::telemetry::duckdb::DuckDbRecording;
    use crate::telemetry::TelemetrySource;
    use crate::testdata::local_recordings;

    /* Prints the plain-English proposal for the latest flying stint of every
       (car, layout) with each toggle OFF and ON, and checks each toggle's
       physical direction.
       Run: cargo test -p pitwall --release release_gate -- --ignored --nocapture  */
    #[test]
    #[ignore = "requires local LMU .duckdb recordings (PITWALL_TELEMETRY_DIR)"]
    fn release_gate_prints_proposals_with_toggles_off_and_on() {
        let mut latest: BTreeMap<(String, String), std::path::PathBuf> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(rec) = DuckDbRecording::open(&path) else { continue };
            let Ok(meta) = rec.metadata() else { continue };
            if !meta.setup.params.is_empty() {
                latest.insert((meta.car_name.clone(), meta.layout.clone()), path);
            }
        }
        let mut changed = 0;
        let mut planned = 0;
        for ((car, layout), path) in latest {
            let rec = DuckDbRecording::open(&path).unwrap();
            let meta = rec.metadata().unwrap();
            let Ok(m) = analyse_stint(&rec) else { continue };
            if !m.laps.iter().any(|l| l.is_flying()) {
                continue;
            }
            planned += 1;
            let cells = m.cells_off_kerb();
            let (model, verification) = crate::physics::calibration::model_for(m.class);
            let run = |sliders: Sliders, flags: DriverFlags, toggles: Toggles| {
                plan(&model, &PlanInput {
                    class: m.class,
                    setup: &meta.setup,
                    cells: &cells,
                    fingerprint: None,
                    sliders,
                    flags,
                    toggles,
                    locks: BTreeSet::new(),
                    verification,
                })
            };
            let step = m.class.feel_step();
            let base = run(Sliders::default(), DriverFlags::default(), Toggles::default());
            println!("\n=== {car} @ {layout} ({:?}) ===\n{}", m.class, explain(&base));

            let toggles: Vec<(&str, Proposal)> = vec![
                ("Pointier", run(Sliders::default().with(FeelAxis::Front, -step), DriverFlags::default(), Toggles::default())),
                ("Safer", run(Sliders::default().with(FeelAxis::Rear, step), DriverFlags::default(), Toggles::default())),
                ("Kerbs unstable", run(Sliders::default().with(FeelAxis::Front, -step), DriverFlags { kerbs_unstable: true, ..DriverFlags::default() }, Toggles::default())),
                ("Lock-ups", run(Sliders::default(), DriverFlags { lockups: true, ..DriverFlags::default() }, Toggles::default())),
                ("Countersteer guard off", run(Sliders::default(), DriverFlags::default(), Toggles { countersteer_guard: false })),
            ];
            for (name, p) in &toggles {
                let w = explain(p);
                println!("--- {name} ON: {}", w.headline);
                for l in w.changes.iter().chain(&w.notes) {
                    println!("    {l}");
                }
            }

            // OFF may already use each setting's full reach toward rotation, so ON
            // must never give less rotation, and must add some on some stint.
            let pointier = &toggles[0].1;
            if let (Some(e_on), Some(e_off)) = (mean_predicted(pointier, CornerPhase::Entry), mean_predicted(&base, CornerPhase::Entry)) {
                assert!(e_on <= e_off + 1e-9, "{car} @ {layout}: Pointier gave less turn-in rotation ({e_on:.3} > {e_off:.3})");
                if e_on < e_off - 1e-6 {
                    changed += 1;
                }
            }
            let safer = &toggles[1].1;
            if let (Some(x_on), Some(x_off)) = (mean_predicted(safer, CornerPhase::Exit), mean_predicted(&base, CornerPhase::Exit)) {
                assert!(x_on >= x_off - 1e-9, "{car} @ {layout}: Safer gave less exit understeer ({x_on:.3} < {x_off:.3})");
            }
            for kind in [DimKind::ArbFront, DimKind::ArbRear, DimKind::SpringFront, DimKind::SpringRear] {
                assert!(!moved_up(&toggles[2].1, kind), "{car} @ {layout}: stiffened {kind:?} with kerbs unstable");
            }
            let pressure = meta.setup.params.get("VM_BRAKE_PRESSURE");
            if pressure.is_some_and(|p| p.adjustable() && p.value > p.min) {
                assert!(toggles[3].1.rules.iter().any(|r| r.rule == Rule::LockupsBrakePressure), "{car} @ {layout}: lock-ups ignored");
            }
            if m.class == CarClass::Gt3 {
                for p in std::iter::once(&base).chain(toggles.iter().map(|t| &t.1)) {
                    let out = p.setup_from(&meta.setup);
                    let fl = &out.params.get("WM_RIDEHEIGHT-W_FL");
                    assert!(fl.is_none_or(|q| !q.adjustable() || q.value == q.min), "{car} @ {layout}: GT3 front ride height not at minimum");
                }
            }
        }
        println!("\n{planned} stints planned, Pointier added turn-in rotation on {changed}");
        assert!(planned > 0, "no flying stint to plan");
        assert!(changed > 0, "Pointier never added turn-in rotation: the planner is degenerate");
    }
}
