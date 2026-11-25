/* Lap segmentation from LMU event tables.
   `Lap` holds one row per change; row 0 is the counter at recording start, not
   a crossing, so the first segment (recording start → first crossing) and the
   last (last crossing → recording end) are partial.  A lap is in the pits when
   `In Pits` is non-zero anywhere in [start, end).  `Lap Dist` wraps to 0 at the
   line; it is unwrapped before interpolation and wrapped again so resampling
   never blends across the start/finish line.  The lap length is the median
   over wraps of (last value before the line + that sample's advance − first
   value after it); the largest sample falls short by up to one sample's
   travel.  */

use serde::{Deserialize, Serialize};

use crate::metrics::stats::interp;
use crate::telemetry::{TelemetryError, TelemetrySource, TimeSeries, CLOCK_CHANNEL};
use crate::units::Seconds;

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Lap {
    pub number: i32,
    pub start: Seconds,
    pub end: Seconds,
    // Both ends are real start/finish crossings.
    pub complete: bool,
    pub in_pits: bool,
}

impl Lap {
    #[inline]
    pub fn time(&self) -> Seconds {
        self.end - self.start
    }

    #[inline]
    pub fn is_flying(&self) -> bool {
        self.complete && !self.in_pits
    }

    #[inline]
    pub fn contains(&self, t: f64) -> bool {
        t >= self.start.raw() && t < self.end.raw()
    }
}

pub fn clock_span<S: TelemetrySource + ?Sized>(src: &S) -> Result<(Seconds, Seconds), TelemetryError> {
    let clock = src.scalar_channel(CLOCK_CHANNEL)?;
    match (clock.values.first(), clock.values.last()) {
        (Some(&a), Some(&b)) => Ok((Seconds::new(a), Seconds::new(b))),
        _ => Err(TelemetryError::MissingChannel(CLOCK_CHANNEL.to_string())),
    }
}

pub fn segment_laps<S: TelemetrySource + ?Sized>(src: &S) -> Result<Vec<Lap>, TelemetryError> {
    let (t_start, t_end) = clock_span(src)?;
    let lap = src.scalar_channel("Lap")?;
    let pits = src.scalar_channel("In Pits")?;
    if lap.is_empty() {
        return Ok(Vec::new());
    }

    let bounds: Vec<(Seconds, i32, bool)> = std::iter::once((t_start, lap.values[0] as i32, false))
        .chain(
            lap.times[1..]
                .iter()
                .zip(&lap.values[1..])
                .map(|(&t, &n)| (t, n as i32, true)),
        )
        .collect();

    Ok(bounds
        .iter()
        .enumerate()
        .filter_map(|(i, &(start, number, real_start))| {
            let (end, real_end) = bounds.get(i + 1).map_or((t_end, false), |b| (b.0, true));
            (end > start).then(|| Lap {
                number,
                start,
                end,
                complete: real_start && real_end,
                in_pits: any_nonzero(&pits, start, end),
            })
        })
        .collect())
}

fn any_nonzero(events: &TimeSeries<f64>, t0: Seconds, t1: Seconds) -> bool {
    if events.is_empty() {
        return false;
    }
    let at_start = events.times.partition_point(|&t| t <= t0);
    let state_at_start = at_start.checked_sub(1).map(|i| events.values[i] != 0.0);
    state_at_start.unwrap_or(false)
        || events
            .times
            .iter()
            .zip(&events.values)
            .any(|(&t, &v)| t >= t0 && t < t1 && v != 0.0)
}

// Lap distance (m) on `grid`, unwrapped across the line before interpolation.
pub fn lap_distance<S: TelemetrySource + ?Sized>(src: &S, grid: &[f64]) -> Result<Vec<f64>, TelemetryError> {
    let (resampled, lap_len) = unwrapped_lap_distance(src, grid)?;
    Ok(if lap_len > 0.0 {
        resampled.into_iter().map(|x| x.rem_euclid(lap_len)).collect()
    } else {
        resampled
    })
}

// Distance driven (m) on `grid`, continuous through the line, and the lap length.
pub fn unwrapped_lap_distance<S: TelemetrySource + ?Sized>(src: &S, grid: &[f64]) -> Result<(Vec<f64>, f64), TelemetryError> {
    let d = src.scalar_channel("Lap Dist")?;
    if d.len() < 2 {
        return Err(TelemetryError::MissingChannel("Lap Dist".to_string()));
    }
    let longest = d.values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let wraps = |w: &[f64]| w[1] - w[0] < -longest / 2.0;
    let mut at_wraps: Vec<f64> = d.values.windows(3).filter(|w| wraps(&w[1..])).map(|w| w[1] + (w[1] - w[0]) - w[2]).collect();
    at_wraps.sort_by(f64::total_cmp);
    let lap_len = crate::metrics::stats::median(&at_wraps).filter(|&l| l >= longest).unwrap_or(longest);
    let t: Vec<f64> = d.times.iter().map(|s| s.raw()).collect();
    let unwrapped: Vec<f64> = std::iter::once(0.0)
        .chain(d.values.windows(2).map(|w| f64::from(u8::from(wraps(w)))))
        .scan(0.0, |wraps, step| {
            *wraps += step;
            Some(*wraps)
        })
        .zip(&d.values)
        .map(|(wraps, &v)| v + wraps * lap_len)
        .collect();
    Ok((interp(grid, &t, &unwrapped, None), lap_len))
}
