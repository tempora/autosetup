/* Setup proposals: mechanical search inside the driver's balance band plus
   deterministic rules, with a plain-English explanation.
   `plan` takes one stint's balance cells, the driver's feel sliders and
   flags, and returns a Proposal carrying the chosen changes, every gate's
   rejection count, an optional pace hint just outside the band, and the
   predictions behind the choice.  Rule-moved keys (rules.rs) are fixed on
   every candidate, excluded from the search, and included in every
   prediction.  */

pub mod dampers;
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
use crate::metrics::summary::StintSummary;
use crate::physics::calibrate::Verification;
use crate::physics::MechanicalModel;
use crate::setup::CarSetup;
use dampers::{plan_dampers, DamperInput, DamperPlan, DamperStint};
use rules::{apply_rules, RuleChange, RuleInput};
use solve::{DriverFlags, Solution, Solver, Toggles};
use space::Space;
use target::{BalanceTarget, Sliders};

pub struct PlanInput<'a> {
    pub class: CarClass,
    pub setup: &'a CarSetup,
    pub cells: &'a [CellStats],
    pub summary: &'a StintSummary,
    // Damper metrics of this car and layout's stints, the current one last.
    pub damper_history: &'a [DamperStint],
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
    pub dampers: DamperPlan,
}

impl Proposal {
    // The stint's setup with every proposed change, rule, and damper move applied.
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
        for d in &self.dampers.changes {
            for key in &d.keys {
                out.set_value(key, d.to);
            }
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.solution.changes.is_empty() && self.rules.is_empty() && self.dampers.changes.is_empty()
    }
}

pub fn plan<M: MechanicalModel>(model: &M, input: &PlanInput<'_>) -> Proposal {
    let rules = apply_rules(&RuleInput {
        setup: input.setup,
        class: input.class,
        flags: &input.flags,
        locks: &input.locks,
        summary: input.summary,
    });
    let fixed: Vec<(String, i32)> = rules.iter().flat_map(|r| r.keys.iter().map(|k| (k.clone(), r.to))).collect();
    // A key a rule moves is not also searched.
    let mut search_locks = input.locks.clone();
    search_locks.extend(fixed.iter().map(|(k, _)| k.clone()));
    let space = Space::new(input.setup, input.class, &search_locks).with_fixed(fixed);
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
    let dampers = plan_dampers(&DamperInput {
        setup: input.setup,
        history: input.damper_history,
        kerbs_unstable: input.flags.kerbs_unstable,
        main_changes: !solution.changes.is_empty(),
        locks: &search_locks,
        target: &target,
        chosen: &solution.chosen,
    });
    Proposal { verification: input.verification, rules, dampers, target, solution }
}
