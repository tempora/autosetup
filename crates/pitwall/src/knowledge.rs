/* Per-class constants and rule targets from classes.toml.
   Resolution: [default], then every section whose name is a
   case-insensitive prefix of the class key, shorter first, each replacing
   whole top-level values.  Every ClassKnowledge field is required after
   merging, so a missing or mistyped key fails at load rather than falling
   back silently.  The shipped file is embedded; CarClass::knowledge()
   resolves it once for all classes.  */

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde::de::Error as _;
use serde::{Deserialize, Deserializer};
use thiserror::Error;

use crate::chassis::CarClass;

const SHIPPED: &str = include_str!("../classes.toml");

#[derive(Debug, Error)]
pub enum KnowledgeError {
    #[error("classes.toml: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("classes.toml has no [default] table")]
    NoDefault,
}

// Fast-neutral window centres per phase, (low speed, high speed) deg.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FastNeutral {
    pub entry: (f64, f64),
    pub mid: (f64, f64),
    pub exit: (f64, f64),
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ClassKnowledge {
    pub wheelbase_m: f64,
    pub band_split_kmh: f64,
    pub feel_step_deg: f64,
    pub feel_tolerance_deg: f64,
    pub front_rh_at_min: bool,
    pub regen_at_max: bool,
    pub damper_knee_mm_s: f64,
    pub damper_ls_target: f64,
    pub spread_front: (f64, f64),
    pub spread_rear: (f64, f64),
    pub crown_target: f64,
    pub crown_band: f64,
    pub brake_temp_front: (f64, f64),
    pub brake_temp_rear: (f64, f64),
    #[serde(deserialize_with = "optional_range")]
    pub brake_pressure_pct: Option<(f64, f64)>,
    pub bottoming_s: f64,
    pub min_ride_height_mm: f64,
    pub third_packers_lo: f64,
    pub max_platform_changes: usize,
    pub fast_neutral: FastNeutral,
}

// `[]` is off; `[lo, hi]` with lo ≤ hi is a range.
fn optional_range<'de, D: Deserializer<'de>>(d: D) -> Result<Option<(f64, f64)>, D::Error> {
    match *Vec::<f64>::deserialize(d)?.as_slice() {
        [] => Ok(None),
        [lo, hi] if lo <= hi => Ok(Some((lo, hi))),
        ref other => Err(D::Error::custom(format!("expected [] or [lo, hi], got {other:?}"))),
    }
}

pub fn resolve(text: &str, class_key: &str) -> Result<ClassKnowledge, KnowledgeError> {
    let mut doc: toml::Table = text.parse()?;
    let mut merged = match doc.remove("default") {
        Some(toml::Value::Table(t)) => t,
        _ => return Err(KnowledgeError::NoDefault),
    };
    let key = class_key.to_ascii_lowercase();
    let mut sections: Vec<(String, toml::Table)> = doc
        .into_iter()
        .filter(|(name, _)| key.starts_with(&name.to_ascii_lowercase()))
        .filter_map(|(name, v)| match v {
            toml::Value::Table(t) => Some((name, t)),
            _ => None,
        })
        .collect();
    sections.sort_by_key(|(name, _)| name.len());
    merged.extend(sections.into_iter().flat_map(|(_, t)| t));
    Ok(toml::Value::Table(merged).try_into()?)
}

pub fn shipped(class: CarClass) -> &'static ClassKnowledge {
    static TABLE: OnceLock<BTreeMap<CarClass, ClassKnowledge>> = OnceLock::new();
    &TABLE.get_or_init(|| {
        CarClass::ALL
            .into_iter()
            .map(|c| (c, resolve(SHIPPED, c.key()).unwrap_or_else(|e| panic!("shipped {e}"))))
            .collect()
    })[&class]
}

#[cfg(test)]
mod tests {
    use super::{resolve, shipped, KnowledgeError};
    use crate::chassis::CarClass;

    #[test]
    fn every_class_resolves_from_the_shipped_file() {
        for class in CarClass::ALL {
            let k = shipped(class);
            assert!(k.feel_tolerance_deg < k.feel_step_deg, "{class:?}: band wider than a feel step");
        }
    }

    #[test]
    fn sections_apply_by_prefix_shortest_first_and_empty_range_turns_a_rule_off() {
        let base = include_str!("../classes.toml");
        let text = format!("{base}\n[LMP]\nbrake_pressure_pct = []\ndamper_knee_mm_s = 1.0\n");
        let elms = resolve(&text, "LMP2_ELMS").unwrap();
        // [LMP] < [LMP2] < [LMP2_ELMS]: LMP2 restores the range, ELMS sets the knee.
        assert_eq!(elms.brake_pressure_pct, Some((90.0, 94.0)));
        assert_eq!(elms.damper_knee_mm_s, 40.0);
        let lmp3 = resolve(&text, "LMP3").unwrap();
        assert_eq!((lmp3.brake_pressure_pct, lmp3.damper_knee_mm_s), (None, 1.0));
    }

    #[test]
    fn missing_or_unknown_keys_fail_instead_of_defaulting() {
        let base = include_str!("../classes.toml");
        let missing = base.replace("crown_band = 3.0\n", "");
        assert!(matches!(resolve(&missing, "GT3"), Err(KnowledgeError::Parse(_))));
        let typo = format!("{base}\n[GT3X]\nfront_rh_at_mn = true\n");
        assert!(matches!(resolve(&typo, "GT3X"), Err(KnowledgeError::Parse(_))));
        let bad_range = base.replace("brake_pressure_pct = [90.0, 94.0]", "brake_pressure_pct = [94.0]");
        assert!(resolve(&bad_range, "LMP2").is_err());
    }
}
