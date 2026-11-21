/* LMU telemetry abstraction and resampling primitives.
   ChannelKind distinguishes fixed-rate tables from ts-indexed event tables
   from schema metadata.  Discrete channels use sample_zoh; continuous
   channels use sample_linear.  */

#[cfg(feature = "duckdb-native")]
pub mod duckdb;

use thiserror::Error;

use crate::chassis::Wheel;
use crate::setup::CarSetup;
use crate::units::Seconds;

pub const KERB_SURFACE_CODE: u8 = 5;

#[derive(Debug, Error)]
pub enum TelemetryError {
    #[error("missing required telemetry channel: {0}")]
    MissingChannel(String),
    #[error("unexpected column count for channel {channel}: expected {expected}, got {actual}")]
    BadArity {
        channel: String,
        expected: usize,
        actual: usize,
    },
    #[error("backend error: {0}")]
    Backend(String),
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ChannelKind {
    FixedRate { hz: f64 },
    EventTable,
}

#[derive(Debug, Clone)]
pub struct TimeSeries<T> {
    pub times: Vec<Seconds>,
    pub values: Vec<T>,
}

impl<T: Copy> TimeSeries<T> {
    pub fn len(&self) -> usize {
        self.times.len().min(self.values.len())
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    // Zero-order hold onto target_times; requires non-decreasing self.times.
    pub fn sample_zoh(&self, target_times: &[Seconds]) -> Vec<T> {
        debug_assert!(!self.is_empty());
        let last = self.len() - 1;
        target_times
            .iter()
            .map(|&t| {
                let idx = self.times.partition_point(|&ts| ts <= t);
                self.values[idx.saturating_sub(1).min(last)]
            })
            .collect()
    }
}

impl TimeSeries<f64> {
    // Piecewise-linear resampling with endpoint clamping; requires increasing self.times.
    pub fn sample_linear(&self, target_times: &[Seconds]) -> Vec<f64> {
        debug_assert!(!self.is_empty());
        let last = self.len() - 1;
        target_times
            .iter()
            .map(|&t| {
                let hi = self.times.partition_point(|&ts| ts < t);
                if hi == 0 {
                    return self.values[0];
                }
                if hi > last {
                    return self.values[last];
                }
                let lo = hi - 1;
                let dt = (self.times[hi] - self.times[lo]).raw();
                if dt <= 0.0 {
                    self.values[lo]
                } else {
                    let frac = (t - self.times[lo]).raw() / dt;
                    self.values[lo] + frac * (self.values[hi] - self.values[lo])
                }
            })
            .collect()
    }
}

#[derive(Debug, Clone)]
pub struct SessionMeta {
    pub track: String,
    pub car_name: String,
    pub car_class: String,
    pub session_type: String,
    pub setup: CarSetup,
}

pub trait TelemetrySource {
    fn metadata(&self) -> Result<SessionMeta, TelemetryError>;
    fn channel_kind(&self, name: &str) -> Option<ChannelKind>;
    fn scalar_channel(&self, name: &str) -> Result<TimeSeries<f64>, TelemetryError>;
    fn wheel_channel(&self, name: &str) -> Result<TimeSeries<Wheel<f64>>, TelemetryError>;
    fn wheel_surface(&self) -> Result<TimeSeries<Wheel<u8>>, TelemetryError>;
}

pub fn count_kerb_events(times: &[Seconds], mask: &[Wheel<bool>], merge_gap: Seconds) -> usize {
    let mut events = 0usize;
    let mut in_run = false;
    let mut last_hit: Option<Seconds> = None;

    for (&t, &w) in times.iter().zip(mask.iter()) {
        if w.any(|b| b) {
            if !in_run {
                let merged = last_hit
                    .map(|prev| (t - prev) <= merge_gap)
                    .unwrap_or(false);
                if !merged {
                    events += 1;
                }
                in_run = true;
            }
            last_hit = Some(t);
        } else {
            in_run = false;
        }
    }
    events
}
