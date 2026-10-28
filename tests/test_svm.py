from pathlib import Path

import pytest

from autosetup.lmu.mapping import SVM_TO_VM, VM_TO_SVM
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import SvmFile, best_match, match_score, next_free_name
from tests.conftest import SVM_TEXT, setup_json


def test_mapping_is_one_to_one():
    assert len(SVM_TO_VM) == len(VM_TO_SVM)


def test_round_trip_is_byte_identical(tmp_path):
    src = tmp_path / "a.svm"
    raw = SVM_TEXT.encode("latin-1") + b'\r\nNotes2="caf\xe9 \xff"\r\n'  # non-UTF-8 bytes survive
    src.write_bytes(raw)
    out = SvmFile.read(src).write(tmp_path / "autosetup_copy.svm")
    assert out.read_bytes() == raw


def test_header_and_values():
    svm = SvmFile.parse(SVM_TEXT)
    assert svm.vehicle_class == "GT3 Test_Car WEC2025"
    assert svm.veh_file.endswith("TEST.VEH")
    v = svm.vm_values()
    assert v["VM_REAR_ANTISWAY"] == 3 and v["WM_CAMBER-W_FL"] == 28
    assert svm.vm_labels()["VM_BRAKE_BALANCE"] == "52.5:47.5"


def test_set_keeps_crlf_and_only_touches_one_line():
    svm = SvmFile.parse(SVM_TEXT)
    before = svm.lines[:]
    svm.set("SUSPENSION", "RearAntiSwaySetting", 2)
    changed = [i for i, (a, b) in enumerate(zip(before, svm.lines)) if a != b]
    assert len(changed) == 1
    assert svm.lines[changed[0]] == "RearAntiSwaySetting=2\r\n"
    assert svm.vm_values()["VM_REAR_ANTISWAY"] == 2


def test_set_vm_skips_compound_and_reports_missing():
    svm = SvmFile.parse(SVM_TEXT)
    missing = svm.set_vm({"WM_COMPOUND-W_FL": 3, "VM_REAR_WING": 2, "VM_FRONT_ANTISWAY": 1})
    assert missing == ["VM_REAR_WING"]
    assert svm.get("FRONTLEFT", "CompoundSetting").index == 0
    assert svm.vm_values()["VM_FRONT_ANTISWAY"] == 1


def test_write_never_replaces_users_files(tmp_path):
    svm = SvmFile.parse(SVM_TEXT)
    theirs = tmp_path / "my_setup.svm"
    theirs.write_text("x")
    with pytest.raises(FileExistsError):
        svm.write(theirs)
    with pytest.raises(PermissionError):
        svm.write(theirs, overwrite=True)
    assert theirs.read_text() == "x"


def test_setups_written_under_the_old_name_are_still_ours(tmp_path):
    old = tmp_path / "AutoSetup_Team_1_Track_1.svm"  # older builds' capitalisation
    old.write_text("x")
    SvmFile.parse(SVM_TEXT).write(old, overwrite=True)
    assert old.read_text() != "x"


def test_next_free_name(tmp_path):
    first = next_free_name(tmp_path, "Team #1_Track")
    assert first.name == "autosetup_Team_1_Track_1.svm"
    first.write_text("")
    assert next_free_name(tmp_path, "Team #1_Track").name == "autosetup_Team_1_Track_2.svm"


def test_match_score():
    setup = CarSetup.from_json(setup_json())
    assert match_score(setup, SvmFile.parse(SVM_TEXT)) == 1.0
    other = SvmFile.parse(SVM_TEXT.replace("RearAntiSwaySetting=3//P4", "RearAntiSwaySetting=3//D40"))
    assert match_score(setup, other) < 1.0


def test_best_match_agrees_with_scoring_every_file():
    setup = CarSetup.from_json(setup_json())
    exact = SvmFile.parse(SVM_TEXT)
    one_off = SvmFile.parse(SVM_TEXT.replace("RearAntiSwaySetting=3//P4", "RearAntiSwaySetting=3//D40"))
    svms = {Path("a.svm"): one_off, Path("b.svm"): exact}
    assert best_match(setup, svms, 0.85) == (1.0, Path("b.svm"))
    assert best_match(setup, {Path("a.svm"): one_off}, 0.85) == (match_score(setup, one_off), Path("a.svm"))
    # Out of reach: stops scoring and reports no match rather than a low score.
    assert best_match(setup, {Path("a.svm"): one_off}, 0.999) == (0.0, None)
