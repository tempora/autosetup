pub mod chassis;
pub mod physics;
pub mod setup;
pub mod telemetry;
pub mod units;
pub mod validation;

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

        let state = CornerState {
            speed: MetersPerSec::from_kmh(100.0),
            phase: CornerPhase::Mid,
        };
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

        let state = CornerState {
            speed: MetersPerSec::from_kmh(110.0),
            phase: CornerPhase::Mid,
        };
        let p_bal = model.evaluate(&balanced, state);
        let p_ext = model.evaluate(&extreme_front, state);

        assert!(p_bal.max_lat_g > p_ext.max_lat_g);
        assert!(p_ext.understeer > p_bal.understeer);
    }

    #[test]
    fn holdout_validator_scores_direction_and_mae() {
        let model = QssModel::new(VehicleParams::gt3_baseline());
        let state = CornerState {
            speed: MetersPerSec::from_kmh(100.0),
            phase: CornerPhase::Mid,
        };
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
    fn reads_real_monza_duckdb_fixed_rate_surface_types_and_setup() {
        use std::path::Path;

        use crate::telemetry::duckdb::DuckDbRecording;
        use crate::telemetry::{count_kerb_events, ChannelKind, TelemetrySource, KERB_SURFACE_CODE};
        use crate::units::Seconds;

        let monza = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../Autodromo Nazionale Monza_P_2026-11-13T16_58_22Z.duckdb");
        if !monza.exists() {
            return;
        }

        let rec = DuckDbRecording::open(&monza).expect("open real Monza recording");
        let meta = rec.metadata().expect("read metadata");
        assert!(meta.track.contains("Monza"));
        assert!(!meta.setup.params.is_empty());

        match rec.channel_kind("SurfaceTypes") {
            Some(ChannelKind::FixedRate { hz }) => assert!((hz - 5.0).abs() < 1e-6),
            other => panic!("expected SurfaceTypes FixedRate(5 Hz), got {other:?}"),
        }

        let susp = rec.wheel_channel("Susp Pos").expect("read Susp Pos");
        let surf = rec.wheel_surface().expect("read SurfaceTypes");
        let zoh = surf.sample_zoh(&susp.times);
        let kerb_mask: Vec<_> = zoh
            .into_iter()
            .map(|w| w.map(|code| code == KERB_SURFACE_CODE))
            .collect();
        let events = count_kerb_events(&susp.times, &kerb_mask, Seconds::new(0.25));
        assert!(events >= 60, "expected >= 60 Monza kerb events, got {events}");
    }
}
