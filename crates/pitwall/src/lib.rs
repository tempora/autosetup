pub mod chassis;
pub mod lmu;
pub mod metrics;
pub mod opt;
pub mod physics;
pub mod setup;
pub mod telemetry;
pub mod units;
pub mod validation;

#[cfg(all(test, feature = "duckdb-native"))]
mod testdata;

#[cfg(test)]
mod tests {
    use crate::chassis::CornerPhase;
    use crate::physics::{CornerState, MechanicalModel, QssModel, VehicleParams};
    use crate::setup::MechanicalSetup;
    use crate::units::{Degrees, Gs, MetersPerSec};
    use crate::validation::{evaluate_holdout, HoldoutPair, StintObservation};

    #[test]
    fn qss_stiffer_rear_arb_reduces_understeer_and_shifts_lltd_rearward() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let base = MechanicalSetup::default();
        let mut stiffer_rear = base;
        stiffer_rear.arb.rear = 0.9;

        let state = CornerState::steady(MetersPerSec::from_kmh(100.0), CornerPhase::Mid);
        let p0 = model.evaluate(&base, state);
        let p1 = model.evaluate(&stiffer_rear, state);

        assert!(p1.lltd_front < p0.lltd_front);
        assert!(p1.understeer < p0.understeer);
    }

    #[test]
    fn qss_balanced_lltd_yields_higher_peak_lat_g_than_extreme_imbalance() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let balanced = MechanicalSetup::default();
        let mut extreme_front = balanced;
        extreme_front.arb.front = 1.0;
        extreme_front.spring.front = 1.0;
        extreme_front.arb.rear = 0.0;
        extreme_front.spring.rear = 0.0;

        let state = CornerState::steady(MetersPerSec::from_kmh(110.0), CornerPhase::Mid);
        let p_bal = model.evaluate(&balanced, state);
        let p_ext = model.evaluate(&extreme_front, state);

        assert!(p_bal.max_lat_g > p_ext.max_lat_g);
        assert!(p_ext.understeer > p_bal.understeer);
    }

    #[test]
    fn qss_trail_braking_with_more_front_bias_adds_entry_understeer() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let rear_bias = MechanicalSetup { brake_bias: 0.0, ..MechanicalSetup::default() };
        let front_bias = MechanicalSetup { brake_bias: 1.0, ..MechanicalSetup::default() };
        let trail = CornerState { a_lon: Gs::new(-0.6), ..CornerState::steady(MetersPerSec::from_kmh(120.0), CornerPhase::Entry) };

        let p_rear = model.evaluate(&rear_bias, trail);
        let p_front = model.evaluate(&front_bias, trail);
        assert!(p_front.understeer > p_rear.understeer);

        // Trail braking costs lateral grip compared with a coasting entry.
        let coast = model.evaluate(&front_bias, CornerState { a_lon: Gs::ZERO, ..trail });
        assert!(p_front.max_lat_g < coast.max_lat_g);
    }

    #[test]
    fn qss_entry_lateral_capacity_falls_monotonically_with_braking_and_never_collapses() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let setup = MechanicalSetup::default();
        let lat_at = |a_lon: f64| {
            let state = CornerState { a_lon: Gs::new(a_lon), ..CornerState::steady(MetersPerSec::from_kmh(150.0), CornerPhase::Entry) };
            model.evaluate(&setup, state).max_lat_g.raw()
        };
        let sweep: Vec<f64> = (0..=30).map(|k| lat_at(-0.1 * k as f64)).collect();
        assert!(sweep.iter().all(|g| g.is_finite() && *g > 0.0), "{sweep:?}");
        assert!(sweep.windows(2).all(|w| w[1] <= w[0] + 1e-9), "not monotone: {sweep:?}");

        // Straight-line braking spends no lateral grip on the friction circle.
        let braking = |a_lon: f64| {
            let state = CornerState { a_lon: Gs::new(a_lon), ..CornerState::steady(MetersPerSec::from_kmh(150.0), CornerPhase::Braking) };
            model.evaluate(&setup, state).max_lat_g.raw()
        };
        assert!(braking(-1.5) > lat_at(-1.5));
    }

    #[test]
    fn holdout_validator_scores_direction_and_mae() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let state = CornerState::steady(MetersPerSec::from_kmh(100.0), CornerPhase::Mid);
        let a = MechanicalSetup::default();
        let mut b = a;
        b.arb.rear = 0.85;

        let pred_a = model.evaluate(&a, state);
        let pred_b = model.evaluate(&b, state);
        let pair = HoldoutPair {
            baseline: StintObservation {
                setup: a,
                state,
                measured_ua: Degrees::new(0.40),
                measured_lat_g: Gs::new(1.50),
            },
            holdout: StintObservation {
                setup: b,
                state,
                measured_ua: Degrees::new(0.40) + (pred_b.understeer - pred_a.understeer),
                measured_lat_g: Gs::new(1.50) + (pred_b.max_lat_g - pred_a.max_lat_g),
            },
        };

        let report = evaluate_holdout(&model, &[pair]);
        assert_eq!(report.pairs, 1);
        assert!(report.ua_mae_deg < 1e-9);
        assert!(report.lat_g_mae < 1e-9);
        assert_eq!(report.ua_direction_accuracy, 1.0);
    }

    #[cfg(feature = "duckdb-native")]
    #[test]
    fn duckdb_reads_fixed_rate_surface_types_and_flat_car_setup() {
        use ::duckdb::Connection;

        use crate::telemetry::duckdb::DuckDbRecording;
        use crate::telemetry::{count_kerb_events, ChannelKind, TelemetrySource, KERB_SURFACE_CODE};
        use crate::units::Seconds;

        let path = std::env::temp_dir().join(format!(
            "pitwall_fixture_{}.duckdb",
            std::process::id()
        ));
        let _ = std::fs::remove_file(&path);

        {
            let conn = Connection::open(&path).expect("create fixture duckdb");
            let setup_json = r#"{
                "VM_REAR_ANTISWAY": {"available": true, "minValue": 0, "maxValue": 11, "value": 8, "stringValue": "8"},
                "WM_SPRING-W_FL": {"available": true, "minValue": 0, "maxValue": 5, "value": 2, "stringValue": "150 N/mm"},
                "WM_SPRING-W_FR": {"available": true, "minValue": 0, "maxValue": 5, "value": 2, "stringValue": "150 N/mm"}
            }"#;
            conn.execute_batch(&format!(
                "create table metadata (key varchar, value varchar);
                 insert into metadata values
                   ('TrackName', 'Autodromo Nazionale Monza'),
                   ('CarName', 'Test GT3 #1:LM'),
                   ('CarClass', 'GT3'),
                   ('SessionType', 'Practice'),
                   ('CarSetup', '{}');
                 create table channelsList (channelName varchar, frequency integer, unit varchar);
                 insert into channelsList values
                   ('GPS Time', 100, 's'),
                   ('Susp Pos', 100, 'm'),
                   ('SurfaceTypes', 5, '');
                 create table \"GPS Time\" (value double);
                 create table \"Susp Pos\" (value1 double, value2 double, value3 double, value4 double);
                 create table \"SurfaceTypes\" (value1 utinyint, value2 utinyint, value3 utinyint, value4 utinyint);",
                setup_json.replace('\'', "''")
            ))
            .expect("init schema");

            for i in 0..200 {
                let t = i as f64 / 100.0;
                conn.execute("insert into \"GPS Time\" values (?)", [t])
                    .unwrap();
                conn.execute(
                    "insert into \"Susp Pos\" values (0.05, 0.05, 0.05, 0.05)",
                    [],
                )
                .unwrap();
            }
            // 5 Hz over 2.0 s = 10 samples; two separated kerb strikes at 0.2 s and 1.2 s.
            for i in 0..10 {
                let fl = if i == 1 || i == 6 { KERB_SURFACE_CODE } else { 0 };
                conn.execute(
                    "insert into \"SurfaceTypes\" values (?, 0, 0, 0)",
                    [fl],
                )
                .unwrap();
            }
        }

        let rec = DuckDbRecording::open(&path).expect("open fixture recording");
        let meta = rec.metadata().expect("read metadata");
        assert_eq!(meta.track, "Autodromo Nazionale Monza");
        assert_eq!(meta.car_name, "Test GT3 #1");
        assert!((meta.setup.fraction("VM_REAR_ANTISWAY").unwrap() - 0.8).abs() < 1e-9);
        assert!((meta.setup.mechanical_vector().spring.front - 0.5).abs() < 1e-9);

        match rec.channel_kind("SurfaceTypes") {
            Some(ChannelKind::FixedRate { hz }) => assert!((hz - 5.0).abs() < 1e-6),
            other => panic!("expected FixedRate(5 Hz), got {other:?}"),
        }

        let susp = rec.wheel_channel("Susp Pos").expect("read Susp Pos");
        let surf = rec.wheel_surface().expect("read SurfaceTypes");
        let kerb_mask: Vec<_> = surf
            .sample_zoh(&susp.times)
            .into_iter()
            .map(|w| w.map(|code| code == KERB_SURFACE_CODE))
            .collect();
        assert_eq!(count_kerb_events(&susp.times, &kerb_mask, Seconds::new(0.25)), 2);

        drop(rec);
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(feature = "duckdb-native")]
    #[test]
    #[ignore = "requires local LMU .duckdb recording (set PITWALL_TEST_RECORDING or place Monza recording at repo root)"]
    fn local_recording_smoke_test() {
        use std::path::PathBuf;

        use crate::telemetry::duckdb::DuckDbRecording;
        use crate::telemetry::{count_kerb_events, ChannelKind, TelemetrySource, KERB_SURFACE_CODE};
        use crate::units::Seconds;

        let path = std::env::var("PITWALL_TEST_RECORDING")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("../../Autodromo Nazionale Monza_P_2026-11-13T16_58_22Z.duckdb")
            });
        let rec = DuckDbRecording::open(&path).expect("open local LMU recording");
        let meta = rec.metadata().expect("read metadata");
        assert!(!meta.track.is_empty());
        assert!(!meta.setup.params.is_empty());

        match rec.channel_kind("SurfaceTypes") {
            Some(ChannelKind::FixedRate { hz }) => assert!(hz > 0.0),
            other => panic!("expected SurfaceTypes FixedRate, got {other:?}"),
        }

        let susp = rec.wheel_channel("Susp Pos").expect("read Susp Pos");
        let surf = rec.wheel_surface().expect("read SurfaceTypes");
        let kerb_mask: Vec<_> = surf
            .sample_zoh(&susp.times)
            .into_iter()
            .map(|w| w.map(|code| code == KERB_SURFACE_CODE))
            .collect();
        let events = count_kerb_events(&susp.times, &kerb_mask, Seconds::new(0.25));
        assert!(events > 0, "expected kerb events in local recording");
    }
}
