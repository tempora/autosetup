/* LMU CarSetup JSON parser and normalized mechanical setup vector.
   LMU stores parameters flat at the top level with inclusive minValue and
   exclusive maxValue (total selectable steps = maxValue - minValue).
   MechanicalSetup projects adjustable parameters onto [0.0, 1.0].  */

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::chassis::Axle;

#[derive(Debug, Error)]
pub enum SetupError {
    #[error("failed to parse LMU CarSetup JSON: {0}")]
    InvalidJson(#[from] serde_json::Error),
}

// LMU stores min inclusive and max_exclusive as step count upper bound.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SetupParam {
    pub value: i32,
    pub min: i32,
    pub max_exclusive: i32,
    pub available: bool,
    pub text: String,
}

impl SetupParam {
    #[inline]
    pub fn steps(&self) -> i32 {
        (self.max_exclusive - self.min).max(0)
    }

    #[inline]
    pub fn adjustable(&self) -> bool {
        self.available && self.steps() > 1
    }

    #[inline]
    pub fn fraction(&self) -> f64 {
        let steps = self.steps();
        if steps <= 1 {
            0.0
        } else {
            f64::from(self.value - self.min) / f64::from(steps - 1)
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CarSetup {
    pub params: BTreeMap<String, SetupParam>,
}

impl CarSetup {
    pub fn from_lmu_json(raw: &str) -> Result<Self, SetupError> {
        let root: serde_json::Value = serde_json::from_str(raw)?;
        let mut params = BTreeMap::new();
        if let Some(entries) = root.as_object() {
            for (key, val) in entries {
                if let Some(p) = parse_param(val) {
                    params.insert(key.clone(), p);
                }
            }
        }
        Ok(Self { params })
    }

    pub fn fraction(&self, key: &str) -> Option<f64> {
        self.params.get(key).map(SetupParam::fraction)
    }

    pub fn pair_fraction(&self, left: &str, right: &str) -> Option<f64> {
        match (self.fraction(left), self.fraction(right)) {
            (Some(l), Some(r)) => Some(0.5 * (l + r)),
            (Some(v), None) | (None, Some(v)) => Some(v),
            (None, None) => None,
        }
    }

    pub fn mechanical_vector(&self) -> MechanicalSetup {
        MechanicalSetup {
            spring: Axle::new(
                self.pair_fraction("WM_SPRINGRATE-W_FL", "WM_SPRINGRATE-W_FR")
                    .unwrap_or(0.5),
                self.pair_fraction("WM_SPRINGRATE-W_RL", "WM_SPRINGRATE-W_RR")
                    .unwrap_or(0.5),
            ),
            arb: Axle::new(
                self.fraction("VM_FRONT_ANTISWAY").unwrap_or(0.5),
                self.fraction("VM_REAR_ANTISWAY").unwrap_or(0.5),
            ),
            ride_height: Axle::new(
                self.pair_fraction("WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR")
                    .unwrap_or(0.5),
                self.pair_fraction("WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR")
                    .unwrap_or(0.5),
            ),
            wing: self
                .fraction("VM_REAR_WING")
                .or_else(|| self.fraction("VM_FRONT_WING"))
                .unwrap_or(0.5),
            diff_preload: self.fraction("VM_DIFF_PRELOAD").unwrap_or(0.5),
            diff_power: self.fraction("VM_DIFF_POWER").unwrap_or(0.5),
            diff_coast: self.fraction("VM_DIFF_COAST").unwrap_or(0.5),
            brake_bias: self.fraction("VM_BRAKE_BALANCE").unwrap_or(0.5),
        }
    }
}

// All fields normalized to [0.0, 1.0] across each parameter's selectable range.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct MechanicalSetup {
    pub spring: Axle<f64>,
    pub arb: Axle<f64>,
    pub ride_height: Axle<f64>,
    pub wing: f64,
    pub diff_preload: f64,
    pub diff_power: f64,
    pub diff_coast: f64,
    pub brake_bias: f64,
}

impl Default for MechanicalSetup {
    fn default() -> Self {
        Self {
            spring: Axle::new(0.5, 0.5),
            arb: Axle::new(0.5, 0.5),
            ride_height: Axle::new(0.5, 0.5),
            wing: 0.5,
            diff_preload: 0.5,
            diff_power: 0.5,
            diff_coast: 0.5,
            brake_bias: 0.5,
        }
    }
}

fn parse_param(val: &serde_json::Value) -> Option<SetupParam> {
    let obj = val.as_object()?;
    let value = obj.get("value").and_then(|v| v.as_i64())? as i32;
    let min = obj
        .get("minValue")
        .or_else(|| obj.get("min"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0) as i32;
    let max_exclusive = obj
        .get("maxValue")
        .or_else(|| obj.get("max"))
        .and_then(|v| v.as_i64())
        .unwrap_or(i64::from(value) + 1) as i32;
    let available = obj
        .get("available")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let text = obj
        .get("stringValue")
        .or_else(|| obj.get("text"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    Some(SetupParam {
        value,
        min,
        max_exclusive,
        available,
        text,
    })
}
