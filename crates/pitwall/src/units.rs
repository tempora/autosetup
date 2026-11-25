use std::fmt;
use std::ops::{Add, Div, Mul, Neg, Sub};

use serde::{Deserialize, Serialize};

#[macro_export]
macro_rules! define_unit {
    ($name:ident, $suffix:literal) => {
        #[derive(Debug, Clone, Copy, Default, PartialEq, PartialOrd, Serialize, Deserialize)]
        #[repr(transparent)]
        pub struct $name(pub f64);

        impl $name {
            pub const ZERO: Self = Self(0.0);

            #[inline]
            pub const fn new(value: f64) -> Self {
                Self(value)
            }

            #[inline]
            pub const fn raw(self) -> f64 {
                self.0
            }

            #[inline]
            pub fn abs(self) -> Self {
                Self(self.0.abs())
            }

            #[inline]
            pub fn is_finite(self) -> bool {
                self.0.is_finite()
            }

            #[inline]
            pub fn clamp(self, lo: Self, hi: Self) -> Self {
                Self(self.0.clamp(lo.0, hi.0))
            }
        }

        impl Add for $name {
            type Output = Self;
            #[inline]
            fn add(self, rhs: Self) -> Self {
                Self(self.0 + rhs.0)
            }
        }

        impl Sub for $name {
            type Output = Self;
            #[inline]
            fn sub(self, rhs: Self) -> Self {
                Self(self.0 - rhs.0)
            }
        }

        impl Neg for $name {
            type Output = Self;
            #[inline]
            fn neg(self) -> Self {
                Self(-self.0)
            }
        }

        impl Mul<f64> for $name {
            type Output = Self;
            #[inline]
            fn mul(self, rhs: f64) -> Self {
                Self(self.0 * rhs)
            }
        }

        impl Mul<$name> for f64 {
            type Output = $name;
            #[inline]
            fn mul(self, rhs: $name) -> $name {
                $name(self * rhs.0)
            }
        }

        impl Div<f64> for $name {
            type Output = Self;
            #[inline]
            fn div(self, rhs: f64) -> Self {
                Self(self.0 / rhs)
            }
        }

        impl Div<$name> for $name {
            type Output = f64;
            #[inline]
            fn div(self, rhs: $name) -> f64 {
                self.0 / rhs.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, "{:.4} {}", self.0, $suffix)
            }
        }
    };
}

pub const GRAVITY: f64 = 9.80665;

define_unit!(Meters, "m");
define_unit!(Millimeters, "mm");
define_unit!(Seconds, "s");
define_unit!(MetersPerSec, "m/s");
define_unit!(MmPerSec, "mm/s"); // + bump, - rebound
define_unit!(Radians, "rad");
define_unit!(Degrees, "deg"); // understeer: + understeer, - oversteer
define_unit!(RadPerSec, "rad/s");
define_unit!(Gs, "g");
define_unit!(Newtons, "N");
define_unit!(Curvature, "1/m"); // + right, - left

impl From<Millimeters> for Meters {
    #[inline]
    fn from(mm: Millimeters) -> Self {
        Self(mm.0 * 1e-3)
    }
}

impl From<Meters> for Millimeters {
    #[inline]
    fn from(m: Meters) -> Self {
        Self(m.0 * 1e3)
    }
}

impl From<Degrees> for Radians {
    #[inline]
    fn from(d: Degrees) -> Self {
        Self(d.0.to_radians())
    }
}

impl From<Radians> for Degrees {
    #[inline]
    fn from(r: Radians) -> Self {
        Self(r.0.to_degrees())
    }
}

impl MetersPerSec {
    #[inline]
    pub fn from_kmh(kmh: f64) -> Self {
        Self(kmh / 3.6)
    }

    #[inline]
    pub fn to_kmh(self) -> f64 {
        self.0 * 3.6
    }
}

impl Gs {
    #[inline]
    pub fn from_mps2(a: f64) -> Self {
        Self(a / GRAVITY)
    }

    #[inline]
    pub fn to_mps2(self) -> f64 {
        self.0 * GRAVITY
    }
}
