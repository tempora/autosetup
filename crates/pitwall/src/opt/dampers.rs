/* Damper block: whole-click damper moves for the contact patch and the
   kerbs, with what a click does learned per car and layout.

   Metrics per stint (SCALE gives each one's unit of importance):
     contact (off-kerb, lower is better)
       cv_<axle>    spring-load variation, std/mean per lap
       sym_<axle>   |bump share − ½| of the shaft-velocity histogram
       knee_<axle>  |share under the class knee − LS_TARGET|
       shape        L1 distance of normalised front and rear histograms (SHAPE_BIN)
     kerbs (lower is better; p90 over events, mean under MIN_P90_EVENTS)
       peak_load, airtime_s, pitch_deg, heave_mm, yaw_dist

   Model: per metric, Bayesian linear regression on each setting's share of
   its range, from the stints of this car and layout, with priors for the
   direction each setting moves each metric (fast circuit mainly the kerbs,
   slow circuit mainly the contact patch and platform; springs and bars are
   inputs too).  A move is scored on the predicted change of the contact sum
   plus KERB_WEIGHT × the kerb sum.

   Gates, each counted on the plan:
     kerb     no single kerb metric may be predicted to get worse than the
              stint by more than KERB_MARGIN (judged per metric: a product
              of per-metric probabilities collapses with every metric added)
     balance  slow dampers move balance (fixed directions, STEP_DEG a click);
              a move may not take the main proposal further outside its band
     kerbs unstable  no fast damper stiffens; kerb weight rises; at least one
              fast damper softens when any can
   At most two settings move (pairs of the best TOP_SINGLES single moves).
   A move must clear its own doubt (gain − ½ sd ≥ MIN_GAIN) unless it moves a
   setting driven at fewer than EXPLORE_VALUES values, while the main block
   leaves the balance alone (what changed on track could not be told apart).  */

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CornerPhase, SpeedBand};
use crate::metrics::stats::{mean, quantile};
use crate::metrics::suspension::{VelocityHistogram, BINS, BIN_MM_S};
use crate::metrics::StintMetrics;
use crate::opt::solve::Evaluated;
use crate::opt::target::BalanceTarget;
use crate::setup::CarSetup;
use crate::units::Degrees;

const LS_TARGET: f64 = 0.85;
const SHAPE_BIN: f64 = 20.0;
const MIN_P90_EVENTS: usize = 4;
const KERB_MARGIN: f64 = 0.5;
const KERB_WEIGHT: f64 = 0.5;
const KERB_WEIGHT_UNSTABLE: f64 = 2.0;
const MAX_CLICKS: i32 = 3;
const MAX_PACKER_CLICKS: i32 = 2;
const MIN_GAIN: f64 = 0.1;
const EXPLORE: f64 = 0.3;
const EXPLORE_VALUES: usize = 3;
const STEP_DEG: f64 = 0.03;
const TOP_SINGLES: usize = 6;
const DEFAULT_PRIOR: (f64, f64) = (0.0, 0.3);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Metric {
    CvFront,
    CvRear,
    SymFront,
    SymRear,
    KneeFront,
    KneeRear,
    Shape,
    PeakLoad,
    AirtimeS,
    PitchDeg,
    HeaveMm,
    YawDist,
}

impl Metric {
    pub const CONTACT: [Self; 7] =
        [Self::CvFront, Self::CvRear, Self::SymFront, Self::SymRear, Self::KneeFront, Self::KneeRear, Self::Shape];
    pub const KERB: [Self; 5] = [Self::PeakLoad, Self::AirtimeS, Self::PitchDeg, Self::HeaveMm, Self::YawDist];

    pub const fn scale(self) -> f64 {
        match self {
            Self::CvFront | Self::CvRear | Self::SymFront | Self::SymRear => 0.03,
            Self::KneeFront | Self::KneeRear => 0.05,
            Self::Shape | Self::PeakLoad => 0.1,
            Self::AirtimeS | Self::YawDist => 0.01,
            Self::PitchDeg => 0.05,
            Self::HeaveMm => 1.0,
        }
    }

    const fn axle(self) -> Option<Axle> {
        match self {
            Self::CvFront | Self::SymFront | Self::KneeFront => Some(Axle::Front),
            Self::CvRear | Self::SymRear | Self::KneeRear => Some(Axle::Rear),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Axle {
    Front,
    Rear,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Family {
    SlowBump,
    SlowRebound,
    FastBump,
    FastRebound,
    Packers,
    Spring,
    Arb,
}

// A setting the damper model reads; `movable` ones are the damper block's to change.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Setting {
    pub family: Family,
    pub axle: Axle,
    pub third: bool,
}

macro_rules! settings {
    ($($family:ident $axle:ident $third:literal => [$($key:literal),+]),+ $(,)?) => {
        const SETTINGS: &[(Setting, &[&str])] = &[$(
            (Setting { family: Family::$family, axle: Axle::$axle, third: $third }, &[$($key),+])
        ),+];
    };
}

settings! {
    SlowBump Front false => ["WM_SLOWBUMP-W_FL", "WM_SLOWBUMP-W_FR"],
    SlowRebound Front false => ["WM_SLOWREBOUND-W_FL", "WM_SLOWREBOUND-W_FR"],
    FastBump Front false => ["WM_FASTBUMP-W_FL", "WM_FASTBUMP-W_FR"],
    FastRebound Front false => ["WM_FASTREBOUND-W_FL", "WM_FASTREBOUND-W_FR"],
    SlowBump Rear false => ["WM_SLOWBUMP-W_RL", "WM_SLOWBUMP-W_RR"],
    SlowRebound Rear false => ["WM_SLOWREBOUND-W_RL", "WM_SLOWREBOUND-W_RR"],
    FastBump Rear false => ["WM_FASTBUMP-W_RL", "WM_FASTBUMP-W_RR"],
    FastRebound Rear false => ["WM_FASTREBOUND-W_RL", "WM_FASTREBOUND-W_RR"],
    SlowBump Front true => ["VM_FRONT_3RD_SLOWBUMP"],
    SlowRebound Front true => ["VM_FRONT_3RD_SLOWREBOUND"],
    FastBump Front true => ["VM_FRONT_3RD_FASTBUMP"],
    FastRebound Front true => ["VM_FRONT_3RD_FASTREBOUND"],
    SlowBump Rear true => ["VM_REAR_3RD_SLOWBUMP"],
    SlowRebound Rear true => ["VM_REAR_3RD_SLOWREBOUND"],
    FastBump Rear true => ["VM_REAR_3RD_FASTBUMP"],
    FastRebound Rear true => ["VM_REAR_3RD_FASTREBOUND"],
    Packers Front false => ["WM_PACKERS-W_FL", "WM_PACKERS-W_FR"],
    Packers Rear false => ["WM_PACKERS-W_RL", "WM_PACKERS-W_RR"],
    Packers Front true => ["VM_FRONT_3RD_PACKERS"],
    Packers Rear true => ["VM_REAR_3RD_PACKERS"],
    Spring Front false => ["WM_SPRING-W_FL", "WM_SPRING-W_FR"],
    Spring Rear false => ["WM_SPRING-W_RL", "WM_SPRING-W_RR"],
    Arb Front false => ["VM_FRONT_ANTISWAY"],
    Arb Rear false => ["VM_REAR_ANTISWAY"],
}

impl Setting {
    pub const fn movable(self) -> bool {
        !matches!(self.family, Family::Spring | Family::Arb)
    }

    pub const fn is_fast(self) -> bool {
        matches!(self.family, Family::FastBump | Family::FastRebound)
    }

    pub fn label(self) -> String {
        let axle = match self.axle {
            Axle::Front => "front",
            Axle::Rear => "rear",
        };
        let what = match self.family {
            Family::SlowBump => "slow bump",
            Family::SlowRebound => "slow rebound",
            Family::FastBump => "fast bump",
            Family::FastRebound => "fast rebound",
            Family::Packers => "packers",
            Family::Spring => "springs",
            Family::Arb => "anti-roll bar",
        };
        if self.third { format!("{axle} third-spring {what}") } else { format!("{axle} {what}") }
    }

    /* Prior effect of the whole range, going up in index (stiffer / more), on
       a metric in SCALE units: (mean, sd).  */
    fn prior(self, m: Metric) -> (f64, f64) {
        let same = m.axle() == Some(self.axle);
        let p = match (self.family, m) {
            (Family::SlowBump | Family::SlowRebound, Metric::CvFront | Metric::CvRear) if same => (0.5, 1.5),
            (Family::SlowBump | Family::SlowRebound, Metric::SymFront | Metric::SymRear) if same => (0.0, 1.0),
            (Family::SlowBump | Family::SlowRebound, Metric::KneeFront | Metric::KneeRear) if same => (-0.5, 1.5),
            (Family::SlowBump | Family::SlowRebound, Metric::PitchDeg | Metric::HeaveMm) => (-0.5, 1.0),
            (Family::SlowBump | Family::SlowRebound, Metric::PeakLoad | Metric::AirtimeS | Metric::YawDist) => (0.0, 0.5),
            (Family::FastBump | Family::FastRebound, Metric::CvFront | Metric::CvRear) if same => (0.2, 0.5),
            (Family::FastBump | Family::FastRebound, Metric::SymFront | Metric::SymRear | Metric::KneeFront | Metric::KneeRear) if same => (0.0, 0.5),
            // Stiffer fast bump: the kerb goes into the car.
            (Family::FastBump, Metric::PeakLoad) => (1.0, 2.0),
            (Family::FastBump, Metric::PitchDeg | Metric::HeaveMm | Metric::YawDist | Metric::AirtimeS) => (0.5, 2.0),
            // Stiffer fast rebound: the wheel comes back down slower after it.
            (Family::FastRebound, Metric::AirtimeS) => (1.0, 2.0),
            (Family::FastRebound, Metric::PeakLoad | Metric::PitchDeg) => (0.0, 1.0),
            (Family::FastRebound, Metric::HeaveMm) => (-0.5, 2.0),
            (Family::FastRebound, Metric::YawDist) => (0.5, 2.0),
            (Family::Packers | Family::Spring, Metric::CvFront | Metric::CvRear) if same => (0.5, 1.5),
            (Family::Packers, Metric::PitchDeg) => (-0.5, 1.5),
            (Family::Packers, Metric::HeaveMm) => (-1.0, 1.5),
            (Family::Packers | Family::Spring, Metric::PeakLoad) => (0.5, 1.5),
            (Family::Spring, Metric::PitchDeg | Metric::HeaveMm) => (-0.5, 1.5),
            (Family::Spring, Metric::YawDist) => (0.3, 1.5),
            // A stiffer bar couples left and right wheels across a one-sided kerb hit.
            (Family::Arb, Metric::PeakLoad | Metric::AirtimeS | Metric::YawDist) => (0.8, 1.5),
            (Family::Arb, Metric::HeaveMm) => (0.3, 1.0),
            (_, Metric::Shape) => (0.0, 1.0),
            _ => DEFAULT_PRIOR,
        };
        // A third element works on the platform, less on one wheel.
        if self.third { (0.5 * p.0, p.1) } else { p }
    }

    /* Balance per click going stiffer, deg (+ understeer).  On the brakes the
       front is in bump and the rear in rebound, on power the other way round;
       damping an end harder moves load across it sooner.  */
    fn balance_per_click(self, cell: BalanceCell) -> f64 {
        let (low, high, braking) = match (self.family, self.axle, self.third) {
            (Family::SlowBump, Axle::Front, false) => (CornerPhase::Entry, 1.0, Some(2.0 / 3.0)),
            (Family::SlowRebound, Axle::Rear, false) => (CornerPhase::Entry, -1.0, Some(-2.0 / 3.0)),
            (Family::SlowRebound, Axle::Front, false) => (CornerPhase::Exit, -1.0, None),
            (Family::SlowBump, Axle::Rear, false) => (CornerPhase::Exit, 1.0, None),
            _ => return 0.0,
        };
        let w = match (cell.phase, cell.band) {
            (p, SpeedBand::Low) if p == low => high,
            (p, SpeedBand::High) if p == low => high * 2.0 / 3.0,
            (CornerPhase::Braking, _) => braking.unwrap_or(0.0),
            _ => 0.0,
        };
        STEP_DEG * w
    }
}

// One stint's damper metrics: (mean, variance) per metric.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DamperStint {
    pub setup: CarSetup,
    pub values: BTreeMap<Metric, (f64, f64)>,
}

fn floor(m: Metric) -> f64 {
    (0.2 * m.scale()).powi(2)
}

fn rebinned(h: &VelocityHistogram) -> Vec<f64> {
    let k = (SHAPE_BIN / BIN_MM_S).round().max(1.0) as usize;
    h.counts.chunks(k).take(BINS / k).map(|c| c.iter().map(|&v| f64::from(v)).sum()).collect()
}

impl DamperStint {
    pub fn new(setup: CarSetup, m: &StintMetrics) -> Self {
        let mut values = BTreeMap::new();
        let d = &m.dampers;
        for (cvs, shape, cv, sym, knee) in [
            (&d.load_cv.front, d.axles.front, Metric::CvFront, Metric::SymFront, Metric::KneeFront),
            (&d.load_cv.rear, d.axles.rear, Metric::CvRear, Metric::SymRear, Metric::KneeRear),
        ] {
            if let Some(mu) = mean(cvs) {
                let var = cvs.iter().map(|v| (v - mu).powi(2)).sum::<f64>() / cvs.len() as f64;
                values.insert(cv, (mu, var / cvs.len() as f64 + floor(cv)));
            }
            if let Some(s) = shape {
                values.insert(sym, (s.symmetry, floor(sym)));
                values.insert(knee, ((s.slow_share - LS_TARGET).abs(), floor(knee)));
            }
        }
        let front = rebinned(&d.wheels.fl.off.merge(&d.wheels.fr.off));
        let rear = rebinned(&d.wheels.rl.off.merge(&d.wheels.rr.off));
        let (sf, sr) = (front.iter().sum::<f64>(), rear.iter().sum::<f64>());
        if sf > 0.0 && sr > 0.0 {
            let l1 = front.iter().zip(&rear).map(|(a, b)| (a / sf - b / sr).abs()).sum();
            values.insert(Metric::Shape, (l1, floor(Metric::Shape)));
        }
        let kerb: [(Metric, Vec<f64>); 5] = [
            (Metric::PeakLoad, m.kerb_events.iter().filter_map(|e| e.peak_load).collect()),
            (Metric::AirtimeS, m.kerb_events.iter().map(|e| e.airtime_s.raw()).collect()),
            (Metric::PitchDeg, m.kerb_events.iter().filter_map(|e| e.pitch_deg).collect()),
            (Metric::HeaveMm, m.kerb_events.iter().filter_map(|e| e.heave_mm).collect()),
            (Metric::YawDist, m.kerb_events.iter().filter_map(|e| e.yaw_dist).collect()),
        ];
        for (metric, vals) in kerb {
            let vals: Vec<f64> = vals.into_iter().filter(|v| v.is_finite()).collect();
            if vals.len() < 2 {
                continue;
            }
            let mu = vals.iter().sum::<f64>() / vals.len() as f64;
            let stat = if vals.len() >= MIN_P90_EVENTS { quantile(&vals, 0.9).unwrap_or(mu) } else { mu };
            let var = vals.iter().map(|v| (v - mu).powi(2)).sum::<f64>() / vals.len() as f64;
            values.insert(metric, (stat, var / vals.len() as f64 + floor(metric)));
        }
        Self { setup, values }
    }
}

// A setting present and adjustable on this car, with its whole index range.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Dim {
    setting: Setting,
    keys: &'static [&'static str],
    lo: i32,
    hi: i32,
}

impl Dim {
    fn index(&self, setup: &CarSetup) -> Option<i32> {
        let vals: Vec<f64> = self.keys.iter().map(|k| setup.params.get(*k).map(|p| f64::from(p.value))).collect::<Option<_>>()?;
        Some((vals.iter().sum::<f64>() / vals.len() as f64).round() as i32)
    }

    fn share(&self, index: i32) -> f64 {
        f64::from(index - self.lo) / f64::from((self.hi - self.lo).max(1))
    }

    fn reach(&self) -> i32 {
        let cap = if self.setting.family == Family::Packers { MAX_PACKER_CLICKS } else { MAX_CLICKS };
        ((0.15 * f64::from(self.hi - self.lo)).round() as i32).clamp(1, cap)
    }
}

fn dims_of(setup: &CarSetup) -> Vec<Dim> {
    SETTINGS
        .iter()
        .filter_map(|&(setting, keys)| {
            let ps: Vec<_> = keys.iter().map(|k| setup.params.get(*k)).collect::<Option<_>>()?;
            ps.iter().all(|p| p.adjustable()).then(|| Dim { setting, keys, lo: ps[0].min, hi: ps[0].max_exclusive - 1 })
        })
        .collect()
}

// Posterior of one metric's linear model: intercept then one slope per dim.
#[derive(Debug, Clone)]
struct Linear {
    mean: Vec<f64>,
    cov: Vec<Vec<f64>>,
}

impl Linear {
    // Predicted change (mean, variance) for a change of shares `d` (no intercept).
    fn delta(&self, d: &[f64]) -> (f64, f64) {
        let m: f64 = d.iter().enumerate().map(|(i, di)| di * self.mean[i + 1]).sum();
        let v: f64 = (0..d.len())
            .flat_map(|i| (0..d.len()).map(move |j| (i, j)))
            .map(|(i, j)| d[i] * self.cov[i + 1][j + 1] * d[j])
            .sum();
        (m, v.max(0.0))
    }
}

fn invert(mut a: Vec<Vec<f64>>) -> Option<Vec<Vec<f64>>> {
    let n = a.len();
    let mut inv: Vec<Vec<f64>> = (0..n).map(|i| (0..n).map(|j| f64::from(u8::from(i == j))).collect()).collect();
    for c in 0..n {
        let p = (c..n).max_by(|&x, &y| a[x][c].abs().total_cmp(&a[y][c].abs()))?;
        if a[p][c].abs() < 1e-15 {
            return None;
        }
        a.swap(c, p);
        inv.swap(c, p);
        let d = a[c][c];
        for j in 0..n {
            a[c][j] /= d;
            inv[c][j] /= d;
        }
        for r in (0..n).filter(|&r| r != c) {
            let f = a[r][c];
            if f != 0.0 {
                for j in 0..n {
                    a[r][j] -= f * a[c][j];
                    inv[r][j] -= f * inv[c][j];
                }
            }
        }
    }
    Some(inv)
}

#[derive(Debug, Clone)]
pub struct DamperModel {
    dims: Vec<Dim>,
    models: BTreeMap<Metric, Linear>,
    // Distinct values each dim was driven at.
    tried: Vec<usize>,
    pub stints: usize,
}

impl DamperModel {
    pub fn fit(setup: &CarSetup, history: &[DamperStint]) -> Self {
        let dims = dims_of(setup);
        let rows: Vec<(Vec<f64>, &DamperStint)> = history
            .iter()
            .filter_map(|s| {
                let x: Vec<f64> = dims.iter().map(|d| d.index(&s.setup).map(|i| d.share(i))).collect::<Option<_>>()?;
                Some((x, s))
            })
            .collect();
        let models = Metric::CONTACT
            .into_iter()
            .chain(Metric::KERB)
            .filter_map(|metric| {
                let obs: Vec<(&Vec<f64>, f64, f64)> =
                    rows.iter().filter_map(|(x, s)| s.values.get(&metric).map(|&(y, v)| (x, y, v.max(1e-12)))).collect();
                if obs.is_empty() {
                    return None;
                }
                let scale = metric.scale();
                let y_mean = obs.iter().map(|o| o.1).sum::<f64>() / obs.len() as f64;
                let m0: Vec<f64> = std::iter::once(y_mean).chain(dims.iter().map(|d| d.setting.prior(metric).0 * scale)).collect();
                let s0: Vec<f64> =
                    std::iter::once(10.0 * scale + y_mean.abs()).chain(dims.iter().map(|d| d.setting.prior(metric).1 * scale)).collect();
                let n = m0.len();
                let mut a: Vec<Vec<f64>> = (0..n).map(|i| (0..n).map(|j| if i == j { 1.0 / s0[i].powi(2) } else { 0.0 }).collect()).collect();
                let mut b: Vec<f64> = (0..n).map(|i| m0[i] / s0[i].powi(2)).collect();
                for (x, y, v) in &obs {
                    let phi: Vec<f64> = std::iter::once(1.0).chain(x.iter().copied()).collect();
                    for i in 0..n {
                        b[i] += phi[i] * y / v;
                        for j in 0..n {
                            a[i][j] += phi[i] * phi[j] / v;
                        }
                    }
                }
                let cov = invert(a)?;
                let mean = (0..n).map(|i| (0..n).map(|j| cov[i][j] * b[j]).sum()).collect();
                Some((metric, Linear { mean, cov }))
            })
            .collect();
        let tried = dims
            .iter()
            .map(|d| history.iter().filter_map(|s| d.index(&s.setup)).collect::<BTreeSet<_>>().len())
            .collect();
        Self { dims, models, tried, stints: history.len() }
    }

    // Predicted change (SCALE units, mean and variance) of a metric group.
    fn change(&self, metrics: &[Metric], d: &[f64]) -> (f64, f64) {
        metrics.iter().filter_map(|m| self.models.get(m).map(|l| (m, l.delta(d)))).fold((0.0, 0.0), |(am, av), (m, (dm, dv))| {
            (am + dm / m.scale(), av + dv / m.scale().powi(2))
        })
    }

    // Kerb metrics predicted to worsen beyond the margin.
    fn kerb_worse(&self, d: &[f64]) -> bool {
        Metric::KERB.iter().any(|m| self.models.get(m).is_some_and(|l| l.delta(d).0 > KERB_MARGIN * m.scale()))
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DamperChange {
    pub setting: Setting,
    pub keys: Vec<String>,
    pub from: i32,
    pub to: i32,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DamperPlan {
    pub changes: Vec<DamperChange>,
    // Predicted change of the contact and kerb sums (SCALE units, negative is better).
    pub contact_change: f64,
    pub kerb_change: f64,
    // A move made to learn what its setting does rather than for a confident gain.
    pub exploring: bool,
    pub stints: usize,
    pub candidates: usize,
    pub rejected_kerb: usize,
    pub rejected_balance: usize,
    pub rejected_unstable: usize,
}

pub struct DamperInput<'a> {
    pub setup: &'a CarSetup,
    // Stints of this car and layout, the current one included.
    pub history: &'a [DamperStint],
    pub kerbs_unstable: bool,
    // The main block moves the balance this proposal: no exploring then.
    pub main_changes: bool,
    pub locks: &'a BTreeSet<String>,
    pub target: &'a BalanceTarget,
    pub chosen: &'a Evaluated,
}

#[derive(Debug, Clone)]
struct Scored {
    idx: Vec<i32>,
    value: f64,
    gain: f64,
    sd: f64,
    new: bool,
    contact: f64,
    kerb: f64,
}

pub fn plan_dampers(input: &DamperInput<'_>) -> DamperPlan {
    let model = DamperModel::fit(input.setup, input.history);
    let Some(base) = model.dims.iter().map(|d| d.index(input.setup)).collect::<Option<Vec<i32>>>() else {
        return DamperPlan::default();
    };
    let weight = if input.kerbs_unstable { KERB_WEIGHT_UNSTABLE } else { KERB_WEIGHT };
    let movable: Vec<usize> = (0..model.dims.len())
        .filter(|&i| model.dims[i].setting.movable() && !model.dims[i].keys.iter().any(|k| input.locks.contains(*k)))
        .collect();
    let mut plan = DamperPlan { stints: model.stints, ..DamperPlan::default() };

    let shares = |idx: &[i32]| -> Vec<f64> {
        model.dims.iter().enumerate().map(|(i, d)| d.share(idx[i]) - d.share(base[i])).collect()
    };
    let balance_excess = |idx: &[i32]| -> f64 {
        input
            .chosen
            .cells
            .iter()
            .filter_map(|p| {
                let t = input.target.get(p.cell)?;
                let shift: f64 = model
                    .dims
                    .iter()
                    .enumerate()
                    .map(|(i, d)| f64::from(idx[i] - base[i]) * d.setting.balance_per_click(p.cell))
                    .sum();
                Some(p.weight * t.excess(p.predicted_ua + Degrees::new(shift)))
            })
            .sum()
    };
    let base_excess = input.chosen.band_excess;
    // Gate a candidate; None when rejected (and counted).
    let gate = |idx: &[i32], plan: &mut DamperPlan| -> Option<Scored> {
        plan.candidates += 1;
        let d = shares(idx);
        if input.kerbs_unstable
            && model.dims.iter().enumerate().any(|(i, dim)| dim.setting.is_fast() && idx[i] > base[i])
        {
            plan.rejected_unstable += 1;
            return None;
        }
        if model.kerb_worse(&d) {
            plan.rejected_kerb += 1;
            return None;
        }
        let excess = balance_excess(idx);
        if excess > base_excess + 1e-9 && excess > 0.0 {
            plan.rejected_balance += 1;
            return None;
        }
        let (cm, cv) = model.change(&Metric::CONTACT, &d);
        let (km, kv) = model.change(&Metric::KERB, &d);
        let gain = -(cm + weight * km);
        let sd = (cv + weight * weight * kv).sqrt();
        let new = !input.main_changes
            && (0..idx.len()).any(|i| idx[i] != base[i] && model.tried[i] < EXPLORE_VALUES);
        Some(Scored { idx: idx.to_vec(), value: gain + if new { EXPLORE * sd } else { 0.0 }, gain, sd, new, contact: cm, kerb: km })
    };

    let mut singles: Vec<(usize, Scored)> = Vec::new();
    for &i in &movable {
        let d = model.dims[i];
        let r = d.reach();
        let steps: BTreeSet<i32> = [1, 2, r, -1, -2, -r].into_iter().filter(|s| s.abs() <= r).collect();
        for s in steps {
            let to = base[i] + s;
            if to < d.lo || to > d.hi {
                continue;
            }
            let mut idx = base.clone();
            idx[i] = to;
            if let Some(sc) = gate(&idx, &mut plan) {
                singles.push((i, sc));
            }
        }
    }
    singles.sort_by(|a, b| b.1.value.total_cmp(&a.1.value));
    let mut pool: Vec<Scored> = singles.iter().map(|(_, s)| s.clone()).collect();
    // Pairs of the best singles: at most MAX_MOVES settings move.
    let top: Vec<(usize, Scored)> = singles.iter().take(TOP_SINGLES).cloned().collect();
    for (a, (ia, sa)) in top.iter().enumerate() {
        for (ib, sb) in &top[a + 1..] {
            if ia == ib {
                continue;
            }
            let mut idx = sa.idx.clone();
            idx[*ib] = sb.idx[*ib];
            if let Some(sc) = gate(&idx, &mut plan) {
                pool.push(sc);
            }
        }
    }
    let ok = |s: &Scored| s.gain - 0.5 * s.sd >= MIN_GAIN || (s.new && s.value >= MIN_GAIN);
    let mut best = pool.iter().filter(|s| ok(s)).max_by(|a, b| a.value.total_cmp(&b.value)).cloned();

    // Kerbs flagged unstable: soften a fast damper even when the gain is uncertain.
    if input.kerbs_unstable {
        let softens_fast = |s: &Scored| model.dims.iter().enumerate().any(|(i, d)| d.setting.is_fast() && s.idx[i] < base[i]);
        if !best.as_ref().is_some_and(softens_fast) {
            best = singles
                .iter()
                .map(|(_, s)| s)
                .filter(|s| softens_fast(s))
                .max_by(|a, b| a.value.total_cmp(&b.value))
                .cloned()
                .or(best);
        }
    }

    if let Some(b) = best {
        plan.exploring = b.new && b.gain - 0.5 * b.sd < MIN_GAIN;
        plan.contact_change = b.contact;
        plan.kerb_change = b.kerb;
        plan.changes = (0..base.len())
            .filter(|&i| b.idx[i] != base[i])
            .map(|i| DamperChange {
                setting: model.dims[i].setting,
                keys: model.dims[i].keys.iter().map(|k| k.to_string()).collect(),
                from: base[i],
                to: b.idx[i],
            })
            .collect();
    }
    plan
}
