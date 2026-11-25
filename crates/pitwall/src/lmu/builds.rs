/* Which game build a recording was driven on.
   Recordings don't say (their Version metadata is the file format), so
   Steam's app manifest names the installed build and when it was installed.
   Every build seen is kept (the store persists them), and a recording
   belongs to the newest build installed at or before its RecordingTime.
   Only the lap-time model is limited to the current build; balance and feel
   use every build.  Times are Unix seconds, UTC.  */

use crate::lmu::paths::{vdf_value, LmuPaths};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GameBuild {
    pub id: String,
    pub installed_at: i64,
}

pub fn parse_manifest(text: &str) -> Option<GameBuild> {
    let id = vdf_value(text, "buildid").filter(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))?;
    let installed_at = vdf_value(text, "LastUpdated")?.parse().ok()?;
    Some(GameBuild { id: id.to_string(), installed_at })
}

pub fn installed_build(paths: &LmuPaths) -> Option<GameBuild> {
    parse_manifest(&std::fs::read_to_string(paths.manifest()?).ok()?)
}

// Known builds, oldest install first, one entry per build id.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BuildHistory(Vec<GameBuild>);

impl BuildHistory {
    pub fn new(builds: impl IntoIterator<Item = GameBuild>) -> Self {
        let mut v: Vec<GameBuild> = builds.into_iter().collect();
        v.sort_by(|a, b| (a.installed_at, &a.id).cmp(&(b.installed_at, &b.id)));
        let mut seen = std::collections::HashSet::new();
        v.retain(|b| seen.insert(b.id.clone()));
        Self(v)
    }

    pub fn builds(&self) -> &[GameBuild] {
        &self.0
    }

    pub fn current(&self) -> Option<&GameBuild> {
        self.0.last()
    }

    // None before the oldest known install (or with no builds known).
    pub fn build_at(&self, recorded_at: i64) -> Option<&GameBuild> {
        self.0.iter().take_while(|b| b.installed_at <= recorded_at).last()
    }
}

/* RecordingTime metadata and the file-name suffix, "2026-11-13T16_58_22Z",
   to Unix seconds.  */
pub fn recording_time(s: &str) -> Option<i64> {
    let b = s.trim().as_bytes();
    if b.len() != 20 || b[4] != b'-' || b[7] != b'-' || b[10] != b'T' || b[13] != b'_' || b[16] != b'_' || b[19] != b'Z' {
        return None;
    }
    let num = |r: std::ops::Range<usize>| s.trim().get(r).filter(|t| t.bytes().all(|c| c.is_ascii_digit()))?.parse::<i64>().ok();
    let (y, mo, d, h, mi, se) = (num(0..4)?, num(5..7)?, num(8..10)?, num(11..13)?, num(14..16)?, num(17..19)?);
    if !(1..=12).contains(&mo) || !(1..=31).contains(&d) || h > 23 || mi > 59 || se > 60 {
        return None;
    }
    Some(days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + se)
}

// Unix seconds back to RecordingTime's "2026-11-13T16_58_22Z".
pub fn format_recording_time(t: i64) -> String {
    let (days, secs) = (t.div_euclid(86_400), t.rem_euclid(86_400));
    let (y, m, d) = civil_from_days(days);
    format!("{y:04}-{m:02}-{d:02}T{:02}_{:02}_{:02}Z", secs / 3600, secs % 3600 / 60, secs % 60)
}

const fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + if m <= 2 { 1 } else { 0 }, m, d)
}

// Days since 1970-01-01 in the proleptic Gregorian calendar (Hinnant).
const fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}
