/* Four-wheel quasi-steady-state (QSS) mechanical vehicle model.
   Evaluates limit lateral acceleration and understeer angle from normalised
   setup coordinates via lateral load transfer distribution (LLTD), nonlinear
   tyre load sensitivity μ(Fz), aero balance shift, longitudinal load transfer
   from the measured a_lon, a per-axle friction circle, and phase-dependent
   differential locking.

   Sign conventions: positive understeer angle means understeer; positive rake
   means higher rear ride height; a_lon is + accelerating, − braking.  Under
   braking the front axle carries the brake-bias share of the longitudinal
   force, under drive the driven axles carry it (drive_front_share).  The
   friction circle applies in Entry/Mid/Exit only; straight-line Braking
   spends no lateral grip.

   Each physics term can be switched off through `Terms` so holdout
   validation can prove that every shipped term improves real-data accuracy.  */

pub mod calibrate;
pub mod calibration;

use serde::{Deserialize, Serialize};

use crate::chassis::{Axle, CarClass, CornerPhase};
use crate::setup::MechanicalSetup;
use crate::units::{Degrees, Gs, MetersPerSec, Newtons, GRAVITY};

const MAX_LONG_UTILISATION: f64 = 0.9;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CornerState {
    pub speed: MetersPerSec,
    pub phase: CornerPhase,
    // Measured longitudinal acceleration of the stint in this phase.
    pub a_lon: Gs,
}

impl CornerState {
    #[inline]
    pub const fn steady(speed: MetersPerSec, phase: CornerPhase) -> Self {
        Self { speed, phase, a_lon: Gs::ZERO }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CornerPrediction {
    pub max_lat_g: Gs,
    pub understeer: Degrees,
    pub lltd_front: f64,
    pub vertical_front: f64,
}

pub trait MechanicalModel {
    fn evaluate(&self, setup: &MechanicalSetup, state: CornerState) -> CornerPrediction;
}

// Declares the switchable physics terms once: the Term enum, the Terms flag
// set, and the iteration order used by ablation reports.
macro_rules! define_terms {
    ($($variant:ident => $field:ident),+ $(,)?) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        pub enum Term { $($variant),+ }

        impl Term {
            pub const ALL: &'static [Term] = &[$(Term::$variant),+];
        }

        #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
        pub struct Terms { $(pub $field: bool),+ }

        impl Terms {
            pub const ALL: Self = Self { $($field: true),+ };
            pub const NONE: Self = Self { $($field: false),+ };

            #[inline]
            pub const fn enabled(self, term: Term) -> bool {
                match term { $(Term::$variant => self.$field),+ }
            }

            #[inline]
            pub fn with(mut self, term: Term, on: bool) -> Self {
                match term { $(Term::$variant => self.$field = on),+ }
                self
            }
        }
    };
}

define_terms! {
    LoadSensitivity => load_sensitivity,
    AeroBalance => aero_balance,
    Diff => diff,
    LongitudinalTransfer => longitudinal_transfer,
    FrictionCircle => friction_circle,
}

impl Term {
    // Terms that ablation may switch off. LoadSensitivity is core to 4-wheel
    // LLTD: without it, lateral load transfer cancels and springs/ARBs do zero.
    pub const OPTIONAL: &'static [Term] = &[
        Term::AeroBalance,
        Term::Diff,
        Term::LongitudinalTransfer,
        Term::FrictionCircle,
    ];
}

impl Terms {
    // Minimum physically meaningful 4-wheel model (LLTD active via tyre load sensitivity).
    pub const CORE: Self = Self {
        load_sensitivity: true,
        aero_balance: false,
        diff: false,
        longitudinal_transfer: false,
        friction_circle: false,
    };
}

impl Default for Terms {
    fn default() -> Self {
        Self::ALL
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct VehicleParams {
    pub mass_kg: f64,
    pub static_front_weight: f64,
    pub cg_height_m: f64,
    pub wheelbase_m: f64,
    pub track_width: Axle<f64>,
    pub spring_roll_range: Axle<(f64, f64)>,
    pub arb_roll_range: Axle<(f64, f64)>,
    // Front share of brake force across the brake-bias setting range.
    pub brake_bias_range: (f64, f64),
    // Front share of drive force (0 for rear-wheel drive).
    pub drive_front_share: f64,
    pub mu_nominal: f64,
    pub fz_ref: Newtons,
    pub load_sensitivity: f64,
    pub aero_cl_a: f64, // 0.5 * rho * Cl * A, N/(m/s)^2
    pub aero_wing_span: f64,
    pub aero_front_base: f64,
    pub aero_rake_gain: f64,
    pub aero_wing_balance_gain: f64,
    pub understeer_gain_deg: f64,
    pub diff_gain_deg: f64,
}

/* Class baselines are published-spec starting points (mass with driver,
   wheelbase, track) plus rough tyre/aero figures; `calibrate` fits them
   against real holdout pairs before they are trusted.  */
impl VehicleParams {
    pub const fn gt3_baseline() -> Self {
        Self {
            mass_kg: 1320.0,
            static_front_weight: 0.48,
            cg_height_m: 0.44,
            wheelbase_m: 2.65,
            track_width: Axle::new(1.66, 1.66),
            spring_roll_range: Axle::new((1.0, 2.2), (1.0, 2.2)),
            arb_roll_range: Axle::new((0.2, 2.0), (0.2, 2.0)),
            brake_bias_range: (0.50, 0.62),
            drive_front_share: 0.0,
            mu_nominal: 1.55,
            fz_ref: Newtons::new(3300.0),
            load_sensitivity: 0.14,
            aero_cl_a: 0.95,
            aero_wing_span: 0.30,
            aero_front_base: 0.44,
            aero_rake_gain: 0.08,
            aero_wing_balance_gain: -0.06,
            understeer_gain_deg: 6.0,
            diff_gain_deg: 0.6,
        }
    }

    pub const fn lmp2_baseline() -> Self {
        Self {
            mass_kg: 1010.0,
            static_front_weight: 0.45,
            cg_height_m: 0.30,
            wheelbase_m: 3.00,
            track_width: Axle::new(1.60, 1.55),
            spring_roll_range: Axle::new((1.2, 2.6), (1.2, 2.6)),
            arb_roll_range: Axle::new((0.3, 2.4), (0.3, 2.4)),
            brake_bias_range: (0.50, 0.60),
            drive_front_share: 0.0,
            mu_nominal: 1.70,
            fz_ref: Newtons::new(3000.0),
            load_sensitivity: 0.16,
            aero_cl_a: 2.0,
            aero_wing_span: 0.25,
            aero_front_base: 0.42,
            aero_rake_gain: 0.10,
            aero_wing_balance_gain: -0.08,
            understeer_gain_deg: 5.0,
            diff_gain_deg: 0.5,
        }
    }

    pub const fn lmp2_elms_baseline() -> Self {
        let mut p = Self::lmp2_baseline();
        p.aero_cl_a = 2.25;
        p
    }

    pub const fn hyper_baseline() -> Self {
        Self {
            mass_kg: 1110.0,
            static_front_weight: 0.47,
            cg_height_m: 0.32,
            wheelbase_m: 3.15,
            track_width: Axle::new(1.65, 1.60),
            spring_roll_range: Axle::new((1.2, 2.6), (1.2, 2.6)),
            arb_roll_range: Axle::new((0.3, 2.4), (0.3, 2.4)),
            brake_bias_range: (0.48, 0.60),
            drive_front_share: 0.0,
            mu_nominal: 1.70,
            fz_ref: Newtons::new(3300.0),
            load_sensitivity: 0.16,
            aero_cl_a: 2.1,
            aero_wing_span: 0.20,
            aero_front_base: 0.43,
            aero_rake_gain: 0.10,
            aero_wing_balance_gain: -0.06,
            understeer_gain_deg: 5.0,
            diff_gain_deg: 0.5,
        }
    }

    pub const fn for_class(class: CarClass) -> Self {
        match class {
            CarClass::Gt3 | CarClass::Other => Self::gt3_baseline(),
            CarClass::Lmp2 => Self::lmp2_baseline(),
            CarClass::Lmp2Elms => Self::lmp2_elms_baseline(),
            CarClass::Hyper => Self::hyper_baseline(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct QssModel {
    pub params: VehicleParams,
    pub terms: Terms,
}

impl QssModel {
    pub const fn new(params: VehicleParams) -> Self {
        Self { params, terms: Terms::ALL }
    }

    pub const fn with_terms(params: VehicleParams, terms: Terms) -> Self {
        Self { params, terms }
    }

    pub fn lltd(&self, setup: &MechanicalSetup) -> f64 {
        let p = &self.params;
        let k_spring = Axle::new(
            lerp(p.spring_roll_range.front, setup.spring.front),
            lerp(p.spring_roll_range.rear, setup.spring.rear),
        );
        let k_arb = Axle::new(
            lerp(p.arb_roll_range.front, setup.arb.front),
            lerp(p.arb_roll_range.rear, setup.arb.rear),
        );
        k_spring.zip_with(k_arb, |s, a| s + a).front_share()
    }

    // Clamp load transfer to [-fz_axle/2, +fz_axle/2] so fz_in + fz_out == fz_axle.
    fn axle_max_fy(&self, fz_axle: f64, delta_fz: f64) -> f64 {
        let half = 0.5 * fz_axle.max(0.0);
        let d = delta_fz.clamp(-half, half);
        self.tyre_fy(half - d) + self.tyre_fy(half + d)
    }

    #[inline]
    fn tyre_fy(&self, fz: f64) -> f64 {
        if fz <= 0.0 {
            return 0.0;
        }
        let p = &self.params;
        if !self.terms.load_sensitivity {
            return p.mu_nominal * fz;
        }
        let fz_ref = p.fz_ref.raw().max(1.0);
        let mu = (p.mu_nominal * (1.0 - p.load_sensitivity * (fz - fz_ref) / fz_ref)).max(0.2);
        mu * fz
    }

    // Longitudinal force per axle (N) for the measured a_lon.
    fn axle_fx(&self, setup: &MechanicalSetup, a_lon: Gs) -> Axle<f64> {
        let p = &self.params;
        let fx = p.mass_kg * a_lon.raw().abs() * GRAVITY;
        let front = if a_lon.raw() < 0.0 {
            lerp(p.brake_bias_range, setup.brake_bias)
        } else {
            p.drive_front_share
        };
        Axle::new(fx * front, fx * (1.0 - front))
    }

    fn diff_understeer(&self, setup: &MechanicalSetup, phase: CornerPhase) -> f64 {
        if !self.terms.diff {
            return 0.0;
        }
        let g = self.params.diff_gain_deg;
        match phase {
            CornerPhase::Braking => g * 0.4 * (setup.diff_coast - 0.5),
            CornerPhase::Entry => g * (0.7 * (setup.diff_coast - 0.5) + 0.5 * (setup.diff_preload - 0.5)),
            CornerPhase::Mid => g * 0.8 * (setup.diff_preload - 0.5),
            CornerPhase::Exit => g * (0.6 * (setup.diff_power - 0.5) + 0.2 * (setup.diff_preload - 0.5)),
        }
    }
}

impl MechanicalModel for QssModel {
    fn evaluate(&self, setup: &MechanicalSetup, state: CornerState) -> CornerPrediction {
        let p = &self.params;
        let v = state.speed.raw().max(0.0);
        let weight = p.mass_kg * GRAVITY;

        let wing_factor = 1.0 + p.aero_wing_span * (setup.wing - 0.5);
        let downforce = p.aero_cl_a * wing_factor * v * v;
        let aero_front = if self.terms.aero_balance {
            let rake = setup.ride_height.rear - setup.ride_height.front;
            (p.aero_front_base + p.aero_rake_gain * rake + p.aero_wing_balance_gain * (setup.wing - 0.5))
                .clamp(0.25, 0.70)
        } else {
            p.aero_front_base
        };

        // + a_lon (accelerating) moves load rearward.
        let long_transfer = if self.terms.longitudinal_transfer {
            weight * state.a_lon.raw() * p.cg_height_m / p.wheelbase_m.max(0.5)
        } else {
            0.0
        };
        let fz_front = (weight * p.static_front_weight + downforce * aero_front - long_transfer).max(0.0);
        let fz_rear = (weight * (1.0 - p.static_front_weight) + downforce * (1.0 - aero_front) + long_transfer).max(0.0);
        let vertical_front = fz_front / (fz_front + fz_rear).max(1e-6);
        let lltd_front = self.lltd(setup);
        // Straight-line braking precedes turn-in, so it spends no lateral grip.
        let combined = self.terms.friction_circle && state.phase != CornerPhase::Braking;
        let fx = if combined { self.axle_fx(setup, state.a_lon) } else { Axle::new(0.0, 0.0) };

        // Longitudinal use of each axle is capped below its capacity: the driver
        // stayed on the road, so a model that underestimates grip must not
        // collapse lateral capacity to zero.
        let lateral_left = |cap: f64, fx: f64| -> f64 {
            let used = if cap > 0.0 { (fx / cap).min(MAX_LONG_UTILISATION) } else { 0.0 };
            cap * (1.0 - used * used).sqrt()
        };

        // Steady-state yaw equilibrium: Fy_f = w_f * m * a_y, Fy_r = (1 - w_f) * m * a_y.
        let mut ay_g = 1.2;
        let mut cap_f = 1.2;
        let mut cap_r = 1.2;
        for _ in 0..8 {
            let total_lat_transfer = p.mass_kg * (ay_g * GRAVITY) * p.cg_height_m;
            let dfz_f = (total_lat_transfer * lltd_front) / p.track_width.front;
            let dfz_r = (total_lat_transfer * (1.0 - lltd_front)) / p.track_width.rear;

            cap_f = lateral_left(self.axle_max_fy(fz_front, dfz_f), fx.front) / (weight * p.static_front_weight);
            cap_r = lateral_left(self.axle_max_fy(fz_rear, dfz_r), fx.rear) / (weight * (1.0 - p.static_front_weight));
            ay_g = 0.5 * ay_g + 0.5 * cap_f.min(cap_r);
        }

        let imbalance = (cap_r - cap_f) / (cap_r + cap_f).max(1e-6);
        let understeer =
            Degrees::new(p.understeer_gain_deg * imbalance + self.diff_understeer(setup, state.phase));

        CornerPrediction {
            max_lat_g: Gs::new(ay_g),
            understeer,
            lltd_front,
            vertical_front,
        }
    }
}

#[inline]
pub(crate) fn lerp((lo, hi): (f64, f64), t: f64) -> f64 {
    lo + (hi - lo) * t.clamp(0.0, 1.0)
}
