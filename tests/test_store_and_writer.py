import pytest

from autosetup.lmu.svm import SvmFile
from autosetup.lmu.tracks import guess_folder
from autosetup.setup_writer import WriteError, parse_change, write_setup
from autosetup.store import Store


@pytest.fixture
def store(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as s:
        added, errors, busy = s.import_all(install.telemetry)
        assert len(added) == 1 and not errors and not busy
        assert s.learn_links(install.telemetry, install.settings) == 1
        yield s


def test_import_is_idempotent(store, install):
    assert store.import_all(install.telemetry) == ([], [], [])
    rows = store.recordings()
    assert len(rows) == 1
    assert rows[0]["clean_laps"] == 2  # laps 2 and 3; 1 and 4 are partial, 4 also in pits
    assert rows[0]["best_lap"] == pytest.approx(60.0)


def test_learns_template_and_folder(store, install):
    rec = store.recording(1)
    assert store.template_for(rec["car_signature"]).name == "base.svm"
    assert store.folder_for("Test Raceway") == "Testway"


def test_template_falls_back_to_vehicle_class(store, install):
    rec = store.recording(1)
    t = store.template_for("unknown-signature", rec["car_name"], install.settings)
    assert t is not None and t.name == "base.svm"
    assert store.template_for("unknown-signature", "Nobody #0", install.settings) is None


def test_template_from_a_setup_saved_since_the_last_import(tmp_path, install):
    # Saved in the garage, never matched by an import: the recorded setup finds it.
    with Store(tmp_path / "fresh.duckdb") as s:
        s.import_all(install.telemetry)
        rec = s.recording(1)
        assert s.template_for(rec["car_signature"], rec["car_name"], install.settings) is None
        t = s.template_for(rec["car_signature"], rec["car_name"], install.settings, s.setup_of(1))
        assert t is not None and t.name == "base.svm"


def test_write_without_a_saved_setup_says_so(tmp_path, install):
    (install.settings / "Testway" / "base.svm").unlink()
    with Store(tmp_path / "fresh.duckdb") as s:
        s.import_all(install.telemetry)
        with pytest.raises(WriteError, match="Save a setup before driving"):
            write_setup(s, install, 1, {"VM_REAR_ANTISWAY": 2}, dry_run=True)


def test_write_applies_changes_and_keeps_everything_else(store, install):
    setup = store.setup_of(1)
    changes = dict(parse_change(s, setup) for s in ["VM_REAR_ANTISWAY-=1", "WM_CAMBER-W_FL+=2"])
    res = write_setup(store, install, 1, changes)
    assert res.path.parent.name == "Testway"
    assert res.path.name == "autosetup_TestTeam1_Testway_1.svm"
    assert res.changes == {"VM_REAR_ANTISWAY": (3, 2), "WM_CAMBER-W_FL": (28, 30)}

    out = SvmFile.read(res.path)
    values = out.vm_values()
    assert values["VM_REAR_ANTISWAY"] == 2 and values["WM_CAMBER-W_FL"] == 30
    assert values["WM_CAMBER-W_FR"] == 28 and values["VM_BRAKE_BALANCE"] == 18
    assert out.get("FRONTLEFT", "CompoundSetting").index == 0  # template's compound kept
    assert out.get("GENERAL", "Symmetric").index == 0  # FL camber now differs from FR
    assert out.vehicle_class == "GT3 Test_Car WEC2025"
    assert out.get("SUSPENSION", "RearAntiSwaySetting").comment is None
    assert out.vm_labels()["VM_FRONT_ANTISWAY"] == "P6"
    # The user's file is untouched and the next write gets a new name.
    assert (install.settings / "Testway" / "base.svm").read_bytes().count(b"RearAntiSwaySetting=3") == 1
    assert write_setup(store, install, 1, changes).path.name.endswith("_2.svm")


def test_dry_run_writes_nothing(store, install):
    res = write_setup(store, install, 1, {"VM_REAR_ANTISWAY": 2}, dry_run=True)
    assert not res.path.exists()


@pytest.mark.parametrize("change,msg", [
    ({"VM_REAR_ANTISWAY": 8}, "outside"),
    ({"VM_GEAR_1": 0}, "not adjustable"),
])
def test_write_rejects_invalid_changes(store, install, change, msg):
    with pytest.raises(WriteError, match=msg):
        write_setup(store, install, 1, change)


@pytest.mark.parametrize("spec", ["VM_REAR_ANTISWAY", "VM_NOPE=1", "VM_REAR_ANTISWAY*=2"])
def test_parse_change_rejects_bad_specs(store, spec):
    with pytest.raises(WriteError):
        parse_change(spec, store.setup_of(1))


@pytest.mark.parametrize("track,folders,expected", [
    ("Fuji Speedway", ["Fuji", "Spa"], "Fuji"),
    ("Circuit de la Sarthe", ["Lemans", "Spa"], "Lemans"),
    ("Autódromo José Carlos Pace", ["Interlagos"], "Interlagos"),
    ("Lusail International Circuit", ["Qatar"], "Qatar"),
    ("Circuit de Spa-Francorchamps", ["Spa"], "Spa"),
    ("Sebring International Raceway", ["Sebring", "Spa"], "Sebring"),
    ("Nowhere Ring", ["Spa"], None),
])
def test_guess_folder(tmp_path, track, folders, expected):
    for f in folders:
        (tmp_path / f).mkdir()
    got = guess_folder(track, tmp_path)
    assert (got.name if got else None) == expected


def test_store_can_be_used_from_several_threads_at_once(tmp_path):
    # The server reads from request threads while the watcher imports: one shared DuckDB
    # connection raised "Connection is busy with another call" whenever two met.
    import threading

    errors = []
    with Store(tmp_path / "db.duckdb") as store:
        def work(i):
            try:
                for j in range(30):
                    store.con.execute("select count(*) from range(300000)").fetchone()
                    store.put_setting(f"t{i}", j)
                    assert store.get_setting(f"t{i}") == j
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert [store.get_setting(f"t{i}") for i in range(6)] == [29] * 6  # visible to the owner too
