/* Planner tests: every toggle is run OFF vs ON through the full plan()
   (space → target → QSS search → gates → rules) and checked for the expected
   physical direction and for non-degenerate, counted gate behaviour.  The
   ignored release-gate test prints plain-English proposals for local LMU
   recordings with each toggle OFF and ON.  */

use std::collections::BTreeSet;

use crate::chassis::{BalanceCell, CarClass, CornerPhase, SpeedBand, Wheel};
use crate::metrics::balance::CellStats;
use crate::metrics::summary::StintSummary;
use crate::opt::dampers::{DamperPlan, DamperStint, Family, Metric};
use crate::opt::rules::Rule;
use crate::opt::solve::{DriverFlags, Gate, Outcome, Toggles};
use crate::opt::space::DimKind;
use crate::opt::target::{Anchor, FeelAxis, Sliders};
use crate::opt::why::explain;
use crate::opt::{plan, PlanInput, Proposal};
use crate::physics::calibrate::Verification;
use crate::physics::{CornerState, MechanicalModel, QssModel, VehicleParams};
use crate::setup::{CarSetup, MechanicalSetup};
use crate::units::{Degrees, Gs, MetersPerSec, Seconds};

fn param(min: i32, max: i32, value: i32) -> String {
    format!(r#"{{"available": true, "minValue": {min}, "maxValue": {max}, "value": {value}, "stringValue": "{value}"}}"#)
}

fn setup_from(entries: &[(&str, String)]) -> CarSetup {
    let json = format!(
        "{{{}}}",
        entries.iter().map(|(k, v)| format!("\"{k}\": {v}")).collect::<Vec<_>>().join(", ")
    );
    CarSetup::from_lmu_json(&json).unwrap()
}

const WHEEL_SUFFIX: [&str; 4] = ["FL", "FR", "RL", "RR"];

fn tyre_and_brake_params() -> Vec<(String, String)> {
    let mut v: Vec<(String, String)> = WHEEL_SUFFIX
        .iter()
        .flat_map(|w| {
            [
                (format!("WM_PRESSURE-W_{w}"), param(0, 36, 10)),
                (format!("WM_CAMBER-W_{w}"), param(0, 31, 20)),
                (format!("WM_SLOWBUMP-W_{w}"), param(0, 13, 6)),
                (format!("WM_SLOWREBOUND-W_{w}"), param(0, 13, 6)),
                (format!("WM_FASTBUMP-W_{w}"), param(0, 13, 6)),
                (format!("WM_FASTREBOUND-W_{w}"), param(0, 13, 6)),
            ]
        })
        .collect();
    v.push(("VM_BRAKE_DUCTS".into(), param(0, 11, 3)));
    v.push(("VM_BRAKE_DUCTS_REAR".into(), param(0, 11, 3)));
    v
}

fn with_tyres_and_brakes(entries: &[(&str, String)]) -> CarSetup {
    let extra = tyre_and_brake_params();
    let mut all: Vec<(&str, String)> = entries.to_vec();
    all.extend(extra.iter().map(|(k, v)| (k.as_str(), v.clone())));
    setup_from(&all)
}

fn hyper_setup() -> CarSetup {
    with_tyres_and_brakes(&[
        ("VM_REAR_WING", param(0, 20, 8)),
        ("WM_RIDEHEIGHT-W_FL", param(0, 31, 4)),
        ("WM_RIDEHEIGHT-W_FR", param(0, 31, 4)),
        ("WM_RIDEHEIGHT-W_RL", param(0, 26, 10)),
        ("WM_RIDEHEIGHT-W_RR", param(0, 26, 10)),
        ("VM_FRONT_ANTISWAY", param(0, 16, 5)),
        ("VM_REAR_ANTISWAY", param(0, 16, 3)),
        ("VM_FRONT_3RD_PACKERS", param(0, 21, 2)),
        ("VM_REGEN_LEVEL", param(0, 11, 10)),
        (
            "VM_BRAKE_PRESSURE",
            r#"{"available": true, "minValue": 0, "maxValue": 81, "value": 78, "stringValue": "118 kgf (98%)"}"#.to_string(),
        ),
    ])
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
    let mut all: Vec<(&str, String)> = entries.to_vec();
    for family in ["SLOWBUMP", "SLOWREBOUND", "FASTBUMP", "FASTREBOUND"] {
        for w in WHEEL_SUFFIX {
            let key: &'static str = Box::leak(format!("WM_{family}-W_{w}").into_boxed_str());
            all.push((key, param(0, 12, 5)));
        }
    }
    with_tyres_and_brakes(&all)
}

// A stint's damper metrics at their variance floor.
fn damper_stint(setup: CarSetup, values: &[(Metric, f64)]) -> DamperStint {
    DamperStint {
        setup,
        values: values.iter().map(|&(m, v)| (m, (v, (0.2 * m.scale()).powi(2)))).collect(),
    }
}

fn usual_damper_values() -> Vec<(Metric, f64)> {
    vec![
        (Metric::CvFront, 0.20),
        (Metric::CvRear, 0.20),
        (Metric::SymFront, 0.03),
        (Metric::SymRear, 0.03),
        (Metric::KneeFront, 0.05),
        (Metric::KneeRear, 0.05),
        (Metric::Shape, 0.3),
        (Metric::PeakLoad, 1.6),
        (Metric::AirtimeS, 0.03),
        (Metric::PitchDeg, 0.4),
        (Metric::HeaveMm, 6.0),
        (Metric::YawDist, 0.15),
    ]
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
    class: CarClass,
    summary: StintSummary,
    damper_history: Vec<DamperStint>,
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
            class: CarClass::Gt3,
            summary: StintSummary::default(),
            damper_history: vec![damper_stint(gt3_setup(), &usual_damper_values())],
            setup: gt3_setup(),
            cells: cells(),
            sliders: Sliders::default(),
            flags: DriverFlags::default(),
            toggles: Toggles::default(),
            locks: BTreeSet::new(),
        }
    }

    fn slider(mut self, axis: FeelAxis, steps: f64) -> Self {
        self.sliders = self.sliders.with(axis, self.class.feel_step() * steps);
        self
    }

    fn run(&self) -> Proposal {
        let model = QssModel::new(VehicleParams::for_class(self.class));
        plan(
            &model,
            &PlanInput {
                class: self.class,
                setup: &self.setup,
                cells: &self.cells,
                summary: &self.summary,
                damper_history: &self.damper_history,
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
fn default_fast_neutral_fixes_understeer_without_touching_sliders_and_never_moves_wing() {
    let p = Case::new().run();
    assert_eq!(p.target.anchor, Anchor::FastNeutral);
    assert_eq!(p.solution.outcome, Outcome::Changed, "{}", explain(&p));
    assert!(p.solution.chosen.band_excess < p.solution.base.band_excess);
    let mid_before: f64 = p.solution.base.cells.iter().filter(|c| c.cell.phase == CornerPhase::Mid).map(|c| c.measured_ua.raw()).sum();
    let mid_after: f64 = p.solution.chosen.cells.iter().filter(|c| c.cell.phase == CornerPhase::Mid).map(|c| c.predicted_ua.raw()).sum();
    assert!(mid_after < mid_before - 0.05, "fast-neutral did not reduce understeer wash: {mid_before:.3} -> {mid_after:.3}\n{}", explain(&p));
    let out = p.setup_from(&Case::new().setup);
    assert_eq!(out.params["VM_REAR_WING"].value, 5, "drag-blind QSS must not alter rear wing");
}

#[test]
fn shipped_calibration_never_disables_load_sensitivity_or_roll_stiffness() {
    let base = MechanicalSetup::default();
    let mut stiffer_rear = base;
    stiffer_rear.arb.rear = 0.9;
    let state = CornerState::steady(MetersPerSec::from_kmh(100.0), CornerPhase::Mid);
    for class in [CarClass::Gt3, CarClass::Lmp2, CarClass::Lmp2Elms, CarClass::Hyper] {
        let (model, _) = crate::physics::calibration::model_for(class);
        assert!(model.terms.load_sensitivity, "{class:?} disabled load_sensitivity");
        let p0 = model.evaluate(&base, state);
        let p1 = model.evaluate(&stiffer_rear, state);
        assert!(
            p1.understeer < p0.understeer - Degrees::new(1e-4),
            "{class:?} rear ARB had no effect on understeer ({:?} -> {:?})",
            p0.understeer,
            p1.understeer
        );
    }
}

fn damper_moves(p: &DamperPlan, family: Family, front: bool) -> Vec<i32> {
    p.changes
        .iter()
        .filter(|c| c.setting.family == family && !c.setting.third && (c.setting.axle == crate::opt::dampers::Axle::Front) == front)
        .map(|c| c.to - c.from)
        .collect()
}

// Stints of the test car with front fast bump at each index and the kerb
// numbers `peak(index)` measured there.
fn fast_bump_history(indices: &[i32], peak: impl Fn(i32) -> f64) -> Vec<DamperStint> {
    indices
        .iter()
        .map(|&i| {
            let mut setup = gt3_setup();
            setup.set_value("WM_FASTBUMP-W_FL", i);
            setup.set_value("WM_FASTBUMP-W_FR", i);
            let mut v = usual_damper_values();
            for (m, x) in v.iter_mut() {
                if *m == Metric::PeakLoad {
                    *x = peak(i);
                }
            }
            damper_stint(setup, &v)
        })
        .collect()
}

#[test]
fn damper_effects_are_learned_from_this_cars_stints() {
    // Kerb peaks grow with front fast bump, as the prior expects: soften it.
    let mut case = Case::new();
    case.damper_history = fast_bump_history(&[3, 7, 4, 6, 5], |i| 1.2 + 0.15 * f64::from(i));
    let p = case.run();
    assert!(
        damper_moves(&p.dampers, Family::FastBump, true).iter().any(|&d| d < 0),
        "kerb peaks rise with fast bump, yet it was not softened: {:?}\n{}",
        p.dampers,
        explain(&p)
    );

    // This car's stints say the opposite: softer front fast bump hit the kerbs harder.
    case.damper_history = fast_bump_history(&[3, 7, 4, 6, 3, 7, 5], |i| 2.6 - 0.15 * f64::from(i));
    let q = case.run();
    assert!(
        damper_moves(&q.dampers, Family::FastBump, true).iter().all(|&d| d > 0),
        "the stints show softer is worse, yet front fast bump was softened: {:?}\n{}",
        q.dampers,
        explain(&q)
    );
}

#[test]
fn kerb_gate_judges_each_metric_and_never_collapses() {
    // One stint, every kerb number uncertain: softening fast dampers must still pass.
    let p = Case::new().run();
    let d = &p.dampers;
    assert!(d.candidates > 0);
    let passed = d.candidates - d.rejected_kerb - d.rejected_balance - d.rejected_unstable;
    assert!(passed > 0, "every damper move rejected: {d:?}");
}

#[test]
fn kerbs_unstable_softens_a_fast_damper_and_never_stiffens_one() {
    let mut case = Case::new();
    case.flags.kerbs_unstable = true;
    let p = case.run();
    let fast: Vec<i32> = [(Family::FastBump, true), (Family::FastBump, false), (Family::FastRebound, true), (Family::FastRebound, false)]
        .into_iter()
        .flat_map(|(f, front)| damper_moves(&p.dampers, f, front))
        .collect();
    assert!(fast.iter().any(|&d| d < 0), "no fast damper softened: {:?}", p.dampers);
    assert!(fast.iter().all(|&d| d < 0), "a fast damper stiffened: {:?}", p.dampers);
    assert!(p.dampers.rejected_unstable > 0);
    let out = p.setup_from(&case.setup);
    assert!(p.dampers.changes.iter().all(|c| c.keys.iter().all(|k| out.params[k].value == c.to)));
}

#[test]
fn slow_damper_moves_that_add_turn_in_understeer_are_held_by_the_balance_band() {
    // The fast-neutral target wants less turn-in understeer than the stint has;
    // the main block cannot reach it, so anything adding entry understeer is out.
    let p = Case::new().run();
    assert!(p.solution.chosen.band_excess > 0.0);
    assert!(p.dampers.rejected_balance > 0, "{:?}", p.dampers);
    assert!(damper_moves(&p.dampers, Family::SlowBump, true).iter().all(|&d| d <= 0));
    assert!(damper_moves(&p.dampers, Family::SlowRebound, false).iter().all(|&d| d >= 0));
}

#[test]
fn pointier_slider_moves_turn_in_toward_rotation() {
    let mut base = Case::new();
    // Start inside the fast-neutral window so Pointier is tested purely on slider delta.
    for c in &mut base.cells {
        c.ua = CarClass::Gt3.fast_neutral(c.cell).0;
    }
    let off = base.run();
    let on = base.slider(FeelAxis::Front, -1.0).run();
    assert_eq!(on.solution.outcome, Outcome::Changed, "{}", explain(&on));
    let (e_off, e_on) = (mean_predicted(&off, CornerPhase::Entry).unwrap(), mean_predicted(&on, CornerPhase::Entry).unwrap());
    assert!(e_on < e_off - 0.02, "entry ua OFF {e_off:.3} ON {e_on:.3}\n{}", explain(&on));
    assert!(on.solution.chosen.band_excess < on.solution.base.band_excess);
}

#[test]
fn safer_slider_moves_exit_toward_understeer() {
    let mut base = Case::new();
    for c in &mut base.cells {
        c.ua = CarClass::Gt3.fast_neutral(c.cell).0;
    }
    let off = base.run();
    let on = base.slider(FeelAxis::Rear, 1.0).run();
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

fn hyper_case() -> Case {
    Case { class: CarClass::Hyper, setup: hyper_setup(), ..Case::new() }
}

fn rule(p: &Proposal, rule: Rule) -> Vec<&crate::opt::rules::RuleChange> {
    p.rules.iter().filter(|r| r.rule == rule).collect()
}

#[test]
fn lockups_take_brake_pressure_down_a_step_only_where_it_is_set_by_feel() {
    let mut case = hyper_case();
    assert!(rule(&case.run(), Rule::BrakePressure).is_empty(), "98 % sits inside the Hyper range");
    case.flags.lockups = true;
    let on = case.run();
    let r = rule(&on, Rule::BrakePressure);
    assert_eq!(r.len(), 1);
    assert_eq!((r[0].from, r[0].to), (78, 76));

    // GT3 brake pressure is not managed by feel: lock-ups leave it alone.
    let mut gt3 = Case::new();
    gt3.flags.lockups = true;
    assert!(rule(&gt3.run(), Rule::BrakePressure).is_empty());
}

#[test]
fn tyre_pressure_only_ever_comes_down() {
    let mut case = Case::new();
    case.summary.t_crown = Some(Wheel::new(12.0, 12.0, 5.0, 5.0));
    let p = case.run();
    let r = rule(&p, Rule::TyrePressure);
    assert_eq!(r.len(), 1, "{:?}", p.rules);
    assert_eq!(r[0].keys, vec!["WM_PRESSURE-W_FL", "WM_PRESSURE-W_FR"]);
    assert!(r[0].to < r[0].from);
    case.summary.t_crown = Some(Wheel::new(-6.0, -6.0, -6.0, -6.0));
    assert!(rule(&case.run(), Rule::TyrePressure).is_empty(), "a cool tread centre never raises pressure");
}

#[test]
fn hot_brakes_open_the_ducts_and_camber_follows_the_spread() {
    let mut case = Case::new();
    case.summary.brake_temp_max = Some(Wheel::new(900.0, 880.0, 600.0, 600.0));
    case.summary.t_spread = Some(Wheel::new(16.0, 16.0, 9.0, 9.0));
    let p = case.run();
    let ducts = rule(&p, Rule::BrakeDucts);
    assert_eq!(ducts.len(), 1);
    assert_eq!((ducts[0].keys[0].as_str(), ducts[0].to - ducts[0].from), ("VM_BRAKE_DUCTS", -1));
    let camber = rule(&p, Rule::Camber);
    assert_eq!(camber.len(), 1, "only the front spread is out of band");
    assert!(camber[0].to > camber[0].from, "too much inner heat: less negative camber");
}

#[test]
fn bottoming_raises_the_platform_but_never_a_gt3_front() {
    let bottoming = StintSummary {
        rh_min_mm: Some(Wheel::new(0.5, 0.5, 20.0, 20.0)),
        bottoming_s: Some(0.5),
        third_packers: crate::chassis::Axle::new(Some(0.01), None),
        ..StintSummary::default()
    };
    let mut gt3 = Case::new();
    gt3.summary = bottoming;
    let p = gt3.run();
    assert!(rule(&p, Rule::RideHeight).is_empty() && rule(&p, Rule::ThirdPackers).is_empty());
    assert_eq!(p.setup_from(&gt3.setup).params["WM_RIDEHEIGHT-W_FL"].value, 0);

    // A prototype barely on its third packers gets a packer step, not a higher car.
    let mut hyper = hyper_case();
    hyper.summary = bottoming;
    let p = hyper.run();
    let packers = rule(&p, Rule::ThirdPackers);
    assert_eq!(packers.len(), 1);
    assert_eq!(packers[0].to - packers[0].from, 1);
    assert!(rule(&p, Rule::RideHeight).is_empty());

    // Packers already in use: the front comes up instead, and the search leaves it alone.
    hyper.summary.third_packers.front = Some(0.4);
    let p = hyper.run();
    let rh = rule(&p, Rule::RideHeight);
    assert_eq!(rh.len(), 1);
    assert!(rh[0].to > rh[0].from);
    assert!(p.solution.changes.iter().all(|c| c.kind != DimKind::RideHeightFront));
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
        // Every analysable stint, grouped by car model and layout, in time order.
        type Recorded = (String, crate::telemetry::SessionMeta, crate::metrics::StintMetrics);
        let mut groups: BTreeMap<(String, String), Vec<Recorded>> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(rec) = DuckDbRecording::open(&path) else { continue };
            let Ok(meta) = rec.metadata() else { continue };
            if meta.setup.params.is_empty() {
                continue;
            }
            let Ok(m) = analyse_stint(&rec) else { continue };
            let label = path.file_name().unwrap().to_string_lossy().to_string();
            groups.entry((meta.setup.signature(), meta.layout.clone())).or_default().push((label, meta, m));
        }
        let mut changed = 0;
        let mut planned = 0;
        let mut fast_softened = 0;
        let mut damper_candidates = 0;
        for ((_, layout), mut stints) in groups {
            stints.sort_by(|a, b| a.0.cmp(&b.0));
            let history: Vec<DamperStint> = stints.iter().map(|(_, meta, m)| DamperStint::new(meta.setup.clone(), m)).collect();
            let Some((_, meta, m)) = stints.last() else { continue };
            let car = meta.car_name.clone();
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
                    summary: &m.summary,
                    damper_history: &history,
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
            for p in std::iter::once(&base).chain(toggles.iter().map(|t| &t.1)) {
                for r in p.rules.iter().filter(|r| r.rule == Rule::TyrePressure) {
                    assert!(r.to < r.from, "{car} @ {layout}: tyre pressure raised");
                }
            }
            for r in toggles[3].1.rules.iter().filter(|r| r.rule == Rule::BrakePressure) {
                assert!(r.to < r.from, "{car} @ {layout}: lock-ups raised brake pressure");
            }
            // Kerbs unstable: no fast damper stiffens, and one softens if any can.
            let is_fast = |c: &&crate::opt::dampers::DamperChange| matches!(c.setting.family, Family::FastBump | Family::FastRebound);
            let kerbs = &toggles[2].1.dampers;
            assert!(kerbs.changes.iter().filter(is_fast).all(|c| c.to < c.from), "{car} @ {layout}: fast damper stiffened with kerbs unstable");
            let can_soften = ["WM_FASTBUMP-W_FL", "WM_FASTBUMP-W_RL", "WM_FASTREBOUND-W_FL", "WM_FASTREBOUND-W_RL"]
                .iter()
                .any(|k| meta.setup.params.get(*k).is_some_and(|p| p.adjustable() && p.value > p.min));
            if can_soften {
                assert!(kerbs.changes.iter().filter(is_fast).any(|c| c.to < c.from), "{car} @ {layout}: kerbs unstable softened no fast damper");
            }
            if base.dampers.changes.iter().filter(is_fast).any(|c| c.to < c.from) {
                fast_softened += 1;
            }
            damper_candidates += base.dampers.candidates;
            if m.class == CarClass::Gt3 {
                for p in std::iter::once(&base).chain(toggles.iter().map(|t| &t.1)) {
                    let out = p.setup_from(&meta.setup);
                    let fl = &out.params.get("WM_RIDEHEIGHT-W_FL");
                    assert!(fl.is_none_or(|q| !q.adjustable() || q.value == q.min), "{car} @ {layout}: GT3 front ride height not at minimum");
                }
            }
        }
        println!("\n{planned} stints planned, Pointier added turn-in rotation on {changed}, unflagged fast-damper softening on {fast_softened}");
        assert!(damper_candidates > 0, "the damper block evaluated nothing");
        assert!(planned > 0, "no flying stint to plan");
        assert!(changed > 0, "Pointier never added turn-in rotation: the planner is degenerate");
    }
}
