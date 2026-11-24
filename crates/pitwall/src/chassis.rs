use serde::{Deserialize, Serialize};

use crate::units::{Meters, MetersPerSec, MmPerSec};

// Canonical LMU wheel order: FL, FR, RL, RR.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Wheel<T> {
    pub fl: T,
    pub fr: T,
    pub rl: T,
    pub rr: T,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Axle<T> {
    pub front: T,
    pub rear: T,
}

impl<T> Wheel<T> {
    #[inline]
    pub const fn new(fl: T, fr: T, rl: T, rr: T) -> Self {
        Self { fl, fr, rl, rr }
    }

    #[inline]
    pub fn from_array([fl, fr, rl, rr]: [T; 4]) -> Self {
        Self { fl, fr, rl, rr }
    }

    #[inline]
    pub fn into_array(self) -> [T; 4] {
        [self.fl, self.fr, self.rl, self.rr]
    }

    #[inline]
    pub fn as_array(&self) -> [&T; 4] {
        [&self.fl, &self.fr, &self.rl, &self.rr]
    }

    #[inline]
    pub fn map<U>(self, mut f: impl FnMut(T) -> U) -> Wheel<U> {
        Wheel {
            fl: f(self.fl),
            fr: f(self.fr),
            rl: f(self.rl),
            rr: f(self.rr),
        }
    }

    #[inline]
    pub fn zip_with<U, V>(self, other: Wheel<U>, mut f: impl FnMut(T, U) -> V) -> Wheel<V> {
        Wheel {
            fl: f(self.fl, other.fl),
            fr: f(self.fr, other.fr),
            rl: f(self.rl, other.rl),
            rr: f(self.rr, other.rr),
        }
    }

    #[inline]
    pub fn any(self, mut pred: impl FnMut(T) -> bool) -> bool {
        pred(self.fl) || pred(self.fr) || pred(self.rl) || pred(self.rr)
    }

    #[inline]
    pub fn iter(&self) -> impl Iterator<Item = &T> {
        self.as_array().into_iter()
    }
}

impl Wheel<f64> {
    #[inline]
    pub fn axle_means(self) -> Axle<f64> {
        Axle {
            front: 0.5 * (self.fl + self.fr),
            rear: 0.5 * (self.rl + self.rr),
        }
    }

    #[inline]
    pub fn mean(self) -> f64 {
        0.25 * (self.fl + self.fr + self.rl + self.rr)
    }
}

impl<T> Axle<T> {
    #[inline]
    pub const fn new(front: T, rear: T) -> Self {
        Self { front, rear }
    }

    #[inline]
    pub fn map<U>(self, mut f: impl FnMut(T) -> U) -> Axle<U> {
        Axle {
            front: f(self.front),
            rear: f(self.rear),
        }
    }

    #[inline]
    pub fn zip_with<U, V>(self, other: Axle<U>, mut f: impl FnMut(T, U) -> V) -> Axle<V> {
        Axle {
            front: f(self.front, other.front),
            rear: f(self.rear, other.rear),
        }
    }
}

impl Axle<f64> {
    #[inline]
    pub fn front_share(self) -> f64 {
        let sum = self.front + self.rear;
        if sum > 0.0 { self.front / sum } else { 0.5 }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CornerPhase {
    Braking,
    Entry,
    Mid,
    Exit,
}

impl CornerPhase {
    pub const ALL: [Self; 4] = [Self::Braking, Self::Entry, Self::Mid, Self::Exit];

    #[inline]
    pub const fn index(self) -> usize {
        self as usize
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SpeedBand {
    Low,
    High,
}

impl SpeedBand {
    pub const ALL: [Self; 2] = [Self::Low, Self::High];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct BalanceCell {
    pub phase: CornerPhase,
    pub band: SpeedBand,
}

impl BalanceCell {
    pub fn all() -> impl Iterator<Item = Self> {
        CornerPhase::ALL
            .into_iter()
            .flat_map(|phase| SpeedBand::ALL.into_iter().map(move |band| Self { phase, band }))
    }
}

// Turning direction: the sign of steering and of yaw rate (+ right).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Direction {
    Left,
    Right,
}

impl Direction {
    #[inline]
    pub fn from_sign(x: f64) -> Option<Self> {
        if x > 0.0 {
            Some(Self::Right)
        } else if x < 0.0 {
            Some(Self::Left)
        } else {
            None
        }
    }

    #[inline]
    pub const fn sign(self) -> f64 {
        match self {
            Self::Left => -1.0,
            Self::Right => 1.0,
        }
    }
}

// Samples split by whether the wheel was on a kerb (SurfaceTypes == 5).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct KerbSplit<T> {
    pub off: T,
    pub on: T,
}

impl<T> KerbSplit<T> {
    #[inline]
    pub fn get_mut(&mut self, on_kerb: bool) -> &mut T {
        if on_kerb {
            &mut self.on
        } else {
            &mut self.off
        }
    }
}

/* Car classes in v1.0 scope; everything else maps to Other with generic
   defaults.  Constants mirror classes.toml (band_split_kmh, damper_knee_mm_s)
   and the per-class wheelbase fallback used by the understeer angle.  */
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum CarClass {
    Gt3,
    Lmp2,
    Lmp2Elms,
    Hyper,
    Other,
}

impl CarClass {
    pub fn parse(raw: &str) -> Self {
        let s = raw.trim().to_ascii_uppercase();
        if s.starts_with("GT3") || s.starts_with("LMGT3") {
            Self::Gt3
        } else if s.starts_with("LMP2") && s.contains("ELMS") {
            Self::Lmp2Elms
        } else if s.starts_with("LMP2") {
            Self::Lmp2
        } else if s.starts_with("HYPER") || s.starts_with("LMH") || s.starts_with("LMDH") {
            Self::Hyper
        } else {
            Self::Other
        }
    }

    pub const fn wheelbase(self) -> Meters {
        Meters::new(match self {
            Self::Gt3 => 2.65,
            Self::Lmp2 | Self::Lmp2Elms => 3.00,
            Self::Hyper => 3.15,
            Self::Other => 2.80,
        })
    }

    // Apex speed separating low- from high-speed corners.
    pub fn band_split(self) -> MetersPerSec {
        MetersPerSec::from_kmh(match self {
            Self::Gt3 | Self::Other => 140.0,
            Self::Lmp2 | Self::Lmp2Elms | Self::Hyper => 150.0,
        })
    }

    // Shaft speed separating slow from fast damper circuits.
    pub const fn damper_knee(self) -> MmPerSec {
        MmPerSec::new(match self {
            Self::Hyper => 22.0,
            Self::Lmp2Elms => 40.0,
            Self::Gt3 | Self::Lmp2 | Self::Other => 50.0,
        })
    }
}
