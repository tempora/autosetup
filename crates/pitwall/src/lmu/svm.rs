/* LMU setup files (.svm): read, match against a recording, write.
   Format: INI-like, usually CRLF, one `Key=<index>//<display value>` line
   per setting under `[SECTION]` headers, with a few header lines
   (VehicleClassSetting, UpgradeSetting, `//VEH=`) before the first section.
   The game ignores `//` comments, so a changed index gets our label or none.

   Files are handled as Latin-1 (one char per byte) so every byte
   round-trips: some community setups hold bytes that are not valid UTF-8.
   A write never replaces an existing file.  */

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use thiserror::Error;

use crate::lmu::mapping::{do_not_write, mapping};
use crate::setup::CarSetup;

#[derive(Debug, Error)]
pub enum SvmError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("[{0}] {1} is not in the setup file")]
    MissingKey(String, String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SvmEntry {
    pub section: Option<String>,
    pub key: String,
    // Value text before any // comment.
    pub raw: String,
    pub comment: Option<String>,
    pub line: usize,
}

impl SvmEntry {
    pub fn index(&self) -> Option<i32> {
        self.raw.trim().parse().ok()
    }
}

#[derive(Debug, Clone)]
pub struct SvmFile {
    lines: Vec<String>,
    entries: HashMap<(Option<String>, String), SvmEntry>,
    pub path: Option<PathBuf>,
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| char::from(b)).collect()
}

fn split_eol(line: &str) -> (&str, &str) {
    let body = line.trim_end_matches(['\r', '\n']);
    (body, &line[body.len()..])
}

fn parse_section(body: &str) -> Option<&str> {
    let inner = body.trim_end().strip_prefix('[')?.strip_suffix(']')?;
    (!inner.is_empty() && !inner.contains(']')).then_some(inner)
}

// `Key=value` or `Key=value//comment`; a single '/' in the value is not a setting.
fn parse_setting(body: &str) -> Option<(&str, &str, Option<&str>)> {
    let (key, rest) = body.split_once('=')?;
    if key.is_empty() || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_') {
        return None;
    }
    match rest.find('/') {
        None => Some((key, rest, None)),
        Some(i) if rest[i..].starts_with("//") => Some((key, &rest[..i], Some(&rest[i + 2..]))),
        Some(_) => None,
    }
}

impl SvmFile {
    pub fn parse(text: &str) -> Self {
        let mut lines = Vec::new();
        let mut rest = text;
        while !rest.is_empty() {
            let end = rest.find('\n').map_or(rest.len(), |i| i + 1);
            lines.push(rest[..end].to_string());
            rest = &rest[end..];
        }
        let mut f = Self { lines, entries: HashMap::new(), path: None };
        f.reindex();
        f
    }

    pub fn read(path: impl AsRef<Path>) -> Result<Self, SvmError> {
        let bytes = std::fs::read(path.as_ref())?;
        let mut f = Self::parse(&latin1(&bytes));
        f.path = Some(path.as_ref().to_path_buf());
        Ok(f)
    }

    fn reindex(&mut self) {
        self.entries.clear();
        let mut section: Option<String> = None;
        for (i, line) in self.lines.iter().enumerate() {
            let (body, _) = split_eol(line);
            if let Some(s) = parse_section(body) {
                section = Some(s.to_string());
            } else if let Some((key, raw, comment)) = parse_setting(body) {
                self.entries.insert(
                    (section.clone(), key.to_string()),
                    SvmEntry {
                        section: section.clone(),
                        key: key.to_string(),
                        raw: raw.to_string(),
                        comment: comment.map(str::to_string),
                        line: i,
                    },
                );
            }
        }
    }

    pub fn text(&self) -> String {
        self.lines.concat()
    }

    // Latin-1 bytes; any char above U+00FF becomes '?'.
    pub fn bytes(&self) -> Vec<u8> {
        self.text().chars().map(|c| u8::try_from(u32::from(c)).unwrap_or(b'?')).collect()
    }

    pub fn get(&self, section: Option<&str>, key: &str) -> Option<&SvmEntry> {
        self.entries.get(&(section.map(str::to_string), key.to_string()))
    }

    pub fn vehicle_class(&self) -> String {
        self.get(None, "VehicleClassSetting").map(|e| e.raw.trim().trim_matches('"').to_string()).unwrap_or_default()
    }

    // The .VEH the setup was saved for (`//VEH=` header).
    pub fn veh_file(&self) -> String {
        self.lines.iter().find_map(|l| l.strip_prefix("//VEH=")).map(|v| v.trim().to_string()).unwrap_or_default()
    }

    // Index and label of every mapped setting, keyed by telemetry name.
    pub fn vm_values(&self) -> HashMap<String, (i32, Option<String>)> {
        let map = &mapping().svm_to_vm;
        self.entries
            .values()
            .filter_map(|e| {
                let vm = map.get(&(e.section.clone()?, e.key.clone()))?;
                Some((vm.clone(), (e.index()?, e.comment.clone())))
            })
            .collect()
    }

    pub fn set(&mut self, section: &str, key: &str, index: i32, label: Option<&str>) -> Result<(), SvmError> {
        let e = self
            .get(Some(section), key)
            .ok_or_else(|| SvmError::MissingKey(section.to_string(), key.to_string()))?
            .clone();
        let (_, eol) = split_eol(&self.lines[e.line]);
        let comment = label.map(|l| format!("//{l}")).unwrap_or_default();
        self.lines[e.line] = format!("{key}={index}{comment}{eol}");
        self.entries.insert(
            (Some(section.to_string()), key.to_string()),
            SvmEntry { raw: index.to_string(), comment: label.map(str::to_string), ..e },
        );
        Ok(())
    }

    /* Applies telemetry-keyed indices with optional labels; returns keys with no
       line in this file.  DO_NOT_WRITE keys are skipped silently.  */
    pub fn set_vm<'a>(&mut self, values: impl IntoIterator<Item = (&'a str, i32, Option<&'a str>)>) -> Vec<String> {
        let mut missing = Vec::new();
        for (vm, index, label) in values {
            if do_not_write(vm) {
                continue;
            }
            match mapping().vm_to_svm.get(vm) {
                Some((sec, key)) if self.get(Some(sec), key).is_some() => {
                    let _ = self.set(sec, key, index, label);
                }
                _ => missing.push(vm.to_string()),
            }
        }
        missing
    }

    /* Garage notes: Latin-1 only (arrows and dashes in plain characters,
       anything else '?'), no '"', no newlines, and no '/' (it would start a
       // comment).  */
    pub fn set_notes(&mut self, notes: &str) {
        let Some(e) = self.get(Some("GENERAL"), "Notes").cloned() else { return };
        let safe: String = notes
            .replace('"', "'")
            .replace(['\r', '\n'], " ")
            .replace('→', "->")
            .replace('≈', "~")
            .replace(['−', '–', '—', '·', '/'], "-")
            .chars()
            .map(|c| if u32::from(c) <= 0xFF { c } else { '?' })
            .collect();
        let (_, eol) = split_eol(&self.lines[e.line]);
        self.lines[e.line] = format!("Notes=\"{safe}\"{eol}");
        self.reindex();
    }

    // Writes to `path`; never replaces an existing file.
    pub fn write_new(&self, path: impl AsRef<Path>) -> Result<PathBuf, SvmError> {
        let path = path.as_ref();
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut f = OpenOptions::new().write(true).create_new(true).open(path)?;
        f.write_all(&self.bytes())?;
        Ok(path.to_path_buf())
    }
}

/* Share of the setup's adjustable settings whose index and label equal the
   file's.  ~1.0: this very setup; ≥ 0.85: reliably the same car model
   (labels differ between cars at equal indices); below ~0.5: another car or
   a file from an older game version.  */
pub fn match_score(setup: &CarSetup, svm: &SvmFile) -> f64 {
    let values = svm.vm_values();
    let present: Vec<_> = setup
        .params
        .iter()
        .filter(|(k, p)| p.adjustable() && !do_not_write(k) && values.contains_key(*k))
        .collect();
    if present.is_empty() {
        return 0.0;
    }
    let misses = present
        .iter()
        .filter(|(k, p)| {
            let (index, label) = &values[*k];
            *index != p.value || label.as_deref().map(str::trim) != Some(p.text.trim())
        })
        .count();
    1.0 - misses as f64 / present.len() as f64
}

// Every saved setup under `<settings>/<track>/*.svm` that parses.
pub fn read_settings(settings_dir: &Path) -> Vec<SvmFile> {
    let Ok(tracks) = std::fs::read_dir(settings_dir) else { return Vec::new() };
    tracks
        .filter_map(Result::ok)
        .filter(|t| t.path().is_dir())
        .flat_map(|t| std::fs::read_dir(t.path()).into_iter().flatten().filter_map(Result::ok))
        .map(|f| f.path())
        .filter(|p| p.extension().is_some_and(|e| e.eq_ignore_ascii_case("svm")))
        .filter_map(|p| SvmFile::read(&p).ok())
        .collect()
}

// The saved setup that best matches `setup`, if any reaches `min_score`.
pub fn best_match<'a>(setup: &CarSetup, saved: &'a [SvmFile], min_score: f64) -> Option<(f64, &'a SvmFile)> {
    saved
        .iter()
        .map(|f| (match_score(setup, f), f))
        .filter(|(s, _)| *s >= min_score)
        .max_by(|a, b| a.0.total_cmp(&b.0))
}

// `<folder>/<name_at(n)>.svm` with the lowest revision n not yet taken.
pub fn next_free_name(folder: &Path, name_at: impl Fn(u32) -> String) -> PathBuf {
    (1..).map(|n| folder.join(format!("{}.svm", name_at(n)))).find(|p| !p.exists()).expect("unbounded")
}
