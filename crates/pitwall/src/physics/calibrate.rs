/* Per-class VehicleParams calibration against real holdout pairs.
   `fit` runs bounded coordinate descent over KNOBS, minimising
   HoldoutReport::score on the pairs it is given, with shrinking steps
   expressed as fractions of each knob's range.

   Accuracy is only ever reported out of sample: `cross_validate` fits on all
   but one fold and scores the held-out fold, with folds formed by (car,
   layout) group when at least two groups exist (leave-one-group-out), else by
   pair index.  `ablate` compares cross-validated accuracy with and without
   each physics term; `calibrate_class` drops every term that does not
   improve held-out accuracy by more than MIN_TERM_GAIN before the final fit.  */

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::physics::{QssModel, Term, Terms, VehicleParams};
use crate::validation::{evaluate_holdout, GroupedPair, HoldoutPair, HoldoutReport};

const STEPS: [f64; 3] = [0.25, 0.08, 0.025];
const MAX_SWEEPS: usize = 25;
const MIN_IMPROVEMENT: f64 = 1e-9;
pub const MIN_TERM_GAIN: f64 = 1e-3;
pub const DEFAULT_FOLDS: usize = 5;

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
    understeer_gain_deg in (1.0, 12.0),
    diff_gain_deg in (0.0, 2.0),
    mu_nominal in (1.2, 2.2),
    load_sensitivity in (0.0, 0.35),
    cg_height_m in (0.20, 0.60),
    aero_cl_a in (0.3, 3.5),
    aero_front_base in (0.35, 0.55),
    aero_rake_gain in (0.0, 0.25),
}

fn score(params: VehicleParams, terms: Terms, pairs: &[HoldoutPair]) -> f64 {
    evaluate_holdout(&QssModel::with_terms(params, terms), pairs).score()
}

pub fn fit(start: VehicleParams, terms: Terms, pairs: &[HoldoutPair]) -> VehicleParams {
    if pairs.is_empty() {
        return start;
    }
    let mut best = start;
    let mut best_score = score(best, terms, pairs);
    for frac in STEPS {
        for _ in 0..MAX_SWEEPS {
            let mut improved = false;
            for knob in KNOBS {
                let step = frac * (knob.hi - knob.lo);
                for dir in [1.0, -1.0] {
                    let mut cand = best;
                    (knob.set)(&mut cand, ((knob.get)(&best) + dir * step).clamp(knob.lo, knob.hi));
                    let s = score(cand, terms, pairs);
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

pub fn cross_validate(start: VehicleParams, terms: Terms, pairs: &[GroupedPair], folds: usize) -> HoldoutReport {
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
            let params = fit(start, terms, &train);
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

pub fn ablate(start: VehicleParams, terms: Terms, pairs: &[GroupedPair], folds: usize) -> Vec<Ablation> {
    let with = cross_validate(start, terms, pairs, folds);
    Term::ALL
        .iter()
        .filter(|&&t| terms.enabled(t))
        .map(|&term| Ablation {
            term,
            with,
            without: cross_validate(start, terms.with(term, false), pairs, folds),
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClassCalibration {
    pub class: CarClass,
    pub params: VehicleParams,
    // Terms that improved held-out accuracy.
    pub terms: Terms,
    // Uncalibrated class baseline, all terms, scored on every pair.
    pub baseline: HoldoutReport,
    // Cross-validated accuracy of the pruned, fitted model.
    pub calibrated: HoldoutReport,
    pub ablations: Vec<Ablation>,
}

pub fn calibrate_class(class: CarClass, pairs: &[GroupedPair], folds: usize) -> ClassCalibration {
    let start = VehicleParams::for_class(class);
    let all: Vec<HoldoutPair> = pairs.iter().map(|p| p.pair).collect();
    let baseline = evaluate_holdout(&QssModel::new(start), &all);
    let ablations = ablate(start, Terms::ALL, pairs, folds);
    let terms = ablations.iter().fold(Terms::ALL, |t, a| t.with(a.term, a.improves()));
    ClassCalibration {
        class,
        params: fit(start, terms, &all),
        terms,
        baseline,
        calibrated: cross_validate(start, terms, pairs, folds),
        ablations,
    }
}
