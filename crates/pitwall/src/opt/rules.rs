/* Deterministic setup rules applied alongside the mechanical search.
   FrontRideHeightAtMin: GT3 runs the front ride height at its minimum
   index, always.  LockupsBrakePressure: a stint flagged for lock-ups takes
   brake pressure down one click (a higher VM_BRAKE_PRESSURE index is more
   pressure).  Locked keys are never touched.  */

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::opt::solve::DriverFlags;
use crate::setup::CarSetup;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Rule {
    FrontRideHeightAtMin,
    LockupsBrakePressure,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuleChange {
    pub rule: Rule,
    pub keys: Vec<String>,
    pub from: i32,
    pub to: i32,
}

fn movable<'a>(setup: &CarSetup, keys: &[&'a str], locks: &BTreeSet<String>) -> Option<Vec<&'a str>> {
    keys.iter()
        .all(|k| setup.params.get(*k).is_some_and(|p| p.adjustable()) && !locks.contains(*k))
        .then(|| keys.to_vec())
}

pub fn apply_rules(setup: &CarSetup, class: CarClass, flags: &DriverFlags, locks: &BTreeSet<String>) -> Vec<RuleChange> {
    let mut out = Vec::new();
    if class.front_ride_height_at_min() {
        if let Some(keys) = movable(setup, &["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"], locks) {
            let from = keys.iter().map(|k| setup.params[*k].value).max().unwrap_or(0);
            let to = keys.iter().map(|k| setup.params[*k].min).max().unwrap_or(0);
            if from > to {
                out.push(RuleChange { rule: Rule::FrontRideHeightAtMin, keys: keys.iter().map(|k| k.to_string()).collect(), from, to });
            }
        }
    }
    if flags.lockups {
        if let Some(keys) = movable(setup, &["VM_BRAKE_PRESSURE"], locks) {
            let p = &setup.params[keys[0]];
            if p.value > p.min {
                out.push(RuleChange { rule: Rule::LockupsBrakePressure, keys: vec![keys[0].to_string()], from: p.value, to: p.value - 1 });
            }
        }
    }
    out
}
