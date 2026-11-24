/* Setup proposals: mechanical search inside the driver's balance band plus
   deterministic rules, with a plain-English explanation.
   `plan` takes one stint's balance cells, the driver's feel sliders and
   flags, and returns a Proposal carrying the chosen changes, every gate's
   rejection count, an optional pace hint just outside the band, and the
   predictions behind the choice.  Rules (GT3 front ride height, lock-ups)
   are fixed on every candidate, never searched, and included in every
   prediction.  */

pub mod rules;
pub mod solve;
pub mod space;
pub mod target;
pub mod why;

#[cfg(test)]
mod tests;

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::metrics::balance::CellStats;
use crate::physics::calibrate::Verification;
use crate::physics::MechanicalModel;
use crate::setup::CarSetup;
use rules::{apply_rules, RuleChange};
use solve::{DriverFlags, Solution, Solver, Toggles};
use space::Space;
use target::{BalanceTarget, Sliders};

pub struct PlanInput<'a> {
    pub class: CarClass,
    pub setup: &'a CarSetup,
    pub cells: &'a [CellStats],
    pub fingerprint: Option<&'a [CellStats]>,
    pub sliders: Sliders,
    pub flags: DriverFlags,
    pub toggles: Toggles,
    pub locks: BTreeSet<String>,
    // Whether the model's predictions beat "no change" on real stints.
    pub verification: Verification,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Proposal {
    pub verification: Verification,
    pub target: BalanceTarget,
    pub solution: Solution,
    pub rules: Vec<RuleChange>,
}

impl Proposal {
    // The stint's setup with every proposed change and rule applied.
    pub fn setup_from(&self, base: &CarSetup) -> CarSetup {
        let mut out = base.clone();
        for c in &self.solution.changes {
            for key in c.kind.keys() {
                out.set_value(key, c.to);
            }
        }
        for r in &self.rules {
            for key in &r.keys {
                out.set_value(key, r.to);
            }
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.solution.changes.is_empty() && self.rules.is_empty()
    }
}

pub fn plan<M: MechanicalModel>(model: &M, input: &PlanInput<'_>) -> Proposal {
    let rules = apply_rules(input.setup, input.class, &input.flags, &input.locks);
    let fixed = rules.iter().flat_map(|r| r.keys.iter().map(|k| (k.clone(), r.to))).collect();
    let space = Space::new(input.setup, input.class, &input.locks).with_fixed(fixed);
    let target = BalanceTarget::new(input.class, input.cells, input.fingerprint, input.sliders);
    let solution = Solver {
        model,
        space: &space,
        cells: input.cells,
        target: &target,
        flags: &input.flags,
        toggles: input.toggles,
    }
    .solve();
    Proposal { verification: input.verification, rules, target, solution }
}
