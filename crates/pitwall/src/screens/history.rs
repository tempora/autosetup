/* Home, History and stint-list payloads.
   A session is one sitting with one car at one track: consecutive usable
   stints of the same car name and layout less than SESSION_GAP_S apart
   (end of one to start of the next).  Its id is its first stint's id.  A
   stint starts at its RecordingTime, else its file time minus its length.
   A lap is clean when flying and within CLEAN_SLOW_FACTOR of the stint's
   median flying lap; nothing here detects track-limit excursions, so
   off_track is always false.  */

use serde::Serialize;

use crate::lmu::builds::format_recording_time;
use crate::metrics::laps::Lap;
use crate::metrics::StintMetrics;
use crate::screens::names::{setup_diff, SetupChange};
use crate::store::{Rating, Store, StoreError, StoredStint};

pub const SESSION_GAP_S: i64 = 2 * 3600;
pub const CLEAN_SLOW_FACTOR: f64 = 1.03;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Session {
    pub id: i64,
    // "car name|layout".
    pub context: String,
    pub car_name: String,
    pub car_class: String,
    pub track: String,
    pub layout: String,
    pub recorded_at: Option<String>,
    pub stint_ids: Vec<i64>,
    // Flying laps.
    pub laps: usize,
    pub best: Option<f64>,
    pub stints: usize,
    // Car model from a saved setup; not learned yet.
    pub model: Option<String>,
}

pub fn context_of(s: &StoredStint) -> String {
    format!("{}|{}", s.meta.car_name, s.meta.layout)
}

fn span(m: &StintMetrics) -> f64 {
    match (m.laps.first(), m.laps.last()) {
        (Some(a), Some(b)) => (b.end - a.start).raw(),
        _ => 0.0,
    }
}

fn started(s: &StoredStint) -> i64 {
    s.meta.recorded_at.unwrap_or_else(|| s.file_mtime - s.usable().map_or(0.0, span) as i64)
}

fn flying(m: &StintMetrics) -> impl Iterator<Item = &Lap> {
    m.laps.iter().filter(|l| l.is_flying())
}

fn best_lap(m: &StintMetrics) -> Option<f64> {
    flying(m).map(|l| l.time().raw()).min_by(f64::total_cmp)
}

// Every session, newest first; `stints` are usable stints, oldest first.
pub fn sessions(stints: &[StoredStint]) -> Vec<Session> {
    let mut order: Vec<&StoredStint> = stints.iter().filter(|s| s.usable().is_some()).collect();
    order.sort_by_key(|s| (started(s), s.id));
    let mut out: Vec<(Session, i64)> = Vec::new();
    for s in order {
        let m = s.usable().expect("filtered to usable");
        let (start, context) = (started(s), context_of(s));
        let open = out.iter_mut().rev().find(|(x, _)| x.context == context).filter(|(_, end)| start - *end <= SESSION_GAP_S);
        let (cur, end) = match open {
            Some(c) => c,
            None => {
                out.push((
                    Session {
                        id: s.id,
                        context,
                        car_name: s.meta.car_name.clone(),
                        car_class: s.meta.car_class.clone(),
                        track: s.meta.track.clone(),
                        layout: s.meta.layout.clone(),
                        recorded_at: s.meta.recorded_at.map(format_recording_time),
                        stint_ids: Vec::new(),
                        laps: 0,
                        best: None,
                        stints: 0,
                        model: None,
                    },
                    start,
                ));
                out.last_mut().expect("just pushed")
            }
        };
        cur.stint_ids.push(s.id);
        cur.stints += 1;
        cur.laps += flying(m).count();
        cur.best = [cur.best, best_lap(m)].into_iter().flatten().min_by(f64::total_cmp);
        *end = (*end).max(start + span(m) as i64);
    }
    let mut sessions: Vec<Session> = out.into_iter().map(|(s, _)| s).collect();
    sessions.sort_by_key(|s| std::cmp::Reverse(*s.stint_ids.last().expect("a session has a stint")));
    sessions
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LapRow {
    pub seq: usize,
    pub lap: i32,
    pub time: f64,
    pub complete: bool,
    pub in_pits: bool,
    pub clean: bool,
    pub off_track: bool,
}

pub fn lap_rows(m: &StintMetrics) -> Vec<LapRow> {
    let mut times: Vec<f64> = flying(m).map(|l| l.time().raw()).collect();
    times.sort_by(f64::total_cmp);
    let limit = crate::metrics::stats::median(&times).map_or(0.0, |md| md * CLEAN_SLOW_FACTOR);
    m.laps
        .iter()
        .enumerate()
        .map(|(seq, l)| LapRow {
            seq,
            lap: l.number,
            time: l.time().raw(),
            complete: l.complete,
            in_pits: l.in_pits,
            clean: l.is_flying() && l.time().raw() <= limit,
            off_track: false,
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StintSummaryRow {
    pub laps: usize,
    pub racing_laps: usize,
    pub clean_laps: usize,
    pub best: Option<f64>,
    pub median: Option<f64>,
    // Standard deviation of clean laps (3 or more).
    pub consistency: Option<f64>,
}

fn summary_row(laps: &[LapRow]) -> StintSummaryRow {
    let clean: Vec<f64> = laps.iter().filter(|l| l.clean).map(|l| l.time).collect();
    let mean = clean.iter().sum::<f64>() / clean.len().max(1) as f64;
    let mut sorted = clean.clone();
    sorted.sort_by(f64::total_cmp);
    StintSummaryRow {
        laps: laps.len(),
        racing_laps: laps.iter().filter(|l| l.complete && !l.in_pits).count(),
        clean_laps: clean.len(),
        best: sorted.first().copied(),
        median: crate::metrics::stats::median(&sorted),
        consistency: (clean.len() >= 3).then(|| (clean.iter().map(|t| (t - mean).powi(2)).sum::<f64>() / (clean.len() - 1) as f64).sqrt()),
    }
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StintRow {
    pub id: i64,
    pub file_name: String,
    pub recorded_at: Option<String>,
    pub session_type: String,
    // Saved setup it matches; not learned yet.
    pub setup_name: Option<String>,
    pub laps: Vec<LapRow>,
    pub summary: StintSummaryRow,
    pub rating: Option<Rating>,
    // Against the previous stint of the same car and layout.
    pub changes: Vec<SetupChange>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Stints {
    pub context: String,
    pub session: i64,
    pub stints: Vec<StintRow>,
}

// The stints of the session that starts with stint `session_id`, oldest first.
pub fn session_stints(store: &Store, session_id: i64) -> Result<Option<Stints>, StoreError> {
    let all = store.usable_stints()?;
    let Some(session) = sessions(&all).into_iter().find(|s| s.id == session_id) else { return Ok(None) };
    let mut context: Vec<&StoredStint> = all.iter().filter(|s| context_of(s) == session.context).collect();
    context.sort_by_key(|s| (started(s), s.id));
    let stints = context
        .iter()
        .enumerate()
        .filter(|(_, s)| session.stint_ids.contains(&s.id))
        .map(|(i, s)| {
            let laps = lap_rows(s.usable().expect("usable"));
            Ok(StintRow {
                id: s.id,
                file_name: s.file_name.clone(),
                recorded_at: s.meta.recorded_at.map(format_recording_time),
                session_type: s.meta.session_type.clone(),
                setup_name: None,
                summary: summary_row(&laps),
                laps,
                rating: store.rating(&s.file_name)?,
                changes: i.checked_sub(1).map_or_else(Vec::new, |p| setup_diff(&context[p].meta.setup, &s.meta.setup)),
            })
        })
        .collect::<Result<_, StoreError>>()?;
    Ok(Some(Stints { context: session.context, session: session.id, stints }))
}

/* Every true sentence the Welcome screen may show about the driver's own
   laps, empty with no flying laps.  The caller picks one.  */
pub fn facts(stints: &[StoredStint]) -> Vec<String> {
    use std::collections::BTreeMap;
    let mut order: Vec<&StoredStint> = stints.iter().filter(|s| s.usable().is_some()).collect();
    order.sort_by_key(|s| (started(s), s.id));
    let (mut by_track, mut by_car): (BTreeMap<&str, usize>, BTreeMap<&str, usize>) = Default::default();
    let mut bests: BTreeMap<(&str, &str), Vec<f64>> = BTreeMap::new();
    for s in &order {
        let m = s.usable().expect("usable");
        let n = flying(m).count();
        if n == 0 {
            continue;
        }
        *by_track.entry(s.meta.layout.as_str()).or_default() += n;
        *by_car.entry(s.meta.car_name.as_str()).or_default() += n;
        if let Some(b) = lap_rows(m).iter().filter(|l| l.clean).map(|l| l.time).min_by(f64::total_cmp) {
            bests.entry((s.meta.car_name.as_str(), s.meta.layout.as_str())).or_default().push(b);
        }
    }
    let most = |m: &BTreeMap<&str, usize>| m.iter().max_by_key(|(k, n)| (**n, std::cmp::Reverse(**k))).map(|(k, n)| (k.to_string(), *n));
    let plural = |n: usize, w: &str| format!("{n} {w}{}", if n == 1 { "" } else { "s" });
    let mut out = Vec::new();
    let Some((track, n)) = most(&by_track) else { return out };
    out.push(format!("You've done {n} laps at {track}."));
    if let Some((car, n)) = most(&by_car).filter(|_| by_car.len() > 1) {
        out.push(format!("Your most driven car is the {car}: {n} laps."));
    }
    let total: usize = by_car.values().sum();
    out.push(format!("{total} laps so far, with {} at {}.", plural(by_car.len(), "car"), plural(by_track.len(), "track")));
    for ((_, layout), b) in &bests {
        let best = b.iter().copied().fold(f64::INFINITY, f64::min);
        if b.len() >= 2 && b[0] - best >= 0.2 {
            out.push(format!("At {layout} your best is {:.1} s quicker than in your first stint there.", b[0] - best));
        }
    }
    out
}
