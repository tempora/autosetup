/* Suspension at its native rate: shaft velocity histograms and kerb events.
   `Susp Pos` (100 Hz, m) is corner-spring compression; shaft velocity is its
   quadratic Savitzky–Golay derivative over WINDOW_S, in mm/s (+ bump,
   − rebound).  Kerb samples are exactly those where `SurfaceTypes` (5 Hz,
   zero-order hold) reads KERB_SURFACE_CODE for that wheel; there is no
   spike-based fallback.  Only moving samples (> MIN_SPEED) of non-pit laps
   count.

   Histograms bin shaft velocity on a fixed BIN_MM_S grid over ±RANGE_MM_S
   (faster samples land in the end bins).  Per axle: symmetry = |bump share −
   ½|, slow share = share below the class damper knee; shape = L1 distance
   between front and rear normalised off-kerb histograms.

   Kerb events merge any-wheel contacts closer than MERGE_S.  Per event:
   peak_load (max spring compression / the wheel's usual off-kerb median),
   airtime_s (longest run under UNLOADED of usual), pitch_deg and heave_mm
   (max deviation from the BEFORE_S median; pitch + nose down), yaw_dist (RMS
   yaw-rate error in YAW_S after minus before).  The stint summary takes the
   p90 of each over events (mean below MIN_P90_EVENTS).  */

use serde::{Deserialize, Serialize};

use crate::chassis::{Axle, KerbSplit, Wheel};
use crate::metrics::laps::Lap;
use crate::metrics::signals::Signals;
use crate::metrics::stats::{interp, longest_run, mean, median, quantile, runs, savgol_derivative, savgol_window};
use crate::telemetry::{TelemetryError, TelemetrySource, KERB_SURFACE_CODE};
use crate::units::{Meters, MmPerSec, Seconds};

const WINDOW_S: f64 = 0.04;
pub const BIN_MM_S: f64 = 5.0;
pub const RANGE_MM_S: f64 = 1000.0;
pub const BINS: usize = (2.0 * RANGE_MM_S / BIN_MM_S) as usize;
const MIN_SPEED: f64 = 15.0;
const MERGE_S: f64 = 0.25;
const AFTER_S: f64 = 0.3;
const BEFORE_S: f64 = 0.5;
const YAW_S: f64 = 0.5;
const UNLOADED: f64 = 0.1;
const MIN_P90_EVENTS: usize = 4;

#[derive(Debug, Clone)]
pub struct Suspension {
    pub t: Vec<f64>,
    pub rate: f64,
    // Spring compression, m.
    pub pos: Vec<Wheel<f64>>,
    // Shaft velocity, mm/s (+ bump).
    pub vel: Vec<Wheel<f64>>,
    pub ride_height: Vec<Wheel<f64>>,
    pub speed: Vec<f64>,
    pub kerb: Vec<Wheel<bool>>,
}

impl Suspension {
    pub fn load<S: TelemetrySource + ?Sized>(src: &S, sig: &Signals) -> Result<Self, TelemetryError> {
        let susp = src.wheel_channel("Susp Pos")?;
        let rate = match src.channel_kind("Susp Pos") {
            Some(crate::telemetry::ChannelKind::FixedRate { hz }) => hz,
            _ => return Err(TelemetryError::MissingChannel("Susp Pos (fixed-rate)".into())),
        };
        let times = susp.times.clone();
        let t: Vec<f64> = times.iter().map(|s| s.raw()).collect();
        let window = savgol_window(rate, WINDOW_S, 2);
        let per_wheel = |f: fn(&Wheel<f64>) -> f64| -> Vec<f64> {
            savgol_derivative(&susp.values.iter().map(f).collect::<Vec<_>>(), window, 1.0 / rate)
        };
        let [fl, fr, rl, rr] = [|w: &Wheel<f64>| w.fl, |w: &Wheel<f64>| w.fr, |w: &Wheel<f64>| w.rl, |w: &Wheel<f64>| w.rr]
            .map(per_wheel);
        let vel = (0..t.len()).map(|i| Wheel::new(fl[i], fr[i], rl[i], rr[i]).map(|v| v * 1e3)).collect();
        let kerb = src
            .wheel_surface()?
            .sample_zoh(&times)
            .into_iter()
            .map(|w| w.map(|code| code == KERB_SURFACE_CODE))
            .collect();
        Ok(Self {
            ride_height: src.wheel_channel("RideHeights")?.sample_linear(&times),
            speed: interp(&t, &sig.t, &sig.speed, None),
            kerb,
            vel,
            pos: susp.values,
            rate,
            t,
        })
    }

    // Any-wheel kerb flag at each time of another grid (nearest following sample).
    pub fn kerb_on_grid(&self, grid: &[f64]) -> Vec<bool> {
        if self.t.is_empty() {
            return vec![false; grid.len()];
        }
        grid.iter()
            .map(|&t| {
                let i = self.t.partition_point(|&s| s < t).min(self.t.len() - 1);
                self.kerb[i].any(|b| b)
            })
            .collect()
    }

    fn moving_in(&self, laps: &[Lap]) -> Vec<bool> {
        let racing: Vec<&Lap> = laps.iter().filter(|l| !l.in_pits).collect();
        self.t
            .iter()
            .zip(&self.speed)
            .map(|(&t, &v)| v > MIN_SPEED && racing.iter().any(|l| l.contains(t)))
            .collect()
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VelocityHistogram {
    pub counts: Vec<u32>,
    sum: f64,
    sum2: f64,
    sum3: f64,
}

impl Default for VelocityHistogram {
    fn default() -> Self {
        Self { counts: vec![0; BINS], sum: 0.0, sum2: 0.0, sum3: 0.0 }
    }
}

impl VelocityHistogram {
    pub fn push(&mut self, v: MmPerSec) {
        let v = v.raw();
        if !v.is_finite() {
            return;
        }
        let bin = ((v + RANGE_MM_S) / BIN_MM_S).floor().clamp(0.0, (BINS - 1) as f64) as usize;
        self.counts[bin] += 1;
        self.sum += v;
        self.sum2 += v * v;
        self.sum3 += v * v * v;
    }

    pub fn merge(&self, other: &Self) -> Self {
        Self {
            counts: self.counts.iter().zip(&other.counts).map(|(a, b)| a + b).collect(),
            sum: self.sum + other.sum,
            sum2: self.sum2 + other.sum2,
            sum3: self.sum3 + other.sum3,
        }
    }

    pub fn total(&self) -> u64 {
        self.counts.iter().map(|&c| u64::from(c)).sum()
    }

    #[inline]
    pub fn bin_centre(bin: usize) -> f64 {
        -RANGE_MM_S + (bin as f64 + 0.5) * BIN_MM_S
    }

    fn share(&self, pred: impl Fn(f64) -> bool) -> Option<f64> {
        let total = self.total();
        (total > 0).then(|| {
            let hit: u64 = self
                .counts
                .iter()
                .enumerate()
                .filter(|(b, _)| pred(Self::bin_centre(*b)))
                .map(|(_, &c)| u64::from(c))
                .sum();
            hit as f64 / total as f64
        })
    }

    pub fn bump_share(&self) -> Option<f64> {
        self.share(|c| c > 0.0)
    }

    pub fn slow_share(&self, knee: MmPerSec) -> Option<f64> {
        self.share(|c| c.abs() < knee.raw())
    }

    pub fn skew(&self) -> Option<f64> {
        let n = self.total() as f64;
        if n < 10.0 {
            return None;
        }
        let m = self.sum / n;
        let var = self.sum2 / n - m * m;
        if var <= 0.0 {
            return Some(0.0);
        }
        let m3 = self.sum3 / n - 3.0 * m * self.sum2 / n + 2.0 * m.powi(3);
        Some(m3 / var.powf(1.5))
    }

    fn normalised(&self) -> Option<Vec<f64>> {
        let total = self.total();
        (total > 0).then(|| self.counts.iter().map(|&c| f64::from(c) / total as f64).collect())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct AxleDamperShape {
    // |bump share − ½| off kerbs.
    pub symmetry: f64,
    // Share of off-kerb samples below the damper knee.
    pub slow_share: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DamperMetrics {
    pub knee: MmPerSec,
    pub wheels: Wheel<KerbSplit<VelocityHistogram>>,
    pub axles: Axle<Option<AxleDamperShape>>,
    // L1 distance between normalised front and rear off-kerb histograms, 0..2.
    pub front_rear_shape: Option<f64>,
}

pub fn damper_metrics(susp: &Suspension, laps: &[Lap], knee: MmPerSec) -> DamperMetrics {
    let moving = susp.moving_in(laps);
    let mut wheels: Wheel<KerbSplit<VelocityHistogram>> = Wheel::default();
    for ((vel, kerb), _) in susp.vel.iter().zip(&susp.kerb).zip(&moving).filter(|(_, &m)| m) {
        let hists = [&mut wheels.fl, &mut wheels.fr, &mut wheels.rl, &mut wheels.rr];
        for ((h, &v), &k) in hists.into_iter().zip(vel.as_array()).zip(kerb.as_array()) {
            h.get_mut(k).push(MmPerSec::new(v));
        }
    }
    let axle_off = Axle::new(wheels.fl.off.merge(&wheels.fr.off), wheels.rl.off.merge(&wheels.rr.off));
    let axles = axle_off.clone().map(|h| {
        Some(AxleDamperShape {
            symmetry: (h.bump_share()? - 0.5).abs(),
            slow_share: h.slow_share(knee)?,
        })
    });
    let front_rear_shape = match (axle_off.front.normalised(), axle_off.rear.normalised()) {
        (Some(f), Some(r)) => Some(f.iter().zip(&r).map(|(a, b)| (a - b).abs()).sum()),
        _ => None,
    };
    DamperMetrics { knee, wheels, axles, front_rear_shape }
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct KerbEvent {
    pub t0: Seconds,
    pub t1: Seconds,
    pub wheels: Wheel<bool>,
    pub peak_load: Option<f64>,
    pub airtime_s: Seconds,
    pub pitch_deg: Option<f64>,
    pub heave_mm: Option<f64>,
    // RMS yaw-rate error after minus before, rad/s.
    pub yaw_dist: Option<f64>,
    pub dist: Option<Meters>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct KerbSummary {
    pub events: usize,
    pub peak_load: Option<f64>,
    pub airtime_s: Option<f64>,
    pub pitch_deg: Option<f64>,
    pub heave_mm: Option<f64>,
    pub yaw_dist: Option<f64>,
}

fn pitch_heave(rh: &Wheel<f64>, wheelbase: f64) -> (f64, f64) {
    let a = rh.axle_means();
    ((a.rear - a.front).atan2(wheelbase).to_degrees(), rh.mean() * 1e3)
}

fn slice_between(t: &[f64], lo: f64, hi: f64, lo_incl: bool, hi_incl: bool) -> std::ops::Range<usize> {
    let a = if lo_incl { t.partition_point(|&x| x < lo) } else { t.partition_point(|&x| x <= lo) };
    let b = if hi_incl { t.partition_point(|&x| x <= hi) } else { t.partition_point(|&x| x < hi) };
    a..b.max(a)
}

// `yaw_error`: r_ref − r on the Signals grid.
pub fn kerb_events(
    susp: &Suspension,
    sig: &Signals,
    laps: &[Lap],
    yaw_error: &[f64],
    wheelbase: Meters,
) -> Vec<KerbEvent> {
    let any: Vec<bool> = susp.kerb.iter().map(|w| w.any(|b| b)).collect();
    let gap = (MERGE_S * susp.rate) as usize;
    let merged = runs(&any).into_iter().fold(Vec::<(usize, usize)>::new(), |mut acc, (a, b)| {
        match acc.last_mut() {
            Some(last) if a - last.1 <= gap => last.1 = b,
            _ => acc.push((a, b)),
        }
        acc
    });

    let usual: [Option<f64>; 4] = std::array::from_fn(|w| {
        let xs: Vec<f64> = (0..susp.t.len())
            .filter(|&i| !*susp.kerb[i].as_array()[w] && susp.speed[i] > MIN_SPEED)
            .map(|i| *susp.pos[i].as_array()[w])
            .collect();
        median(&xs).filter(|&u| u > 0.0)
    });
    let ph: Vec<(f64, f64)> = susp.ride_height.iter().map(|rh| pitch_heave(rh, wheelbase.raw())).collect();
    let t = &susp.t;

    merged
        .into_iter()
        .filter(|&(a, _)| laps.iter().any(|l| !l.in_pits && l.contains(t[a])))
        .map(|(a, b)| {
            let (t0, t1) = (t[a], t[b]);
            let win = slice_between(t, t0, t1 + AFTER_S, true, true);
            let pre = slice_between(t, t0 - BEFORE_S, t0, true, false);
            let wheels = Wheel::from_array(std::array::from_fn(|w| (a..=b).any(|i| *susp.kerb[i].as_array()[w])));
            let per_wheel = |w: usize| -> Vec<f64> { win.clone().map(|i| *susp.pos[i].as_array()[w]).collect() };
            let peak_load = (0..4)
                .filter_map(|w| {
                    let u = usual[w]?;
                    per_wheel(w).into_iter().filter(|x| x.is_finite()).reduce(f64::max).map(|m| m / u)
                })
                .reduce(f64::max);
            let air = (0..4)
                .filter_map(|w| usual[w].map(|u| longest_run(per_wheel(w).into_iter().map(|x| x < UNLOADED * u))))
                .max()
                .unwrap_or(0);
            let deviation = |f: fn(&(f64, f64)) -> f64| -> Option<f64> {
                let base = median(&ph[pre.clone()].iter().map(f).collect::<Vec<_>>())?;
                ph[win.clone()].iter().map(f).filter(|x| x.is_finite()).map(|x| (x - base).abs()).reduce(f64::max)
            };
            KerbEvent {
                t0: Seconds::new(t0),
                t1: Seconds::new(t1),
                wheels,
                peak_load,
                airtime_s: Seconds::new(air as f64 / susp.rate),
                pitch_deg: deviation(|p| p.0),
                heave_mm: deviation(|p| p.1),
                yaw_dist: yaw_disturbance(&sig.t, yaw_error, t0, t1),
                dist: interp(&[t0], &sig.t, &sig.dist, None).first().copied().filter(|d| d.is_finite()).map(Meters::new),
            }
        })
        .collect()
}

fn yaw_disturbance(t: &[f64], err: &[f64], t0: f64, t1: f64) -> Option<f64> {
    let pick = |r: std::ops::Range<usize>| -> Vec<f64> { err[r].iter().copied().filter(|x| x.is_finite()).collect() };
    let after = pick(slice_between(t, t1, t1 + YAW_S, false, true));
    let before = pick(slice_between(t, t0 - YAW_S, t0, true, false));
    if after.len() < 3 || before.len() < 3 {
        return None;
    }
    let rms = |x: &[f64]| (x.iter().map(|v| v * v).sum::<f64>() / x.len() as f64).sqrt();
    Some(rms(&after) - rms(&before))
}

pub fn summarise_kerbs(events: &[KerbEvent]) -> KerbSummary {
    let stat = |f: fn(&KerbEvent) -> Option<f64>| -> Option<f64> {
        let vals: Vec<f64> = events.iter().filter_map(f).filter(|v| v.is_finite()).collect();
        if vals.len() >= MIN_P90_EVENTS { quantile(&vals, 0.9) } else { mean(&vals) }
    };
    KerbSummary {
        events: events.len(),
        peak_load: stat(|e| e.peak_load),
        airtime_s: stat(|e| Some(e.airtime_s.raw())),
        pitch_deg: stat(|e| e.pitch_deg),
        heave_mm: stat(|e| e.heave_mm),
        yaw_dist: stat(|e| e.yaw_dist),
    }
}
