/* Telemetry CarSetup keys (VM_* / WM_*) to .svm (section, key) pairs.
   Both sides store the same step index; only the names differ.  The table
   covers every .svm key seen in the development install's saved setups.  The
   .svm CompoundSetting index also encodes the tyre set and does not follow
   WM_COMPOUND, so it is never written from telemetry (DO_NOT_WRITE).  */

use std::collections::HashMap;
use std::sync::OnceLock;

const CAR_WIDE: &[(&str, &str, &str)] = &[
    ("VM_WEIGHT_VERTICAL", "GENERAL", "CGHeightSetting"),
    ("VM_WEIGHT_LATERAL", "GENERAL", "CGRightSetting"),
    ("VM_WEIGHT_DISTRIB", "GENERAL", "CGRearSetting"),
    ("VM_WEIGHT_WEDGE", "GENERAL", "WedgeSetting"),
    ("VM_FUEL_LEVEL", "GENERAL", "FuelSetting"),
    ("VM_FUEL_CAPACITY", "GENERAL", "FuelCapacitySetting"),
    ("VM_VIRTUAL_ENERGY", "GENERAL", "VirtualEnergySetting"),
    ("VM_NUM_PITSTOPS", "GENERAL", "NumPitstopsSetting"),
    ("VM_PITSTOP_1", "GENERAL", "Pitstop1Setting"),
    ("VM_PITSTOP_2", "GENERAL", "Pitstop2Setting"),
    ("VM_PITSTOP_3", "GENERAL", "Pitstop3Setting"),
    ("VM_LEFT_FENDER_FLARE", "LEFTFENDER", "FenderFlareSetting"),
    ("VM_RIGHT_FENDER_FLARE", "RIGHTFENDER", "FenderFlareSetting"),
    ("VM_FRONT_WING", "FRONTWING", "FWSetting"),
    ("VM_REAR_WING", "REARWING", "RWSetting"),
    ("VM_WATER_RADIATOR", "BODYAERO", "WaterRadiatorSetting"),
    ("VM_OIL_RADIATOR", "BODYAERO", "OilRadiatorSetting"),
    ("VM_BRAKE_DUCTS", "BODYAERO", "BrakeDuctSetting"),
    ("VM_BRAKE_DUCTS_REAR", "BODYAERO", "BrakeDuctRearSetting"),
    ("VM_FRONT_WHEEL_TRACK", "SUSPENSION", "FrontWheelTrackSetting"),
    ("VM_REAR_WHEEL_TRACK", "SUSPENSION", "RearWheelTrackSetting"),
    ("VM_FRONT_ANTISWAY", "SUSPENSION", "FrontAntiSwaySetting"),
    ("VM_REAR_ANTISWAY", "SUSPENSION", "RearAntiSwaySetting"),
    ("VM_FRONT_TOEIN", "SUSPENSION", "FrontToeInSetting"),
    ("VM_FRONT_TOEOFFSET", "SUSPENSION", "FrontToeOffsetSetting"),
    ("VM_REAR_TOEIN", "SUSPENSION", "RearToeInSetting"),
    ("VM_REAR_TOEOFFSET", "SUSPENSION", "RearToeOffsetSetting"),
    ("VM_LEFT_CASTER", "SUSPENSION", "LeftCasterSetting"),
    ("VM_RIGHT_CASTER", "SUSPENSION", "RightCasterSetting"),
    ("VM_LEFT_TRACK_BAR", "SUSPENSION", "LeftTrackBarSetting"),
    ("VM_RIGHT_TRACK_BAR", "SUSPENSION", "RightTrackBarSetting"),
    ("VM_STEER_LOCK", "CONTROLS", "SteerLockSetting"),
    ("VM_BRAKE_BALANCE", "CONTROLS", "RearBrakeSetting"),
    ("VM_BRAKE_MIGRATION", "CONTROLS", "BrakeMigrationSetting"),
    ("VM_BRAKE_PRESSURE", "CONTROLS", "BrakePressureSetting"),
    ("VM_HANDFRONTBRAKE_PRESSURE", "CONTROLS", "HandfrontbrakePressSetting"),
    ("VM_HANDBRAKE_PRESSURE", "CONTROLS", "HandbrakePressSetting"),
    ("VM_TRACTION_CONTROL", "CONTROLS", "TCSetting"),
    ("VM_ANTILOCK_BRAKES", "CONTROLS", "ABSSetting"),
    ("VM_TRACTIONCONTROLMAP", "CONTROLS", "TractionControlMapSetting"),
    ("VM_TRACTIONCONTROLPOWERCUTMAP", "CONTROLS", "TCPowerCutMapSetting"),
    ("VM_TRACTIONCONTROLSLIPANGLEMAP", "CONTROLS", "TCSlipAngleMapSetting"),
    ("VM_ANTILOCKBRAKESYSTEMMAP", "CONTROLS", "AntilockBrakeSystemMapSetting"),
    ("VM_REV_LIMITER", "ENGINE", "RevLimitSetting"),
    ("VM_ENGINE_BOOST", "ENGINE", "EngineBoostSetting"),
    ("VM_REGEN_LEVEL", "ENGINE", "RegenerationMapSetting"),
    ("VM_ELECTRIC_MOTOR_MAP", "ENGINE", "ElectricMotorMapSetting"),
    ("VM_ENGINE_MIXTURE", "ENGINE", "EngineMixtureSetting"),
    ("VM_ENGINE_BRAKEMAP", "ENGINE", "EngineBrakingMapSetting"),
    ("VM_GEAR_FINAL", "DRIVELINE", "FinalDriveSetting"),
    ("VM_GEAR_REVERSE", "DRIVELINE", "ReverseSetting"),
    ("VM_RATIO_SET", "DRIVELINE", "RatioSetSetting"),
    ("VM_DIFF_PUMP", "DRIVELINE", "DiffPumpSetting"),
    ("VM_DIFF_POWER", "DRIVELINE", "DiffPowerSetting"),
    ("VM_DIFF_COAST", "DRIVELINE", "DiffCoastSetting"),
    ("VM_DIFF_PRELOAD", "DRIVELINE", "DiffPreloadSetting"),
    ("VM_FRONT_DIFF_PUMP", "DRIVELINE", "FrontDiffPumpSetting"),
    ("VM_FRONT_DIFF_POWER", "DRIVELINE", "FrontDiffPowerSetting"),
    ("VM_FRONT_DIFF_COAST", "DRIVELINE", "FrontDiffCoastSetting"),
    ("VM_FRONT_DIFF_PRELOAD", "DRIVELINE", "FrontDiffPreloadSetting"),
    ("VM_TORQUE_SPLIT", "DRIVELINE", "RearSplitSetting"),
    ("VM_GEAR_AUTOUPSHIFT", "DRIVELINE", "GearAutoUpShiftSetting"),
    ("VM_GEAR_AUTODOWNSHIFT", "DRIVELINE", "GearAutoDownShiftSetting"),
];

const THIRD: &[(&str, &str)] = &[
    ("PACKERS", "Packer"),
    ("SPRING", "Spring"),
    ("TENDERSPRING", "TenderSpring"),
    ("TENDERSPRINGTRAVEL", "TenderTravel"),
    ("SLOWBUMP", "SlowBump"),
    ("FASTBUMP", "FastBump"),
    ("SLOWREBOUND", "SlowRebound"),
    ("FASTREBOUND", "FastRebound"),
];

const WHEEL_SECTIONS: &[(&str, &str)] = &[("FL", "FRONTLEFT"), ("FR", "FRONTRIGHT"), ("RL", "REARLEFT"), ("RR", "REARRIGHT")];

const WHEEL_KEYS: &[(&str, &str)] = &[
    ("CAMBER", "CamberSetting"),
    ("PRESSURE", "PressureSetting"),
    ("PACKERS", "PackerSetting"),
    ("SPRING", "SpringSetting"),
    ("TENDERSPRING", "TenderSpringSetting"),
    ("TENDERSPRINGTRAVEL", "TenderTravelSetting"),
    ("SRUBBER", "SpringRubberSetting"),
    ("RIDEHEIGHT", "RideHeightSetting"),
    ("SLOWBUMP", "SlowBumpSetting"),
    ("FASTBUMP", "FastBumpSetting"),
    ("SLOWREBOUND", "SlowReboundSetting"),
    ("FASTREBOUND", "FastReboundSetting"),
    ("BRAKEDISC", "BrakeDiscSetting"),
    ("BRAKEPAD", "BrakePadSetting"),
    ("COMPOUND", "CompoundSetting"),
];

pub type SvmKey = (String, String);

pub struct Mapping {
    pub vm_to_svm: HashMap<String, SvmKey>,
    pub svm_to_vm: HashMap<SvmKey, String>,
}

pub fn mapping() -> &'static Mapping {
    static MAP: OnceLock<Mapping> = OnceLock::new();
    MAP.get_or_init(|| {
        let car_wide = CAR_WIDE.iter().map(|&(vm, sec, key)| (vm.to_string(), (sec.to_string(), key.to_string())));
        let gears = (1..10).map(|n| (format!("VM_GEAR_{n}"), ("DRIVELINE".to_string(), format!("Gear{n}Setting"))));
        let chassis = (0..12).map(|n| (format!("VM_CHASSIS_ADJ_{n:02}"), ("SUSPENSION".to_string(), format!("ChassisAdj{n:02}Setting"))));
        let third = [("FRONT", "Front3rd"), ("REAR", "Rear3rd")].into_iter().flat_map(|(axle, prefix)| {
            THIRD.iter().map(move |(vm, svm)| (format!("VM_{axle}_3RD_{vm}"), ("SUSPENSION".to_string(), format!("{prefix}{svm}Setting"))))
        });
        let wheels = WHEEL_SECTIONS.iter().flat_map(|&(corner, section)| {
            WHEEL_KEYS.iter().map(move |&(vm, svm)| (format!("WM_{vm}-W_{corner}"), (section.to_string(), svm.to_string())))
        });
        let vm_to_svm: HashMap<String, SvmKey> = car_wide.chain(gears).chain(chassis).chain(third).chain(wheels).collect();
        let svm_to_vm = vm_to_svm.iter().map(|(k, v)| (v.clone(), k.clone())).collect();
        Mapping { vm_to_svm, svm_to_vm }
    })
}

pub fn do_not_write(vm: &str) -> bool {
    vm.starts_with("WM_COMPOUND-W_")
}
