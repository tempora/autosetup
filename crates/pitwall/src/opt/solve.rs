/* Mechanical setup search inside the driver's balance band.
   Predictions are anchored on the stint: per cell, predicted = measured +
   (model(candidate) − model(stint setup as driven)), both evaluated at the stint's own
   apex speed and measured a_lon, so the driver's pedal habits cancel in the
   delta instead of being baked into the candidate.

   Ranking is lexicographic: first the time-weighted band excess (distance
   outside the target band, in tolerances), then pace (time-weighted
   predicted gain in p95 lateral G over cornering cells, minus a small cost
   per click moved).  Local search walks single- and two-click moves from the
   stint's setup within each Dim's reach.

   Gates reject candidates and are counted on every result, so a broken gate
   shows as a rejection count rather than an empty "already optimal" answer:
     Balance         band excess worse than the stint's own
     Undrivable      repeats, as far or further, a move of a stint flagged undrivable
     KerbStiffening  stiffens a bar or spring while kerbs are flagged unstable
     Countersteer    (proposal, switchable) adds rotation in a cell where the stint
                     already countersteered, unless the driver asked for rotation there  */

use std::collections::{BTreeMap, HashSet};

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CornerPhase};
use crate::metrics::balance::CellStats;
use crate::opt::space::{Candidate, DimKind, Space};
use crate::opt::target::{BalanceTarget, CellTarget};
use crate::physics::{CornerState, MechanicalModel};
use crate::setup::CarSetup;
use crate::units::{Degrees, Gs};
use crate::validation::MIN_CELL_PASSES;

const CHANGE_COST_G: f64 = 0.001;
const EXCESS_EPS: f64 = 0.05;
const MAX_STEPS: usize = 20;
pub const HINT_MIN_G: f64 = 0.01;
pub const COUNTERSTEER_S_PER_PASS: f64 = 0.05;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Gate {
    Balance,
    Undrivable,
    KerbStiffening,
    Countersteer,
}

#[derive(Debug, Clone, Default)]
pub struct DriverFlags {
    pub kerbs_unstable: bool,
    pub lockups: bool,
    // Setup of a stint the driver could not drive.
    pub undrivable: Option<CarSetup>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Toggles {
    pub countersteer_guard: bool,
}

impl Default for Toggles {
    fn default() -> Self {
        Self { countersteer_guard: true }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CellPrediction {
    pub cell: BalanceCell,
    // Share of cornering passes.
    pub weight: f64,
    pub measured_ua: Degrees,
    pub predicted_ua: Degrees,
    pub measured_lat: Gs,
    pub predicted_lat: Gs,
    pub target: Option<CellTarget>,
    pub countersteer_s: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Change {
    pub kind: DimKind,
    pub from: i32,
    pub to: i32,
    // The cell this change alone moves most, and by how much.
    pub strongest: Option<(BalanceCell, Degrees, Gs)>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Evaluated {
    pub candidate: Candidate,
    pub cells: Vec<CellPrediction>,
    pub band_excess: f64,
    pub pace_g: f64,
    pub clicks: i32,
    pub failed: Vec<Gate>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Outcome {
    Changed,
    AlreadyBest,
    AllRejected,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PaceHint {
    pub changes: Vec<Change>,
    pub extra_grip_g: f64,
    // Cells the hint would take outside the band: (cell, predicted − centre).
    pub leaves_band: Vec<(BalanceCell, Degrees)>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Solution {
    pub outcome: Outcome,
    pub changes: Vec<Change>,
    pub base: Evaluated,
    pub chosen: Evaluated,
    pub candidates: usize,
    pub rejected: BTreeMap<Gate, usize>,
    pub hint: Option<PaceHint>,
}

pub struct Solver<'a, M: MechanicalModel> {
    pub model: &'a M,
    pub space: &'a Space,
    pub cells: &'a [CellStats],
    pub target: &'a BalanceTarget,
    pub flags: &'a DriverFlags,
    pub toggles: Toggles,
}

impl<M: MechanicalModel> Solver<'_, M> {
    fn usable(&self) -> Vec<&CellStats> {
        self.cells.iter().filter(|c| c.passes >= MIN_CELL_PASSES).collect()
    }

    fn state(c: &CellStats) -> CornerState {
        CornerState { speed: c.v_min, phase: c.cell.phase, a_lon: c.a_lon }
    }

    fn predict(&self, cand: &[i32]) -> Vec<CellPrediction> {
        let base_setup = self.space.setup.mechanical_vector();
        let cand_setup = self.space.mechanical(cand);
        let usable = self.usable();
        let total: f64 = usable.iter().map(|c| c.passes as f64).sum::<f64>().max(1.0);
        usable
            .into_iter()
            .map(|c| {
                let p0 = self.model.evaluate(&base_setup, Self::state(c));
                let p1 = self.model.evaluate(&cand_setup, Self::state(c));
                CellPrediction {
                    cell: c.cell,
                    weight: c.passes as f64 / total,
                    measured_ua: c.ua,
                    predicted_ua: c.ua + (p1.understeer - p0.understeer),
                    measured_lat: c.a_lat_p95,
                    predicted_lat: c.a_lat_p95 + (p1.max_lat_g - p0.max_lat_g),
                    target: self.target.get(c.cell).copied(),
                    countersteer_s: c.countersteer_s.raw(),
                }
            })
            .collect()
    }

    fn evaluate(&self, cand: &[i32], base_excess: Option<f64>) -> Evaluated {
        let cells = self.predict(cand);
        let band_excess: f64 = cells.iter().filter_map(|p| Some(p.weight * p.target?.excess(p.predicted_ua))).sum();
        let base_idx = self.space.base();
        let clicks: i32 = cand.iter().zip(&base_idx).map(|(a, b)| (a - b).abs()).sum();
        let pace_g = cells
            .iter()
            .filter(|p| p.cell.phase != CornerPhase::Braking)
            .map(|p| p.weight * (p.predicted_lat - p.measured_lat).raw())
            .sum::<f64>()
            - CHANGE_COST_G * f64::from(clicks);

        let mut failed = Vec::new();
        if base_excess.is_some_and(|b| band_excess > b + 1e-9 && band_excess > 0.0) {
            failed.push(Gate::Balance);
        }
        if self.repeats_undrivable(cand) {
            failed.push(Gate::Undrivable);
        }
        if self.flags.kerbs_unstable
            && self.space.dims.iter().zip(cand).any(|(d, &v)| d.kind.is_roll_stiffness() && v > d.base)
        {
            failed.push(Gate::KerbStiffening);
        }
        if self.toggles.countersteer_guard
            && cells.iter().any(|p| {
                let asked_rotation = p.target.is_some_and(|t| t.centre < p.measured_ua);
                p.countersteer_s > COUNTERSTEER_S_PER_PASS
                    && !asked_rotation
                    && p.predicted_ua < p.measured_ua - Degrees::new(1e-6)
            })
        {
            failed.push(Gate::Countersteer);
        }
        Evaluated { candidate: cand.to_vec(), cells, band_excess, pace_g, clicks, failed }
    }

    fn repeats_undrivable(&self, cand: &[i32]) -> bool {
        let Some(bad) = &self.flags.undrivable else { return false };
        self.space.dims.iter().zip(cand).any(|(d, &v)| {
            let keys = d.kind.keys();
            let Some(vals) = keys.iter().map(|k| bad.params.get(*k).map(|p| f64::from(p.value))).collect::<Option<Vec<_>>>()
            else {
                return false;
            };
            let bad_idx = (vals.iter().sum::<f64>() / vals.len() as f64).round() as i32;
            let step = bad_idx - d.base;
            step != 0 && (v - d.base) * step.signum() >= step.abs()
        })
    }

    fn better(a: &Evaluated, b: &Evaluated) -> bool {
        if (a.band_excess - b.band_excess).abs() > EXCESS_EPS {
            a.band_excess < b.band_excess
        } else {
            a.pace_g > b.pace_g + 1e-9
        }
    }

    fn changes(&self, from: &[i32], to: &[i32]) -> Vec<Change> {
        self.space
            .dims
            .iter()
            .enumerate()
            .filter(|(i, _)| from[*i] != to[*i])
            .map(|(i, d)| {
                let mut alone = from.to_vec();
                alone[i] = to[i];
                let strongest = self
                    .predict(&alone)
                    .into_iter()
                    .map(|p| (p.cell, p.predicted_ua - p.measured_ua, p.predicted_lat - p.measured_lat))
                    .max_by(|a, b| a.1.raw().abs().total_cmp(&b.1.raw().abs()));
                Change { kind: d.kind, from: from[i], to: to[i], strongest }
            })
            .collect()
    }

    pub fn solve(&self) -> Solution {
        let base_idx = self.space.base();
        let base = self.evaluate(&base_idx, None);
        let mut seen: HashSet<Candidate> = HashSet::from([base_idx.clone()]);
        let mut all: Vec<Evaluated> = Vec::new();
        let mut current = base.clone();
        for _ in 0..MAX_STEPS {
            let step: Vec<Evaluated> = self
                .space
                .neighbours(&current.candidate)
                .into_iter()
                .filter(|c| seen.insert(c.clone()))
                .map(|c| self.evaluate(&c, Some(base.band_excess)))
                .collect();
            let best = step
                .iter()
                .filter(|e| e.failed.is_empty())
                .fold(None::<&Evaluated>, |acc, e| match acc {
                    Some(a) if !Self::better(e, a) => Some(a),
                    _ => Some(e),
                })
                .cloned();
            all.extend(step);
            match best {
                Some(b) if Self::better(&b, &current) => current = b,
                _ => break,
            }
        }

        let mut rejected: BTreeMap<Gate, usize> = BTreeMap::new();
        for g in all.iter().flat_map(|e| e.failed.iter()) {
            *rejected.entry(*g).or_default() += 1;
        }
        let outcome = if current.candidate != base_idx {
            Outcome::Changed
        } else if !all.is_empty() && all.iter().all(|e| !e.failed.is_empty()) {
            Outcome::AllRejected
        } else {
            Outcome::AlreadyBest
        };
        let hint = all
            .iter()
            .filter(|e| e.failed == [Gate::Balance])
            .max_by(|a, b| a.pace_g.total_cmp(&b.pace_g))
            .filter(|e| e.pace_g - current.pace_g > HINT_MIN_G)
            .map(|e| PaceHint {
                changes: self.changes(&current.candidate, &e.candidate),
                extra_grip_g: e.pace_g - current.pace_g,
                leaves_band: e
                    .cells
                    .iter()
                    .filter_map(|p| {
                        let t = p.target?;
                        (t.excess(p.predicted_ua) > 0.0).then(|| (p.cell, p.predicted_ua - t.centre))
                    })
                    .collect(),
            });
        Solution {
            outcome,
            changes: self.changes(&base_idx, &current.candidate),
            candidates: all.len(),
            rejected,
            hint,
            base,
            chosen: current,
        }
    }
}
