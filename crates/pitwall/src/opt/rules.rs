/* Deterministic rules for settings with a clear target, outside the search:
     tyres     pressure per wheel from the tread crown; pressure only ever
               comes down.  Camber per axle from the inner−outer spread.
     brakes    ducts per axle from peak disc temperature; regen at maximum on
               classes that always harvest it; brake pressure by feel on cars
               without ABS inside the class's comfortable range (down a step
               for flagged lock-ups).
     platform  GT3 front ride height at its minimum, always; otherwise a ride
               height up (or, on prototypes barely using their third packers,
               a packer step) when an axle bottoms or runs under the minimum.
   Each key moves once per proposal; locked keys never move.  Index direction
   is taken as label direction (a higher index is more pressure, less
   negative camber, more closed ducts, higher ride height) until per-car
   click-to-label scales exist.  */

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::chassis::CarClass;
use crate::metrics::summary::StintSummary;
use crate::opt::solve::{AeroTrim, DriverFlags};
use crate::setup::CarSetup;

const CROWN_TARGET: f64 = 5.0;
const CROWN_BAND: f64 = 3.0;
const BOTTOMING_S: f64 = 0.2;
const MIN_RIDE_HEIGHT_MM: f64 = 2.0;
const THIRD_PACKERS_LO: f64 = 0.05;
const MAX_PLATFORM_CHANGES: usize = 1;
const BRAKE_PRESSURE_STEP: i32 = 2;
const WING_TRIM_STEP: i32 = 2;
const FRONT_RH: [&str; 2] = ["WM_RIDEHEIGHT-W_FL", "WM_RIDEHEIGHT-W_FR"];
const REAR_RH: [&str; 2] = ["WM_RIDEHEIGHT-W_RL", "WM_RIDEHEIGHT-W_RR"];

// Per-class rule targets (classes.toml).
struct ClassRules {
    spread_front: (f64, f64),
    spread_rear: (f64, f64),
    brake_temp_front: (f64, f64),
    brake_temp_rear: (f64, f64),
    brake_pressure_pct: Option<(f64, f64)>,
    regen_at_max: bool,
}

impl ClassRules {
    const fn of(class: CarClass) -> Self {
        match class {
            CarClass::Hyper => Self {
                spread_front: (3.0, 10.0),
                spread_rear: (3.0, 12.0),
                brake_temp_front: (380.0, 1000.0),
                brake_temp_rear: (300.0, 1000.0),
                brake_pressure_pct: Some((93.0, 100.0)),
                regen_at_max: true,
            },
            CarClass::Lmp2 | CarClass::Lmp2Elms => Self {
                spread_front: (5.0, 12.0),
                spread_rear: (6.0, 13.0),
                brake_temp_front: (400.0, 1000.0),
                brake_temp_rear: (400.0, 1000.0),
                brake_pressure_pct: Some((90.0, 94.0)),
                regen_at_max: false,
            },
            CarClass::Gt3 | CarClass::Other => Self {
                spread_front: (5.0, 12.0),
                spread_rear: (6.0, 13.0),
                brake_temp_front: (450.0, 850.0),
                brake_temp_rear: (450.0, 850.0),
                brake_pressure_pct: None,
                regen_at_max: false,
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Rule {
    AeroTrim,
    TyrePressure,
    Camber,
    BrakeDucts,
    Regen,
    BrakePressure,
    FrontRideHeightAtMin,
    RideHeight,
    ThirdPackers,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RuleChange {
    pub rule: Rule,
    pub keys: Vec<String>,
    pub from: i32,
    pub to: i32,
    pub reason: String,
}

pub struct RuleInput<'a> {
    pub setup: &'a CarSetup,
    pub class: CarClass,
    pub flags: &'a DriverFlags,
    pub locks: &'a BTreeSet<String>,
    pub summary: &'a StintSummary,
}

struct Builder<'a> {
    setup: &'a CarSetup,
    locks: &'a BTreeSet<String>,
    out: Vec<RuleChange>,
}

impl Builder<'_> {
    fn usable(&self, key: &str) -> bool {
        self.setup.params.get(key).is_some_and(|p| p.adjustable()) && !self.locks.contains(key)
    }

    // Moves `keys` together by `delta` clicks (clamped); false when nothing moves.
    fn add(&mut self, rule: Rule, keys: &[&str], delta: i32, reason: String) -> bool {
        if keys.is_empty()
            || !keys.iter().all(|k| self.usable(k))
            || self.out.iter().any(|c| c.keys.iter().any(|k| keys.contains(&k.as_str())))
        {
            return false;
        }
        let p = &self.setup.params[keys[0]];
        let to = (p.value + delta).clamp(p.min, p.max_exclusive - 1);
        if to == p.value {
            return false;
        }
        self.out.push(RuleChange { rule, keys: keys.iter().map(|k| k.to_string()).collect(), from: p.value, to, reason });
        true
    }

    fn move_to(&mut self, rule: Rule, keys: &[&str], index: i32, reason: String) -> bool {
        let Some(p) = keys.first().and_then(|k| self.setup.params.get(*k)) else { return false };
        self.add(rule, keys, index - p.value, reason)
    }
}

const WHEEL_KEYS: [&str; 4] = ["FL", "FR", "RL", "RR"];

fn tyres(b: &mut Builder, s: &StintSummary, k: &ClassRules) {
    if let Some(crown) = s.t_crown {
        let crowns = crown.into_array();
        for (axle, wheels) in [("front", [0usize, 1]), ("rear", [2, 3])] {
            let mut by_steps: Vec<(i32, Vec<usize>)> = Vec::new();
            for w in wheels {
                let c = crowns[w];
                if c > CROWN_TARGET + CROWN_BAND {
                    let steps = (((c - CROWN_TARGET).abs() - CROWN_BAND) / 1.5).ceil().min(3.0) as i32;
                    match by_steps.iter_mut().find(|(n, _)| *n == steps) {
                        Some((_, ws)) => ws.push(w),
                        None => by_steps.push((steps, vec![w])),
                    }
                }
            }
            for (steps, ws) in by_steps {
                let keys: Vec<String> = ws.iter().map(|&w| format!("WM_PRESSURE-W_{}", WHEEL_KEYS[w])).collect();
                let refs: Vec<&str> = keys.iter().map(String::as_str).collect();
                let avg = ws.iter().map(|&w| crowns[w]).sum::<f64>() / ws.len() as f64;
                b.add(
                    Rule::TyrePressure,
                    &refs,
                    -steps,
                    format!(
                        "{axle} tread centre {avg:+.1} °C against the edges (normal {:.0} to {:.0}) → pressure down",
                        CROWN_TARGET - CROWN_BAND,
                        CROWN_TARGET + CROWN_BAND
                    ),
                );
            }
        }
    }
    if let Some(spread) = s.t_spread {
        for (axle, (a, c), (lo, hi), keys) in [
            ("Front", (spread.fl, spread.fr), k.spread_front, ["WM_CAMBER-W_FL", "WM_CAMBER-W_FR"]),
            ("Rear", (spread.rl, spread.rr), k.spread_rear, ["WM_CAMBER-W_RL", "WM_CAMBER-W_RR"]),
        ] {
            let v = 0.5 * (a + c);
            if (lo..=hi).contains(&v) {
                continue;
            }
            let less = v > hi;
            let steps = if (v - if less { hi } else { lo }).abs() > 3.0 { 2 } else { 1 };
            b.add(
                Rule::Camber,
                &keys,
                if less { steps } else { -steps },
                format!(
                    "{axle} inner edge {v:.1} °C hotter than outer (target {lo:.0} to {hi:.0}) → {} negative camber",
                    if less { "less" } else { "more" }
                ),
            );
        }
    }
}

fn brakes(b: &mut Builder, s: &StintSummary, k: &ClassRules) {
    if k.regen_at_max {
        if let Some(p) = b.setup.params.get("VM_REGEN_LEVEL") {
            let top = p.max_exclusive - 1;
            b.move_to(Rule::Regen, &["VM_REGEN_LEVEL"], top, "Hybrid regen always runs at maximum".into());
        }
    }
    let Some(t) = s.brake_temp_max else { return };
    for (axle, key, peak, (lo, hi)) in [
        ("Front", "VM_BRAKE_DUCTS", t.fl.max(t.fr), k.brake_temp_front),
        ("Rear", "VM_BRAKE_DUCTS_REAR", t.rl.max(t.rr), k.brake_temp_rear),
    ] {
        if peak > hi {
            b.add(Rule::BrakeDucts, &[key], -1, format!("{axle} brakes peak at {peak:.0} °C (target under {hi:.0}) → open the ducts a step"));
        } else if peak < lo {
            b.add(Rule::BrakeDucts, &[key], 1, format!("{axle} brakes peak at only {peak:.0} °C (target over {lo:.0}) → close the ducts a step"));
        }
    }
}

// kgf of a brake pressure label such as "118 kgf (98%)".
fn kgf(text: &str) -> Option<f64> {
    let end = text.find("kgf")?;
    text[..end].trim().parse().ok()
}

fn brake_pressure(b: &mut Builder, k: &ClassRules, flags: &DriverFlags) {
    const KEY: &str = "VM_BRAKE_PRESSURE";
    let Some((lo_pct, hi_pct)) = k.brake_pressure_pct else { return };
    let has_abs = b.setup.params.get("VM_ANTILOCKBRAKESYSTEMMAP").is_some_and(|p| p.adjustable());
    let Some(p) = b.setup.params.get(KEY).filter(|_| b.usable(KEY) && !has_abs) else { return };
    let Some(now) = kgf(&p.text) else { return };
    // LMU: 1 kgf a click, 100 % at the highest index.
    let full = now + f64::from(p.max_exclusive - 1 - p.value);
    let pct = |i: i32| 100.0 * (now + f64::from(i - p.value)) / full;
    let lo = (p.value + (at_clicks(lo_pct, full, now)).ceil() as i32).clamp(p.min, p.max_exclusive - 1);
    let hi = (p.value + (at_clicks(hi_pct, full, now)).floor() as i32).clamp(p.min, p.max_exclusive - 1);
    let value = p.value;
    if flags.lockups {
        b.add(Rule::BrakePressure, &[KEY], -BRAKE_PRESSURE_STEP, format!("You said {:.0} % is too much brake pressure → down a step", pct(value)));
    } else if value > hi {
        b.move_to(Rule::BrakePressure, &[KEY], hi, format!("Brake pressure {:.0} % is above the usual {lo_pct:.0}–{hi_pct:.0} % → down to {:.0} %", pct(value), pct(hi)));
    } else if value < lo && lo <= hi {
        b.move_to(Rule::BrakePressure, &[KEY], lo, format!("Brake pressure {:.0} % is below the usual {lo_pct:.0}–{hi_pct:.0} % → up to {:.0} %", pct(value), pct(lo)));
    }
}

// Clicks from the current index to `pct` of full pressure.
fn at_clicks(pct: f64, full: f64, now: f64) -> f64 {
    ((pct / 100.0 * full - now) * 1e6).round() / 1e6
}

fn platform(b: &mut Builder, class: CarClass, s: &StintSummary) {
    let pinned = class.front_ride_height_at_min();
    if pinned {
        if let Some(p) = b.setup.params.get(FRONT_RH[0]) {
            let min = p.min;
            b.move_to(Rule::FrontRideHeightAtMin, &FRONT_RH, min, "a GT3 always runs it at the minimum".into());
        }
    }
    let Some(rh) = s.rh_min_mm else { return };
    let mut done = 0;
    let bottoming = s.bottoming_s.unwrap_or(0.0);
    let front_low = !pinned && (bottoming > BOTTOMING_S || rh.fl.min(rh.fr) < MIN_RIDE_HEIGHT_MM);
    if front_low {
        let why = if bottoming > BOTTOMING_S {
            format!("The front bottoms {bottoming:.2} s a lap")
        } else {
            format!("The front ride height drops to {:.1} mm", rh.fl.min(rh.fr))
        };
        let steps = if bottoming > 3.0 * BOTTOMING_S { 2 } else { 1 };
        if packers_in(b, s.third_packers.front, "VM_FRONT_3RD_PACKERS", "front", &why)
            || b.add(Rule::RideHeight, &FRONT_RH, steps, format!("{why} → raise the front"))
        {
            done += 1;
        }
    }
    let bottoming_rear = s.bottoming_rear_s.unwrap_or(0.0);
    if done < MAX_PLATFORM_CHANGES && (bottoming_rear > BOTTOMING_S || rh.rl.min(rh.rr) < MIN_RIDE_HEIGHT_MM) {
        let why = if bottoming_rear > BOTTOMING_S {
            format!("The rear bottoms {bottoming_rear:.2} s a lap")
        } else {
            format!("The rear ride height drops to {:.1} mm", rh.rl.min(rh.rr))
        };
        let _ = packers_in(b, s.third_packers.rear, "VM_REAR_3RD_PACKERS", "rear", &why)
            || b.add(Rule::RideHeight, &REAR_RH, 1, format!("{why} → raise the rear"));
    }
}

fn packers_in(b: &mut Builder, frac: Option<f64>, key: &str, axle: &str, why: &str) -> bool {
    match frac {
        Some(f) if f < THIRD_PACKERS_LO && b.usable(key) => b.add(
            Rule::ThirdPackers,
            &[key],
            1,
            format!("{why} while its third spring is on the packers only {:.0} % of the lap → one step more {axle} third packer", 100.0 * f),
        ),
        _ => false,
    }
}

fn aero_trim(b: &mut Builder, trim: AeroTrim) {
    if trim == AeroTrim::Keep {
        return;
    }
    const KEY: &str = "VM_REAR_WING";
    let Some(p) = b.setup.params.get(KEY).filter(|_| b.usable(KEY)) else { return };
    let hi = p.max_exclusive - 1;
    let (target, label) = match trim {
        AeroTrim::Keep => return,
        AeroTrim::LowWing => (p.min, "low-wing trim"),
        AeroTrim::MidWing => (p.min + (hi - p.min) / 2, "mid-wing trim"),
        AeroTrim::HighWing => (hi, "high-wing trim"),
    };
    let delta = (target - p.value).clamp(-WING_TRIM_STEP, WING_TRIM_STEP);
    if delta != 0 {
        b.add(
            Rule::AeroTrim,
            &[KEY],
            delta,
            format!("You chose {label} → step rear wing toward {target}"),
        );
    }
}

pub fn apply_rules(input: &RuleInput) -> Vec<RuleChange> {
    let k = ClassRules::of(input.class);
    let mut b = Builder { setup: input.setup, locks: input.locks, out: Vec::new() };
    aero_trim(&mut b, input.flags.aero_trim);
    tyres(&mut b, input.summary, &k);
    brakes(&mut b, input.summary, &k);
    brake_pressure(&mut b, &k, input.flags);
    platform(&mut b, input.class, input.summary);
    b.out
}
