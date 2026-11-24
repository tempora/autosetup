/* Per-class VehicleParams calibration against real holdout pairs.
   `fit` runs bounded coordinate descent over KNOBS, minimising
   HoldoutReport::score plus a ridge penalty λ·Σ((p − p0)/range)² that holds
   each knob at its class baseline unless the pairs pay for moving it.  λ is
   chosen from LAMBDAS by cross-validated score.

   Accuracy is only ever reported out of sample: `cross_validate` fits on all
   but one fold and scores the held-out fold, with folds formed by (car,
   layout) group when at least two groups exist (leave-one-group-out), else by
   pair index.  `ablate` compares cross-validated accuracy with and without
   each physics term; `calibrate_class` drops every term that does not
   improve held-out accuracy by more than MIN_TERM_GAIN before the final fit.
   Balance (understeer) and grip (lateral G) are verified separately: each
   only when the cross-validated model beats NoChange (predicting no
   difference between setups) on the same pairs by UA_MARGIN_DEG or
   LAT_MARGIN_G.  */

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::physics::{QssModel, Term, Terms, VehicleParams};
use crate::validation::{evaluate_holdout, GroupedPair, HoldoutPair, HoldoutReport, NoChange};

const STEPS: [f64; 3] = [0.25, 0.08, 0.025];
const MAX_SWEEPS: usize = 25;
const MIN_IMPROVEMENT: f64 = 1e-9;
pub const MIN_TERM_GAIN: f64 = 1e-3;
pub const DEFAULT_FOLDS: usize = 5;
pub const LAMBDAS: [f64; 5] = [0.0, 0.03, 0.1, 0.3, 1.0];
pub const UA_MARGIN_DEG: f64 = 0.005;
pub const LAT_MARGIN_G: f64 = 0.002;

#[derive(Clone, Copy)]
pub struct Knob {
    pub name: &'static str,
    get: fn(&VehicleParams) -> f64,
    set: fn(&mut VehicleParams, f64),
    lo: f64,
    hi: f64,
}

macro_rules! knobs {
    ($($field:ident in ($lo:expr, $hi:expr)),+ $(,)?) => {
        pub const KNOBS: &[Knob] = &[$(Knob {
            name: stringify!($field),
            get: |p| p.$field,
            set: |p, v| p.$field = v,
            lo: $lo,
            hi: $hi,
        }),+];
    };
}

knobs! {
    understeer_gain_deg in (0.0, 12.0),
    diff_gain_deg in (0.0, 2.0),
    mu_nominal in (1.2, 2.2),
    load_sensitivity in (0.0, 0.35),
    cg_height_m in (0.20, 0.60),
    aero_cl_a in (0.3, 3.5),
    aero_front_base in (0.35, 0.55),
    aero_rake_gain in (0.0, 0.25),
}

fn penalty(params: &VehicleParams, start: &VehicleParams, lambda: f64) -> f64 {
    lambda
        * KNOBS
            .iter()
            .map(|k| (((k.get)(params) - (k.get)(start)) / (k.hi - k.lo)).powi(2))
            .sum::<f64>()
}

fn objective(params: VehicleParams, start: &VehicleParams, terms: Terms, pairs: &[HoldoutPair], lambda: f64) -> f64 {
    evaluate_holdout(&QssModel::with_terms(params, terms), pairs).score() + penalty(&params, start, lambda)
}

pub fn fit(start: VehicleParams, terms: Terms, pairs: &[HoldoutPair], lambda: f64) -> VehicleParams {
    if pairs.is_empty() {
        return start;
    }
    let mut best = start;
    let mut best_score = objective(best, &start, terms, pairs, lambda);
    for frac in STEPS {
        for _ in 0..MAX_SWEEPS {
            let mut improved = false;
            for knob in KNOBS {
                let step = frac * (knob.hi - knob.lo);
                for dir in [1.0, -1.0] {
                    let mut cand = best;
                    (knob.set)(&mut cand, ((knob.get)(&best) + dir * step).clamp(knob.lo, knob.hi));
                    let s = objective(cand, &start, terms, pairs, lambda);
                    if s < best_score - MIN_IMPROVEMENT {
                        best = cand;
                        best_score = s;
                        improved = true;
                        break;
                    }
                }
            }
            if !improved {
                break;
            }
        }
    }
    best
}

// Knobs the fit left at a bound of their range.
pub fn pinned(params: &VehicleParams) -> Vec<&'static str> {
    KNOBS
        .iter()
        .filter(|k| {
            let v = (k.get)(params);
            (v - k.lo).abs() < 1e-9 || (v - k.hi).abs() < 1e-9
        })
        .map(|k| k.name)
        .collect()
}

// Fold label per pair: its group when two or more groups exist, else index mod folds.
fn fold_labels(pairs: &[GroupedPair], folds: usize) -> Vec<usize> {
    let mut groups: Vec<usize> = pairs.iter().map(|p| p.group).collect();
    groups.sort_unstable();
    groups.dedup();
    if groups.len() >= 2 {
        pairs.iter().map(|p| p.group).collect()
    } else {
        (0..pairs.len()).map(|i| i % folds.max(2)).collect()
    }
}

pub fn cross_validate(start: VehicleParams, terms: Terms, pairs: &[GroupedPair], folds: usize, lambda: f64) -> HoldoutReport {
    let labels = fold_labels(pairs, folds);
    let mut distinct = labels.clone();
    distinct.sort_unstable();
    distinct.dedup();
    distinct
        .into_iter()
        .map(|fold| {
            let (test, train): (Vec<HoldoutPair>, Vec<HoldoutPair>) = pairs
                .iter()
                .zip(&labels)
                .fold((Vec::new(), Vec::new()), |(mut te, mut tr), (p, &l)| {
                    if l == fold { te.push(p.pair) } else { tr.push(p.pair) }
                    (te, tr)
                });
            let params = fit(start, terms, &train, lambda);
            evaluate_holdout(&QssModel::with_terms(params, terms), &test)
        })
        .fold(HoldoutReport::EMPTY, HoldoutReport::merge)
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Ablation {
    pub term: Term,
    pub with: HoldoutReport,
    pub without: HoldoutReport,
}

impl Ablation {
    // Held-out score gained by keeping the term (positive = term helps).
    #[inline]
    pub fn gain(&self) -> f64 {
        self.without.score() - self.with.score()
    }

    #[inline]
    pub fn improves(&self) -> bool {
        self.gain() > MIN_TERM_GAIN
    }
}

pub fn ablate(start: VehicleParams, terms: Terms, pairs: &[GroupedPair], folds: usize, lambda: f64) -> Vec<Ablation> {
    let with = cross_validate(start, terms, pairs, folds, lambda);
    Term::ALL
        .iter()
        .filter(|&&t| terms.enabled(t))
        .map(|&term| Ablation {
            term,
            with,
            without: cross_validate(start, terms.with(term, false), pairs, folds, lambda),
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClassCalibration {
    pub class: CarClass,
    pub params: VehicleParams,
    // Terms that improved held-out accuracy.
    pub terms: Terms,
    pub lambda: f64,
    // Predicting no change, scored on every pair.
    pub no_change: HoldoutReport,
    // Uncalibrated class baseline, all terms, scored on every pair.
    pub baseline: HoldoutReport,
    // Cross-validated accuracy of the pruned, fitted model.
    pub calibrated: HoldoutReport,
    pub ablations: Vec<Ablation>,
    // Knobs left at a bound by the final fit.
    pub pinned: Vec<String>,
}

impl ClassCalibration {
    #[inline]
    pub fn verification(&self) -> Verification {
        Verification {
            balance: self.calibrated.ua_mae_deg < self.no_change.ua_mae_deg - UA_MARGIN_DEG,
            grip: self.calibrated.lat_g_mae < self.no_change.lat_g_mae - LAT_MARGIN_G,
            pairs: self.calibrated.pairs,
        }
    }
}

// Whether a class's model predicts setup changes better than "no change".
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Verification {
    pub balance: bool,
    pub grip: bool,
    pub pairs: usize,
}

pub fn calibrate_class(class: CarClass, pairs: &[GroupedPair], folds: usize) -> ClassCalibration {
    let start = VehicleParams::for_class(class);
    let all: Vec<HoldoutPair> = pairs.iter().map(|p| p.pair).collect();
    let lambda = LAMBDAS
        .iter()
        .map(|&l| (l, cross_validate(start, Terms::ALL, pairs, folds, l).score()))
        .min_by(|a, b| a.1.total_cmp(&b.1))
        .map_or(0.0, |(l, _)| l);
    let ablations = ablate(start, Terms::ALL, pairs, folds, lambda);
    let terms = ablations.iter().fold(Terms::ALL, |t, a| t.with(a.term, a.improves()));
    let params = fit(start, terms, &all, lambda);
    ClassCalibration {
        class,
        terms,
        lambda,
        no_change: evaluate_holdout(&NoChange, &all),
        baseline: evaluate_holdout(&QssModel::new(start), &all),
        calibrated: cross_validate(start, terms, pairs, folds, lambda),
        ablations,
        pinned: pinned(&params).into_iter().map(String::from).collect(),
        params,
    }
}
