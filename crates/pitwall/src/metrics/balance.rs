/* Balance per corner pass and its per-stint aggregation by BalanceCell.
   The kinematic reference is the yaw rate a neutral car would have at this
   speed and road-wheel angle, r_ref = v·δ/L.  The understeer angle is

       ua = s · (δ − L·r/v)        (road-wheel deg, + understeer)

   where s is the sign of the yaw rate (the way the car is turning), falling
   back to the steering sign when barely turning.  Signing by yaw keeps a
   countersteer correction reading as oversteer.

   Each pass yields one PhaseObs per phase with at least MIN_PHASE_WEIGHT
   samples of weight, carrying the variance of its median.  Cells pool passes
   with a DerSimonian–Laird random-effects mean (τ² ≥ TAU2_MIN), so one long
   pass cannot outweigh many short ones.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CornerPhase};
use crate::metrics::corners::Pass;
use crate::metrics::signals::{Signals, SteeringGeometry, DT, GRID_HZ};
use crate::metrics::stats::{mean, median, sign, weighted_mean, weighted_quantile};
use crate::units::{Degrees, Gs, MetersPerSec, Seconds};

const MIN_PHASE_WEIGHT: f64 = GRID_HZ / 5.0;
const MIN_REF_YAW: f64 = 0.05;
const NOISE_FLOOR: f64 = 0.02 * 0.02;
const CORR_S: f64 = 0.2;
const TAU2_MIN: f64 = 0.1 * 0.1;
const COUNTER_MIN_SW_DEG: f64 = 5.0;
const COUNTER_MIN_YAW: f64 = 0.1;

#[derive(Debug, Clone)]
pub struct SampleBalance {
    // Understeer angle, deg.
    pub ua: Vec<f64>,
    // Neutral-car yaw rate, rad/s.
    pub r_ref: Vec<f64>,
    pub countersteer: Vec<bool>,
}

impl SampleBalance {
    pub fn compute(sig: &Signals, geom: &SteeringGeometry) -> Self {
        let l = geom.wheelbase.raw();
        let (ua, r_ref, countersteer) = (0..sig.len())
            .map(|i| {
                let delta = geom.road_wheel_rad(sig.steer[i]);
                let r = sig.yaw_rate[i];
                let v = sig.speed[i].max(1.0);
                let s = if r.abs() > MIN_REF_YAW / 2.0 { sign(r) } else { sign(delta) };
                let sw_deg = sig.steer[i] * geom.half_lock.raw();
                let counter = sign(sig.steer[i]) != sign(r)
                    && sw_deg.abs() > COUNTER_MIN_SW_DEG
                    && r.abs() > COUNTER_MIN_YAW;
                ((s * (delta - l * r / v)).to_degrees(), v * delta / l, counter)
            })
            .fold((Vec::new(), Vec::new(), Vec::new()), |(mut a, mut b, mut c), (x, y, z)| {
                a.push(x);
                b.push(y);
                c.push(z);
                (a, b, c)
            });
        Self { ua, r_ref, countersteer }
    }

    // r_ref − r (rad/s) per sample.
    pub fn yaw_error(&self, sig: &Signals) -> Vec<f64> {
        self.r_ref.iter().zip(&sig.yaw_rate).map(|(a, b)| a - b).collect()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PhaseObs {
    pub lap: usize,
    pub corner: usize,
    pub cell: BalanceCell,
    // Phase weight in samples at GRID_HZ.
    pub weight: f64,
    pub v_min: MetersPerSec,
    pub v_mean: MetersPerSec,
    pub a_lat_mean: Gs,
    pub a_lat_p95: Gs,
    pub a_lon: Gs,
    pub ua: Degrees,
    // Variance of the ua median, deg².
    pub ua_var: f64,
    pub countersteer_s: Seconds,
    pub tc_s: Seconds,
    pub kerb: bool,
}

// Variance of a weighted median: (1.4826·MAD)² · π/2 / n_eff, n_eff one per CORR_S.
fn median_var(x: &[f64], w: &[f64]) -> f64 {
    let Some(med) = weighted_quantile(x, w, 0.5) else { return f64::NAN };
    let dev: Vec<f64> = x.iter().map(|v| (v - med).abs()).collect();
    let mad = weighted_quantile(&dev, w, 0.5).unwrap_or(0.0) * 1.4826;
    let n_eff = (w.iter().sum::<f64>() / (CORR_S * GRID_HZ)).max(1.0);
    (mad * mad * std::f64::consts::FRAC_PI_2 / n_eff).max(NOISE_FLOOR)
}

pub fn observe(
    sig: &Signals,
    per: &SampleBalance,
    pass: &Pass,
    lap: usize,
    kerb_grid: &[bool],
) -> Vec<PhaseObs> {
    CornerPhase::ALL
        .into_iter()
        .filter_map(|phase| {
            let (sel, w): (Vec<usize>, Vec<f64>) = pass
                .idx
                .clone()
                .zip(pass.weight(phase))
                .filter(|&(i, &wt)| wt > 0.0 && per.ua[i].is_finite())
                .map(|(i, &wt)| (i, wt))
                .unzip();
            let total: f64 = w.iter().sum();
            if total < MIN_PHASE_WEIGHT {
                return None;
            }
            let pick = |xs: &[f64]| -> Vec<f64> { sel.iter().map(|&i| xs[i]).collect() };
            let ua = pick(&per.ua);
            let abs_lat: Vec<f64> = sel.iter().map(|&i| sig.a_lat[i].abs()).collect();
            let weighted_secs = |flag: &dyn Fn(usize) -> bool| {
                Seconds::new(sel.iter().zip(&w).filter(|(&i, _)| flag(i)).map(|(_, wt)| wt).sum::<f64>() * DT)
            };
            Some(PhaseObs {
                lap,
                corner: pass.corner,
                cell: BalanceCell { phase, band: pass.band },
                weight: total,
                v_min: pass.v_min,
                v_mean: MetersPerSec::new(weighted_mean(&pick(&sig.speed), &w)?),
                a_lat_mean: Gs::new(weighted_mean(&abs_lat, &w)?),
                a_lat_p95: Gs::new(weighted_quantile(&abs_lat, &w, 0.95)?),
                a_lon: Gs::new(weighted_mean(&pick(&sig.a_lon), &w)?),
                ua: Degrees::new(weighted_quantile(&ua, &w, 0.5)?),
                ua_var: median_var(&ua, &w),
                countersteer_s: weighted_secs(&|i| per.countersteer[i]),
                tc_s: weighted_secs(&|i| sig.tc[i]),
                kerb: sel.iter().any(|&i| kerb_grid.get(i).copied().unwrap_or(false)),
            })
        })
        .collect()
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CellStats {
    pub cell: BalanceCell,
    pub passes: usize,
    pub ua: Degrees,
    pub ua_se: Degrees,
    // Median over passes of each pass's p95 |a_lat|.
    pub a_lat_p95: Gs,
    pub v_min: MetersPerSec,
    pub a_lon: Gs,
    // Per-pass means.
    pub countersteer_s: Seconds,
    pub tc_s: Seconds,
    // Total time the stint spent in this cell (phase weight × sample period).
    pub time_s: Seconds,
}

// Random-effects pooled mean and standard error of (value, variance) pairs.
pub fn pooled_mean(y: &[f64], v: &[f64]) -> Option<(f64, f64)> {
    if y.is_empty() {
        return None;
    }
    let w: Vec<f64> = v.iter().map(|v| 1.0 / v).collect();
    let sw: f64 = w.iter().sum();
    let fixed = y.iter().zip(&w).map(|(y, w)| y * w).sum::<f64>() / sw;
    let tau2 = if y.len() > 1 {
        let q: f64 = y.iter().zip(&w).map(|(y, w)| w * (y - fixed).powi(2)).sum();
        let c = sw - w.iter().map(|w| w * w).sum::<f64>() / sw;
        if c > 0.0 { ((q - (y.len() - 1) as f64) / c).max(0.0) } else { 0.0 }
    } else {
        0.0
    }
    .max(TAU2_MIN);
    let w2: Vec<f64> = v.iter().map(|v| 1.0 / (v + tau2)).collect();
    let sw2: f64 = w2.iter().sum();
    let m = y.iter().zip(&w2).map(|(y, w)| y * w).sum::<f64>() / sw2;
    Some((m, (1.0 / sw2).sqrt()))
}

pub fn aggregate<'a>(obs: impl IntoIterator<Item = &'a PhaseObs> + Clone) -> Vec<CellStats> {
    BalanceCell::all()
        .filter_map(|cell| {
            let sel: Vec<&PhaseObs> = obs.clone().into_iter().filter(|o| o.cell == cell).collect();
            let col = |f: &dyn Fn(&PhaseObs) -> f64| -> Vec<f64> { sel.iter().map(|o| f(o)).collect() };
            let (ua, se) = pooled_mean(&col(&|o| o.ua.raw()), &col(&|o| o.ua_var))?;
            Some(CellStats {
                cell,
                passes: sel.len(),
                ua: Degrees::new(ua),
                ua_se: Degrees::new(se),
                a_lat_p95: Gs::new(median(&col(&|o| o.a_lat_p95.raw()))?),
                v_min: MetersPerSec::new(median(&col(&|o| o.v_min.raw()))?),
                a_lon: Gs::new(weighted_mean(&col(&|o| o.a_lon.raw()), &col(&|o| o.weight))?),
                countersteer_s: Seconds::new(mean(&col(&|o| o.countersteer_s.raw()))?),
                tc_s: Seconds::new(mean(&col(&|o| o.tc_s.raw()))?),
                time_s: Seconds::new(col(&|o| o.weight).iter().sum::<f64>() * DT),
            })
        })
        .collect()
}
