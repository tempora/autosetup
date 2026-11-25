/* Local LMU recordings for ignored real-data tests.
   PITWALL_TELEMETRY_DIR lists every finished `.duckdb` in a directory
   (recordings with a `.duckdb.wal` beside them are still being driven and are
   skipped); otherwise PITWALL_TEST_RECORDING or the Monza recording at the
   repository root is used.  */

use std::path::{Path, PathBuf};

pub fn local_recordings() -> Vec<PathBuf> {
    if let Ok(dir) = std::env::var("PITWALL_TELEMETRY_DIR") {
        let mut v: Vec<PathBuf> = std::fs::read_dir(dir)
            .map(|rd| rd.filter_map(|e| e.ok().map(|e| e.path())).collect())
            .unwrap_or_default();
        v.retain(|p| p.extension().is_some_and(|e| e == "duckdb") && !wal_of(p).exists());
        v.sort();
        return v;
    }
    let single = std::env::var("PITWALL_TEST_RECORDING").map(PathBuf::from).unwrap_or_else(|_| {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../Autodromo Nazionale Monza_P_2026-11-13T16_58_22Z.duckdb")
    });
    vec![single]
}

fn wal_of(p: &Path) -> PathBuf {
    let mut s = p.as_os_str().to_owned();
    s.push(".wal");
    PathBuf::from(s)
}
