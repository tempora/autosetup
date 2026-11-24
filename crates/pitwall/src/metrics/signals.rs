/* Whole-recording signals on one uniform 50 Hz grid, plus steering geometry.
   Units: t s, dist m, speed m/s, steer -1..1 of full lock (+ right), pedals
   0..1, yaw rate rad/s signed like the steering (+ turning right), a_lat and
   a_lon in g (+ right, + accelerating), ride heights m, tyre temps °C.

   Measured behaviour of LMU channels this module relies on:
   `G Force Lat/Long` (10 Hz) are vibration-dominated, so accelerations are
   derived: longitudinal from 100 Hz `Ground Speed`, lateral from speed × yaw
   rate, yaw rate from the GPS heading.  GPS yaw is sign-aligned to the
   steering and shifted back by its best-correlating lag (≤ 0.4 s).
   `Steering Pos` is -1..1 in older files and -100..100 (unit '%') since
   October 2026.  `TyresTempLeft/Right` are car-frame sides: the inner edge is
   Right on the left-hand wheels and Left on the right-hand ones.  Brake
   temperatures are °C; third-spring deflection (m) is optional because only
   cars with a third element log it.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{Axle, CarClass, Wheel};
use crate::metrics::laps::{clock_span, lap_distance};
use crate::metrics::stats::{correlation, gradient, interp, moving_average};
use crate::setup::CarSetup;
use crate::telemetry::{TelemetryError, TelemetrySource};
use crate::units::{Degrees, Meters, Seconds, GRAVITY};

pub const GRID_HZ: f64 = 50.0;
pub const DT: f64 = 1.0 / GRID_HZ;
const EARTH_RADIUS_M: f64 = 6_371_000.0;
const YAW_MAX_LAG_S: f64 = 0.4;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SteeringGeometry {
    // Steering-wheel angle at full lock one way.
    pub half_lock: Degrees,
    // Steering-wheel angle / road-wheel angle.
    pub ratio: f64,
    pub wheelbase: Meters,
}

impl SteeringGeometry {
    /* VM_STEER_LOCK labels give lock-to-lock wheel degrees and one-way road-wheel
       lock in brackets: '560 (16.5) deg', '516 deg(19.7 )', '527 (20) deg'.
       ratio = (lock_total / 2) / road_wheel_lock.  */
    pub fn from_setup(setup: &CarSetup, class: CarClass) -> Self {
        let text = setup.params.get("VM_STEER_LOCK").map(|p| p.text.as_str()).unwrap_or("");
        let lock_total = leading_number(text).filter(|&v| v > 0.0).unwrap_or(540.0);
        let ratio = bracketed_number(text)
            .filter(|&w| w > 0.0)
            .map_or(15.0, |wheel_lock| (lock_total / 2.0) / wheel_lock);
        Self {
            half_lock: Degrees::new(lock_total / 2.0),
            ratio,
            wheelbase: class.wheelbase(),
        }
    }

    // Road-wheel angle (rad) for a normalised steering position.
    #[inline]
    pub fn road_wheel_rad(&self, steer: f64) -> f64 {
        (steer * self.half_lock.raw() / self.ratio).to_radians()
    }
}

fn leading_number(s: &str) -> Option<f64> {
    let start = s.find(|c: char| c.is_ascii_digit())?;
    let rest = &s[start..];
    let end = rest.find(|c: char| !(c.is_ascii_digit() || c == '.')).unwrap_or(rest.len());
    rest[..end].parse().ok()
}

fn bracketed_number(s: &str) -> Option<f64> {
    let open = s.find('(')?;
    let close = open + s[open..].find(')')?;
    leading_number(&s[open + 1..close])
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct TreadTemps {
    pub inner: f64,
    pub centre: f64,
    pub outer: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Signals {
    pub t: Vec<f64>,
    pub dist: Vec<f64>,
    pub speed: Vec<f64>,
    pub steer: Vec<f64>,
    pub throttle: Vec<f64>,
    pub brake: Vec<f64>,
    pub yaw_rate: Vec<f64>,
    pub a_lat: Vec<f64>,
    pub a_lon: Vec<f64>,
    pub ride_height: Vec<Wheel<f64>>,
    pub tc: Vec<bool>,
    pub abs: Vec<bool>,
    pub tyre_temp: Vec<Wheel<TreadTemps>>,
    // Brake disc temperature, °C.
    pub brake_temp: Vec<Wheel<f64>>,
    // Front / rear third-spring deflection, m; None where the car logs none.
    pub third: Option<Vec<Axle<f64>>>,
}

impl Signals {
    #[inline]
    pub fn len(&self) -> usize {
        self.t.len()
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.t.is_empty()
    }

    // Grid index range [lo, hi) of samples with t in [t0, t1).
    pub fn range(&self, t0: Seconds, t1: Seconds) -> std::ops::Range<usize> {
        let lo = self.t.partition_point(|&t| t < t0.raw());
        let hi = self.t.partition_point(|&t| t < t1.raw());
        lo..hi
    }

    pub fn load<S: TelemetrySource + ?Sized>(src: &S) -> Result<Self, TelemetryError> {
        let (t0, t1) = clock_span(src)?;
        let n = ((t1 - t0).raw() * GRID_HZ).floor().max(0.0) as usize;
        let t: Vec<f64> = (0..n).map(|i| t0.raw() + i as f64 * DT).collect();
        let grid: Vec<Seconds> = t.iter().copied().map(Seconds::new).collect();

        let linear = |name: &str| -> Result<Vec<f64>, TelemetryError> {
            Ok(src.scalar_channel(name)?.sample_linear(&grid))
        };
        let wheel = |name: &str| -> Result<Vec<Wheel<f64>>, TelemetryError> {
            Ok(src.wheel_channel(name)?.sample_linear(&grid))
        };
        let flag = |name: &str| -> Result<Vec<bool>, TelemetryError> {
            Ok(src.scalar_channel(name)?.sample_zoh(&grid).into_iter().map(|v| v > 0.5).collect())
        };

        let speed: Vec<f64> = linear("Ground Speed")?.into_iter().map(|v| v / 3.6).collect();
        let raw_steer = linear("Steering Pos")?;
        let percent = src.channel_unit("Steering Pos") == Some("%")
            || raw_steer.iter().any(|v| v.abs() > 1.5);
        let steer: Vec<f64> = raw_steer
            .into_iter()
            .map(|v| if percent { v / 100.0 } else { v })
            .collect();
        let yaw_rate = yaw_rate(src, &t, &speed, &steer)?;
        let a_lat = speed.iter().zip(&yaw_rate).map(|(v, r)| v * r / GRAVITY).collect();
        let a_lon = gradient(&moving_average(&speed, 5), &t)
            .into_iter()
            .map(|a| a / GRAVITY)
            .collect();

        let [left, centre, right] =
            ["TyresTempLeft", "TyresTempCentre", "TyresTempRight"].map(&wheel);
        let (left, centre, right) = (left?, centre?, right?);
        let tyre_temp = left
            .iter()
            .zip(&centre)
            .zip(&right)
            .map(|((l, c), r)| Wheel {
                fl: TreadTemps { inner: r.fl, centre: c.fl, outer: l.fl },
                fr: TreadTemps { inner: l.fr, centre: c.fr, outer: r.fr },
                rl: TreadTemps { inner: r.rl, centre: c.rl, outer: l.rl },
                rr: TreadTemps { inner: l.rr, centre: c.rr, outer: r.rr },
            })
            .collect();

        let third = match (src.channel_kind("Front3rdDeflection"), src.channel_kind("Rear3rdDeflection")) {
            (Some(_), Some(_)) => {
                let (f, r) = (linear("Front3rdDeflection")?, linear("Rear3rdDeflection")?);
                Some(f.into_iter().zip(r).map(|(f, r)| Axle::new(f, r)).collect())
            }
            _ => None,
        };

        Ok(Self {
            dist: lap_distance(src, &t)?,
            brake_temp: wheel("Brakes Temp")?,
            third,
            throttle: linear("Throttle Pos")?.into_iter().map(|v| v / 100.0).collect(),
            brake: linear("Brake Pos")?.into_iter().map(|v| v / 100.0).collect(),
            ride_height: wheel("RideHeights")?,
            tc: flag("TC")?,
            abs: flag("ABS")?,
            tyre_temp,
            t,
            speed,
            steer,
            yaw_rate,
            a_lat,
            a_lon,
        })
    }
}

// Yaw rate from the GPS track heading, signed and lag-aligned to the steering.
fn yaw_rate<S: TelemetrySource + ?Sized>(
    src: &S,
    t: &[f64],
    speed: &[f64],
    steer: &[f64],
) -> Result<Vec<f64>, TelemetryError> {
    let lat = src.scalar_channel("GPS Latitude")?;
    let lon = src.scalar_channel("GPS Longitude")?;
    let n = lat.len().min(lon.len());
    if n < 5 {
        return Ok(vec![0.0; t.len()]);
    }
    let tg: Vec<f64> = lat.times[..n].iter().map(|s| s.raw()).collect();
    let mean_lat = lat.values[..n].iter().sum::<f64>() / n as f64;
    let x: Vec<f64> = lon.values[..n]
        .iter()
        .map(|v| v.to_radians() * EARTH_RADIUS_M * mean_lat.to_radians().cos())
        .collect();
    let y: Vec<f64> = lat.values[..n].iter().map(|v| v.to_radians() * EARTH_RADIUS_M).collect();
    let vx = gradient(&moving_average(&x, 3), &tg);
    let vy = gradient(&moving_average(&y, 3), &tg);
    let heading = unwrap_angle(vx.iter().zip(&vy).map(|(vx, vy)| vy.atan2(*vx)));
    let yaw_native: Vec<f64> = moving_average(&gradient(&heading, &tg), 3)
        .into_iter()
        .zip(vx.iter().zip(&vy))
        .map(|(r, (vx, vy))| if vx.hypot(*vy) < 5.0 { 0.0 } else { r })
        .collect();
    let mut yaw = interp(t, &tg, &yaw_native, None);

    let moving: Vec<usize> = (0..t.len()).filter(|&i| speed[i] > 15.0).collect();
    if moving.len() < 50 {
        return Ok(yaw);
    }
    let steer_m: Vec<f64> = moving.iter().map(|&i| steer[i]).collect();
    let yaw_m: Vec<f64> = moving.iter().map(|&i| yaw[i]).collect();
    // GPS heading is counter-clockwise positive; flip onto the steering convention.
    let flip = correlation(&steer_m, &yaw_m) < 0.0;
    if flip {
        yaw.iter_mut().for_each(|r| *r = -*r);
    }
    let yaw_m: Vec<f64> = if flip { yaw_m.into_iter().map(|r| -r).collect() } else { yaw_m };

    let max_lag = (YAW_MAX_LAG_S * GRID_HZ) as usize;
    let best_lag = (0..=max_lag)
        .step_by(2)
        .map(|lag| {
            let m = steer_m.len() - lag;
            (lag, correlation(&steer_m[..m], &yaw_m[lag..]))
        })
        .filter(|(_, c)| c.is_finite())
        .fold((0usize, -1.0f64), |best, cur| if cur.1 > best.1 { cur } else { best })
        .0;
    if best_lag > 0 {
        let last = *yaw.last().unwrap_or(&0.0);
        yaw.drain(..best_lag);
        yaw.extend(std::iter::repeat_n(last, best_lag));
    }
    Ok(yaw)
}

fn unwrap_angle(angles: impl Iterator<Item = f64>) -> Vec<f64> {
    use std::f64::consts::{PI, TAU};
    let mut out: Vec<f64> = Vec::new();
    for a in angles {
        let v = match out.last() {
            Some(&prev) => {
                let d = (a - prev + PI).rem_euclid(TAU) - PI;
                prev + d
            }
            None => a,
        };
        out.push(v);
    }
    out
}
