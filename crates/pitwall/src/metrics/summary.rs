/* Per-stint summary numbers the deterministic rules act on: the mean over
   flying laps (complete, outside the pits) of per-lap values.
     tyres     tread crown (centre − mean of edges) and spread (inner − outer)
               per wheel, °C, over samples above MOVING_MS
     brakes    peak disc temperature per wheel, °C
     platform  1st-percentile ride height per wheel, mm; time per lap with a
               front (or, apart, a rear) corner under BOTTOMING_MM; share of the
               lap the third spring sits on its packers, per axle
     aids      TC-active seconds and TC events per lap; countersteer moments
               on exit (on power, ≥ MIN_RUN_S) per lap
   Packer contact is the knee of median third deflection against speed²
   over the whole recording (two lines joined at a knee, least squares): past
   contact the deflection gains under PACKER_FLAT of the free rate.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{Axle, Wheel};
use crate::metrics::balance::SampleBalance;
use crate::metrics::laps::Lap;
use crate::metrics::signals::{Signals, DT, GRID_HZ};
use crate::metrics::stats::{mean, median, quantile, runs};
use crate::setup::CarSetup;

const MOVING_MS: f64 = 20.0;
const MIN_SPEED: f64 = 15.0;
const BOTTOMING_MM: f64 = 1.0;
const MIN_RUN_S: f64 = 0.2;
const PACKER_FLAT: f64 = 0.25;
const PACKER_BIN_KMH: f64 = 20.0;

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct StintSummary {
    pub flying_laps: usize,
    pub t_crown: Option<Wheel<f64>>,
    pub t_spread: Option<Wheel<f64>>,
    pub brake_temp_max: Option<Wheel<f64>>,
    pub rh_min_mm: Option<Wheel<f64>>,
    pub bottoming_s: Option<f64>,
    pub bottoming_rear_s: Option<f64>,
    // None on an axle without an adjustable third element; Some(0) if it never reaches its packers.
    pub third_packers: Axle<Option<f64>>,
    pub tc_s: Option<f64>,
    pub tc_events: Option<f64>,
    pub cs_exit: Option<f64>,
}

#[derive(Debug, Clone, Copy)]
struct LapNumbers {
    t_crown: Option<Wheel<f64>>,
    t_spread: Option<Wheel<f64>>,
    brake_temp_max: Wheel<f64>,
    rh_min_mm: Wheel<f64>,
    bottoming_s: f64,
    bottoming_rear_s: f64,
    third_packers: Axle<Option<f64>>,
    tc_s: f64,
    tc_events: f64,
    cs_exit: f64,
}

fn wheel_of<F: Fn(&Wheel<f64>) -> f64>(rows: &[Wheel<f64>], f: F) -> Vec<f64> {
    rows.iter().map(f).collect()
}

fn per_wheel(rows: &[Wheel<f64>], stat: impl Fn(&[f64]) -> Option<f64>) -> Option<Wheel<f64>> {
    Some(Wheel::new(
        stat(&wheel_of(rows, |w| w.fl))?,
        stat(&wheel_of(rows, |w| w.fr))?,
        stat(&wheel_of(rows, |w| w.rl))?,
        stat(&wheel_of(rows, |w| w.rr))?,
    ))
}

fn has_third(setup: &CarSetup, axle: &str) -> bool {
    let prefix = format!("VM_{axle}_3RD_");
    setup.params.iter().any(|(k, p)| k.starts_with(&prefix) && p.adjustable())
}

// Third deflection (mm) where the packers start, or None if never reached.
fn packer_contact(x_mm: &[f64], kmh: &[f64]) -> Option<f64> {
    let top = kmh.iter().copied().filter(|v| v.is_finite()).fold(0.0, f64::max);
    let (centres, meds): (Vec<f64>, Vec<f64>) = (0..)
        .map(|k| 40.0 + k as f64 * PACKER_BIN_KMH)
        .take_while(|&lo| lo < top)
        .filter_map(|lo| {
            let sel: Vec<usize> = (0..kmh.len()).filter(|&i| kmh[i] >= lo && kmh[i] < lo + PACKER_BIN_KMH).collect();
            (sel.len() as f64 >= 0.3 * GRID_HZ).then(|| {
                let c = sel.iter().map(|&i| kmh[i]).sum::<f64>() / sel.len() as f64;
                let m = median(&sel.iter().map(|&i| x_mm[i]).collect::<Vec<_>>()).unwrap_or(f64::NAN);
                (c, m)
            })
        })
        .unzip();
    if meds.len() < 5 {
        return None;
    }
    let load: Vec<f64> = centres.iter().map(|c| (c / 100.0).powi(2)).collect();
    let n = load.len();
    let (k_lo, k_hi) = (load[1], load[n - 3]);
    let best = (0..200)
        .filter_map(|j| {
            let k = k_lo + (k_hi - k_lo) * j as f64 / 199.0;
            let rows: Vec<[f64; 3]> = load.iter().map(|&l| [1.0, l.min(k), (l - k).max(0.0)]).collect();
            let coef = least_squares3(&rows, &meds)?;
            let sse: f64 = rows
                .iter()
                .zip(&meds)
                .map(|(r, y)| (r[0] * coef[0] + r[1] * coef[1] + r[2] * coef[2] - y).powi(2))
                .sum();
            Some((sse, k, coef))
        })
        .min_by(|a, b| a.0.total_cmp(&b.0))?;
    let (_, k, [a0, free, packed]) = best;
    (free > 0.0 && packed < PACKER_FLAT * free).then_some(a0 + free * k)
}

// Least squares for three columns via the normal equations (Cramer's rule).
fn least_squares3(rows: &[[f64; 3]], y: &[f64]) -> Option<[f64; 3]> {
    let mut a = [[0.0; 3]; 3];
    let mut b = [0.0; 3];
    for (r, &yi) in rows.iter().zip(y) {
        for i in 0..3 {
            b[i] += r[i] * yi;
            for j in 0..3 {
                a[i][j] += r[i] * r[j];
            }
        }
    }
    let det = |m: &[[f64; 3]; 3]| {
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    };
    let d = det(&a);
    if d.abs() < 1e-12 {
        return None;
    }
    Some(std::array::from_fn(|c| {
        let mut m = a;
        for i in 0..3 {
            m[i][c] = b[i];
        }
        det(&m) / d
    }))
}

fn lap_numbers(sig: &Signals, per: &SampleBalance, range: std::ops::Range<usize>, contact: Axle<Option<Option<f64>>>) -> Option<LapNumbers> {
    if range.len() < GRID_HZ as usize {
        return None;
    }
    let moving: Vec<usize> = range.clone().filter(|&i| sig.speed[i] > MOVING_MS).collect();
    let treads = (moving.len() >= GRID_HZ as usize).then(|| {
        let side = |f: fn(&crate::metrics::signals::TreadTemps) -> f64| -> Vec<Wheel<f64>> {
            moving.iter().map(|&i| sig.tyre_temp[i].map(|t| f(&t))).collect()
        };
        let (inner, centre, outer) = (side(|t| t.inner), side(|t| t.centre), side(|t| t.outer));
        let (i, c, o) = (per_wheel(&inner, mean), per_wheel(&centre, mean), per_wheel(&outer, mean));
        match (i, c, o) {
            (Some(i), Some(c), Some(o)) => (
                Some(c.zip_with(i.zip_with(o, |a, b| (a, b)), |c, (i, o)| c - 0.5 * (i + o))),
                Some(i.zip_with(o, |i, o| i - o)),
            ),
            _ => (None, None),
        }
    });
    let rh_mm: Vec<Wheel<f64>> = range.clone().map(|i| sig.ride_height[i].map(|v| v * 1e3)).collect();
    let bottom = |front: bool| {
        rh_mm
            .iter()
            .filter(|w| if front { w.fl < BOTTOMING_MM || w.fr < BOTTOMING_MM } else { w.rl < BOTTOMING_MM || w.rr < BOTTOMING_MM })
            .count() as f64
            * DT
    };
    let tc: Vec<bool> = range.clone().map(|i| sig.tc[i]).collect();
    let on_power_counter: Vec<bool> = range
        .clone()
        .map(|i| per.countersteer[i] && sig.speed[i] > MIN_SPEED && sig.brake[i] <= 0.05 && sig.throttle[i] >= 0.5)
        .collect();
    let min_run = (MIN_RUN_S * GRID_HZ) as usize;
    let third_packers = Axle::new(0usize, 1usize).zip_with(contact, |axle, c| {
        let c = c?;
        let Some(c) = c else { return Some(0.0) };
        let x: Vec<f64> = sig.third.as_ref()?.get(range.clone())?.iter().map(|a| if axle == 0 { a.front } else { a.rear } * 1e3).collect();
        Some(x.iter().filter(|&&v| v >= c).count() as f64 / x.len().max(1) as f64)
    });
    Some(LapNumbers {
        t_crown: treads.and_then(|t| t.0),
        t_spread: treads.and_then(|t| t.1),
        brake_temp_max: per_wheel(&sig.brake_temp[range.clone()], |xs| xs.iter().copied().filter(|v| v.is_finite()).reduce(f64::max))?,
        rh_min_mm: per_wheel(&rh_mm, |xs| quantile(xs, 0.01))?,
        bottoming_s: bottom(true),
        bottoming_rear_s: bottom(false),
        third_packers,
        tc_s: tc.iter().filter(|&&b| b).count() as f64 * DT,
        tc_events: tc.windows(2).filter(|w| !w[0] && w[1]).count() as f64,
        cs_exit: runs(&on_power_counter).into_iter().filter(|(a, b)| b - a + 1 >= min_run).count() as f64,
    })
}

pub fn summarise(sig: &Signals, per: &SampleBalance, laps: &[Lap], setup: &CarSetup) -> StintSummary {
    let kmh: Vec<f64> = sig.speed.iter().map(|v| v * 3.6).collect();
    let contact = Axle::new("FRONT", "REAR").zip_with(Axle::new(0usize, 1usize), |name, axle| {
        if !has_third(setup, name) {
            return None;
        }
        let third = sig.third.as_ref()?;
        let x: Vec<f64> = third.iter().map(|a| if axle == 0 { a.front } else { a.rear } * 1e3).collect();
        let span = x.iter().copied().filter(|v| v.is_finite()).fold((f64::INFINITY, f64::NEG_INFINITY), |(lo, hi), v| (lo.min(v), hi.max(v)));
        if span.1 - span.0 < 1.0 {
            return None;
        }
        Some(packer_contact(&x, &kmh))
    });
    let rows: Vec<LapNumbers> = laps
        .iter()
        .filter(|l| l.is_flying())
        .filter_map(|l| lap_numbers(sig, per, sig.range(l.start, l.end), contact))
        .collect();
    let avg = |f: &dyn Fn(&LapNumbers) -> Option<f64>| -> Option<f64> {
        let v: Vec<f64> = rows.iter().filter_map(f).collect();
        mean(&v)
    };
    let avg_wheel = |f: &dyn Fn(&LapNumbers) -> Option<Wheel<f64>>| -> Option<Wheel<f64>> {
        let v: Vec<Wheel<f64>> = rows.iter().filter_map(f).collect();
        per_wheel(&v, mean)
    };
    StintSummary {
        flying_laps: rows.len(),
        t_crown: avg_wheel(&|r| r.t_crown),
        t_spread: avg_wheel(&|r| r.t_spread),
        brake_temp_max: avg_wheel(&|r| Some(r.brake_temp_max)),
        rh_min_mm: avg_wheel(&|r| Some(r.rh_min_mm)),
        bottoming_s: avg(&|r| Some(r.bottoming_s)),
        bottoming_rear_s: avg(&|r| Some(r.bottoming_rear_s)),
        third_packers: Axle::new(avg(&|r| r.third_packers.front), avg(&|r| r.third_packers.rear)),
        tc_s: avg(&|r| Some(r.tc_s)),
        tc_events: avg(&|r| Some(r.tc_events)),
        cs_exit: avg(&|r| Some(r.cs_exit)),
    }
}
