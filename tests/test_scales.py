from autosetup.scales import Scale, build_scales
from autosetup.store import Store


def test_exact_and_interpolated_labels():
    s = Scale("VM_BRAKE_BALANCE", {38: "47.5:52.5", 44: "46.0:54.0"})
    assert s.label(38) == ("47.5:52.5", True)
    assert s.label(40) == ("47.0:53.0", False)
    assert s.per_step < 0  # a step up moves bias rearward


def test_extrapolation_uses_known_step_sizes():
    s = Scale("WM_PRESSURE-W_FL", {0: "136 kPa"})
    assert s.per_step == 1.0
    assert s.label(3) == ("139 kPa", False)
    cam = Scale("WM_CAMBER-W_RL", {20: "-1.8 deg"})
    assert cam.label(22) == ("-1.6 deg", False)


def test_unknown_non_numeric_labels():
    s = Scale("VM_REAR_ANTISWAY", {2: "D38x1_1-2", 3: "D38x1_1-3"})
    assert s.label(3) == ("D38x1_1-3", True)
    assert s.label(6) == (None, False)
    assert s.per_step is None


def test_step_number_labels():
    assert Scale("VM_TRACTIONCONTROLMAP", {6: "6"}).label(7) == ("7", False)


def test_soft_hard_suffix_is_not_copied():
    s = Scale("WM_SPRING-W_FL", {1: "1 (soft)", 2: "2"})
    assert s.label(4) == ("4", False)


def test_build_scales_merges_recordings_and_matching_svms(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        store.import_all(install.telemetry)
        sig = store.recording(1)["car_signature"]
        sc = build_scales(store, sig, install.settings)
    assert sc.label("VM_REAR_ANTISWAY", 3) == ("P4", True)
    assert sc.label("WM_CAMBER-W_FL", 28) == ("-2.2 deg", True)


def test_other_versions_of_the_car_add_labels_only_where_they_agree(tmp_path, install):
    def old(name: str, front: str, rear: str) -> None:
        (install.settings / "Testway" / name).write_bytes("\r\n".join([
            'VehicleClassSetting="GT3 Test_Car WEC2024"', "", "[SUSPENSION]", front, rear, "",
            "[CONTROLS]", "SteerLockSetting=2//500 (15.0) deg", "RearBrakeSetting=40//47.0:53.0", "",
            "[FRONTLEFT]", "CamberSetting=10//-4.0 deg", "", "[REARLEFT]", "RideHeightSetting=30//8.0 cm", ""]).encode())

    old("old_a.svm", "FrontAntiSwaySetting=5//P9", "RearAntiSwaySetting=3//P4")
    old("old_b.svm", "FrontAntiSwaySetting=7//P8", "RearAntiSwaySetting=6//P7")
    with Store(tmp_path / "db.duckdb") as store:
        store.import_all(install.telemetry)
        store.learn_links(install.telemetry, install.settings)
        sc = build_scales(store, store.recording(1)["car_signature"], install.settings)
    assert sc.label("VM_REAR_ANTISWAY", 6) == ("P7", True)  # P4 at 3 agrees with this version
    assert sc.label("VM_FRONT_ANTISWAY", 5) == ("P6", True)
    assert sc.label("VM_FRONT_ANTISWAY", 7)[0] != "P8"  # P9 at 5 says the front bar was relabelled
