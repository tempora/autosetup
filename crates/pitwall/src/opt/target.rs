/* The balance the optimiser aims for, and the feel sliders that move it.
   Three axes, each set on a stint in degrees of understeer angle (− is more
   rotation), clamped to ± RANGE_STEPS class feel steps:
     front  Pointier ↔ Lazier   turn-in (full), mid-corner (0.3)
     rear   Looser ↔ Safer      mid-corner (0.7), exit (full)
     fast   Looser ↔ Planted    fast corners only: entry 0.5, mid 1.0, exit 0.5
   The target is the anchor balance (a locked fingerprint, else the stint as
   driven) moved per cell by the sliders.  The band is two-sided: ± the class
   tolerance, kept under half of a cell's move so the stint as driven lies
   outside it, and never narrower than the cell's measurement resolution.  A
   move smaller than that resolution in every cell it covers changes nothing.
   Sliders never move a setting themselves.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CarClass, CornerPhase, SpeedBand};
use crate::metrics::balance::CellStats;
use crate::units::Degrees;

const RANGE_STEPS: f64 = 2.0;
const RES_MIN_DEG: f64 = 0.05;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum FeelAxis {
    Front,
    Rear,
    Fast,
}

impl FeelAxis {
    pub const ALL: [Self; 3] = [Self::Front, Self::Rear, Self::Fast];

    pub const fn weight(self, cell: BalanceCell) -> f64 {
        match (self, cell.phase, cell.band) {
            (Self::Front, CornerPhase::Entry, _) => 1.0,
            (Self::Front, CornerPhase::Mid, _) => 0.3,
            (Self::Rear, CornerPhase::Mid, _) => 0.7,
            (Self::Rear, CornerPhase::Exit, _) => 1.0,
            (Self::Fast, CornerPhase::Entry, SpeedBand::High) => 0.5,
            (Self::Fast, CornerPhase::Mid, SpeedBand::High) => 1.0,
            (Self::Fast, CornerPhase::Exit, SpeedBand::High) => 0.5,
            _ => 0.0,
        }
    }

    // Words for (− end, + end).
    pub const fn ends(self) -> (&'static str, &'static str) {
        match self {
            Self::Front => ("Pointier", "Lazier"),
            Self::Rear => ("Looser", "Safer"),
            Self::Fast => ("Fast looser", "Planted"),
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Sliders {
    pub front: Degrees,
    pub rear: Degrees,
    pub fast: Degrees,
}

impl Sliders {
    pub fn get(&self, axis: FeelAxis) -> Degrees {
        match axis {
            FeelAxis::Front => self.front,
            FeelAxis::Rear => self.rear,
            FeelAxis::Fast => self.fast,
        }
    }

    pub fn with(mut self, axis: FeelAxis, value: Degrees) -> Self {
        match axis {
            FeelAxis::Front => self.front = value,
            FeelAxis::Rear => self.rear = value,
            FeelAxis::Fast => self.fast = value,
        }
        self
    }

    pub fn clamped(self, class: CarClass) -> Self {
        let span = class.feel_step().raw() * RANGE_STEPS;
        let c = |d: Degrees| Degrees::new(d.raw().clamp(-span, span));
        Self { front: c(self.front), rear: c(self.rear), fast: c(self.fast) }
    }

    pub fn moved(&self) -> bool {
        FeelAxis::ALL.iter().any(|&a| self.get(a).raw().abs() > 1e-9)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CellTarget {
    pub cell: BalanceCell,
    pub centre: Degrees,
    pub tolerance: Degrees,
}

impl CellTarget {
    // Distance outside the band, in tolerances (0 inside).
    #[inline]
    pub fn excess(&self, ua: Degrees) -> f64 {
        ((ua - self.centre).raw().abs() - self.tolerance.raw()).max(0.0) / self.tolerance.raw()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Anchor {
    Fingerprint,
    Stint,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BalanceTarget {
    pub anchor: Anchor,
    pub sliders: Sliders,
    pub cells: Vec<CellTarget>,
}

impl BalanceTarget {
    /* `anchor`: the fingerprint's cells when one is locked, else the stint's
       own.  `stint`: the stint the sliders were set on (its measurement
       resolution bounds the band).  */
    pub fn new(class: CarClass, stint: &[CellStats], fingerprint: Option<&[CellStats]>, sliders: Sliders) -> Self {
        let sliders = sliders.clamped(class);
        let anchor_cells = fingerprint.unwrap_or(stint);
        let resolution = |cell: BalanceCell| -> f64 {
            stint.iter().find(|c| c.cell == cell).map_or(RES_MIN_DEG, |c| c.ua_se.raw().max(RES_MIN_DEG))
        };
        // An axis whose move no covered cell can resolve changes nothing.
        let effective = FeelAxis::ALL.into_iter().fold(Sliders::default(), |acc, axis| {
            let v = sliders.get(axis).raw();
            let resolves = anchor_cells
                .iter()
                .any(|c| axis.weight(c.cell) > 0.0 && (v * axis.weight(c.cell)).abs() >= resolution(c.cell));
            acc.with(axis, Degrees::new(if resolves { v } else { 0.0 }))
        });
        let cells = anchor_cells
            .iter()
            .map(|c| {
                let shift: f64 = FeelAxis::ALL.iter().map(|&a| a.weight(c.cell) * effective.get(a).raw()).sum();
                let mut tol = class.feel_tolerance().raw();
                if shift.abs() > 1e-9 {
                    tol = tol.min(0.5 * shift.abs());
                }
                CellTarget {
                    cell: c.cell,
                    centre: c.ua + Degrees::new(shift),
                    tolerance: Degrees::new(tol.max(resolution(c.cell))),
                }
            })
            .collect();
        Self {
            anchor: if fingerprint.is_some() { Anchor::Fingerprint } else { Anchor::Stint },
            sliders: effective,
            cells,
        }
    }

    pub fn get(&self, cell: BalanceCell) -> Option<&CellTarget> {
        self.cells.iter().find(|t| t.cell == cell)
    }
}
