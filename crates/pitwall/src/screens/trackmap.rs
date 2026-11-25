/* Track outlines and per-lap traces from the driver's own recordings.
   Coordinates are metres east (x) and north (y) of the outline's origin,
   the mean GPS position of the lap it was drawn from (equirectangular
   projection; fine at track scale).  The outline is the fastest flying lap
   recorded on a layout, resampled every STEP_M of lap distance and cached
   per layout with MAP_VERSION.  A trace resamples one lap every
   TRACE_STEP_M in the same frame, so two laps from any stints line up point
   by point.  Distance in a lap runs continuously through the line from the
   lap's own crossing: the `Lap Dist` wrap nearest the lap event (it can
   trail the event), so samples just before the line read negative and the
   outline interpolates across it from GPS samples either side of the lap.
   Speed is km/h as logged; pedals 0..1; steering is steering-wheel
   degrees.  */

use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::metrics::laps::{unwrapped_lap_distance, Lap};
use crate::metrics::signals::SteeringGeometry;
use crate::metrics::stats::interp;
use crate::screens::history::lap_rows;
use crate::store::{Store, StoreError, StoredStint};
use crate::telemetry::duckdb::{wal_of, DuckDbRecording};
use crate::telemetry::{TelemetryError, TelemetrySource, CLOCK_CHANNEL};
use crate::units::Seconds;

pub const STEP_M: f64 = 10.0;
pub const TRACE_STEP_M: f64 = 5.0;
pub const MAP_VERSION: u32 = 1;
const EARTH_R: f64 = 6_371_000.0;
const MIN_GPS_POINTS: usize = 20;
// GPS taken either side of the lap so the outline spans the line.
const MARGIN_S: f64 = 1.0;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrackMap {
    pub layout: String,
    pub length: f64,
    pub step_m: f64,
    // [lap distance, x, y], m.
    pub points: Vec<[f64; 3]>,
    // Lap distance where sectors 2 and 3 start.
    pub sectors: Vec<f64>,
    pub lap_time: f64,
    // [latitude, longitude] of (0, 0).
    pub origin: [f64; 2],
    pub version: u32,
}

fn round(v: f64, places: i32) -> f64 {
    let k = 10f64.powi(places);
    (v * k).round() / k
}

fn project(lat: f64, lon: f64, [lat0, lon0]: [f64; 2]) -> (f64, f64) {
    ((lon - lon0).to_radians() * EARTH_R * lat0.to_radians().cos(), (lat - lat0).to_radians() * EARTH_R)
}

fn has(src: &impl TelemetrySource, names: &[&str]) -> bool {
    names.iter().all(|n| src.channel_kind(n).is_some())
}

fn lap_length(src: &impl TelemetrySource) -> Result<f64, TelemetryError> {
    Ok(src.scalar_channel("Lap Dist")?.values.iter().copied().fold(0.0, f64::max))
}

// Distance from `lap`'s crossing of the line at `times`, m.
fn distance_in_lap(src: &impl TelemetrySource, lap: &Lap, times: &[f64]) -> Result<Vec<f64>, TelemetryError> {
    let (at_start, length) = unwrapped_lap_distance(src, &[lap.start.raw()])?;
    let line = if length > 0.0 { length * (at_start[0] / length).round() } else { 0.0 };
    Ok(unwrapped_lap_distance(src, times)?.0.into_iter().map(|d| d - line).collect())
}

// Points whose distance strictly increases, for interpolation by distance.
fn increasing(d: &[f64], v: &[f64]) -> (Vec<f64>, Vec<f64>) {
    let mut last = f64::NEG_INFINITY;
    d.iter()
        .zip(v)
        .filter(|(&x, _)| {
            let up = x > last;
            last = last.max(x);
            up
        })
        .map(|(&x, &y)| (x, y))
        .unzip()
}

// Samples of a channel inside [lap.start − margin, lap.end + margin).
fn around_lap(src: &impl TelemetrySource, name: &str, lap: &Lap, margin: f64) -> Result<(Vec<f64>, Vec<f64>), TelemetryError> {
    let s = src.scalar_channel(name)?;
    let (a, b) = (lap.start.raw() - margin, lap.end.raw() + margin);
    Ok(s.times.iter().zip(&s.values).filter(|(t, _)| t.raw() >= a && t.raw() < b).map(|(t, &v)| (t.raw(), v)).unzip())
}

pub fn build_track_map(src: &impl TelemetrySource, laps: &[Lap], layout: &str) -> Result<Option<TrackMap>, TelemetryError> {
    let Some(lap) = laps.iter().filter(|l| l.is_flying()).min_by(|a, b| a.time().raw().total_cmp(&b.time().raw())) else { return Ok(None) };
    if !has(src, &["GPS Latitude", "GPS Longitude", "Lap Dist"]) {
        return Ok(None);
    }
    let (t, lat) = around_lap(src, "GPS Latitude", lap, MARGIN_S)?;
    let (_, lon) = around_lap(src, "GPS Longitude", lap, MARGIN_S)?;
    if t.len() < MIN_GPS_POINTS || lon.len() != t.len() {
        return Ok(None);
    }
    let length = lap_length(src)?;
    let d = distance_in_lap(src, lap, &t)?;
    let origin = [lat.iter().sum::<f64>() / lat.len() as f64, lon.iter().sum::<f64>() / lon.len() as f64];
    let (x, y): (Vec<f64>, Vec<f64>) = lat.iter().zip(&lon).map(|(&a, &o)| project(a, o, origin)).unzip();
    let grid: Vec<f64> = (0..).map(|i| f64::from(i) * STEP_M).take_while(|&g| g < length).collect();
    let (dx, xs) = increasing(&d, &x);
    let (_, ys) = increasing(&d, &y);
    let (gx, gy) = (interp(&grid, &dx, &xs, None), interp(&grid, &dx, &ys, None));
    let sectors = if src.channel_kind("Current Sector").is_some() {
        let ev = src.scalar_channel("Current Sector")?;
        // Entering sector 2, then sector 3 (logged as 0).
        [2.0, 0.0]
            .iter()
            .filter_map(|&target| {
                let hit = ev.times.iter().zip(&ev.values).find(|(ts, &v)| ts.raw() > lap.start.raw() && ts.raw() < lap.end.raw() && v == target)?;
                Some(round(interp(&[hit.0.raw()], &t, &d, None)[0], 1))
            })
            .collect()
    } else {
        Vec::new()
    };
    Ok(Some(TrackMap {
        layout: layout.to_string(),
        length: round(length, 1),
        step_m: STEP_M,
        points: grid.iter().zip(gx).zip(gy).map(|((&g, x), y)| [round(g, 1), round(x, 1), round(y, 1)]).collect(),
        sectors,
        lap_time: lap.time().raw(),
        origin,
        version: MAP_VERSION,
    }))
}

/* The layout's outline: cached, else built from the stored stint holding
   its fastest flying lap whose recording is in `telemetry_dir` and not
   being written.  */
pub fn track_map(store: &Store, layout: &str, telemetry_dir: &Path) -> Result<Option<TrackMap>, StoreError> {
    if let Some(json) = store.track_map_json(layout)? {
        if let Ok(map) = serde_json::from_str::<TrackMap>(&json) {
            if map.version == MAP_VERSION {
                return Ok(Some(map));
            }
        }
    }
    let mut candidates: Vec<(f64, StoredStint)> = store
        .usable_stints()?
        .into_iter()
        .filter(|s| s.meta.layout == layout)
        .filter_map(|s| {
            let best = s.usable()?.laps.iter().filter(|l| l.is_flying()).map(|l| l.time().raw()).min_by(f64::total_cmp)?;
            Some((best, s))
        })
        .collect();
    candidates.sort_by(|a, b| a.0.total_cmp(&b.0));
    for (_, s) in candidates {
        let path = telemetry_dir.join(&s.file_name);
        if !path.is_file() || wal_of(&path).exists() {
            continue;
        }
        let rec = match DuckDbRecording::open_copy(&path) {
            Ok(r) => r,
            Err(TelemetryError::Busy(_)) => continue,
            Err(e) => return Err(e.into()),
        };
        if let Some(map) = build_track_map(&rec, &s.usable().expect("usable").laps, layout)? {
            store.put_track_map_json(layout, &s.file_name, &serde_json::to_string(&map)?)?;
            return Ok(Some(map));
        }
    }
    Ok(None)
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LapTrace {
    pub step_m: f64,
    pub distance: Vec<f64>,
    // Seconds into the lap.
    pub time: Vec<f64>,
    pub speed: Vec<Option<f64>>,
    pub throttle: Vec<Option<f64>>,
    pub brake: Vec<Option<f64>>,
    pub steer: Vec<Option<f64>>,
    pub gear: Vec<Option<i32>>,
    // Metres from the track centre, and from the centre to the edge.
    pub plat: Vec<Option<f64>>,
    pub edge: Vec<Option<f64>>,
    pub x: Vec<Option<f64>>,
    pub y: Vec<Option<f64>>,
    // Sector times, when the map has sectors.
    pub sectors: Vec<f64>,
}

// A pedal channel's scale to 0..1: logged in percent in newer files.
fn pedal_scale(src: &impl TelemetrySource, name: &str, values: &[f64]) -> f64 {
    if src.channel_unit(name) == Some("%") || values.iter().any(|v| v.abs() > 1.5) {
        0.01
    } else {
        1.0
    }
}

pub fn lap_trace(src: &impl TelemetrySource, lap: &Lap, map: Option<&TrackMap>, geometry: &SteeringGeometry) -> Result<LapTrace, TelemetryError> {
    let clock: Vec<f64> = src.scalar_channel(CLOCK_CHANNEL)?.values.into_iter().filter(|&t| lap.contains(t)).collect();
    let d = distance_in_lap(src, lap, &clock)?;
    let (d, tc) = increasing(&d, &clock);
    let end = d.last().copied().unwrap_or(0.0);
    let grid: Vec<f64> = (0..).map(|i| f64::from(i) * TRACE_STEP_M).take_while(|&g| g < end.max(TRACE_STEP_M)).collect();
    let t_at = interp(&grid, &d, &tc, None);
    let none = || vec![None; grid.len()];
    let chan = |name: &str, scale: Option<f64>, places: i32| -> Result<Vec<Option<f64>>, TelemetryError> {
        if src.channel_kind(name).is_none() {
            return Ok(none());
        }
        let s = src.scalar_channel(name)?;
        let k = scale.unwrap_or_else(|| pedal_scale(src, name, &s.values));
        let ts: Vec<f64> = s.times.iter().map(|t| t.raw()).collect();
        Ok(interp(&t_at, &ts, &s.values, None).into_iter().map(|v| Some(round(v * k, places))).collect())
    };
    let steer_k = {
        let pct = src.channel_kind("Steering Pos").is_some() && {
            let s = src.scalar_channel("Steering Pos")?;
            pedal_scale(src, "Steering Pos", &s.values) < 1.0
        };
        (if pct { 0.01 } else { 1.0 }) * geometry.half_lock.raw()
    };
    let gear = if src.channel_kind("Gear").is_some() {
        let s = src.scalar_channel("Gear")?;
        let grid_t: Vec<Seconds> = t_at.iter().copied().map(Seconds::new).collect();
        s.sample_zoh(&grid_t).into_iter().map(|g| Some(g as i32)).collect()
    } else {
        vec![None; grid.len()]
    };
    let (x, y) = match map {
        Some(m) if has(src, &["GPS Latitude", "GPS Longitude"]) => {
            let (lat, lon) = (src.scalar_channel("GPS Latitude")?, src.scalar_channel("GPS Longitude")?);
            let tg: Vec<f64> = lat.times.iter().map(|t| t.raw()).collect();
            let (px, py): (Vec<f64>, Vec<f64>) = lat.values.iter().zip(&lon.values).map(|(&a, &o)| project(a, o, m.origin)).unzip();
            let r = |v: Vec<f64>| v.into_iter().map(|q| Some(round(q, 2))).collect::<Vec<_>>();
            (r(interp(&t_at, &tg, &px, None)), r(interp(&t_at, &tg, &py, None)))
        }
        _ => (none(), none()),
    };
    let rel: Vec<f64> = t_at.iter().map(|t| t - lap.start.raw()).collect();
    let sectors = match map {
        Some(m) if !m.sectors.is_empty() => {
            let marks: Vec<f64> = m.sectors.iter().filter(|&&b| b < end).map(|&b| interp(&[b], &grid, &rel, None)[0]).collect();
            let bounds: Vec<f64> = std::iter::once(0.0).chain(marks).chain([lap.time().raw()]).collect();
            bounds.windows(2).map(|w| round(w[1] - w[0], 3)).collect()
        }
        _ => Vec::new(),
    };
    Ok(LapTrace {
        step_m: TRACE_STEP_M,
        distance: grid.iter().map(|&g| round(g, 1)).collect(),
        time: rel.iter().map(|&t| round(t, 3)).collect(),
        speed: chan("Ground Speed", Some(1.0), 3)?,
        throttle: chan("Throttle Pos", None, 3)?,
        brake: chan("Brake Pos", None, 3)?,
        steer: chan("Steering Pos", Some(steer_k), 1)?,
        gear,
        plat: chan("Path Lateral", Some(1.0), 3)?,
        edge: chan("Track Edge", Some(1.0), 3)?,
        x,
        y,
        sectors,
    })
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LapPayload {
    pub stint: i64,
    pub seq: usize,
    pub lap: i32,
    pub time: f64,
    pub clean: bool,
    pub off_track: bool,
    pub setup_name: Option<String>,
    pub trace: LapTrace,
}

// One lap of a stored stint, traced from its recording in `telemetry_dir`.
pub fn lap(store: &Store, telemetry_dir: &Path, stint_id: i64, seq: usize) -> Result<Option<LapPayload>, StoreError> {
    let Some(s) = store.stint_by_id(stint_id)? else { return Ok(None) };
    let Some(m) = s.usable() else { return Ok(None) };
    let (Some(l), Some(row)) = (m.laps.get(seq), lap_rows(m).into_iter().nth(seq)) else { return Ok(None) };
    let map = track_map(store, &s.meta.layout, telemetry_dir)?;
    let rec = DuckDbRecording::open_copy(telemetry_dir.join(&s.file_name))?;
    let trace = lap_trace(&rec, l, map.as_ref(), &m.geometry)?;
    Ok(Some(LapPayload { stint: s.id, seq, lap: row.lap, time: row.time, clean: row.clean, off_track: row.off_track, setup_name: None, trace }))
}
