/* Corner map of a recording and each lap's pass through it.
   The map comes from the median lap: lateral acceleration, speed, brake and
   steering per STEP_M of lap distance over the flying laps (or, without one,
   the non-pit laps covering at least half the lap).  A corner is a run where
   the median lap corners harder than CORNER_G in one direction (a chicane is
   two corners), merged across gaps under MERGE_GAP_M, with its braking zone
   (≤ MAX_ENTRY_M) in front as entry.  Its apex is the slowest point.

   Within a pass, phases follow what the car does, not the pedals: progress is
   the share of the pass's heading change done so far, and every cornering
   sample carries soft Entry/Mid/Exit weights summing to one (Entry fades out
   by halfway, Mid peaks there, Exit fades in from it).  Braking is on the
   brakes before the car starts cornering.  The speed band is set by the
   pass's apex speed against the class band split.  */

use serde::{Deserialize, Serialize};

use crate::chassis::{CornerPhase, Direction, SpeedBand};
use crate::metrics::laps::Lap;
use crate::metrics::signals::Signals;
use crate::metrics::stats::{box_filter_zero_pad, interp, median, sign};
use crate::units::{Meters, MetersPerSec};

pub const STEP_M: f64 = 5.0;
pub const CORNER_G: f64 = 0.4;
pub const MIN_SPEED: f64 = 15.0;
const MERGE_GAP_M: f64 = 30.0;
const MIN_CORNER_M: f64 = 30.0;
const MAX_ENTRY_M: f64 = 250.0;
const BRAKE_ON: f64 = 0.05;
const MIN_LAP_SAMPLES: usize = 50;
const MIN_PASS_SAMPLES: usize = 10;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Corner {
    pub id: usize,
    pub direction: Direction,
    pub d_start: Meters,
    pub d_apex: Meters,
    pub d_end: Meters,
    pub v_apex: MetersPerSec,
}

#[derive(Debug, Clone)]
pub struct Pass {
    pub corner: usize,
    // Contiguous grid indices of the pass.
    pub idx: std::ops::Range<usize>,
    // Per-sample weight of each phase over idx, indexed by CornerPhase::index.
    pub weights: [Vec<f64>; 4],
    pub band: SpeedBand,
    pub v_min: MetersPerSec,
}

impl Pass {
    #[inline]
    pub fn weight(&self, phase: CornerPhase) -> &[f64] {
        &self.weights[phase.index()]
    }
}

pub fn lap_length(sig: &Signals) -> f64 {
    sig.dist.iter().copied().filter(|d| d.is_finite()).fold(0.0, f64::max)
}

struct LapGrid {
    a_lat: Vec<f64>,
    speed: Vec<f64>,
    brake: Vec<f64>,
    steer: Vec<f64>,
}

fn lap_grid(sig: &Signals, lap: &Lap, lap_len: f64, grid: &[f64]) -> Option<LapGrid> {
    let idx: Vec<usize> = sig.range(lap.start, lap.end).filter(|&i| sig.dist[i].is_finite()).collect();
    if idx.len() < MIN_LAP_SAMPLES {
        return None;
    }
    // A complete lap starts at the line, but the distance wrap can trail the Lap event.
    let mut d: Vec<f64> = idx
        .iter()
        .enumerate()
        .map(|(k, &i)| {
            let v = sig.dist[i];
            if lap.complete && k < MIN_LAP_SAMPLES && v > lap_len / 2.0 { 0.0 } else { v }
        })
        .scan(f64::NEG_INFINITY, |m, v| {
            *m = m.max(v);
            Some(*m)
        })
        .collect();
    let keep: Vec<usize> = (0..d.len()).filter(|&k| k == 0 || d[k] > d[k - 1]).collect();
    if keep.len() < 10 {
        return None;
    }
    let pick = |xs: &[f64]| -> Vec<f64> { keep.iter().map(|&k| xs[idx[k]]).collect() };
    d = keep.iter().map(|&k| d[k]).collect();
    let on_grid = |xs: &[f64]| interp(grid, &d, &pick(xs), Some(f64::NAN));
    Some(LapGrid {
        a_lat: on_grid(&sig.a_lat),
        speed: on_grid(&sig.speed),
        brake: on_grid(&sig.brake),
        steer: on_grid(&sig.steer),
    })
}

fn covered(sig: &Signals, lap: &Lap) -> f64 {
    let ds: Vec<f64> = sig.range(lap.start, lap.end).map(|i| sig.dist[i]).filter(|d| d.is_finite()).collect();
    let (lo, hi) = ds.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |(lo, hi), &d| (lo.min(d), hi.max(d)));
    if ds.is_empty() { 0.0 } else { hi - lo }
}

fn bin_median(rows: &[LapGrid], field: impl Fn(&LapGrid) -> &[f64], bins: usize) -> Vec<f64> {
    (0..bins)
        .map(|b| median(&rows.iter().map(|r| field(r)[b]).collect::<Vec<_>>()).unwrap_or(f64::NAN))
        .collect()
}

pub fn corner_map(sig: &Signals, laps: &[Lap]) -> Vec<Corner> {
    let lap_len = lap_length(sig);
    if lap_len <= 0.0 {
        return Vec::new();
    }
    let flying: Vec<&Lap> = laps.iter().filter(|l| l.is_flying()).collect();
    let chosen: Vec<&Lap> = if flying.is_empty() {
        laps.iter().filter(|l| !l.in_pits && covered(sig, l) >= 0.5 * lap_len).collect()
    } else {
        flying
    };
    let grid: Vec<f64> = (0..).map(|k| k as f64 * STEP_M).take_while(|&d| d < lap_len).collect();
    let rows: Vec<LapGrid> = chosen.iter().filter_map(|l| lap_grid(sig, l, lap_len, &grid)).collect();
    if rows.is_empty() {
        return Vec::new();
    }
    let bins = grid.len();
    let a_lat = box_filter_zero_pad(&bin_median(&rows, |r| &r.a_lat, bins), 5);
    let speed = bin_median(&rows, |r| &r.speed, bins);
    let brake = bin_median(&rows, |r| &r.brake, bins);
    let steer = bin_median(&rows, |r| &r.steer, bins);
    let direction: Vec<f64> = a_lat.iter().map(|&a| sign(a)).collect();
    let cornering: Vec<bool> = (0..bins).map(|i| a_lat[i].abs() > CORNER_G && speed[i] > MIN_SPEED).collect();

    let mut runs: Vec<(usize, usize)> = Vec::new();
    let mut i = 0;
    while i < bins {
        if !cornering[i] {
            i += 1;
            continue;
        }
        let mut j = i;
        while j + 1 < bins && cornering[j + 1] && direction[j + 1] == direction[i] {
            j += 1;
        }
        match runs.last_mut() {
            Some(last) if direction[last.0] == direction[i] && (i - last.1) as f64 * STEP_M <= MERGE_GAP_M => {
                last.1 = j;
            }
            _ => runs.push((i, j)),
        }
        i = j + 1;
    }

    let mut corners: Vec<Corner> = Vec::new();
    for (i, j) in runs {
        if (j - i + 1) as f64 * STEP_M < MIN_CORNER_M {
            continue;
        }
        let apex = (i..=j)
            .filter(|&k| speed[k].is_finite())
            .min_by(|&a, &b| speed[a].total_cmp(&speed[b]))
            .unwrap_or(i);
        let mut start = i;
        while start > 0
            && ((i - start) as f64) * STEP_M < MAX_ENTRY_M
            && brake[start - 1].is_finite()
            && brake[start - 1] > BRAKE_ON
        {
            start -= 1;
        }
        if let Some(prev) = corners.last() {
            if grid[start] < prev.d_end.raw() {
                start = (prev.d_end.raw() / STEP_M).round() as usize;
            }
        }
        let steer_dir = median(&steer[i..=j]).map(sign).unwrap_or(0.0);
        let dir_sign = if steer_dir != 0.0 { steer_dir } else { direction[i] };
        let Some(dir) = Direction::from_sign(dir_sign) else { continue };
        corners.push(Corner {
            id: corners.len(),
            direction: dir,
            d_start: Meters::new(grid[start.min(bins - 1)]),
            d_apex: Meters::new(grid[apex]),
            d_end: Meters::new(grid[j]),
            v_apex: MetersPerSec::new(speed[apex]),
        });
    }
    corners
}

// Soft Entry/Mid/Exit weights at a progress in [0, 1]; they sum to one.
#[inline]
pub fn phase_weights(progress: f64) -> [f64; 3] {
    let p = progress.clamp(0.0, 1.0);
    [
        (1.0 - p / 0.5).clamp(0.0, 1.0),
        (1.0 - (p - 0.5).abs() / 0.5).clamp(0.0, 1.0),
        (1.0 - (1.0 - p) / 0.5).clamp(0.0, 1.0),
    ]
}

// Passes of one lap through the corner map; `lap` is a grid index range.
pub fn passes(sig: &Signals, lap: std::ops::Range<usize>, corners: &[Corner], band_split: MetersPerSec) -> Vec<Pass> {
    corners
        .iter()
        .filter(|c| c.d_end > c.d_start)
        .filter_map(|c| {
            let inside: Vec<bool> = lap
                .clone()
                .map(|i| {
                    let d = sig.dist[i];
                    d >= c.d_start.raw() && d <= c.d_end.raw() && sig.speed[i].is_finite()
                })
                .collect();
            // Longest contiguous stretch: the lap's first samples may still read last lap's distance.
            let (s, e) = crate::metrics::stats::runs(&inside)
                .into_iter()
                .max_by_key(|(s, e)| e - s)?;
            let idx = lap.start + s..lap.start + e + 1;
            if idx.len() < MIN_PASS_SAMPLES {
                return None;
            }
            Some(build_pass(sig, c.id, idx, band_split))
        })
        .collect()
}

fn build_pass(sig: &Signals, corner: usize, idx: std::ops::Range<usize>, band_split: MetersPerSec) -> Pass {
    let n = idx.len();
    let sp = &sig.speed[idx.clone()];
    let cornering: Vec<bool> = idx
        .clone()
        .map(|i| sig.a_lat[i].abs() > CORNER_G && sig.speed[i] > MIN_SPEED)
        .collect();
    let turned: Vec<f64> = idx
        .clone()
        .zip(&cornering)
        .map(|(i, &c)| if c && sig.yaw_rate[i].is_finite() { sig.yaw_rate[i].abs() } else { 0.0 })
        .scan(0.0, |acc, y| {
            *acc += y;
            Some(*acc)
        })
        .collect();
    let total = *turned.last().unwrap_or(&0.0);
    let first_cornering = cornering.iter().position(|&c| c).unwrap_or(n);

    let mut weights: [Vec<f64>; 4] = std::array::from_fn(|_| vec![0.0; n]);
    for k in 0..n {
        let i = idx.start + k;
        if cornering[k] {
            let progress = if total > 0.0 { turned[k] / total } else { 0.0 };
            let [en, mi, ex] = phase_weights(progress);
            weights[CornerPhase::Entry.index()][k] = en;
            weights[CornerPhase::Mid.index()][k] = mi;
            weights[CornerPhase::Exit.index()][k] = ex;
        } else if sig.brake[i] > BRAKE_ON && sp[k] > MIN_SPEED && k < first_cornering {
            weights[CornerPhase::Braking.index()][k] = 1.0;
        }
    }
    let v_min = sp.iter().copied().filter(|v| v.is_finite()).fold(f64::INFINITY, f64::min);
    Pass {
        corner,
        idx,
        weights,
        band: if v_min < band_split.raw() { SpeedBand::Low } else { SpeedBand::High },
        v_min: MetersPerSec::new(v_min),
    }
}
