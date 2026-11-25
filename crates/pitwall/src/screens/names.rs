/* Display names of LMU setting keys and the setup diff between stints.
   'WM_CAMBER-W_FL' → 'Camber FL', 'VM_REAR_ANTISWAY' → 'Rear anti-roll
   bar', 'VM_FRONT_3RD_SPRING' → 'Front 3rd spring'.  The diff lists
   adjustable settings whose index changed, in key order; a left/right pair
   that moved identically collapses into one row ('Camber FL/FR', 'Caster'),
   and a compound that kept its label (another tyre set) is left out.  */

use serde::{Deserialize, Serialize};

use crate::setup::CarSetup;

const NAMES: &[(&str, &str)] = &[
    ("ANTISWAY", "anti-roll bar"),
    ("TOEIN", "toe"),
    ("TOEOFFSET", "toe offset"),
    ("RIDEHEIGHT", "ride height"),
    ("SLOWBUMP", "slow bump"),
    ("FASTBUMP", "fast bump"),
    ("SLOWREBOUND", "slow rebound"),
    ("FASTREBOUND", "fast rebound"),
    ("BRAKE_BALANCE", "brake bias"),
    ("BRAKE_MIGRATION", "brake migration"),
    ("BRAKE_PRESSURE", "brake pressure"),
    ("DIFF_PRELOAD", "diff preload"),
    ("DIFF_POWER", "diff power"),
    ("DIFF_COAST", "diff coast"),
    ("DIFF_PUMP", "diff pump"),
    ("ENGINE_BRAKEMAP", "engine braking"),
    ("LEFT_CASTER", "left caster"),
    ("RIGHT_CASTER", "right caster"),
    ("TRACTIONCONTROLMAP", "TC map"),
    ("TRACTIONCONTROLPOWERCUTMAP", "TC cut"),
    ("TRACTIONCONTROLSLIPANGLEMAP", "TC slip"),
    ("ANTILOCKBRAKESYSTEMMAP", "ABS map"),
    ("REAR_WING", "rear wing"),
    ("FRONT_WING", "front wing"),
    ("FUEL_LEVEL", "fuel"),
    ("VIRTUAL_ENERGY", "virtual energy"),
    ("STEER_LOCK", "steering lock"),
    ("ENGINE_MIXTURE", "engine mix"),
    ("REGEN_LEVEL", "regen"),
    ("BRAKE_DUCTS", "front brake ducts"),
    ("BRAKE_DUCTS_REAR", "rear brake ducts"),
    ("WATER_RADIATOR", "radiator"),
    ("OIL_RADIATOR", "oil cooler"),
    ("PACKERS", "packers"),
    ("SPRING", "spring"),
    ("TENDERSPRING", "tender spring"),
    ("TENDERSPRINGTRAVEL", "tender travel"),
    ("CAMBER", "camber"),
    ("PRESSURE", "pressure"),
    ("COMPOUND", "compound"),
    ("BRAKEDISC", "brake disc"),
    ("BRAKEPAD", "brake pad"),
];

fn name_of(body: &str) -> Option<&'static str> {
    NAMES.iter().find(|(k, _)| *k == body).map(|&(_, n)| n)
}

fn sentence(s: &str) -> String {
    crate::opt::why::capitalise(s)
}

pub fn pretty_name(key: &str) -> String {
    if let Some((body, wheel)) = key.strip_prefix("WM_").and_then(|k| k.rsplit_once("-W_")) {
        return format!("{} {wheel}", sentence(&name_of(body).map_or_else(|| body.to_lowercase(), str::to_string)));
    }
    let body = key.strip_prefix("VM_").unwrap_or(key);
    for (prefix, axle) in [("FRONT_3RD_", "Front"), ("REAR_3RD_", "Rear")] {
        if let Some(rest) = body.strip_prefix(prefix) {
            return format!("{axle} 3rd {}", name_of(rest).map_or_else(|| rest.to_lowercase(), str::to_string));
        }
    }
    if let Some(n) = name_of(body) {
        return sentence(n);
    }
    for (side, word) in [("FRONT_", "Front"), ("REAR_", "Rear")] {
        if let Some(n) = body.strip_prefix(side).and_then(name_of) {
            return format!("{word} {n}");
        }
    }
    sentence(&body.replace('_', " ").to_lowercase())
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SetupChange {
    // One key, or two joined by '+' for a collapsed pair.
    pub key: String,
    pub name: String,
    pub before: i32,
    pub after: i32,
    pub delta: i32,
    pub before_text: String,
    pub after_text: String,
}

pub fn setup_diff(before: &CarSetup, after: &CarSetup) -> Vec<SetupChange> {
    let rows: Vec<SetupChange> = after
        .params
        .iter()
        .filter(|(_, p)| p.adjustable())
        .filter_map(|(key, p)| {
            let q = before.params.get(key).filter(|q| q.value != p.value)?;
            if key.starts_with("WM_COMPOUND") && q.text.trim() == p.text.trim() {
                return None;
            }
            Some(SetupChange {
                key: key.clone(),
                name: pretty_name(key),
                before: q.value,
                after: p.value,
                delta: p.value - q.value,
                before_text: q.text.trim().to_string(),
                after_text: p.text.trim().to_string(),
            })
        })
        .collect();
    let twin_of = |key: &str| -> Option<String> {
        if key == "VM_LEFT_CASTER" {
            return Some("VM_RIGHT_CASTER".into());
        }
        let (body, wheel) = key.rsplit_once("-W_")?;
        let other = match wheel {
            "FL" => "FR",
            "RL" => "RR",
            _ => return None,
        };
        Some(format!("{body}-W_{other}"))
    };
    let mut used = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    for r in &rows {
        if used.contains(&r.key) {
            continue;
        }
        let twin = twin_of(&r.key).and_then(|t| rows.iter().find(|q| q.key == t)).filter(|t| (t.before, t.after) == (r.before, r.after));
        out.push(match twin {
            Some(t) => {
                used.insert(t.key.clone());
                let name = if r.key == "VM_LEFT_CASTER" { "Caster".to_string() } else { format!("{}/{}", r.name, &t.key[t.key.len() - 2..]) };
                SetupChange { key: format!("{}+{}", r.key, t.key), name, ..r.clone() }
            }
            None => r.clone(),
        });
    }
    out
}
