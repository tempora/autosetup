/* The mechanical search space in the game's own click indices.
   Each Dim ties the keys that move together (per-wheel settings per axle)
   and allows indices minValue ..= maxValue − 1.  A proposal moves a Dim at
   most `reach` clicks from the stint's setup.  Locked keys, settings the car
   lacks, and GT3 front ride height (always at its minimum, never searched)
   are not dimensions.  Rule-fixed keys are applied to every candidate,
   including the unmoved one, so predictions include the rules' effect.

   Index direction is taken as the physical direction (a higher index is
   stiffer / higher / more), except brake balance, whose index runs rearward;
   CarSetup::mechanical_vector maps both onto the model's conventions.  */

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::setup::{CarSetup, MechanicalSetup};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum DimKind {
    RideHeightFront,
    RideHeightRear,
    ArbFront,
    ArbRear,
    SpringFront,
    SpringRear,
    DiffPreload,
    DiffPower,
    DiffCoast,
    BrakeBias,
}

impl DimKind {
    // Wing is excluded from the pure-mechanical QSS search until a calibrated
    // drag model exists: without drag, adding wing would look like free grip.
    pub const ALL: [Self; 10] = [
        Self::RideHeightFront,
        Self::RideHeightRear,
        Self::ArbFront,
        Self::ArbRear,
        Self::SpringFront,
        Self::SpringRear,
        Self::DiffPreload,
        Self::DiffPower,
        Self::DiffCoast,
        Self::BrakeBias,
    ];

    pub const fn keys(self) -> &'static [&'static str] {
        match self {
            Self::RideHeightFront => &["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"],
            Self::RideHeightRear => &["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"],
            Self::ArbFront => &["VM_FRONT_ANTISWAY"],
            Self::ArbRear => &["VM_REAR_ANTISWAY"],
            Self::SpringFront => &["WM_SPRING-W_FL", "WM_SPRING-W_FR"],
            Self::SpringRear => &["WM_SPRING-W_RL", "WM_SPRING-W_RR"],
            Self::DiffPreload => &["VM_DIFF_PRELOAD"],
            Self::DiffPower => &["VM_DIFF_POWER"],
            Self::DiffCoast => &["VM_DIFF_COAST"],
            Self::BrakeBias => &["VM_BRAKE_BALANCE"],
        }
    }

    // Clicks one proposal may move this setting.
    pub const fn reach(self) -> i32 {
        match self {
            Self::ArbFront | Self::ArbRear | Self::DiffPower | Self::DiffCoast => 1,
            Self::SpringFront | Self::SpringRear => 1,
            Self::RideHeightFront | Self::RideHeightRear | Self::DiffPreload | Self::BrakeBias => 2,
        }
    }

    pub const fn is_roll_stiffness(self) -> bool {
        matches!(self, Self::ArbFront | Self::ArbRear | Self::SpringFront | Self::SpringRear)
    }

    pub const fn label(self) -> &'static str {
        match self {
            Self::RideHeightFront => "front ride height",
            Self::RideHeightRear => "rear ride height",
            Self::ArbFront => "front anti-roll bar",
            Self::ArbRear => "rear anti-roll bar",
            Self::SpringFront => "front springs",
            Self::SpringRear => "rear springs",
            Self::DiffPreload => "diff preload",
            Self::DiffPower => "diff power ramp",
            Self::DiffCoast => "diff coast ramp",
            Self::BrakeBias => "brake bias",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Dim {
    pub kind: DimKind,
    pub lo: i32,
    pub hi: i32,
    pub base: i32,
}

impl Dim {
    #[inline]
    pub fn clamp(&self, index: i32) -> i32 {
        index.clamp(self.lo.max(self.base - self.kind.reach()), self.hi.min(self.base + self.kind.reach()))
    }
}

#[derive(Debug, Clone)]
pub struct Space {
    // The stint's setup as driven: the anchor of every prediction.
    pub setup: CarSetup,
    pub dims: Vec<Dim>,
    // (key, index) set by deterministic rules on every candidate.
    pub fixed: Vec<(String, i32)>,
}

// Click index per Dim, aligned with Space::dims.
pub type Candidate = Vec<i32>;

impl Space {
    pub fn new(setup: &CarSetup, class: CarClass, locks: &BTreeSet<String>) -> Self {
        let dims = DimKind::ALL
            .into_iter()
            .filter(|k| !(class.front_ride_height_at_min() && *k == DimKind::RideHeightFront))
            .filter_map(|kind| {
                let params: Vec<_> = kind.keys().iter().map(|k| setup.params.get(*k)).collect::<Option<_>>()?;
                if params.iter().any(|p| !p.adjustable()) || kind.keys().iter().any(|k| locks.contains(*k)) {
                    return None;
                }
                let base = (params.iter().map(|p| f64::from(p.value)).sum::<f64>() / params.len() as f64).round() as i32;
                Some(Dim { kind, lo: params[0].min, hi: params[0].max_exclusive - 1, base })
            })
            .collect();
        Self { setup: setup.clone(), dims, fixed: Vec::new() }
    }

    pub fn with_fixed(mut self, fixed: Vec<(String, i32)>) -> Self {
        self.fixed = fixed;
        self
    }

    pub fn base(&self) -> Candidate {
        self.dims.iter().map(|d| d.base).collect()
    }

    pub fn apply(&self, cand: &[i32]) -> CarSetup {
        let mut out = self.setup.clone();
        for (key, v) in &self.fixed {
            out.set_value(key, *v);
        }
        for (d, &v) in self.dims.iter().zip(cand) {
            for key in d.kind.keys() {
                out.set_value(key, v);
            }
        }
        out
    }

    pub fn mechanical(&self, cand: &[i32]) -> MechanicalSetup {
        self.apply(cand).mechanical_vector()
    }

    // Candidates one or two single-click moves away from `from`, within reach.
    pub fn neighbours(&self, from: &[i32]) -> Vec<Candidate> {
        let singles: Vec<(usize, i32)> = self
            .dims
            .iter()
            .enumerate()
            .flat_map(|(i, d)| [-1, 1].into_iter().map(move |s| (i, s)).filter(move |&(_, s)| d.clamp(from[i] + s) != from[i]))
            .collect();
        let step = |moves: &[(usize, i32)]| -> Candidate {
            let mut c = from.to_vec();
            for &(i, s) in moves {
                c[i] = self.dims[i].clamp(c[i] + s);
            }
            c
        };
        let pairs = singles.iter().enumerate().flat_map(|(a, &ma)| {
            singles[a + 1..].iter().filter(move |mb| mb.0 != ma.0).map(move |&mb| step(&[ma, mb]))
        });
        singles.iter().map(|&m| step(&[m])).chain(pairs).collect()
    }
}
