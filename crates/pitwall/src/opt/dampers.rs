/* Damper and kerb compliance planner.
   Drives whole-click fast and slow damper changes from native-rate suspension
   telemetry (DamperMetrics off-kerb shaft velocity histograms and KerbSummary
   p90 kerb violence) plus the driver's kerbs_unstable flag.

   Fast circuit serves kerbs (softer fast bump lowers peak impact load, softer
   fast rebound lets the wheel drop back onto the track sooner after a strike).
   Slow circuit serves off-kerb contact patch symmetry (50/50 bump vs rebound)
   and low-speed share around the class damper_ls_target.  When kerbs_unstable
   is set, no damper may stiffen and blocked stiffening proposals are counted
   explicitly on DamperPlan::rejected_kerb.  */

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::metrics::suspension::{DamperMetrics, KerbSummary};
use crate::opt::solve::DriverFlags;
use crate::setup::CarSetup;

const MAX_DAMPER_MOVES: usize = 2;
const MIN_KERB_EVENTS: usize = 4;
const KERB_PEAK_LOAD_HI: f64 = 1.8;
const KERB_AIRTIME_HI_S: f64 = 0.04;
const KERB_YAW_HI: f64 = 0.04;
const SYM_TOL: f64 = 0.04;
const LS_LO_OFFSET: f64 = 0.10;
const LS_HI_OFFSET: f64 = 0.09;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum DamperKind {
    FastBumpFront,
    FastReboundFront,
    FastBumpRear,
    FastReboundRear,
    SlowBumpFront,
    SlowReboundFront,
    SlowBumpRear,
    SlowReboundRear,
}

impl DamperKind {
    pub const fn keys(self) -> &'static [&'static str] {
        match self {
            Self::FastBumpFront => &["WM_FASTBUMP-W_FL", "WM_FASTBUMP-W_FR"],
            Self::FastReboundFront => &["WM_FASTREBOUND-W_FL", "WM_FASTREBOUND-W_FR"],
            Self::FastBumpRear => &["WM_FASTBUMP-W_RL", "WM_FASTBUMP-W_RR"],
            Self::FastReboundRear => &["WM_FASTREBOUND-W_RL", "WM_FASTREBOUND-W_RR"],
            Self::SlowBumpFront => &["WM_SLOWBUMP-W_FL", "WM_SLOWBUMP-W_FR"],
            Self::SlowReboundFront => &["WM_SLOWREBOUND-W_FL", "WM_SLOWREBOUND-W_FR"],
            Self::SlowBumpRear => &["WM_SLOWBUMP-W_RL", "WM_SLOWBUMP-W_RR"],
            Self::SlowReboundRear => &["WM_SLOWREBOUND-W_RL", "WM_SLOWREBOUND-W_RR"],
        }
    }

    pub const fn label(self) -> &'static str {
        match self {
            Self::FastBumpFront => "front fast bump",
            Self::FastReboundFront => "front fast rebound",
            Self::FastBumpRear => "rear fast bump",
            Self::FastReboundRear => "rear fast rebound",
            Self::SlowBumpFront => "front slow bump",
            Self::SlowReboundFront => "front slow rebound",
            Self::SlowBumpRear => "rear slow bump",
            Self::SlowReboundRear => "rear slow rebound",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DamperChange {
    pub kind: DamperKind,
    pub keys: Vec<String>,
    pub from: i32,
    pub to: i32,
    pub reason: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DamperPlan {
    pub changes: Vec<DamperChange>,
    // Candidate stiffening moves blocked because kerbs_unstable was set.
    pub rejected_kerb: usize,
}

pub struct DamperInput<'a> {
    pub setup: &'a CarSetup,
    pub class: CarClass,
    pub dampers: &'a DamperMetrics,
    pub kerbs: &'a KerbSummary,
    pub flags: &'a DriverFlags,
    pub locks: &'a BTreeSet<String>,
}

struct Builder<'a> {
    setup: &'a CarSetup,
    locks: &'a BTreeSet<String>,
    kerbs_unstable: bool,
    plan: DamperPlan,
}

impl Builder<'_> {
    fn usable(&self, keys: &[&str]) -> bool {
        !keys.is_empty()
            && keys.iter().all(|k| self.setup.params.get(*k).is_some_and(|p| p.adjustable()) && !self.locks.contains(*k))
            && !self.plan.changes.iter().any(|c| c.keys.iter().any(|k| keys.contains(&k.as_str())))
    }

    fn push(&mut self, kind: DamperKind, delta: i32, reason: String) -> bool {
        if self.plan.changes.len() >= MAX_DAMPER_MOVES {
            return false;
        }
        let keys = kind.keys();
        if !self.usable(keys) {
            return false;
        }
        if self.kerbs_unstable && delta > 0 {
            self.plan.rejected_kerb += 1;
            return false;
        }
        let p = &self.setup.params[keys[0]];
        let to = (p.value + delta).clamp(p.min, p.max_exclusive - 1);
        if to == p.value {
            return false;
        }
        self.plan.changes.push(DamperChange {
            kind,
            keys: keys.iter().map(|k| (*k).to_string()).collect(),
            from: p.value,
            to,
            reason,
        });
        true
    }
}

pub fn plan_dampers(input: &DamperInput<'_>) -> DamperPlan {
    let mut b = Builder {
        setup: input.setup,
        locks: input.locks,
        kerbs_unstable: input.flags.kerbs_unstable,
        plan: DamperPlan::default(),
    };

    // Fast circuit: driver-flagged kerb instability takes priority, followed by
    // measured kerb violence (p90 peak load, airtime, or yaw disturbance).
    let k = input.kerbs;
    let harsh_load = k.events >= MIN_KERB_EVENTS && k.peak_load.is_some_and(|v| v > KERB_PEAK_LOAD_HI);
    let harsh_air = k.events >= MIN_KERB_EVENTS && k.airtime_s.is_some_and(|v| v > KERB_AIRTIME_HI_S);
    let harsh_yaw = k.events >= MIN_KERB_EVENTS && k.yaw_dist.is_some_and(|v| v > KERB_YAW_HI);

    if input.flags.kerbs_unstable {
        b.push(
            DamperKind::FastBumpFront,
            -2,
            "You flagged the car unstable over kerbs → soften front fast bump to absorb impacts".into(),
        );
        b.push(
            DamperKind::FastReboundFront,
            -2,
            "You flagged the car unstable over kerbs → soften front fast rebound so the tyre settles faster".into(),
        );
        b.push(
            DamperKind::FastBumpRear,
            -2,
            "You flagged the car unstable over kerbs → soften rear fast bump to absorb impacts".into(),
        );
    } else if harsh_load || harsh_yaw {
        let why = if harsh_load {
            format!("Kerb strikes spike to {:.2}× usual spring load (p90 over {} hits) → soften fast bump", k.peak_load.unwrap_or(0.0), k.events)
        } else {
            format!("Kerb strikes disturb yaw by {:.3} rad/s (p90 over {} hits) → soften fast bump", k.yaw_dist.unwrap_or(0.0), k.events)
        };
        b.push(DamperKind::FastBumpFront, -1, why.clone());
        if harsh_air {
            b.push(
                DamperKind::FastReboundFront,
                -1,
                format!("Wheels unload for {:.0} ms over kerbs → soften fast rebound", k.airtime_s.unwrap_or(0.0) * 1e3),
            );
        } else {
            b.push(DamperKind::FastBumpRear, -1, why);
        }
    } else if harsh_air {
        let why = format!("Wheels unload for {:.0} ms over kerbs (p90 over {} hits) → soften fast rebound", k.airtime_s.unwrap_or(0.0) * 1e3, k.events);
        b.push(DamperKind::FastReboundFront, -1, why.clone());
        b.push(DamperKind::FastReboundRear, -1, why);
    }

    // Slow circuit: off-kerb shaft-velocity symmetry and slow/fast knee split.
    let target_ls = input.class.damper_ls_target();
    let shares = input.dampers.axle_bump_shares();
    let axles = [
        ("Front", input.dampers.axles.front, shares.front, DamperKind::SlowBumpFront, DamperKind::SlowReboundFront),
        ("Rear", input.dampers.axles.rear, shares.rear, DamperKind::SlowBumpRear, DamperKind::SlowReboundRear),
    ];
    for (name, shape, bump_share, sb, sr) in axles {
        let Some(shape) = shape else { continue };
        if let Some(bs) = bump_share {
            if bs > 0.5 + SYM_TOL {
                b.push(
                    sb,
                    -1,
                    format!("{name} off-kerb shaft motion spends {:.0} % in bump (target ~50 %) → soften slow bump", bs * 100.0),
                );
            } else if bs < 0.5 - SYM_TOL {
                b.push(
                    sr,
                    -1,
                    format!("{name} off-kerb shaft motion spends {:.0} % in rebound (target ~50 %) → soften slow rebound", (1.0 - bs) * 100.0),
                );
            }
        }
        if shape.slow_share < target_ls - LS_LO_OFFSET {
            b.push(
                sb,
                1,
                format!("{name} off-kerb slow share is {:.0} % (target ~{:.0} %) → stiffen slow bump for body control", shape.slow_share * 100.0, target_ls * 100.0),
            );
        } else if shape.slow_share > target_ls + LS_HI_OFFSET {
            b.push(
                sb,
                -1,
                format!("{name} off-kerb slow share is {:.0} % (target ~{:.0} %) → soften slow bump for road compliance", shape.slow_share * 100.0, target_ls * 100.0),
            );
        }
    }

    b.plan
}
