/* Four-wheel quasi-steady-state (QSS) mechanical vehicle model.
   Evaluates steady-state limit lateral acceleration and understeer angle from
   normalized setup coordinates via lateral load transfer distribution (LLTD),
   nonlinear tyre load sensitivity (mu(Fz)), aero balance shift, and phase-
   dependent differential locking.  Sign conventions: positive understeer angle
   means understeer; positive rake means higher rear ride height.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{Axle, CornerPhase};
use crate::setup::MechanicalSetup;
use crate::units::{Degrees, Gs, MetersPerSec, Newtons, GRAVITY};

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CornerState {
    pub speed: MetersPerSec,
    pub phase: CornerPhase,
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

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct VehicleParams {
    pub mass_kg: f64,
    pub static_front_weight: f64,
    pub cg_height_m: f64,
    pub track_width: Axle<f64>,
    pub spring_roll_range: Axle<(f64, f64)>,
    pub arb_roll_range: Axle<(f64, f64)>,
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

impl VehicleParams {
    pub const fn gt3_baseline() -> Self {
        Self {
            mass_kg: 1320.0,
            static_front_weight: 0.48,
            cg_height_m: 0.44,
            track_width: Axle::new(1.66, 1.66),
            spring_roll_range: Axle::new((1.0, 2.2), (1.0, 2.2)),
            arb_roll_range: Axle::new((0.2, 2.0), (0.2, 2.0)),
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
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct QssModel {
    pub params: VehicleParams,
}

impl QssModel {
    pub const fn new(params: VehicleParams) -> Self {
        Self { params }
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
        let fz_ref = p.fz_ref.raw().max(1.0);
        let mu = (p.mu_nominal * (1.0 - p.load_sensitivity * (fz - fz_ref) / fz_ref)).max(0.2);
        mu * fz
    }
}

impl MechanicalModel for QssModel {
    fn evaluate(&self, setup: &MechanicalSetup, state: CornerState) -> CornerPrediction {
        let p = &self.params;
        let v = state.speed.raw().max(0.0);
        let weight = p.mass_kg * GRAVITY;

        let wing_factor = 1.0 + p.aero_wing_span * (setup.wing - 0.5);
        let downforce = p.aero_cl_a * wing_factor * v * v;
        let rake = setup.ride_height.rear - setup.ride_height.front;
        let aero_front = (p.aero_front_base
            + p.aero_rake_gain * rake
            + p.aero_wing_balance_gain * (setup.wing - 0.5))
            .clamp(0.25, 0.70);

        let fz_front = weight * p.static_front_weight + downforce * aero_front;
        let fz_rear = weight * (1.0 - p.static_front_weight) + downforce * (1.0 - aero_front);
        let vertical_front = fz_front / (fz_front + fz_rear);
        let lltd_front = self.lltd(setup);

        // Steady-state yaw equilibrium: Fy_f = w_f * m * a_y, Fy_r = (1 - w_f) * m * a_y.
        let mut ay_g = 1.2;
        let mut cap_f = 1.2;
        let mut cap_r = 1.2;
        for _ in 0..8 {
            let total_lat_transfer = p.mass_kg * (ay_g * GRAVITY) * p.cg_height_m;
            let dfz_f = (total_lat_transfer * lltd_front) / p.track_width.front;
            let dfz_r = (total_lat_transfer * (1.0 - lltd_front)) / p.track_width.rear;

            cap_f = self.axle_max_fy(fz_front, dfz_f) / (weight * p.static_front_weight);
            cap_r = self.axle_max_fy(fz_rear, dfz_r) / (weight * (1.0 - p.static_front_weight));
            ay_g = 0.5 * ay_g + 0.5 * cap_f.min(cap_r);
        }

        let diff_us = match state.phase {
            CornerPhase::Braking => {
                p.diff_gain_deg * (0.4 * (setup.diff_coast - 0.5) + 0.8 * (setup.brake_bias - 0.5))
            }
            CornerPhase::Entry => {
                p.diff_gain_deg * (0.7 * (setup.diff_coast - 0.5) + 0.5 * (setup.diff_preload - 0.5))
            }
            CornerPhase::Mid => p.diff_gain_deg * (0.8 * (setup.diff_preload - 0.5)),
            CornerPhase::Exit => {
                p.diff_gain_deg * (0.6 * (setup.diff_power - 0.5) + 0.2 * (setup.diff_preload - 0.5))
            }
        };

        let imbalance = (cap_r - cap_f) / (cap_r + cap_f).max(1e-6);
        let understeer = Degrees::new(p.understeer_gain_deg * imbalance + diff_us);

        CornerPrediction {
            max_lat_g: Gs::new(ay_g),
            understeer,
            lltd_front,
            vertical_front,
        }
    }
}

#[inline]
fn lerp((lo, hi): (f64, f64), t: f64) -> f64 {
    lo + (hi - lo) * t.clamp(0.0, 1.0)
}
