use serde::{Deserialize, Serialize};

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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CornerPhase {
    Braking,
    Entry,
    Mid,
    Exit,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SpeedBand {
    Low,
    High,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BalanceCell {
    pub phase: CornerPhase,
    pub band: SpeedBand,
}
