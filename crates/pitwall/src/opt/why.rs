/* Plain-English "Why?" summary of a Proposal for the Why? popup.
   Four short sections: what the telemetry showed, what changes and why,
   what it should do on track, and notes (blocked moves, pace just outside
   the band).  Degrees are understeer angle at the road wheels (+ understeer,
   − oversteer); grip is p95 lateral G.  */

use std::fmt;

use serde::{Deserialize, Serialize};

use crate::chassis::{BalanceCell, CornerPhase, SpeedBand};
use crate::opt::rules::Rule;
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
        (DimKind::BrakeBias, true) => "more to the front",
        (DimKind::BrakeBias, false) => "more to the rear",
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

fn change_line(c: &Change) -> String {
    let mut s = format!("{}: {} → {} ({})", capitalise(c.kind.label()), c.from, c.to, direction(c.kind, c.to > c.from));
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

fn capitalise(s: &str) -> String {
    let mut c = s.chars();
    c.next().map_or_else(String::new, |f| f.to_uppercase().collect::<String>() + c.as_str())
}

fn rule_label(r: Rule) -> &'static str {
    match r {
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

    why.changes = s.changes.iter().map(change_line).collect();
    why.changes.extend(p.rules.iter().map(|r| format!("{}: {} → {} ({})", rule_label(r.rule), r.from, r.to, r.reason)));
    let dp = &p.dampers;
    let damper_reason = if dp.exploring {
        "learning what it does to the tyre load and kerbs".to_string()
    } else if dp.kerb_change.abs() > dp.contact_change.abs() {
        "takes the kerbs better".to_string()
    } else {
        "steadier tyre load".to_string()
    };
    why.changes.extend(dp.changes.iter().map(|d| {
        format!(
            "{}: {} → {} ({}, {})",
            capitalise(&d.setting.label()),
            d.from,
            d.to,
            if d.to > d.from { "stiffer" } else { "softer" },
            damper_reason
        )
    }));

    for after in &s.chosen.cells {
        let dua = after.predicted_ua - after.measured_ua;
        let dlat = after.predicted_lat - after.measured_lat;
        if dua.raw().abs() > NOTABLE_DEG || dlat.raw().abs() > NOTABLE_G {
            why.effect.push(format!(
                "{}: {:.2}° {} understeer, {:+.3} g grip",
                capitalise(&place(after.cell)),
                dua.raw().abs(),
                if dua.raw() < 0.0 { "less" } else { "more" },
                dlat.raw()
            ));
        }
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
            "More grip is available just outside your balance: {} gives {:+.3} g, but the car would feel {}.",
            moves.join(" and "),
            h.extra_grip_g,
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
