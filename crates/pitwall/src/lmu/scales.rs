/* What a setting's index means for one car model: index → display label.
   The game only reports the label at the index driven, so every
   (index, label) pair seen for the model is collected: from its recordings,
   from saved .svm files matching one of those setups (match_score ≥
   SAME_CAR_SCORE), and from saved setups of other versions of the same model
   (same VehicleClassSetting model word) for settings where those files agree
   with each other and with every label this version has shown.  Updates
   relabel some settings, and the disagreement gives that away.  Recordings
   win on conflicts.
   A scale is numeric when its labels' values move strictly one way with the
   index; it then interpolates between the nearest seen points (extrapolates
   from the nearest with the per-step slope).  Interpolated labels keep the
   seen label's decimals and unit but drop its bracketed notes, which are
   either derived from other settings ("116 kgf (97%)", "85% (5.0 laps)") or
   mark a range end ("(soft)", "(Off)").  Other labels ("P3", "25x2.5 2-2")
   are known only where seen.  Brake balance "53.5:46.5" reads as front %;
   diff ramps "FF6-30 deg" read as the ramp angle.  */

use std::collections::{BTreeMap, BTreeSet, HashMap};

use crate::lmu::svm::{match_score, SvmFile};
use crate::setup::CarSetup;

pub const SAME_CAR_SCORE: f64 = 0.6;

// Step sizes used to extrapolate when only one point is known.
const DEFAULT_STEP: &[(&str, f64)] = &[("WM_PRESSURE", 1.0), ("WM_CAMBER", 0.1), ("WM_RIDEHEIGHT", 0.1)];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Label {
    pub text: String,
    // Seen at this index, not interpolated.
    pub exact: bool,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Scale {
    key: String,
    points: BTreeMap<i32, String>,
}

impl Scale {
    // Seen (index, value) pairs; None unless every label has a value moving strictly one way.
    fn numeric(&self) -> Option<Vec<(i32, f64)>> {
        let pts: Vec<(i32, f64)> = self.points.iter().map(|(&i, l)| value_of(&self.key, l).map(|v| (i, v))).collect::<Option<_>>()?;
        let steps: Vec<f64> = pts.windows(2).map(|w| w[1].1 - w[0].1).collect();
        (!pts.is_empty() && (steps.iter().all(|&d| d > 0.0) || steps.iter().all(|&d| d < 0.0))).then_some(pts)
    }

    // Every (index, label) seen.
    pub fn points(&self) -> &BTreeMap<i32, String> {
        &self.points
    }

    // Signed change in the label's value per index step.
    pub fn per_step(&self) -> Option<f64> {
        match self.numeric()?.as_slice() {
            [(i0, v0), .., (i1, v1)] => Some((v1 - v0) / f64::from(i1 - i0)),
            _ => {
                let base = self.key.split("-W_").next().unwrap_or(&self.key);
                DEFAULT_STEP.iter().find(|(k, _)| *k == base).map(|&(_, s)| s)
            }
        }
    }

    pub fn label(&self, index: i32) -> Option<Label> {
        if let Some(text) = self.points.get(&index) {
            return Some(Label { text: text.clone(), exact: true });
        }
        // Settings labelled by their own index (TC and ABS maps: '6' at index 6).
        if !self.points.is_empty() && self.points.iter().all(|(i, l)| l.trim() == i.to_string()) {
            return Some(Label { text: index.to_string(), exact: false });
        }
        let pts = self.numeric()?;
        let step = self.per_step()?;
        let below = pts.iter().rev().find(|p| p.0 < index);
        let above = pts.iter().find(|p| p.0 > index);
        let (value, nearest) = match (below, above) {
            (Some(&(i0, v0)), Some(&(i1, v1))) => (v0 + (v1 - v0) * f64::from(index - i0) / f64::from(i1 - i0), i0),
            (Some(&(i0, v0)), None) | (None, Some(&(i0, v0))) => (v0 + step * f64::from(index - i0), i0),
            (None, None) => return None,
        };
        Some(Label { text: format_like(&self.key, &self.points[&nearest], value), exact: false })
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct CarScales(BTreeMap<String, Scale>);

impl CarScales {
    /* `setups`: every recorded setup of one car model (same signature);
       `saved`: every parsed .svm in the Settings folders.  */
    pub fn collect<'a>(setups: impl IntoIterator<Item = &'a CarSetup>, saved: &[SvmFile]) -> Self {
        let setups: Vec<&CarSetup> = setups.into_iter().collect();
        let mut points: BTreeMap<String, BTreeMap<i32, String>> = BTreeMap::new();
        for p in setups.iter().flat_map(|s| s.params.iter()).filter(|(_, p)| !p.text.trim().is_empty()) {
            points.entry(p.0.clone()).or_default().insert(p.1.value, p.1.text.trim().to_string());
        }
        let same_car: Vec<&SvmFile> =
            saved.iter().filter(|f| setups.iter().any(|s| match_score(s, f) >= SAME_CAR_SCORE)).collect();
        for (k, i, label) in same_car.iter().flat_map(|f| labelled(f)) {
            if let Some(seen) = points.get_mut(&k) {
                seen.entry(i).or_insert(label);
            }
        }
        let models: BTreeSet<String> = same_car.iter().filter_map(|f| model_of(&f.vehicle_class())).collect();
        let mut other: HashMap<String, BTreeMap<i32, BTreeSet<String>>> = HashMap::new();
        for f in saved.iter().filter(|f| model_of(&f.vehicle_class()).is_some_and(|m| models.contains(&m))) {
            for (k, i, label) in labelled(f).filter(|(k, ..)| points.contains_key(k)) {
                other.entry(k).or_default().entry(i).or_default().insert(label);
            }
        }
        for (k, seen) in other {
            let mine = points.get_mut(&k).expect("filtered to known keys");
            let consistent = seen.values().all(|ls| ls.len() == 1);
            let shared: Vec<i32> = seen.keys().copied().filter(|i| mine.contains_key(i)).collect();
            if consistent && !shared.is_empty() && shared.iter().all(|i| seen[i].contains(&mine[i])) {
                for (i, ls) in seen {
                    mine.entry(i).or_insert_with(|| ls.into_iter().next().expect("one label"));
                }
            }
        }
        Self(points.into_iter().map(|(key, points)| (key.clone(), Scale { key, points })).collect())
    }

    pub fn get(&self, key: &str) -> Option<&Scale> {
        self.0.get(key)
    }

    pub fn label(&self, key: &str, index: i32) -> Option<Label> {
        self.get(key)?.label(index)
    }

    // The label seen at exactly this index, if any.
    pub fn exact(&self, key: &str, index: i32) -> Option<&str> {
        self.get(key)?.points.get(&index).map(String::as_str)
    }
}

fn labelled(f: &SvmFile) -> impl Iterator<Item = (String, i32, String)> + '_ {
    f.vm_values().into_iter().filter_map(|(k, (i, l))| Some((k, i, l?.trim().to_string()))).filter(|(_, _, l)| !l.is_empty())
}

/* Car model word of a VehicleClassSetting: 'ELMS2026 LMP2_ELMS Oreca_07' →
   'Oreca_07'.  Season tags and class names drop out; a word with an
   underscore wins over plain ones.  */
pub fn model_of(vehicle_class: &str) -> Option<String> {
    const CLASSES: &[&str] = &["gt3", "gte", "lmgt3", "lmp2", "lmp2_elms", "lmp3", "hypercar", "hyper"];
    let season = |w: &str| {
        let cut = w.len().saturating_sub(4);
        w.is_char_boundary(cut) && {
            let (tag, year) = w.split_at(cut);
            ["WEC", "ELMS", "IMSA"].contains(&tag) && year.len() == 4 && year.bytes().all(|b| b.is_ascii_digit())
        }
    };
    let words: Vec<&str> =
        vehicle_class.split_whitespace().filter(|w| !season(w) && !CLASSES.contains(&w.to_ascii_lowercase().as_str())).collect();
    words.iter().find(|w| w.contains('_')).or_else(|| words.first()).map(|w| (*w).to_string())
}

// Numeric value of a label: diff ramps by their angle, everything else by its first number.
pub fn value_of(key: &str, label: &str) -> Option<f64> {
    key.contains("DIFF").then(|| ramp_angle(label)).flatten().map(|(v, _)| v).or_else(|| label_number(label))
}

/* First number of a display label: '-2.2 deg' → -2.2, '52.5:47.5' → 52.5,
   '8,500' → 8500, '1 (soft)' → 1.  A label starting with a letter has none
   unless it starts with the number ('Open', 'D35x1_1-1' → None).  */
pub fn label_number(label: &str) -> Option<f64> {
    let s = label.trim();
    if let Some((v, _)) = number_at(s, 0) {
        return Some(v);
    }
    if s.starts_with(|c: char| c.is_ascii_alphabetic()) {
        return None;
    }
    (1..s.len()).filter(|&i| s.is_char_boundary(i)).find_map(|i| number_at(s, i)).map(|(v, _)| v)
}

// `[-+]?\d+(,\d{3})*(\.\d+)?` at byte `start`: (value, end).
fn number_at(s: &str, start: usize) -> Option<(f64, usize)> {
    let b = s.as_bytes();
    let digits = |from: usize| from + b[from..].iter().take_while(|c| c.is_ascii_digit()).count();
    let mut i = start + usize::from(matches!(b.get(start), Some(b'-' | b'+')));
    let int_end = digits(i);
    if int_end == i {
        return None;
    }
    i = int_end;
    while b.get(i) == Some(&b',') && digits(i + 1) >= i + 4 {
        i += 4;
    }
    if b.get(i) == Some(&b'.') && digits(i + 1) > i + 1 {
        i = digits(i + 1);
    }
    s[start..i].replace(',', "").parse().ok().map(|v| (v, i))
}

// The number right before a standalone 'deg': (angle, its byte range).
fn ramp_angle(label: &str) -> Option<(f64, std::ops::Range<usize>)> {
    label.match_indices("deg").find_map(|(at, _)| {
        let after = label[at + 3..].chars().next();
        if after.is_some_and(|c| c.is_alphanumeric() || c == '_') {
            return None;
        }
        let end = label[..at].trim_end().len();
        let start = label[..end].rfind(|c: char| !(c.is_ascii_digit() || c == '.')).map_or(0, |p| p + 1);
        label[start..end].parse().ok().map(|v| (v, start..end))
    })
}

// `value` written in the style of a seen label: its decimals and unit suffix.
fn format_like(key: &str, template: &str, value: f64) -> String {
    if key.contains("DIFF") {
        if let Some((_, r)) = ramp_angle(template) {
            return format!("{}{}{}", &template[..r.start], trim_float(value), &template[r.end..]);
        }
    }
    let t = template.trim_start();
    let Some((_, end)) = number_at(t, 0) else { return trim_float(value) };
    let decimals = t[..end].split_once('.').map_or(0, |(_, d)| d.len());
    let scale = 10f64.powi(decimals as i32);
    let value = (value * scale).round() / scale + 0.0; // + 0.0: no "-0.0"
    let rest = &t[end..];
    if key == "VM_BRAKE_BALANCE" && rest.starts_with(':') {
        return format!("{value:.decimals$}:{:.decimals$}", 100.0 - value);
    }
    format!("{value:.decimals$}{}", without_notes(rest))
}

// `text` without its bracketed notes; one leading space is kept.
pub fn without_notes(text: &str) -> String {
    let mut depth = 0usize;
    let kept: String = text
        .chars()
        .filter(|&c| {
            let keep = depth == 0 && c != '(';
            depth = match c {
                '(' => depth + 1,
                ')' => depth.saturating_sub(1),
                _ => depth,
            };
            keep && c != ')'
        })
        .collect();
    let words = kept.split_whitespace().collect::<Vec<_>>().join(" ");
    let lead = if kept.starts_with(char::is_whitespace) && !words.is_empty() { " " } else { "" };
    format!("{lead}{words}")
}

fn trim_float(v: f64) -> String {
    let s = format!("{v:.6}");
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}
