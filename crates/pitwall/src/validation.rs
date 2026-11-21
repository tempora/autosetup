/* Holdout validation harness for MechanicalModel implementations.
   Each HoldoutPair calibrates the baseline offset on stint A and evaluates
   predicted understeer and lateral-G deltas against stint B's measured
   telemetry from the same car and layout.  */

use serde::{Deserialize, Serialize};

use crate::physics::{CornerState, MechanicalModel};
use crate::setup::MechanicalSetup;
use crate::units::{Degrees, Gs};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct StintObservation {
    pub setup: MechanicalSetup,
    pub state: CornerState,
    pub measured_ua: Degrees,
    pub measured_lat_g: Gs,
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

pub fn evaluate_holdout<M: MechanicalModel>(model: &M, pairs: &[HoldoutPair]) -> HoldoutReport {
    if pairs.is_empty() {
        return HoldoutReport {
            pairs: 0,
            ua_mae_deg: 0.0,
            lat_g_mae: 0.0,
            ua_direction_accuracy: 0.0,
        };
    }

    let (ua_err_sum, lat_err_sum, dir_hits) = pairs.iter().fold(
        (0.0_f64, 0.0_f64, 0usize),
        |(ua_acc, lat_acc, hits), pair| {
            let pred_a = model.evaluate(&pair.baseline.setup, pair.baseline.state);
            let pred_b = model.evaluate(&pair.holdout.setup, pair.holdout.state);

            let pred_dua = (pred_b.understeer - pred_a.understeer).raw();
            let meas_dua = (pair.holdout.measured_ua - pair.baseline.measured_ua).raw();

            let pred_dlat = (pred_b.max_lat_g - pred_a.max_lat_g).raw();
            let meas_dlat = (pair.holdout.measured_lat_g - pair.baseline.measured_lat_g).raw();

            let dir_ok = (pred_dua == 0.0 && meas_dua.abs() < 0.05)
                || (pred_dua.signum() == meas_dua.signum());

            (
                ua_acc + (pred_dua - meas_dua).abs(),
                lat_acc + (pred_dlat - meas_dlat).abs(),
                hits + usize::from(dir_ok),
            )
        },
    );

    let n = pairs.len() as f64;
    HoldoutReport {
        pairs: pairs.len(),
        ua_mae_deg: ua_err_sum / n,
        lat_g_mae: lat_err_sum / n,
        ua_direction_accuracy: dir_hits as f64 / n,
    }
}
