/* Shipped per-class calibration, generated from real holdout pairs by the
   ignored `real_holdout_pairs_calibrate_and_prune_terms` test with
   PITWALL_WRITE_CALIBRATION set, and compiled in from calibration.json.
   A class without a calibration runs its baseline parameters with no
   optional physics term (none has been shown to help it) and is unverified.  */

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::physics::calibrate::{ClassCalibration, Verification};
use crate::physics::{QssModel, Terms, VehicleParams};

pub const CALIBRATION_FILE: &str = "calibration.json";
const SHIPPED: &str = include_str!("../../calibration.json");

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CalibrationSet {
    pub classes: BTreeMap<CarClass, ClassCalibration>,
}

impl CalibrationSet {
    pub fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).expect("calibration serialises")
    }

    pub fn shipped() -> &'static Self {
        static SET: OnceLock<CalibrationSet> = OnceLock::new();
        SET.get_or_init(|| serde_json::from_str(SHIPPED).expect("calibration.json parses"))
    }

    pub fn model_for(&self, class: CarClass) -> (QssModel, Verification) {
        match self.classes.get(&class) {
            Some(c) => (QssModel::with_terms(c.params, c.terms), c.verification()),
            None => (QssModel::with_terms(VehicleParams::for_class(class), Terms::NONE), Verification::default()),
        }
    }
}

pub fn model_for(class: CarClass) -> (QssModel, Verification) {
    CalibrationSet::shipped().model_for(class)
}
