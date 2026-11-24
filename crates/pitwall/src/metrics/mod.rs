/* Per-stint telemetry metrics from one LMU recording.
   Pipeline: laps → 50 Hz Signals → corner map → passes → PhaseObs per
   corner phase → CellStats per BalanceCell, alongside native-rate suspension
   histograms and kerb events split strictly by SurfaceTypes.

   Passes come from every non-pit lap, partial laps included; a pass whose
   mean speed is under SLOW_PASS of that corner's median pass (traffic, a
   mistake, an out-lap) is dropped.  Recordings under MIN_STINT_S (a garage
   blip) are TooShort.  Missing required channels are errors:
   no metric silently degrades to a fallback source.  */

pub mod balance;
pub mod corners;
pub mod laps;
pub mod signals;
pub mod stats;
pub mod summary;
pub mod suspension;

#[cfg(all(test, feature = "duckdb-native"))]
mod tests;

use serde::Serialize;

use crate::chassis::CarClass;
use crate::telemetry::{check_channel_rates, SessionMeta, TelemetryError, TelemetrySource};
use balance::{aggregate, observe, CellStats, PhaseObs, SampleBalance};
use corners::{corner_map, passes, Corner};
use laps::{segment_laps, Lap};
use signals::{Signals, SteeringGeometry};
use suspension::{damper_metrics, kerb_events, summarise_kerbs, DamperMetrics, KerbEvent, KerbSummary, Suspension};

const SLOW_PASS: f64 = 0.9;
const MIN_STINT_S: f64 = 10.0;

#[derive(Debug, Clone, Serialize)]
pub struct StintMetrics {
    pub class: CarClass,
    pub geometry: SteeringGeometry,
    pub laps: Vec<Lap>,
    pub corners: Vec<Corner>,
    pub observations: Vec<PhaseObs>,
    pub cells: Vec<CellStats>,
    pub dampers: DamperMetrics,
    pub kerb_events: Vec<KerbEvent>,
    pub kerbs: KerbSummary,
    pub summary: summary::StintSummary,
}

impl StintMetrics {
    // Cells pooled from passes that never touched a kerb.
    pub fn cells_off_kerb(&self) -> Vec<CellStats> {
        aggregate(self.observations.iter().filter(|o| !o.kerb))
    }
}

pub fn analyse_stint<S: TelemetrySource + ?Sized>(src: &S) -> Result<StintMetrics, TelemetryError> {
    let meta: SessionMeta = src.metadata()?;
    let class = CarClass::parse(&meta.car_class);
    let geometry = SteeringGeometry::from_setup(&meta.setup, class);
    let (t0, t1) = laps::clock_span(src)?;
    if (t1 - t0).raw() < MIN_STINT_S {
        return Err(TelemetryError::TooShort((t1 - t0).raw()));
    }
    check_channel_rates(src)?;
    let laps = segment_laps(src)?;
    let sig = Signals::load(src)?;
    let susp = Suspension::load(src, &sig)?;

    let per = SampleBalance::compute(&sig, &geometry);
    let kerb_grid = susp.kerb_on_grid(&sig.t);
    let corners = corner_map(&sig, &laps);

    let raw: Vec<(PhaseObs, f64)> = laps
        .iter()
        .enumerate()
        .filter(|(_, l)| !l.in_pits)
        .flat_map(|(seq, l)| {
            let range = sig.range(l.start, l.end);
            passes(&sig, range, &corners, class.band_split())
                .into_iter()
                .flat_map(|p| {
                    let speed = stats::mean(&sig.speed[p.idx.clone()]).unwrap_or(0.0);
                    observe(&sig, &per, &p, seq, &kerb_grid).into_iter().map(move |o| (o, speed))
                })
                .collect::<Vec<_>>()
        })
        .collect();
    let usual: Vec<f64> = (0..corners.len())
        .map(|c| stats::median(&raw.iter().filter(|(o, _)| o.corner == c).map(|(_, v)| *v).collect::<Vec<_>>()).unwrap_or(0.0))
        .collect();
    let observations: Vec<PhaseObs> = raw
        .into_iter()
        .filter(|(o, v)| *v >= SLOW_PASS * usual[o.corner])
        .map(|(o, _)| o)
        .collect();

    let kerb_events = kerb_events(&susp, &sig, &laps, &per.yaw_error(&sig), geometry.wheelbase);
    Ok(StintMetrics {
        class,
        geometry,
        cells: aggregate(&observations),
        dampers: damper_metrics(&susp, &laps, class.damper_knee()),
        kerbs: summarise_kerbs(&kerb_events),
        summary: summary::summarise(&sig, &per, &laps, &meta.setup),
        kerb_events,
        laps,
        corners,
        observations,
    })
}
