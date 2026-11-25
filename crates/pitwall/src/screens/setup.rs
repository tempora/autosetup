/* Setup screen payloads: the proposal for a stint and its damper
   histograms.
   Balance cells are keyed "<phase>_<band>" ("entry_low"); degrees are
   road-wheel understeer angle (+ understeer).  Setting texts are garage
   labels from the car's click-to-label scale: `approx` marks a label
   interpolated between clicks seen, and a click never labelled shows as its
   index.  Ride heights in the side view are the setup's static ride heights
   in mm, per axle (mean of its wheels), from labels in cm or mm.
   `signature` identifies the proposed setup: writing compares it, so a
   proposal that changed since it was shown is not written.  */

use std::collections::BTreeMap;

use serde::Serialize;

use crate::chassis::{BalanceCell, CornerPhase, SpeedBand};
use crate::lmu::scales::{value_of, CarScales};
use crate::lmu::svm::SvmFile;
use crate::metrics::suspension::{VelocityHistogram, BINS, BIN_MM_S, RANGE_MM_S};
use crate::metrics::StintMetrics;
use crate::opt::solve::Toggles;
use crate::opt::target::{Anchor, RANGE_STEPS};
use crate::opt::why::{capitalise, change_reason, damper_reason, explain_with, rule_label, Why};
use crate::screens::names::pretty_name;
use crate::setup::CarSetup;
use crate::store::{Store, StoreError};

pub fn cell_key(c: BalanceCell) -> String {
    let phase = match c.phase {
        CornerPhase::Braking => "braking",
        CornerPhase::Entry => "entry",
        CornerPhase::Mid => "mid",
        CornerPhase::Exit => "exit",
    };
    let band = match c.band {
        SpeedBand::Low => "low",
        SpeedBand::High => "high",
    };
    format!("{phase}_{band}")
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChangeRow {
    pub keys: Vec<String>,
    pub name: String,
    pub before: i32,
    pub after: i32,
    pub before_text: String,
    pub after_text: String,
    pub approx: bool,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LockRow {
    pub key: String,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TargetView {
    pub cells: BTreeMap<String, f64>,
    pub tolerance: BTreeMap<String, f64>,
    // "fingerprint" or "fast_neutral".
    pub anchor: &'static str,
    pub front: f64,
    pub rear: f64,
    pub fast: f64,
    // A slider's end, deg.
    pub span: f64,
    pub car_key: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Platform {
    pub rh_front: f64,
    pub rh_rear: f64,
    pub rake: f64,
    pub wing: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PlatformView {
    pub current: Platform,
    pub proposed: Platform,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Short {
    pub stint: i64,
    // The driver's answer: Some(true) couldn't drive it, Some(false) it was fine.
    pub undrivable: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Measured {
    // [understeer angle, its standard error].
    pub cells: BTreeMap<String, [f64; 2]>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PlanView {
    pub target: TargetView,
    pub measured: Measured,
    // [understeer angle, p95 lateral G] with the proposal.
    pub predicted: BTreeMap<String, [f64; 2]>,
    pub platform: Option<PlatformView>,
    pub short: Option<Short>,
    pub car_key: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ProposalPayload {
    // The stint the proposal is built from.
    pub stint_id: i64,
    pub car_class: String,
    pub signature: String,
    pub changes: Vec<ChangeRow>,
    pub locks: Vec<LockRow>,
    pub why: Why,
    // Brake pressure is set by feel on this car (no ABS): the lock-ups button applies.
    pub brake_pressure_by_feel: bool,
    pub plan: PlanView,
}

struct Labels<'a> {
    scales: &'a CarScales,
}

impl Labels<'_> {
    // (text, approx) of `key` at `index`.
    fn text(&self, key: &str, index: i32) -> (String, bool) {
        self.scales.label(key, index).map_or_else(|| (index.to_string(), false), |l| (l.text, !l.exact))
    }

    fn mm(&self, key: &str, index: i32) -> Option<f64> {
        let l = self.scales.label(key, index)?;
        let factor = if l.text.contains("mm") {
            1.0
        } else if l.text.contains("cm") {
            10.0
        } else {
            return None;
        };
        value_of(key, &l.text).map(|v| v * factor)
    }
}

fn platform(setup: &CarSetup, labels: &Labels) -> Option<Platform> {
    let axle = |wheels: [&str; 2]| -> Option<f64> {
        let v: Vec<f64> = wheels.iter().filter_map(|k| labels.mm(k, setup.params.get(*k)?.value)).collect();
        (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64)
    };
    let (f, r) = (axle(["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"])?, axle(["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"])?);
    let wing = setup.params.get("VM_REAR_WING").filter(|p| p.adjustable()).map(|p| labels.text("VM_REAR_WING", p.value).0);
    Some(Platform { rh_front: f, rh_rear: r, rake: r - f, wing })
}

// FNV-1a 64 of the stint and the proposed settings that differ from it.
fn signature(file_name: &str, base: &CarSetup, proposed: &CarSetup) -> String {
    let moved: Vec<String> = proposed
        .params
        .iter()
        .filter(|(k, p)| base.params.get(*k).is_some_and(|b| b.value != p.value))
        .map(|(k, p)| format!("{k}={}", p.value))
        .collect();
    let text = format!("{file_name}|{}", moved.join("|"));
    format!("{:016x}", text.bytes().fold(0xcbf2_9ce4_8422_2325_u64, |h, b| (h ^ u64::from(b)).wrapping_mul(0x0000_0100_0000_01b3)))
}

/* The proposal for stint `stint_id`; `saved` are the parsed .svm files the
   car's scale draws labels from.  None for an unknown or unusable stint.  */
pub fn proposal(store: &Store, saved: &[SvmFile], stint_id: i64, toggles: Toggles) -> Result<Option<ProposalPayload>, StoreError> {
    let Some(asked) = store.stint_by_id(stint_id)? else { return Ok(None) };
    let Some(planned) = store.plan_for(&asked.file_name, toggles)? else { return Ok(None) };
    let base = &planned.base;
    let p = &planned.proposal;
    let setups: Vec<CarSetup> = store.usable_stints()?.into_iter().filter(|s| s.signature == base.signature).map(|s| s.meta.setup).collect();
    let scales = CarScales::collect(&setups, saved);
    let labels = Labels { scales: &scales };
    let row = |keys: Vec<String>, name: String, from: i32, to: i32, reason: String| {
        let (before_text, _) = labels.text(&keys[0], from);
        let (after_text, approx) = labels.text(&keys[0], to);
        ChangeRow { keys, name, before: from, after: to, before_text, after_text, approx, reason }
    };
    let changes: Vec<ChangeRow> = p
        .solution
        .changes
        .iter()
        .map(|c| row(c.kind.keys().iter().map(|k| k.to_string()).collect(), capitalise(c.kind.label()), c.from, c.to, change_reason(c)))
        .chain(p.rules.iter().map(|r| {
            let name = if r.keys.len() == 1 { pretty_name(&r.keys[0]) } else { rule_label(r.rule).to_string() };
            row(r.keys.clone(), name, r.from, r.to, r.reason.clone())
        }))
        .chain(p.dampers.changes.iter().map(|d| row(d.keys.clone(), capitalise(&d.setting.label()), d.from, d.to, damper_reason(&p.dampers, d))))
        .collect();
    let proposed = p.setup_from(&base.meta.setup);
    let m: &StintMetrics = base.usable().expect("bases are usable");
    let class = m.class;
    let tg = &p.target;
    let target = TargetView {
        cells: tg.cells.iter().map(|c| (cell_key(c.cell), c.centre.raw())).collect(),
        tolerance: tg.cells.iter().map(|c| (cell_key(c.cell), c.tolerance.raw())).collect(),
        anchor: match tg.anchor {
            Anchor::Fingerprint => "fingerprint",
            Anchor::FastNeutral => "fast_neutral",
        },
        front: tg.sliders.front.raw(),
        rear: tg.sliders.rear.raw(),
        fast: tg.sliders.fast.raw(),
        span: class.feel_step().raw() * RANGE_STEPS,
        car_key: base.signature.clone(),
    };
    let short = match &planned.undone {
        Some(u) => Some(Short { stint: u.id, undrivable: store.rating(&u.file_name)?.and_then(|r| r.undrivable) }),
        None => None,
    };
    let has = |k: &str| base.meta.setup.params.get(k).is_some_and(|q| q.adjustable());
    let locks = store.locks(&base.signature, &base.meta.layout)?.into_iter().map(|key| LockRow { name: pretty_name(&key), key }).collect();
    let why = explain_with(p, &|k, i| scales.label(k, i).map(|l| if l.exact { l.text } else { format!("≈ {}", l.text) }));
    Ok(Some(ProposalPayload {
        stint_id: base.id,
        car_class: base.meta.car_class.clone(),
        signature: signature(&base.file_name, &base.meta.setup, &proposed),
        changes,
        locks,
        why,
        brake_pressure_by_feel: class.knowledge().brake_pressure_pct.is_some() && has("VM_BRAKE_PRESSURE") && !has("VM_ANTILOCKBRAKESYSTEMMAP"),
        plan: PlanView {
            target,
            measured: Measured { cells: m.cells_off_kerb().iter().map(|c| (cell_key(c.cell), [c.ua.raw(), c.ua_se.raw()])).collect() },
            predicted: p.solution.chosen.cells.iter().map(|c| (cell_key(c.cell), [c.predicted_ua.raw(), c.predicted_lat.raw()])).collect(),
            platform: platform(&base.meta.setup, &labels).zip(platform(&proposed, &labels)).map(|(current, proposed)| PlatformView { current, proposed }),
            short,
            car_key: base.signature.clone(),
        },
    }))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KerbFilter {
    Off,
    On,
    Both,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct HistStats {
    pub bump: Option<f64>,
    pub slow: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Hist {
    pub counts: Vec<u64>,
    // Bin edges, mm/s (+ bump); one more than counts.
    pub edges: Vec<f64>,
    pub stats: HistStats,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Histograms {
    pub wheels: BTreeMap<&'static str, Hist>,
    pub knee: f64,
}

// Shaft-velocity histograms per wheel, rebinned to a multiple of BIN_MM_S near `bin_mm_s`.
pub fn histograms(m: &StintMetrics, kerbs: KerbFilter, bin_mm_s: f64) -> Histograms {
    let k = (bin_mm_s / BIN_MM_S).round().max(1.0) as usize;
    let knee = m.dampers.knee;
    let names = ["fl", "fr", "rl", "rr"];
    let wheels = names
        .into_iter()
        .zip(m.dampers.wheels.as_array())
        .map(|(name, split)| {
            let h: VelocityHistogram = match kerbs {
                KerbFilter::Off => split.off.clone(),
                KerbFilter::On => split.on.clone(),
                KerbFilter::Both => split.off.merge(&split.on),
            };
            let counts: Vec<u64> = h.counts.chunks(k).map(|c| c.iter().map(|&v| u64::from(v)).sum()).collect();
            let edges = (0..=BINS.div_ceil(k)).map(|i| (-RANGE_MM_S + (i * k) as f64 * BIN_MM_S).min(RANGE_MM_S)).collect();
            (name, Hist { counts, edges, stats: HistStats { bump: h.bump_share(), slow: h.slow_share(knee) } })
        })
        .collect();
    Histograms { wheels, knee: knee.raw() }
}
