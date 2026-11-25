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
pub const CLOCK_CHANNEL: &str = "GPS Time";

// Minimum logging rates (Hz) for vital fixed-rate channels.
pub const MIN_CHANNEL_RATES: &[(&str, f64)] = &[
    (CLOCK_CHANNEL, 50.0),
    ("Ground Speed", 50.0),
    ("Steering Pos", 50.0),
    ("Throttle Pos", 25.0),
    ("Brake Pos", 25.0),
    ("GPS Latitude", 10.0),
    ("GPS Longitude", 10.0),
    ("Lap Dist", 10.0),
    ("RideHeights", 50.0),
    ("Susp Pos", 50.0),
    ("SurfaceTypes", 5.0),
    ("TyresTempLeft", 5.0),
    ("TyresTempCentre", 5.0),
    ("TyresTempRight", 5.0),
];

#[derive(Debug, Error)]
pub enum TelemetryError {
    #[error("missing required telemetry channel: {0}")]
    MissingChannel(String),
    #[error("telemetry channel {channel} logged at {actual_hz:.1} Hz (minimum {min_hz:.0} Hz required)")]
    ChannelTooSlow {
        channel: String,
        min_hz: f64,
        actual_hz: f64,
    },
    #[error("unexpected column count for channel {channel}: expected {expected}, got {actual}")]
    BadArity {
        channel: String,
        expected: usize,
        actual: usize,
    },
    #[error("recording too short to analyse: {0:.2} s")]
    TooShort(f64),
    #[error("backend error: {0}")]
    Backend(String),
}

// Verifies every vital channel is present and logged at or above its minimum rate.
pub fn check_channel_rates<S: TelemetrySource + ?Sized>(src: &S) -> Result<(), TelemetryError> {
    for &(name, min_hz) in MIN_CHANNEL_RATES {
        match src.channel_kind(name) {
            None => return Err(TelemetryError::MissingChannel(name.to_string())),
            Some(ChannelKind::FixedRate { hz }) if hz + 1e-6 < min_hz => {
                return Err(TelemetryError::ChannelTooSlow {
                    channel: name.to_string(),
                    min_hz,
                    actual_hz: hz,
                });
            }
            Some(_) => {}
        }
    }
    Ok(())
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

// Linear blend between two samples; implemented for every continuous sample type.
pub trait Lerp: Copy {
    fn lerp(a: Self, b: Self, frac: f64) -> Self;
}

impl Lerp for f64 {
    #[inline]
    fn lerp(a: Self, b: Self, frac: f64) -> Self {
        a + frac * (b - a)
    }
}

impl<T: Lerp> Lerp for Wheel<T> {
    #[inline]
    fn lerp(a: Self, b: Self, frac: f64) -> Self {
        a.zip_with(b, |x, y| T::lerp(x, y, frac))
    }
}

impl<T: Lerp> TimeSeries<T> {
    // Piecewise-linear resampling with endpoint clamping; requires increasing self.times.
    pub fn sample_linear(&self, target_times: &[Seconds]) -> Vec<T> {
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
                    T::lerp(self.values[lo], self.values[hi], (t - self.times[lo]).raw() / dt)
                }
            })
            .collect()
    }
}

#[derive(Debug, Clone)]
pub struct SessionMeta {
    pub track: String,
    // TrackLayout, falling back to TrackName.
    pub layout: String,
    pub car_name: String,
    pub car_class: String,
    pub session_type: String,
    // RecordingTime as Unix seconds (UTC); None when absent or malformed.
    pub recorded_at: Option<i64>,
    pub setup: CarSetup,
}

pub trait TelemetrySource {
    fn metadata(&self) -> Result<SessionMeta, TelemetryError>;
    fn channel_kind(&self, name: &str) -> Option<ChannelKind>;
    // Unit string from channelsList; None for event tables and unknown channels.
    fn channel_unit(&self, name: &str) -> Option<&str>;
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
