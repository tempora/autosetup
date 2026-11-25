/* Driver screen: where each car's locked fingerprint sits on the three feel
   sliders, per car class.  A car reads against itself: its usual balance
   is the median understeer angle per cell over all its stints (any track),
   and each axis is the slider-weighted mean of fingerprint − usual over the
   cells both have (None when the axis covers none).  − is the slider's
   left end (Pointier, Looser, Fast looser).  `span` is a slider's end, so
   cars of a class draw to one scale.  */

use std::collections::BTreeMap;

use serde::Serialize;

use crate::chassis::{BalanceCell, CarClass};
use crate::lmu::builds::format_recording_time;
use crate::metrics::balance::CellStats;
use crate::metrics::stats::median;
use crate::opt::target::{FeelAxis, RANGE_STEPS};
use crate::store::{Store, StoreError, StoredStint};

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DriverCar {
    pub car_key: String,
    pub car_name: String,
    pub layout: String,
    pub created_at: String,
    pub front: Option<f64>,
    pub rear: Option<f64>,
    pub fast: Option<f64>,
    pub line: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DriverClass {
    pub car_class: String,
    pub laps: usize,
    pub span: f64,
    pub step: f64,
    pub cars: Vec<DriverCar>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Driver {
    pub classes: Vec<DriverClass>,
}

fn usual(stints: &[&StoredStint]) -> BTreeMap<BalanceCell, f64> {
    let mut by: BTreeMap<BalanceCell, Vec<f64>> = BTreeMap::new();
    for c in stints.iter().filter_map(|s| s.usable()).flat_map(|m| m.cells_off_kerb()) {
        by.entry(c.cell).or_default().push(c.ua.raw());
    }
    by.into_iter().filter_map(|(cell, mut v)| {
        v.sort_by(f64::total_cmp);
        median(&v).map(|m| (cell, m))
    }).collect()
}

// Each slider axis's position of `fp` against `usual`, deg.
pub fn axes(fp: &[CellStats], usual: &BTreeMap<BalanceCell, f64>) -> [Option<f64>; 3] {
    FeelAxis::ALL.map(|axis| {
        let (num, den) = fp
            .iter()
            .filter_map(|c| usual.get(&c.cell).map(|u| (axis.weight(c.cell), c.ua.raw() - u)))
            .filter(|(w, _)| *w > 0.0)
            .fold((0.0, 0.0), |(n, d), (w, x)| (n + w * x, d + w));
        (den > 0.0).then(|| num / den)
    })
}

// "Pointier front, safer rear": axes at least half a feel step from usual.
pub fn line(axes: &[Option<f64>; 3], step: f64) -> String {
    let words: Vec<String> = FeelAxis::ALL
        .iter()
        .zip(axes)
        .filter_map(|(axis, v)| {
            let v = v.filter(|v| v.abs() >= step / 2.0)?;
            let (neg, pos) = axis.ends();
            let place = match axis {
                FeelAxis::Front => "front",
                FeelAxis::Rear => "rear",
                FeelAxis::Fast => "in fast corners",
            };
            let word = if v < 0.0 { neg } else { pos };
            Some(format!("{} {place}", word.trim_start_matches("Fast ").to_lowercase()))
        })
        .collect();
    if words.is_empty() { "Close to its usual balance".into() } else { crate::opt::why::capitalise(&words.join(", ")) }
}

pub fn profile(store: &Store) -> Result<Driver, StoreError> {
    let stints = store.usable_stints()?;
    let mut by_class: BTreeMap<&str, Vec<&StoredStint>> = BTreeMap::new();
    for s in &stints {
        by_class.entry(s.meta.car_class.as_str()).or_default().push(s);
    }
    let mut classes = Vec::new();
    for (name, members) in by_class {
        let laps: usize = members.iter().filter_map(|s| s.usable()).map(|m| m.laps.iter().filter(|l| l.is_flying()).count()).sum();
        if laps == 0 {
            continue;
        }
        let step = CarClass::parse(name).feel_step().raw();
        let mut signatures: Vec<&str> = members.iter().map(|s| s.signature.as_str()).collect();
        signatures.sort_unstable();
        signatures.dedup();
        let mut cars = Vec::new();
        for sig in signatures {
            let Some(fp) = store.fingerprint(sig)? else { continue };
            let own: Vec<&StoredStint> = members.iter().copied().filter(|s| s.signature == sig).collect();
            let a = axes(&fp.cells, &usual(&own));
            cars.push(DriverCar {
                car_key: sig.to_string(),
                car_name: fp.car_name.clone(),
                layout: fp.layout.clone(),
                created_at: format_recording_time(fp.locked_at),
                front: a[0],
                rear: a[1],
                fast: a[2],
                line: line(&a, step),
            });
        }
        classes.push(DriverClass { car_class: name.to_string(), laps, span: RANGE_STEPS * step, step, cars });
    }
    classes.sort_by_key(|c| std::cmp::Reverse(c.laps));
    Ok(Driver { classes })
}
