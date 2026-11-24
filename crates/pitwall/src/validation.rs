/* Holdout validation harness for MechanicalModel implementations.
   Each HoldoutPair calibrates the baseline offset on stint A and evaluates
   predicted understeer and lateral-G deltas against stint B's measured
   telemetry from the same car and layout.  Each side carries its own
   measured a_lon, so driver pedal adaptation between stints is an input to
   the model rather than a setup effect it must explain.

   Real pairs come from StintMetrics: stints are grouped by (car, class,
   layout), ordered chronologically, and consecutive stints whose mechanical
   setup changed are paired cell by cell where both have MIN_CELL_PASSES.
   Lateral G is the cell's p95 |a_lat|, scored in cornering phases only;
   speed is the cell's median apex speed.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{CarClass, CornerPhase};
use crate::metrics::balance::CellStats;
use crate::metrics::stats::sign;
use crate::metrics::StintMetrics;
use crate::physics::{CornerState, MechanicalModel};
use crate::setup::MechanicalSetup;
use crate::telemetry::SessionMeta;
use crate::units::{Degrees, Gs};

pub const MIN_CELL_PASSES: usize = 5;
const SETUP_CHANGE_EPS: f64 = 1e-9;
const DIRECTION_DEADBAND_DEG: f64 = 0.05;
// Degrees of understeer error per g of lateral error in the combined score.
pub const LAT_G_WEIGHT: f64 = 3.0;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct StintObservation {
    pub setup: MechanicalSetup,
    pub state: CornerState,
    pub measured_ua: Degrees,
    pub measured_lat_g: Gs,
}

impl StintObservation {
    pub fn from_cell(setup: MechanicalSetup, cell: &CellStats) -> Self {
        Self {
            setup,
            state: CornerState { speed: cell.v_min, phase: cell.cell.phase, a_lon: cell.a_lon },
            measured_ua: cell.ua,
            measured_lat_g: cell.a_lat_p95,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct HoldoutPair {
    pub baseline: StintObservation,
    pub holdout: StintObservation,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct HoldoutReport {
    pub pairs: usize,
    pub ua_mae_deg: f64,
    pub lat_g_mae: f64,
    pub ua_direction_accuracy: f64,
}

impl HoldoutReport {
    pub const EMPTY: Self = Self { pairs: 0, ua_mae_deg: 0.0, lat_g_mae: 0.0, ua_direction_accuracy: 0.0 };

    // Combined error, lower is better.
    #[inline]
    pub fn score(&self) -> f64 {
        self.ua_mae_deg + LAT_G_WEIGHT * self.lat_g_mae
    }

    // Pair-weighted combination of two reports over disjoint pair sets.
    pub fn merge(self, other: Self) -> Self {
        let n = self.pairs + other.pairs;
        if n == 0 {
            return Self::EMPTY;
        }
        let w = |a: f64, b: f64| (a * self.pairs as f64 + b * other.pairs as f64) / n as f64;
        Self {
            pairs: n,
            ua_mae_deg: w(self.ua_mae_deg, other.ua_mae_deg),
            lat_g_mae: w(self.lat_g_mae, other.lat_g_mae),
            ua_direction_accuracy: w(self.ua_direction_accuracy, other.ua_direction_accuracy),
        }
    }
}

pub fn evaluate_holdout<'a, M: MechanicalModel>(
    model: &M,
    pairs: impl IntoIterator<Item = &'a HoldoutPair>,
) -> HoldoutReport {
    let (n, ua_err_sum, lat_n, lat_err_sum, dir_hits) = pairs.into_iter().fold(
        (0usize, 0.0_f64, 0usize, 0.0_f64, 0usize),
        |(n, ua_acc, lat_n, lat_acc, hits), pair| {
            let pred_a = model.evaluate(&pair.baseline.setup, pair.baseline.state);
            let pred_b = model.evaluate(&pair.holdout.setup, pair.holdout.state);

            let pred_dua = (pred_b.understeer - pred_a.understeer).raw();
            let meas_dua = (pair.holdout.measured_ua - pair.baseline.measured_ua).raw();

            // Braking cells sit before turn-in: their a_lat is not limit grip.
            let lat_err = (pair.baseline.state.phase != CornerPhase::Braking).then(|| {
                let pred_dlat = (pred_b.max_lat_g - pred_a.max_lat_g).raw();
                let meas_dlat = (pair.holdout.measured_lat_g - pair.baseline.measured_lat_g).raw();
                (pred_dlat - meas_dlat).abs()
            });

            let dir_ok = if meas_dua.abs() < DIRECTION_DEADBAND_DEG {
                pred_dua.abs() < DIRECTION_DEADBAND_DEG
            } else {
                sign(pred_dua) == sign(meas_dua)
            };

            (
                n + 1,
                ua_acc + (pred_dua - meas_dua).abs(),
                lat_n + usize::from(lat_err.is_some()),
                lat_acc + lat_err.unwrap_or(0.0),
                hits + usize::from(dir_ok),
            )
        },
    );

    if n == 0 {
        return HoldoutReport::EMPTY;
    }
    let nf = n as f64;
    HoldoutReport {
        pairs: n,
        ua_mae_deg: ua_err_sum / nf,
        lat_g_mae: if lat_n > 0 { lat_err_sum / lat_n as f64 } else { 0.0 },
        ua_direction_accuracy: dir_hits as f64 / nf,
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct StintKey {
    pub class: CarClass,
    pub car: String,
    pub layout: String,
}

#[derive(Debug, Clone)]
pub struct Stint {
    pub key: StintKey,
    // Chronological sort key (LMU file names embed the UTC start time).
    pub label: String,
    pub setup: MechanicalSetup,
    pub cells: Vec<CellStats>,
}

impl Stint {
    pub fn new(label: impl Into<String>, meta: &SessionMeta, metrics: &StintMetrics) -> Self {
        Self {
            key: StintKey { class: metrics.class, car: meta.car_name.clone(), layout: meta.layout.clone() },
            label: label.into(),
            setup: meta.setup.mechanical_vector(),
            cells: metrics.cells_off_kerb(),
        }
    }
}

// A pair tagged with its (car, layout) group for grouped cross-validation.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct GroupedPair {
    pub group: usize,
    pub pair: HoldoutPair,
}

// Cell-by-cell pairs between two stints of the same car and layout.
pub fn pair_stints(a: &Stint, b: &Stint) -> Vec<HoldoutPair> {
    a.cells
        .iter()
        .filter(|c| c.passes >= MIN_CELL_PASSES)
        .filter_map(|ca| {
            let cb = b.cells.iter().find(|cb| cb.cell == ca.cell && cb.passes >= MIN_CELL_PASSES)?;
            Some(HoldoutPair {
                baseline: StintObservation::from_cell(a.setup, ca),
                holdout: StintObservation::from_cell(b.setup, cb),
            })
        })
        .collect()
}

// Groups stints by key and pairs chronologically consecutive stints whose
// mechanical setup changed.  Groups are numbered in key order.
pub fn holdout_pairs(stints: &[Stint]) -> Vec<(StintKey, Vec<GroupedPair>)> {
    let mut keys: Vec<&StintKey> = stints.iter().map(|s| &s.key).collect();
    keys.sort();
    keys.dedup();
    keys.into_iter()
        .enumerate()
        .map(|(group, key)| {
            let mut members: Vec<&Stint> = stints.iter().filter(|s| &s.key == key).collect();
            members.sort_by(|a, b| a.label.cmp(&b.label));
            let pairs = members
                .windows(2)
                .filter(|w| w[0].setup.max_abs_diff(&w[1].setup) > SETUP_CHANGE_EPS)
                .flat_map(|w| pair_stints(w[0], w[1]))
                .map(|pair| GroupedPair { group, pair })
                .collect();
            (key.clone(), pairs)
        })
        .collect()
}

#[cfg(all(test, feature = "duckdb-native"))]
mod tests {
    use std::collections::BTreeMap;

    use super::{holdout_pairs, GroupedPair, Stint};
    use crate::chassis::CarClass;
    use crate::metrics::analyse_stint;
    use crate::physics::calibrate::{calibrate_class, DEFAULT_FOLDS};
    use crate::telemetry::duckdb::DuckDbRecording;
    use crate::telemetry::TelemetrySource;
    use crate::testdata::local_recordings;

    const MIN_CLASS_PAIRS: usize = 10;

    /* Groups real stints by (car, layout), pairs consecutive stints whose
       setup changed, then per class: calibrates VehicleParams, prunes terms
       that do not improve held-out accuracy, and requires the cross-validated
       calibrated model to beat the uncalibrated class baseline.
       Run: cargo test -p pitwall --release -- --ignored --nocapture  */
    #[test]
    #[ignore = "requires PITWALL_TELEMETRY_DIR with LMU .duckdb recordings"]
    fn real_holdout_pairs_calibrate_and_prune_terms() {
        let stints: Vec<Stint> = local_recordings()
            .into_iter()
            .filter_map(|path| {
                let name = path.file_name()?.to_string_lossy().to_string();
                let rec = DuckDbRecording::open(&path).map_err(|e| println!("{name}: skipped (open: {e})")).ok()?;
                let meta = rec.metadata().ok()?;
                if meta.setup.params.is_empty() {
                    println!("{name}: skipped (no CarSetup; LMU not in English?)");
                    return None;
                }
                let m = analyse_stint(&rec).map_err(|e| println!("{name}: skipped ({e})")).ok()?;
                Some(Stint::new(name, &meta, &m))
            })
            .collect();

        let mut by_class: BTreeMap<CarClass, Vec<GroupedPair>> = BTreeMap::new();
        for (key, pairs) in holdout_pairs(&stints) {
            println!("{:?} {} @ {}: {} pairs", key.class, key.car, key.layout, pairs.len());
            by_class.entry(key.class).or_default().extend(pairs);
        }

        let mut calibrated = 0;
        for (class, pairs) in by_class.iter().filter(|(_, p)| p.len() >= MIN_CLASS_PAIRS) {
            let cal = calibrate_class(*class, pairs, DEFAULT_FOLDS);
            println!("\n{class:?}: {} pairs", pairs.len());
            println!("  baseline   {:?} score {:.4}", cal.baseline, cal.baseline.score());
            println!("  calibrated {:?} score {:.4}", cal.calibrated, cal.calibrated.score());
            for a in &cal.ablations {
                println!("  {:?}: gain {:+.4} -> {}", a.term, a.gain(), if a.improves() { "kept" } else { "pruned" });
            }
            println!("  params {:?}", cal.params);

            assert_eq!(cal.calibrated.pairs, pairs.len());
            assert!(cal.calibrated.score().is_finite());
            assert!(
                cal.calibrated.score() <= cal.baseline.score(),
                "{class:?}: calibration made held-out accuracy worse ({:.4} > {:.4})",
                cal.calibrated.score(),
                cal.baseline.score()
            );
            calibrated += 1;
        }
        assert!(calibrated > 0, "no class with {MIN_CLASS_PAIRS}+ holdout pairs");
    }
}
