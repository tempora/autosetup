import copy

import pytest

from autosetup import analysis
from autosetup.analysis import Stint, _mark_pareto, pretty_name, setup_diff
from autosetup.lmu.setup_space import CarSetup
from autosetup.store import Store
from tests.conftest import make_recording, setup_json


@pytest.mark.parametrize("key,name", [
    ("WM_CAMBER-W_FL", "Camber FL"),
    ("VM_REAR_ANTISWAY", "Rear anti-roll bar"),
    ("VM_BRAKE_BALANCE", "Brake bias"),
    ("VM_FRONT_3RD_SPRING", "Front 3rd spring"),
    ("VM_DIFF_PRELOAD", "Diff preload"),
    ("VM_SOMETHING_NEW", "Something new"),
])
def test_pretty_name(key, name):
    assert pretty_name(key) == name


def _setup(**values):
    data = copy.deepcopy(setup_json())
    for k, v in values.items():
        data[k]["value"] = v[0]
        data[k]["stringValue"] = v[1]
    return CarSetup.from_json(data)


def test_setup_diff_merges_symmetric_pairs_and_skips_tyre_sets():
    before = _setup()
    after = _setup(**{"WM_CAMBER-W_FL": (27, "-2.3 deg"), "WM_CAMBER-W_FR": (27, "-2.3 deg"),
                      "VM_REAR_ANTISWAY": (2, "P3"), "WM_COMPOUND-W_FL": (2, "Medium")})
    rows = setup_diff(before, after)
    assert [r["name"] for r in rows] == ["Rear anti-roll bar", "Camber FL/FR"]
    assert rows[0]["delta"] == -1 and rows[0]["before_text"] == "P4" and rows[0]["after_text"] == "P3"


def test_setup_diff_keeps_asymmetric_changes_separate():
    rows = setup_diff(_setup(), _setup(**{"WM_CAMBER-W_FL": (27, "-2.3 deg")}))
    assert [r["name"] for r in rows] == ["Camber FL"]


def _stint(i, median, wear, consistency):
    return Stint(id=i, file_name=f"{i}", recorded_at="", session_type="", setup_name=None, laps=[],
                 summary={"median": median, "wear_per_lap": wear, "consistency": consistency})


def test_pareto_marks_non_dominated_stints():
    items = [_stint(1, 64.0, 0.50, 0.20), _stint(2, 64.2, 0.40, 0.20), _stint(3, 64.3, 0.55, 0.30),
             _stint(4, 63.9, 0.60, None)]
    _mark_pareto(items)
    assert [s.pareto for s in items] == [True, True, False, False]  # 4 lacks consistency


def test_stints_summary_and_ratings(tmp_path):
    rec = make_recording(tmp_path / "Test Raceway_P_2026-01-01T10_00_00Z.duckdb")
    with Store(tmp_path / "db.duckdb") as store:
        store.import_recording(rec)
        [ctx] = analysis.contexts(store)
        assert ctx.key == "Test Team 2025 #1|Test Raceway"
        store.set_rating(rec.name, rating=4, mid=1)
        [st] = analysis.stints(store, ctx.car_name, ctx.layout)
        assert st.summary["clean_laps"] == 2
        assert st.summary["median"] == pytest.approx(60.0)
        assert st.summary["theoretical_best"] == pytest.approx(60.0, abs=0.1)
        assert st.summary["wear_per_lap"] > 0
        assert st.rating["rating"] == 4 and st.rating["mid"] == 1
        with pytest.raises(ValueError):
            store.set_rating(rec.name, rating=6)


def test_reimport_replaces_a_recording_that_grew(tmp_path):
    rec = make_recording(tmp_path / "grow.duckdb")
    with Store(tmp_path / "db.duckdb") as store:
        first = store.import_recording(rec)
        store.set_rating(rec.name, rating=3)
        with open(rec, "ab") as f:  # same name, new size: the game kept writing
            f.write(b"\0" * 4096)
        second = store.import_recording(rec)
        assert second is not None and second != first
        assert store.con.execute("select count(*) from recordings").fetchone()[0] == 1
        assert store.con.execute("select count(*) from laps where recording_id = ?", [first]).fetchone()[0] == 0
        assert store.rating(rec.name)["rating"] == 3  # ratings survive


def test_recompute_metrics(tmp_path):
    rec = make_recording(tmp_path / "r.duckdb")
    with Store(tmp_path / "db.duckdb") as store:
        rid = store.import_recording(rec)
        store.con.execute("update recordings set metrics_version = 0")
        store.con.execute("delete from lap_metrics")
        done, missing = store.recompute_metrics(tmp_path)
        assert (done, missing) == (1, [])
        n = store.con.execute("select count(*) from lap_metrics where recording_id = ?", [rid]).fetchone()[0]
        assert n > 0
        assert store.recompute_metrics(tmp_path) == (0, [])
