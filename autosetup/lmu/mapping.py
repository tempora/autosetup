"""Map telemetry ``CarSetup`` keys (VM_*/WM_*) to .svm (section, key) pairs.

Both sides store the same step index; only the names differ. The table below covers every
.svm key seen in the 806 setups of the development install. A few CarSetup keys have no
.svm line (tyre compound per axle, upshift RPMs) and are listed in ``NOT_IN_SVM``.
Run ``autosetup validate-mapping`` to check the table against your own files.
"""

from __future__ import annotations

_CAR_WIDE: dict[str, tuple[str, str]] = {
    # GENERAL
    "VM_WEIGHT_VERTICAL": ("GENERAL", "CGHeightSetting"),
    "VM_WEIGHT_LATERAL": ("GENERAL", "CGRightSetting"),
    "VM_WEIGHT_DISTRIB": ("GENERAL", "CGRearSetting"),
    "VM_WEIGHT_WEDGE": ("GENERAL", "WedgeSetting"),
    "VM_FUEL_LEVEL": ("GENERAL", "FuelSetting"),
    "VM_FUEL_CAPACITY": ("GENERAL", "FuelCapacitySetting"),
    "VM_VIRTUAL_ENERGY": ("GENERAL", "VirtualEnergySetting"),
    "VM_NUM_PITSTOPS": ("GENERAL", "NumPitstopsSetting"),
    "VM_PITSTOP_1": ("GENERAL", "Pitstop1Setting"),
    "VM_PITSTOP_2": ("GENERAL", "Pitstop2Setting"),
    "VM_PITSTOP_3": ("GENERAL", "Pitstop3Setting"),
    # Body
    "VM_LEFT_FENDER_FLARE": ("LEFTFENDER", "FenderFlareSetting"),
    "VM_RIGHT_FENDER_FLARE": ("RIGHTFENDER", "FenderFlareSetting"),
    "VM_FRONT_WING": ("FRONTWING", "FWSetting"),
    "VM_REAR_WING": ("REARWING", "RWSetting"),
    "VM_WATER_RADIATOR": ("BODYAERO", "WaterRadiatorSetting"),
    "VM_OIL_RADIATOR": ("BODYAERO", "OilRadiatorSetting"),
    "VM_BRAKE_DUCTS": ("BODYAERO", "BrakeDuctSetting"),
    "VM_BRAKE_DUCTS_REAR": ("BODYAERO", "BrakeDuctRearSetting"),
    # SUSPENSION
    "VM_FRONT_WHEEL_TRACK": ("SUSPENSION", "FrontWheelTrackSetting"),
    "VM_REAR_WHEEL_TRACK": ("SUSPENSION", "RearWheelTrackSetting"),
    "VM_FRONT_ANTISWAY": ("SUSPENSION", "FrontAntiSwaySetting"),
    "VM_REAR_ANTISWAY": ("SUSPENSION", "RearAntiSwaySetting"),
    "VM_FRONT_TOEIN": ("SUSPENSION", "FrontToeInSetting"),
    "VM_FRONT_TOEOFFSET": ("SUSPENSION", "FrontToeOffsetSetting"),
    "VM_REAR_TOEIN": ("SUSPENSION", "RearToeInSetting"),
    "VM_REAR_TOEOFFSET": ("SUSPENSION", "RearToeOffsetSetting"),
    "VM_LEFT_CASTER": ("SUSPENSION", "LeftCasterSetting"),
    "VM_RIGHT_CASTER": ("SUSPENSION", "RightCasterSetting"),
    "VM_LEFT_TRACK_BAR": ("SUSPENSION", "LeftTrackBarSetting"),
    "VM_RIGHT_TRACK_BAR": ("SUSPENSION", "RightTrackBarSetting"),
    # CONTROLS
    "VM_STEER_LOCK": ("CONTROLS", "SteerLockSetting"),
    "VM_BRAKE_BALANCE": ("CONTROLS", "RearBrakeSetting"),
    "VM_BRAKE_MIGRATION": ("CONTROLS", "BrakeMigrationSetting"),
    "VM_BRAKE_PRESSURE": ("CONTROLS", "BrakePressureSetting"),
    "VM_HANDFRONTBRAKE_PRESSURE": ("CONTROLS", "HandfrontbrakePressSetting"),
    "VM_HANDBRAKE_PRESSURE": ("CONTROLS", "HandbrakePressSetting"),
    "VM_TRACTION_CONTROL": ("CONTROLS", "TCSetting"),
    "VM_ANTILOCK_BRAKES": ("CONTROLS", "ABSSetting"),
    "VM_TRACTIONCONTROLMAP": ("CONTROLS", "TractionControlMapSetting"),
    "VM_TRACTIONCONTROLPOWERCUTMAP": ("CONTROLS", "TCPowerCutMapSetting"),
    "VM_TRACTIONCONTROLSLIPANGLEMAP": ("CONTROLS", "TCSlipAngleMapSetting"),
    "VM_ANTILOCKBRAKESYSTEMMAP": ("CONTROLS", "AntilockBrakeSystemMapSetting"),
    # ENGINE
    "VM_REV_LIMITER": ("ENGINE", "RevLimitSetting"),
    "VM_ENGINE_BOOST": ("ENGINE", "EngineBoostSetting"),
    "VM_REGEN_LEVEL": ("ENGINE", "RegenerationMapSetting"),
    "VM_ELECTRIC_MOTOR_MAP": ("ENGINE", "ElectricMotorMapSetting"),
    "VM_ENGINE_MIXTURE": ("ENGINE", "EngineMixtureSetting"),
    "VM_ENGINE_BRAKEMAP": ("ENGINE", "EngineBrakingMapSetting"),
    # DRIVELINE
    "VM_GEAR_FINAL": ("DRIVELINE", "FinalDriveSetting"),
    "VM_GEAR_REVERSE": ("DRIVELINE", "ReverseSetting"),
    "VM_RATIO_SET": ("DRIVELINE", "RatioSetSetting"),
    "VM_DIFF_PUMP": ("DRIVELINE", "DiffPumpSetting"),
    "VM_DIFF_POWER": ("DRIVELINE", "DiffPowerSetting"),
    "VM_DIFF_COAST": ("DRIVELINE", "DiffCoastSetting"),
    "VM_DIFF_PRELOAD": ("DRIVELINE", "DiffPreloadSetting"),
    "VM_FRONT_DIFF_PUMP": ("DRIVELINE", "FrontDiffPumpSetting"),
    "VM_FRONT_DIFF_POWER": ("DRIVELINE", "FrontDiffPowerSetting"),
    "VM_FRONT_DIFF_COAST": ("DRIVELINE", "FrontDiffCoastSetting"),
    "VM_FRONT_DIFF_PRELOAD": ("DRIVELINE", "FrontDiffPreloadSetting"),
    "VM_TORQUE_SPLIT": ("DRIVELINE", "RearSplitSetting"),
    "VM_GEAR_AUTOUPSHIFT": ("DRIVELINE", "GearAutoUpShiftSetting"),
    "VM_GEAR_AUTODOWNSHIFT": ("DRIVELINE", "GearAutoDownShiftSetting"),
}
for _n in range(1, 10):
    _CAR_WIDE[f"VM_GEAR_{_n}"] = ("DRIVELINE", f"Gear{_n}Setting")
for _n in range(12):
    _CAR_WIDE[f"VM_CHASSIS_ADJ_{_n:02d}"] = ("SUSPENSION", f"ChassisAdj{_n:02d}Setting")
for _axle, _prefix in (("FRONT", "Front3rd"), ("REAR", "Rear3rd")):
    for _vm, _svm in (("PACKERS", "Packer"), ("SPRING", "Spring"), ("TENDERSPRING", "TenderSpring"),
                      ("TENDERSPRINGTRAVEL", "TenderTravel"), ("SLOWBUMP", "SlowBump"),
                      ("FASTBUMP", "FastBump"), ("SLOWREBOUND", "SlowRebound"), ("FASTREBOUND", "FastRebound")):
        _CAR_WIDE[f"VM_{_axle}_3RD_{_vm}"] = ("SUSPENSION", f"{_prefix}{_svm}Setting")

_WHEEL_SECTIONS = {"FL": "FRONTLEFT", "FR": "FRONTRIGHT", "RL": "REARLEFT", "RR": "REARRIGHT"}
_WHEEL_KEYS = {
    "CAMBER": "CamberSetting",
    "PRESSURE": "PressureSetting",
    "PACKERS": "PackerSetting",
    "SPRING": "SpringSetting",
    "TENDERSPRING": "TenderSpringSetting",
    "TENDERSPRINGTRAVEL": "TenderTravelSetting",
    "SRUBBER": "SpringRubberSetting",
    "RIDEHEIGHT": "RideHeightSetting",
    "SLOWBUMP": "SlowBumpSetting",
    "FASTBUMP": "FastBumpSetting",
    "SLOWREBOUND": "SlowReboundSetting",
    "FASTREBOUND": "FastReboundSetting",
    "BRAKEDISC": "BrakeDiscSetting",
    "BRAKEPAD": "BrakePadSetting",
    "COMPOUND": "CompoundSetting",
}

VM_TO_SVM: dict[str, tuple[str, str]] = dict(_CAR_WIDE)
for _corner, _section in _WHEEL_SECTIONS.items():
    for _vm, _svm in _WHEEL_KEYS.items():
        VM_TO_SVM[f"WM_{_vm}-W_{_corner}"] = (_section, _svm)

SVM_TO_VM: dict[tuple[str, str], str] = {v: k for k, v in VM_TO_SVM.items()}

NOT_IN_SVM = {
    "VM_FRONT_TIRE_COMPOUND",
    "VM_REAR_TIRE_COMPOUND",
    *(f"VM_GEAR_UPSHIFT_RPM_{n}" for n in range(1, 10)),
}

# The .svm CompoundSetting index does not follow WM_COMPOUND's value (it also encodes the
# tyre set; validated against 28 matched pairs). Never write it from telemetry, so a
# template's compound choice is kept as it is.
DO_NOT_WRITE = {f"WM_COMPOUND-W_{c}" for c in _WHEEL_SECTIONS}
