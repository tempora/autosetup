/* Plain-English "Why?" summary of a Proposal for the Why? popup.
   Four short sections: what the telemetry showed, what changes and why,
   what it should do on track, and notes (blocked moves, pace just outside
   the band).  Degrees are understeer angle at the road wheels (+ understeer,
   − oversteer); grip is p95 lateral G.  Settings read as their garage
   labels when a labeller knows them (lmu::scales), else as clicks.  */

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CornerPhase, SpeedBand};
use crate::opt::rules::Rule;
use crate::opt::dampers::{DamperChange, DamperPlan};
use crate::opt::solve::{Change, Gate, Outcome};
use crate::opt::space::DimKind;
use crate::opt::target::FeelAxis;
use crate::opt::Proposal;
use crate::units::Degrees;

const NOTABLE_DEG: f64 = 0.05;
const NOTABLE_G: f64 = 0.005;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Why {
    pub headline: String,
    pub observed: Vec<String>,
    pub changes: Vec<String>,
    pub effect: Vec<String>,
    pub notes: Vec<String>,
}

fn place(cell: BalanceCell) -> String {
    let phase = match cell.phase {
        CornerPhase::Braking => "under braking",
        CornerPhase::Entry => "at turn-in",
        CornerPhase::Mid => "at the apex",
        CornerPhase::Exit => "on exit",
    };
    let band = match cell.band {
        SpeedBand::Low => "slow corners",
        SpeedBand::High => "fast corners",
    };
    format!("{phase} in {band}")
}

fn balance_word(ua: Degrees) -> &'static str {
    if ua.raw() > NOTABLE_DEG {
        "understeer"
    } else if ua.raw() < -NOTABLE_DEG {
        "oversteer"
    } else {
        "neutral"
    }
}

fn direction(kind: DimKind, up: bool) -> &'static str {
    match (kind, up) {
        (DimKind::RideHeightFront | DimKind::RideHeightRear, true) => "higher",
        (DimKind::RideHeightFront | DimKind::RideHeightRear, false) => "lower",
        // A higher brake-balance index moves bias rearward.
        (DimKind::BrakeBias, true) => "more to the rear",
        (DimKind::BrakeBias, false) => "more to the front",
        (DimKind::DiffPreload | DimKind::DiffPower | DimKind::DiffCoast, true) => "more locking",
        (DimKind::DiffPreload | DimKind::DiffPower | DimKind::DiffCoast, false) => "less locking",
        (_, true) => "stiffer",
        (_, false) => "softer",
    }
}

// "softer front anti-roll bar", "brake bias more to the rear".
fn move_words(kind: DimKind, up: bool) -> String {
    match kind {
        DimKind::BrakeBias => format!("{} {}", kind.label(), direction(kind, up)),
        DimKind::DiffPreload | DimKind::DiffPower | DimKind::DiffCoast => {
            format!("{} {}", if up { "more" } else { "less" }, kind.label())
        }
        _ => format!("{} {}", direction(kind, up), kind.label()),
    }
}

// A setting's display text at a click: (key, index) → label.
pub type Labeller<'a> = dyn Fn(&str, i32) -> Option<String> + 'a;

fn clicks(label: &Labeller, key: Option<&str>, from: i32, to: i32) -> String {
    let at = |i: i32| key.and_then(|k| label(k, i)).unwrap_or_else(|| i.to_string());
    format!("{} → {}", at(from), at(to))
}

// "stiffer, mainly for less understeer at turn-in in slow corners (−0.12°)".
pub fn change_reason(c: &Change) -> String {
    let mut s = direction(c.kind, c.to > c.from).to_string();
    if let Some((cell, dua, dlat)) = c.strongest {
        if dua.raw().abs() > NOTABLE_DEG {
            let what = if dua.raw() < 0.0 { "less understeer" } else { "more understeer" };
            s += &format!(", mainly for {what} {} ({:+.2}°", place(cell), dua.raw());
            if dlat.raw().abs() > NOTABLE_G {
                s += &format!(", {:+.3} g", dlat.raw());
            }
            s += ")";
        }
    }
    s
}

// "softer, takes the kerbs better".
pub fn damper_reason(dp: &DamperPlan, d: &DamperChange) -> String {
    let why = if dp.exploring {
        "learning what it does to the tyre load and kerbs"
    } else if dp.kerb_change.abs() > dp.contact_change.abs() {
        "takes the kerbs better"
    } else {
        "steadier tyre load"
    };
    format!("{}, {why}", if d.to > d.from { "stiffer" } else { "softer" })
}

pub fn capitalise(s: &str) -> String {
    let mut c = s.chars();
    c.next().map_or_else(String::new, |f| f.to_uppercase().collect::<String>() + c.as_str())
}

pub fn rule_label(r: Rule) -> &'static str {
    match r {
        Rule::AeroTrim => "Rear wing",
        Rule::TyrePressure => "Tyre pressure",
        Rule::Camber => "Camber",
        Rule::BrakeDucts => "Brake ducts",
        Rule::Regen => "Regen",
        Rule::BrakePressure => "Brake pressure",
        Rule::FrontRideHeightAtMin => "Front ride height",
        Rule::RideHeight => "Ride height",
        Rule::ThirdPackers => "Third packers",
    }
}

fn gate_words(g: Gate) -> &'static str {
    match g {
        Gate::Balance => "would move the balance further from what you want",
        Gate::Undrivable => "would repeat a change from a stint you couldn't drive",
        Gate::KerbStiffening => "would stiffen the car while it's unstable over kerbs",
        Gate::Countersteer => "would add rotation where you already caught slides",
    }
}

pub fn explain(p: &Proposal) -> Why {
    explain_with(p, &|_, _| None)
}

pub fn explain_with(p: &Proposal, label: &Labeller) -> Why {
    let s = &p.solution;
    let mut why = Why {
        headline: match s.outcome {
            Outcome::Changed => format!("{} setting change{} for this car.", s.changes.len(), if s.changes.len() == 1 { "" } else { "s" }),
            Outcome::AlreadyBest => "Nothing to change in the mechanical setup: every nearby setup is predicted slower or further from your balance.".into(),
            Outcome::AllRejected => "No mechanical change was possible: every nearby setup was blocked (see notes).".into(),
        },
        ..Why::default()
    };

    for pred in &s.base.cells {
        let line = format!(
            "{}: {} ({:+.2}°), {:.2} g",
            capitalise(&place(pred.cell)),
            balance_word(pred.measured_ua),
            pred.measured_ua.raw(),
            pred.measured_lat.raw()
        );
        why.observed.push(line);
        if pred.countersteer_s > crate::opt::solve::COUNTERSTEER_S_PER_PASS {
            why.observed.push(format!("You caught slides {} ({:.2} s per corner).", place(pred.cell), pred.countersteer_s));
        }
    }
    for axis in FeelAxis::ALL {
        let v = p.target.sliders.get(axis).raw();
        if v.abs() > 1e-9 {
            let (neg, pos) = axis.ends();
            why.observed.push(format!("You asked for {} ({:+.2}°).", if v < 0.0 { neg } else { pos }, v));
        }
    }

    let dp = &p.dampers;
    why.changes = s
        .changes
        .iter()
        .map(|c| format!("{}: {} ({})", capitalise(c.kind.label()), clicks(label, c.kind.keys().first().copied(), c.from, c.to), change_reason(c)))
        .chain(p.rules.iter().map(|r| {
            format!("{}: {} ({})", rule_label(r.rule), clicks(label, r.keys.first().map(String::as_str), r.from, r.to), r.reason)
        }))
        .chain(dp.changes.iter().map(|d| {
            format!(
                "{}: {} ({})",
                capitalise(&d.setting.label()),
                clicks(label, d.keys.first().map(String::as_str), d.from, d.to),
                damper_reason(dp, d)
            )
        }))
        .collect();

    for after in &s.chosen.cells {
        let dua = after.predicted_ua - after.measured_ua;
        let dlat = after.predicted_lat - after.measured_lat;
        if dua.raw().abs() > NOTABLE_DEG || dlat.raw().abs() > NOTABLE_G {
            let mut line = format!(
                "{}: {:.2}° {} understeer",
                capitalise(&place(after.cell)),
                dua.raw().abs(),
                if dua.raw() < 0.0 { "less" } else { "more" }
            );
            // Same line through the corner: v scales with the square root of grip.
            if after.cell.phase != CornerPhase::Braking && after.measured_lat.raw() > 0.0 {
                let dv = after.v_min.to_kmh() * ((after.predicted_lat / after.measured_lat).max(0.0).sqrt() - 1.0);
                line += &format!(", {dv:+.1} km/h");
            }
            why.effect.push(line);
        }
    }
    if s.chosen.pace.abs() > 1e-4 {
        why.effect.push(format!("Overall: about {:.1} % less time spent cornering.", 100.0 * s.chosen.pace));
    }

    let v = p.verification;
    if !v.balance || !v.grip {
        let what = match (v.balance, v.grip) {
            (false, false) => "balance or grip",
            (false, true) => "balance",
            _ => "grip",
        };
        why.notes.push(if v.pairs == 0 {
            format!("The car model is not calibrated for this class yet: its {what} predictions are untested.")
        } else {
            format!(
                "On your {} stint pairs the car model predicted {what} changes no better than assuming nothing changes: treat these as educated guesses.",
                v.pairs
            )
        });
    }
    for (gate, n) in &s.rejected {
        why.notes.push(format!("{n} of {} nearby setups {}.", s.candidates, gate_words(*gate)));
    }
    for (n, words) in [
        (dp.rejected_kerb, "would make a kerb number worse"),
        (dp.rejected_balance, "would move the balance further from what you want"),
        (dp.rejected_unstable, "would stiffen a fast damper while the car is unstable over kerbs"),
    ] {
        if n > 0 {
            why.notes.push(format!("{n} of {} damper moves {words}.", dp.candidates));
        }
    }
    if !dp.changes.is_empty() && dp.stints < 3 {
        why.notes.push(format!(
            "Damper effects are learned from your stints of this car and track; only {} so far, so the damper moves lean on general rules.",
            dp.stints
        ));
    }
    if let Some(h) = &s.hint {
        let moves: Vec<String> = h.changes.iter().map(|c| move_words(c.kind, c.to > c.from)).collect();
        let away: Vec<String> = h
            .leaves_band
            .iter()
            .map(|(cell, d)| format!("{} {}", if d.raw() < 0.0 { "looser" } else { "lazier" }, place(*cell)))
            .collect();
        why.notes.push(format!(
            "More pace is available just outside your balance: {} would cut a further {:.1} % of cornering time, but the car would feel {}.",
            moves.join(" and "),
            100.0 * h.extra_time,
            away.join(", ")
        ));
    }
    why
}

impl fmt::Display for Why {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "{}", self.headline)?;
        for (title, lines) in [
            ("What the telemetry showed", &self.observed),
            ("What changes", &self.changes),
            ("What it should do", &self.effect),
            ("Notes", &self.notes),
        ] {
            if lines.is_empty() {
                continue;
            }
            writeln!(f, "{title}:")?;
            for l in lines {
                writeln!(f, "  {l}")?;
            }
        }
        Ok(())
    }
}
