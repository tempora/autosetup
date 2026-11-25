/* .svm, mapping, scale, path, build, track-folder and template tests.
   Writes only go to temporary folders, never the LMU install.  The ignored
   tests read the local install, saved setups (UserData/player/Settings,
   read-only) and recordings.  */

use std::collections::HashMap;
use std::path::PathBuf;

use crate::lmu::builds::{parse_manifest, recording_time, BuildHistory, GameBuild};
use crate::lmu::paths::library_folders;
use crate::lmu::scales::{label_number, model_of, CarScales, Label};
use crate::lmu::svm::{match_score, SvmFile};
use crate::lmu::templates::{feel_text, file_safe, lap_text, render, NAME};
use crate::lmu::tracks::guess_folder;
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
    let (out, changes, missing) = compose(&sample(), &driven, &proposed, &CarScales::default(), "From stint 3");
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

    let a = write_proposal(&saved, &driven, &proposed, &CarScales::default(), "{track} {n}", &fields, "notes").unwrap();
    let b = write_proposal(&saved, &driven, &proposed, &CarScales::default(), "{track} {n}", &fields, "notes").unwrap();
    assert_eq!(a.path, dir.join("Monza 1.svm"));
    assert_eq!(b.path, dir.join("Monza 2.svm"));
    assert_eq!(SvmFile::read(&a.path).unwrap().vm_values()["VM_REAR_ANTISWAY"].0, 4);
    assert_eq!(std::fs::read(&template_path).unwrap(), sample_bytes(), "the template is untouched");

    let other = setup(&[("VM_FRONT_ANTISWAY", 11, 9, "P9"), ("VM_REAR_ANTISWAY", 11, 9, "P9")]);
    assert!(matches!(write_proposal(&saved, &other, &other, &CarScales::default(), NAME, &fields, ""), Err(WriteError::NoTemplate)));
    assert!(match_score(&other, &saved[0]) < 0.5);
    let _ = std::fs::remove_dir_all(dir.parent().unwrap());
}

fn exact(text: &str) -> Option<Label> {
    Some(Label { text: text.to_string(), exact: true })
}

fn guess(text: &str) -> Option<Label> {
    Some(Label { text: text.to_string(), exact: false })
}

#[test]
fn a_changed_setting_takes_the_label_seen_at_its_new_index() {
    let driven = driven();
    let seen_at_6 = setup(&[("VM_FRONT_ANTISWAY", 11, 6, "P6"), ("VM_REAR_ANTISWAY", 11, 3, "P3 caf")]);
    let scales = CarScales::collect([&driven, &seen_at_6], &[]);
    let mut proposed = driven.clone();
    proposed.set_value("VM_FRONT_ANTISWAY", 6);
    proposed.set_value("VM_REAR_ANTISWAY", 4);
    let (out, ..) = compose(&sample(), &driven, &proposed, &scales, "");
    let v = out.vm_values();
    assert_eq!(v["VM_FRONT_ANTISWAY"], (6, Some("P6".to_string())));
    assert_eq!(v["VM_REAR_ANTISWAY"], (4, None), "never seen at 4: no label rather than a guess");
}

#[test]
fn numeric_labels_interpolate_and_extrapolate_in_their_own_format() {
    let seen = [
        setup(&[("WM_CAMBER-W_FL", 40, 10, "-2.5 deg"), ("VM_BRAKE_BALANCE", 60, 14, "53.5:46.5"), ("VM_TRACTIONCONTROLMAP", 12, 6, "6")]),
        setup(&[("WM_CAMBER-W_FL", 40, 20, "-1.5 deg"), ("VM_BRAKE_BALANCE", 60, 54, "43.5:56.5"), ("VM_TRACTIONCONTROLMAP", 12, 9, "9")]),
        setup(&[("WM_RIDEHEIGHT-W_RL", 30, 5, "6.5 cm"), ("VM_DIFF_POWER", 8, 2, "FF6-30 deg"), ("VM_FRONT_ANTISWAY", 11, 5, "P5")]),
        setup(&[("VM_DIFF_POWER", 8, 4, "FF6-50 deg"), ("VM_ENGINE_BRAKEMAP", 4, 1, "1 (soft)"), ("VM_REAR_ANTISWAY", 6, 1, "25x2.5 1-1")]),
        setup(&[("VM_BRAKE_PRESSURE", 90, 74, "114 kgf (93%)"), ("VM_REAR_ANTISWAY", 6, 3, "25x2.5 3-3")]),
        setup(&[("VM_BRAKE_PRESSURE", 90, 78, "118 kgf (98%)"), ("VM_TRACTIONCONTROLSLIPANGLEMAP", 9, 0, "0 (Off)")]),
        setup(&[("VM_TRACTIONCONTROLSLIPANGLEMAP", 9, 4, "4")]),
    ];
    let s = CarScales::collect(&seen, &[]);
    assert_eq!(s.label("WM_CAMBER-W_FL", 20), exact("-1.5 deg"));
    assert_eq!(s.label("WM_CAMBER-W_FL", 13), guess("-2.2 deg"));
    assert_eq!(s.label("WM_CAMBER-W_FL", 25), guess("-1.0 deg"), "past the last point at the measured slope");
    assert_eq!(s.label("VM_BRAKE_BALANCE", 34), guess("48.5:51.5"), "front:rear, rearward with index");
    assert_eq!(s.label("VM_TRACTIONCONTROLMAP", 3), guess("3"), "labelled by its own index");
    assert_eq!(s.label("WM_RIDEHEIGHT-W_RL", 7), guess("6.7 cm"), "one point: the default 0.1 cm step");
    assert_eq!(s.label("VM_DIFF_POWER", 3), guess("FF6-40 deg"), "the ramp angle moves, the plates stay");
    assert_eq!(s.label("VM_ENGINE_BRAKEMAP", 2), None, "one point, no known step");
    assert_eq!(s.label("VM_FRONT_ANTISWAY", 7), None, "'P5' is not a number");
    assert_eq!(s.label("VM_REAR_ANTISWAY", 2), None, "'25x2.5' bars share one number: not a numeric scale");
    assert_eq!(s.label("VM_BRAKE_PRESSURE", 76), guess("116 kgf"), "the stale percentage is left out");
    assert_eq!(s.label("VM_TRACTIONCONTROLSLIPANGLEMAP", 1), guess("1"), "'(Off)' marks index 0 only");
    assert_eq!(s.get("WM_CAMBER-W_FL").and_then(|c| c.per_step()), Some(0.1));
    assert_eq!(label_number("8,500 N/mm"), Some(8500.0));
    assert_eq!(label_number("D35x1_1-1"), None);
    assert_eq!(label_number("118 kgf (98%)"), Some(118.0));
}

fn svm_with(class: &str, lines: &str) -> SvmFile {
    SvmFile::parse(&format!("VehicleClassSetting=\"{class}\"\r\n[SUSPENSION]\r\n{lines}"))
}

#[test]
fn other_versions_of_a_model_add_labels_only_where_they_agree() {
    let rec = setup(&[("VM_FRONT_ANTISWAY", 11, 5, "P5"), ("VM_REAR_ANTISWAY", 11, 3, "P3")]);
    let this_version = svm_with("WEC2025 GT3 Porsche_911", "FrontAntiSwaySetting=5//P5\r\nRearAntiSwaySetting=3//P3\r\n");
    // Agrees at front index 5 and adds 7; relabels rear index 3 after an update.
    let older = svm_with("WEC2024 GT3 Porsche_911", "FrontAntiSwaySetting=7//P7\r\nRearAntiSwaySetting=3//P2\r\n");
    let older_front = svm_with("WEC2024 GT3 Porsche_911", "FrontAntiSwaySetting=5//P5\r\nRearAntiSwaySetting=4//P3\r\n");
    let other_car = svm_with("WEC2025 GT3 Ferrari_296", "FrontAntiSwaySetting=8//F8\r\nRearAntiSwaySetting=8//F8\r\n");
    let s = CarScales::collect([&rec], &[this_version, older, older_front, other_car]);
    assert_eq!(s.exact("VM_FRONT_ANTISWAY", 7), Some("P7"));
    assert_eq!(s.exact("VM_REAR_ANTISWAY", 4), None, "rear labels disagree with this version at index 3");
    assert_eq!(s.exact("VM_FRONT_ANTISWAY", 8), None, "another car model");
    assert_eq!(model_of("ELMS2026 LMP2_ELMS Oreca_07").as_deref(), Some("Oreca_07"));
    assert_eq!(model_of("Hypercar Peugeot").as_deref(), Some("Peugeot"));
}

#[test]
fn steam_files_give_lmus_library_first_and_its_build() {
    let vdf = "\"libraryfolders\"\n{\n\t\"0\"\n\t{\n\t\t\"path\"\t\t\"C:\\\\Program Files (x86)\\\\Steam\"\n\t\t\"apps\"\n\t\t{\n\t\t\t\"228980\"\t\t\"578387737\"\n\t\t}\n\t}\n\t\"1\"\n\t{\n\t\t\"path\"\t\t\"G:\\\\SteamLibrary\"\n\t\t\"apps\"\n\t\t{\n\t\t\t\"2399420\"\t\t\"53572169078\"\n\t\t}\n\t}\n}\n";
    assert_eq!(library_folders(vdf), vec![PathBuf::from(r"G:\SteamLibrary"), PathBuf::from(r"C:\Program Files (x86)\Steam")]);
    let acf = "\"AppState\"\n{\n\t\"appid\"\t\t\"2399420\"\n\t\"installdir\"\t\t\"Le Mans Ultimate\"\n\t\"LastUpdated\"\t\t\"1791320819\"\n\t\"TargetBuildID\"\t\t\"1\"\n\t\"buildid\"\t\t\"25661166\"\n}\n";
    assert_eq!(parse_manifest(acf), Some(GameBuild { id: "25661166".into(), installed_at: 1_791_320_819 }));
    assert_eq!(parse_manifest("\"buildid\"\t\"\"\n\"LastUpdated\"\t\"1\""), None);
}

#[test]
fn a_recording_belongs_to_the_newest_build_installed_before_it() {
    assert_eq!(recording_time("2026-11-13T16_58_22Z"), Some(1_794_589_102));
    assert_eq!(recording_time("2000-02-29T00_00_00Z"), Some(951_782_400));
    assert_eq!(recording_time("2026-13-13T16_58_22Z"), None);
    assert_eq!(recording_time("2026-11-13T16:58:22Z"), None);
    let b = |id: &str, t: i64| GameBuild { id: id.into(), installed_at: t };
    let h = BuildHistory::new([b("2", 200), b("1", 100), b("2", 250), b("3", 300)]);
    assert_eq!(h.builds().iter().map(|g| g.id.as_str()).collect::<Vec<_>>(), ["1", "2", "3"]);
    assert_eq!(h.build_at(99), None);
    assert_eq!(h.build_at(200).map(|g| g.id.as_str()), Some("2"));
    assert_eq!(h.build_at(299).map(|g| g.id.as_str()), Some("2"));
    assert_eq!(h.current().map(|g| g.id.as_str()), Some("3"));
}

#[test]
fn track_names_find_their_settings_folder() {
    let folders = [
        "Bahrain", "Circuit Of The Americas", "Circuit de Barcelona", "Daytonarc", "Fuji", "Imola", "Interlagos", "LagunaSeca",
        "Lemans", "Longbeach", "Monza", "Paulricard", "Portimao", "Qatar", "Road Atlanta", "Sebring", "Silverstone",
        "Silverstone_International", "Silverstone_National", "Silverstonewec", "Spa",
    ];
    for (track, folder) in [
        ("Circuit de la Sarthe", "Lemans"),
        ("Lusail International Circuit", "Qatar"),
        ("Autódromo José Carlos Pace", "Interlagos"),
        ("Autodromo Enzo e Dino Ferrari", "Imola"),
        ("Circuit de Spa-Francorchamps", "Spa"),
        ("Autodromo Nazionale Monza", "Monza"),
        ("Fuji Speedway", "Fuji"),
        ("Silverstone Circuit", "Silverstone"),
        ("Silverstone National", "Silverstone_National"),
        ("Bahrain International Circuit", "Bahrain"),
    ] {
        assert_eq!(guess_folder(track, folders), Some(folder), "{track}");
    }
    assert_eq!(guess_folder("Nürburgring", folders), None);
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
       with exactly the proposed indices and, for the changed setting, the
       label its car's scale has seen there.  */
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
        let recordings: Vec<(PathBuf, crate::telemetry::SessionMeta)> = local_recordings()
            .into_iter()
            .filter_map(|p| DuckDbRecording::open(&p).and_then(|r| r.metadata()).ok().map(|m| (p, m)))
            .filter(|(_, m)| !m.setup.params.is_empty())
            .collect();
        let (mut matched, mut tried, mut labelled) = (0, 0, 0);
        for (path, meta) in &recordings {
            let sig = meta.setup.signature();
            let scales = CarScales::collect(recordings.iter().map(|(_, m)| &m.setup).filter(|s| s.signature() == sig), &saved);
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
            let (out, changes, _) = compose(template, &meta.setup, &proposed, &scales, "test");
            let back = out.vm_values();
            assert_eq!(changes, vec![(key.to_string(), meta.setup.params[key].value, to)]);
            if let Some((_, label)) = back.get(key) {
                assert_eq!(label.as_deref(), scales.exact(key, to), "{key} label ({})", path.display());
                labelled += usize::from(label.is_some());
            }
            for (k, p) in proposed.params.iter().filter(|(k, p)| p.adjustable() && !crate::lmu::mapping::do_not_write(k)) {
                if let Some((v, _)) = back.get(k) {
                    assert_eq!(*v, p.value, "{k} not written ({})", path.display());
                }
            }
            assert!(score >= 0.85);
        }
        println!("{} saved setups round-trip; {matched} of {tried} recordings matched a saved setup; {labelled} changed settings labelled", saved.len());
        assert!(matched > 0, "no recording matched a saved setup");
    }

    /* Path discovery finds the install holding PITWALL_TELEMETRY_DIR from
       Steam alone, its manifest names a build, and every recording's track
       resolves to an existing Settings folder.  */
    #[test]
    #[ignore = "requires the local LMU install and recordings (PITWALL_TELEMETRY_DIR)"]
    fn real_install_builds_and_track_folders_resolve() {
        use crate::lmu::builds::installed_build;
        use crate::lmu::paths::{find_install, ENV_OVERRIDE};
        use crate::lmu::tracks::guess_folder_in;

        assert!(std::env::var_os(ENV_OVERRIDE).is_none(), "unset {ENV_OVERRIDE} to test Steam discovery");
        let paths = find_install().expect("LMU install");
        let tel = PathBuf::from(std::env::var("PITWALL_TELEMETRY_DIR").expect("PITWALL_TELEMETRY_DIR"));
        assert_eq!(paths.telemetry().canonicalize().unwrap(), tel.canonicalize().unwrap());
        let build = installed_build(&paths).expect("Steam manifest with a build id");
        let history = BuildHistory::new([build.clone()]);

        let mut tracks: std::collections::BTreeMap<String, Option<PathBuf>> = Default::default();
        let (mut timed, mut current, mut total) = (0, 0, 0);
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            total += 1;
            if let Some(t) = meta.recorded_at {
                timed += 1;
                current += usize::from(history.build_at(t).is_some());
            }
            tracks.entry(meta.track.clone()).or_insert_with(|| guess_folder_in(&meta.track, &paths.settings()));
        }
        for (track, folder) in &tracks {
            println!("{track:<40} -> {}", folder.as_ref().map_or("NONE".into(), |f| f.display().to_string()));
        }
        println!("{} at {}; build {} installed {}; {timed}/{total} recordings timed, {current} on this build", paths.root.display(), tel.display(), build.id, build.installed_at);
        let unresolved: Vec<&String> = tracks.iter().filter(|(_, f)| f.is_none()).map(|(t, _)| t).collect();
        assert!(unresolved.is_empty(), "tracks without a Settings folder: {unresolved:?}");
        assert_eq!(timed, total, "recordings without a RecordingTime");
    }

    /* rules.rs takes a higher index as a higher label value for tyre
       pressure, camber (less negative) and ride height.  Every car whose
       scale shows two or more numeric points must agree; brake balance runs
       rearward (lower front share).  The damper planner takes a higher index
       as stiffer: wherever the game marks "(soft)" or "(hard)", soft sits in
       the lower half of the seen range and below every hard mark, hard in the
       upper half, whichever way the label numbers run (an anti-roll bar's
       index 0 can sit below "P1 (soft)").  Ducts and dampers are printed.  */
    #[test]
    #[ignore = "requires local LMU recordings and saved setups (PITWALL_TELEMETRY_DIR)"]
    fn real_label_directions_match_rule_assumptions() {
        use crate::lmu::svm::read_settings;
        use std::collections::BTreeMap;

        let saved = settings_dir().map(|d| read_settings(&d)).unwrap_or_default();
        let mut by_car: BTreeMap<String, (String, Vec<CarSetup>)> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            if !meta.setup.params.is_empty() {
                let e = by_car.entry(meta.setup.signature()).or_insert_with(|| (meta.car_name.clone(), Vec::new()));
                e.1.push(meta.setup);
            }
        }
        let expect = [("WM_PRESSURE", 1.0), ("WM_CAMBER", 1.0), ("WM_RIDEHEIGHT", 1.0), ("VM_BRAKE_BALANCE", -1.0)];
        let shown = ["VM_BRAKE_DUCTS", "VM_BRAKE_DUCTS_REAR", "WM_FASTBUMP", "WM_SLOWBUMP", "WM_FASTREBOUND", "WM_SLOWREBOUND"];
        let mut wrong = Vec::new();
        let (mut checked, mut marked) = (0, 0);
        for (car, setups) in by_car.values() {
            let scales = CarScales::collect(setups, &saved);
            let mut line = Vec::new();
            for key in setups[0].params.keys() {
                let family = key.split("-W_").next().unwrap_or(key);
                let Some(scale) = scales.get(key).filter(|s| s.points().len() >= 2) else { continue };
                let (lo, hi) = (*scale.points().keys().next().unwrap(), *scale.points().keys().last().unwrap());
                let marks = |m: &str| scale.points().iter().filter(|(_, l)| l.contains(m)).map(|(&i, _)| i).collect::<Vec<_>>();
                let (soft, hard) = (marks("(soft)"), marks("(hard)"));
                marked += soft.len() + hard.len();
                let mid = lo + hi; // twice the midpoint
                if soft.iter().any(|&i| 2 * i > mid || hard.iter().any(|&h| h <= i)) || hard.iter().any(|&i| 2 * i < mid) {
                    wrong.push(format!("{car}: {key} soft at {soft:?}, hard at {hard:?} of {lo}..={hi}"));
                }
                let Some(step) = scale.per_step() else { continue };
                if let Some(&(_, sign)) = expect.iter().find(|(f, _)| *f == family) {
                    checked += 1;
                    if step * sign <= 0.0 {
                        wrong.push(format!("{car}: {key} {step:+}"));
                    }
                } else if shown.contains(&family) && !line.iter().any(|(f, _): &(&str, f64)| *f == family) {
                    line.push((family, step));
                }
            }
            println!("{car:<40} {}", line.iter().map(|(f, s)| format!("{f} {s:+.3}")).collect::<Vec<_>>().join("  "));
        }
        println!("{checked} pressure/camber/ride-height/brake-balance scales checked, {marked} soft/hard ends");
        assert!(checked > 0 && marked > 0);
        assert!(wrong.is_empty(), "label direction against the rules: {wrong:?}");
    }

    /* Leave one recording out: scales built from the car's other recordings
       predict the labels of the held-out setup at indices they never saw.
       Between two seen points the label must come out exactly, apart from
       the bracketed notes guesses leave out; past the last one it is
       reported but not asserted.  */
    #[test]
    #[ignore = "requires local LMU recordings (PITWALL_TELEMETRY_DIR)"]
    fn real_scales_predict_held_out_labels() {
        use crate::lmu::scales::without_notes;
        use std::collections::BTreeMap;

        let mut by_car: BTreeMap<String, Vec<CarSetup>> = BTreeMap::new();
        for path in local_recordings() {
            let Ok(meta) = DuckDbRecording::open(&path).and_then(|r| r.metadata()) else { continue };
            if !meta.setup.params.is_empty() {
                by_car.entry(meta.setup.signature()).or_default().push(meta.setup);
            }
        }
        // (key family, between seen points) -> (exact, total, examples of misses)
        let mut tally: BTreeMap<(String, bool), (usize, usize, Vec<String>)> = BTreeMap::new();
        for setups in by_car.values().filter(|s| s.len() >= 3) {
            for (n, held) in setups.iter().enumerate() {
                let rest: Vec<&CarSetup> = setups.iter().enumerate().filter(|&(m, _)| m != n).map(|(_, s)| s).collect();
                let scales = CarScales::collect(rest.iter().copied(), &[]);
                for (key, p) in held.params.iter().filter(|(_, p)| p.adjustable() && !p.text.trim().is_empty()) {
                    let Some(scale) = scales.get(key) else { continue };
                    if scales.exact(key, p.value).is_some() {
                        continue;
                    }
                    let Some(pred) = scale.label(p.value) else { continue };
                    let seen: Vec<i32> = rest.iter().filter_map(|s| s.params.get(key)).map(|q| q.value).collect();
                    let between = seen.iter().any(|&i| i < p.value) && seen.iter().any(|&i| i > p.value);
                    let family = key.split("-W_").next().unwrap_or(key).to_string();
                    let t = tally.entry((family, between)).or_default();
                    t.1 += 1;
                    if pred.text == without_notes(&p.text) {
                        t.0 += 1;
                    } else if t.2.len() < 3 {
                        t.2.push(format!("{key}@{}: {:?} vs {:?}", p.value, pred.text, p.text.trim()));
                    }
                }
            }
        }
        let (mut hit, mut all) = (0, 0);
        for ((family, between), (ok, n, misses)) in &tally {
            println!("{family:<32} {:<8} {ok:>4}/{n:<4} {}", if *between { "between" } else { "outside" }, misses.join("; "));
            if *between {
                hit += ok;
                all += n;
            }
        }
        println!("between seen points: {hit}/{all} exact");
        assert!(all > 0, "no held-out label fell between two seen points");
        assert!(hit as f64 >= 0.95 * all as f64, "only {hit}/{all} interpolated labels exact");
    }
}
