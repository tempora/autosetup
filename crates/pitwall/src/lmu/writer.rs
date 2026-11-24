/* A proposal as a new .svm the garage can load.
   A recording holds every setting's index but not the .svm header
   (VehicleClassSetting, UpgradeSetting, //VEH=), so a saved setup of the same
   car model (best match_score ≥ MIN_TEMPLATE_SCORE) is the template.  Every
   mapped adjustable setting is then overwritten from the proposed setup, so
   the template's own values only survive where telemetry has no say (tyre
   compound).  Indices are already clamped to minValue ..= maxValue − 1 by
   CarSetup::set_value.  An unchanged setting keeps the recording's label; a
   changed one has its label dropped until click-to-label scales exist.  The
   file goes next to the template, under the lowest free revision of its
   name, and never replaces an existing file.  */

use std::collections::HashMap;
use std::path::PathBuf;

use thiserror::Error;

use crate::lmu::svm::{best_match, SvmError, SvmFile};
use crate::lmu::templates::{file_safe, render};
use crate::lmu::svm::next_free_name;
use crate::setup::CarSetup;

pub const MIN_TEMPLATE_SCORE: f64 = 0.5;

#[derive(Debug, Error)]
pub enum WriteError {
    #[error("Save a setup of this car in the garage before driving: no saved setup matches it")]
    NoTemplate,
    #[error("the template has no folder")]
    NoFolder,
    #[error(transparent)]
    Svm(#[from] SvmError),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Written {
    pub path: PathBuf,
    pub template: Option<PathBuf>,
    // (key, from, to) for every setting that differs from the stint as driven.
    pub changes: Vec<(String, i32, i32)>,
    // Proposed settings the template has no line for.
    pub missing: Vec<String>,
}

// The template with `proposed`'s values and the garage notes applied.
pub fn compose(template: &SvmFile, driven: &CarSetup, proposed: &CarSetup, notes: &str) -> (SvmFile, Vec<(String, i32, i32)>, Vec<String>) {
    let mut out = template.clone();
    let rows: Vec<(&str, i32, Option<&str>, i32)> = proposed
        .params
        .iter()
        .filter(|(_, p)| p.adjustable())
        .map(|(k, p)| {
            let before = driven.params.get(k).map_or(p.value, |d| d.value);
            let label = (before == p.value).then_some(p.text.as_str()).filter(|t| !t.is_empty());
            (k.as_str(), p.value, label, before)
        })
        .collect();
    let missing = out.set_vm(rows.iter().map(|&(k, v, l, _)| (k, v, l)));
    out.set_notes(notes);
    let changes = rows.iter().filter(|r| r.1 != r.3).map(|&(k, v, _, b)| (k.to_string(), b, v)).collect();
    (out, changes, missing)
}

/* Writes `proposed` next to the best-matching saved setup.  `name` is a
   templates::NAME-style template; `{n}` takes the first free revision.  */
pub fn write_proposal(
    saved: &[SvmFile],
    driven: &CarSetup,
    proposed: &CarSetup,
    name: &str,
    fields: &HashMap<&str, String>,
    notes: &str,
) -> Result<Written, WriteError> {
    let (_, template) = best_match(driven, saved, MIN_TEMPLATE_SCORE).ok_or(WriteError::NoTemplate)?;
    let folder = template.path.as_ref().and_then(|p| p.parent()).ok_or(WriteError::NoFolder)?.to_path_buf();
    let (svm, changes, missing) = compose(template, driven, proposed, notes);
    let path = next_free_name(&folder, |n| {
        let mut f = fields.clone();
        f.insert("n", n.to_string());
        file_safe(&render(name, &f))
    });
    let path = svm.write_new(&path)?;
    Ok(Written { path, template: template.path.clone(), changes, missing })
}
