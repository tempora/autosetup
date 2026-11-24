/* .svm, mapping and template tests.  Writes only go to temporary folders,
   never the LMU install.  The ignored tests read the local saved setups
   (UserData/player/Settings, read-only) and recordings.  */

use std::collections::HashMap;
use std::path::PathBuf;

use crate::lmu::svm::{match_score, SvmFile};
use crate::lmu::templates::{feel_text, file_safe, lap_text, render, NAME};
use crate::lmu::writer::{compose, write_proposal, WriteError};
use crate::setup::CarSetup;

// A minimal LMU-style file: header, CRLF, a Latin-1 byte (0xE9) in a comment.
fn sample_bytes() -> Vec<u8> {
    let mut v: Vec<u8> = b"VehicleClassSetting=\"Test GT3\"\r\nUpgradeSetting=(0,0,0,0)\r\n//VEH=Vehicles\\Test\\test.veh\r\n\r\n[GENERAL]\r\nNotes=\"\"\r\n[SUSPENSION]\r\nFrontAntiSwaySetting=5//P5\r\nRearAntiSwaySetting=3//P3 caf".to_vec();
    v.push(0xE9);
    v.extend_from_slice(b"\r\nWeird=1/2\r\n[FRONTLEFT]\r\nCompoundSetting=2//Medium\r\nRideHeightSetting=4//5.0 cm\r\n[FRONTRIGHT]\r\nRideHeightSetting=4//5.0 cm\r\n");
    v
}

fn sample() -> SvmFile {
    SvmFile::parse(&sample_bytes().iter().map(|&b| char::from(b)).collect::<String>())
}

fn temp_dir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("pitwall_svm_{tag}_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn setup(entries: &[(&str, i32, i32, &str)]) -> CarSetup {
    let body: Vec<String> = entries
        .iter()
        .map(|(k, max, v, text)| format!(r#""{k}": {{"available": true, "minValue": 0, "maxValue": {max}, "value": {v}, "stringValue": "{text}"}}"#))
        .collect();
    CarSetup::from_lmu_json(&format!("{{{}}}", body.join(", "))).unwrap()
}

fn driven() -> CarSetup {
    setup(&[
        ("VM_FRONT_ANTISWAY", 11, 5, "P5"),
        ("VM_REAR_ANTISWAY", 11, 3, "P3 caf"),
        ("WM_COMPOUND-W_FL", 4, 1, "Soft"),
        ("WM_RIDEHEIGHT-W_FL", 20, 4, "5.0 cm"),
        ("WM_RIDEHEIGHT-W_FR", 20, 4, "5.0 cm"),
        ("VM_BRAKE_DUCTS", 6, 2, "2"),
    ])
}

#[test]
fn files_round_trip_byte_for_byte() {
    let f = sample();
    assert_eq!(f.bytes(), sample_bytes());
    assert_eq!(f.vehicle_class(), "Test GT3");
    assert_eq!(f.veh_file(), "Vehicles\\Test\\test.veh");
    assert!(f.get(Some("SUSPENSION"), "Weird").is_none(), "a single '/' is not a comment");
    let v = f.vm_values();
    assert_eq!(v["VM_FRONT_ANTISWAY"], (5, Some("P5".to_string())));
    assert_eq!(v["WM_RIDEHEIGHT-W_FR"].0, 4);
}

#[test]
fn setting_a_value_changes_only_its_line() {
    let mut f = sample();
    let before = f.text();
    f.set("SUSPENSION", "FrontAntiSwaySetting", 7, None).unwrap();
    let after = f.text();
    let changed: Vec<(&str, &str)> = before.lines().zip(after.lines()).filter(|(a, b)| a != b).collect();
    assert_eq!(changed, vec![("FrontAntiSwaySetting=5//P5", "FrontAntiSwaySetting=7")]);
    assert!(f.text().contains("FrontAntiSwaySetting=7\r\n"), "line ending kept");
    assert_eq!(f.vm_values()["VM_FRONT_ANTISWAY"], (7, None));
}

#[test]
fn notes_are_plain_latin1_without_quotes_or_slashes() {
    let mut f = sample();
    f.set_notes("Pointier 0.25° → rear bar 1/2 \"up\" ✓\nnext");
    let notes = &f.get(Some("GENERAL"), "Notes").unwrap().raw;
    assert_eq!(notes, "\"Pointier 0.25° -> rear bar 1-2 'up' ? next\"");
    assert!(f.bytes().iter().all(|&b| b != 0));
}

#[test]
fn writing_never_replaces_a_file() {
    let dir = temp_dir("nooverwrite");
    let path = dir.join("a.svm");
    sample().write_new(&path).unwrap();
    assert!(sample().write_new(&path).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), sample_bytes());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn templates_drop_empty_fields_with_their_separator() {
    let fields: HashMap<&str, String> =
        [("track", "Monza".to_string()), ("car", String::new()), ("date", "2026-11-25".to_string()), ("n", "3".to_string())].into();
    assert_eq!(render(NAME, &fields), "Monza 2026-11-25 3");
    let lead: HashMap<&str, String> = [("track", String::new()), ("car", "BMW M4".to_string())].into();
    assert_eq!(render("{track} - {car} {unknown}", &lead), "BMW M4 {unknown}");
    assert_eq!(file_safe(" a:b/c?. "), "a-b-c-");
    assert_eq!(lap_text(103.456, false), "1:43.46");
    assert_eq!(lap_text(103.456, true), "1-43.46");
    assert_eq!(feel_text(-0.2, 0.1, 0.0), "Pointier 0.20° + Safer 0.10°");
    assert_eq!(feel_text(0.0, 0.0, 0.0), "As it was");
}

#[test]
fn composing_a_proposal_writes_every_setting_but_the_compound() {
    let driven = driven();
    let mut proposed = driven.clone();
    proposed.set_value("VM_REAR_ANTISWAY", 4);
    proposed.set_value("WM_COMPOUND-W_FL", 3);
    proposed.set_value("WM_RIDEHEIGHT-W_FL", 99);
    let (out, changes, missing) = compose(&sample(), &driven, &proposed, "From stint 3");
    let v = out.vm_values();
    assert_eq!(v["VM_REAR_ANTISWAY"], (4, None), "a changed setting loses its stale label");
    assert_eq!(v["VM_FRONT_ANTISWAY"], (5, Some("P5".to_string())), "an unchanged one keeps the recording's");
    assert_eq!(v["WM_RIDEHEIGHT-W_FL"].0, 19, "clamped to maxValue - 1");
    assert_eq!(v["WM_COMPOUND-W_FL"].0, 2, "the template's compound is kept");
    assert_eq!(missing, vec!["VM_BRAKE_DUCTS".to_string()]);
    let mut keys: Vec<&str> = changes.iter().map(|c| c.0.as_str()).collect();
    keys.sort_unstable();
    assert_eq!(keys, vec!["VM_REAR_ANTISWAY", "WM_COMPOUND-W_FL", "WM_RIDEHEIGHT-W_FL"]);
    assert_eq!(out.get(Some("GENERAL"), "Notes").unwrap().raw, "\"From stint 3\"");
}

#[test]
fn proposals_land_next_to_the_matching_template_under_a_free_name() {
    let dir = temp_dir("write").join("Monza");
    let template_path = dir.join("saved.svm");
    sample().write_new(&template_path).unwrap();
    let saved = vec![SvmFile::read(&template_path).unwrap()];
    let driven = driven();
    let mut proposed = driven.clone();
    proposed.set_value("VM_REAR_ANTISWAY", 4);
    let fields: HashMap<&str, String> = [("track", "Monza".to_string())].into();

    let a = write_proposal(&saved, &driven, &proposed, "{track} {n}", &fields, "notes").unwrap();
    let b = write_proposal(&saved, &driven, &proposed, "{track} {n}", &fields, "notes").unwrap();
    assert_eq!(a.path, dir.join("Monza 1.svm"));
    assert_eq!(b.path, dir.join("Monza 2.svm"));
    assert_eq!(SvmFile::read(&a.path).unwrap().vm_values()["VM_REAR_ANTISWAY"].0, 4);
    assert_eq!(std::fs::read(&template_path).unwrap(), sample_bytes(), "the template is untouched");

    let other = setup(&[("VM_FRONT_ANTISWAY", 11, 9, "P9"), ("VM_REAR_ANTISWAY", 11, 9, "P9")]);
    assert!(matches!(write_proposal(&saved, &other, &other, NAME, &fields, ""), Err(WriteError::NoTemplate)));
    assert!(match_score(&other, &saved[0]) < 0.5);
    let _ = std::fs::remove_dir_all(dir.parent().unwrap());
}

#[cfg(feature = "duckdb-native")]
mod local {
    use super::*;
    use crate::lmu::svm::{best_match, read_settings};
    use crate::telemetry::duckdb::DuckDbRecording;
    use crate::telemetry::TelemetrySource;
    use crate::testdata::local_recordings;

    fn settings_dir() -> Option<PathBuf> {
        let tel = PathBuf::from(std::env::var("PITWALL_TELEMETRY_DIR").ok()?);
        Some(tel.parent()?.join("player").join("Settings"))
    }

    /* Every saved setup round-trips byte for byte; recordings find their own
       car's saved setups, and a proposal composed on the best match reads back
       with exactly the proposed indices.  */
    #[test]
    #[ignore = "requires local LMU saved setups and recordings (PITWALL_TELEMETRY_DIR)"]
    fn real_saved_setups_round_trip_and_take_proposals() {
        let dir = settings_dir().expect("PITWALL_TELEMETRY_DIR");
        let saved = read_settings(&dir);
        assert!(!saved.is_empty(), "no saved setups under {}", dir.display());
        for f in &saved {
            let path = f.path.as_ref().unwrap();
            assert_eq!(f.bytes(), std::fs::read(path).unwrap(), "{} does not round-trip", path.display());
        }
        let (mut matched, mut tried) = (0, 0);
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            if meta.setup.params.is_empty() {
                continue;
            }
            tried += 1;
            let Some((score, template)) = best_match(&meta.setup, &saved, 0.85) else { continue };
            matched += 1;
            let mut proposed = meta.setup.clone();
            let key = ["VM_REAR_ANTISWAY", "VM_FRONT_ANTISWAY", "VM_BRAKE_BALANCE"]
                .into_iter()
                .find(|k| proposed.params.get(*k).is_some_and(|p| p.adjustable()))
                .unwrap();
            let p = &proposed.params[key];
            let to = if p.value > p.min { p.value - 1 } else { p.value + 1 };
            proposed.set_value(key, to);
            let (out, changes, _) = compose(template, &meta.setup, &proposed, "test");
            let back = out.vm_values();
            assert_eq!(changes, vec![(key.to_string(), meta.setup.params[key].value, to)]);
            for (k, p) in proposed.params.iter().filter(|(k, p)| p.adjustable() && !crate::lmu::mapping::do_not_write(k)) {
                if let Some((v, _)) = back.get(k) {
                    assert_eq!(*v, p.value, "{k} not written ({})", path.display());
                }
            }
            assert!(score >= 0.85);
        }
        println!("{} saved setups round-trip; {matched} of {tried} recordings matched a saved setup", saved.len());
        assert!(matched > 0, "no recording matched a saved setup");
    }
}
