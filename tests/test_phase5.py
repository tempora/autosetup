"""Phase 5: the trade-off learned from what you do, and transfer between tracks."""

import numpy as np
import pytest

from autosetup.advisor import load_knowledge
from autosetup.opt import bench
from autosetup.opt.dataset import build_dataset, domain_of
from autosetup.opt.gp import GP
from autosetup.opt.preference import Comparison, evidence, learn
from autosetup.proposals import Advisor
from autosetup.store import Store
from tests.conftest import make_recording

FUEL = lambda cls: load_knowledge(cls)["fuel_s_per_l"]  # noqa: E731


def _import(store, install, name, track="Test Raceway", t=0.0, **values):
    f = make_recording(install.telemetry / name, laps=4, values=values, track=track, mtime=1_700_000_000 + t)
    return store.import_recording(f)


# -- learning the trade-off ----------------------------------------------------------------
def test_without_evidence_the_trade_off_is_the_old_default():
    pref = learn([])
    assert pref.weights["pace"] == 1.0
    assert pref.weights["tyres"] == pytest.approx(0.5, abs=0.04)
    assert pref.weights["drivability"] == pytest.approx(0.75, abs=0.04)


def test_picking_tyres_over_pace_moves_the_weights():
    # Six times the kept setup was slower (+1 sd) but much easier on the tyres (−1.5 sd).
    dz = np.array([-1.0, 1.5, 0.0])  # loser minus winner, standardised
    pref = learn([Comparison(1, 2, "revert", 1.0, dz) for _ in range(6)])
    prior = learn([])
    assert pref.simplex["tyres"] > prior.simplex["tyres"] + 0.1
    assert pref.simplex["pace"] < prior.simplex["pace"]
    assert pref.to_json()["by_source"] == {"revert": 6}


def test_reverts_keeps_and_ratings_become_evidence(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        # Revert: rear bar 3 → 5 → back to 3.
        a = _import(store, install, "r1.duckdb", "Revert Ring", 0, VM_REAR_ANTISWAY=3)
        b = _import(store, install, "r2.duckdb", "Revert Ring", 10, VM_REAR_ANTISWAY=5)
        _import(store, install, "r3.duckdb", "Revert Ring", 20, VM_REAR_ANTISWAY=3)
        # Keep: 3 → 5, then a further change built on 5.
        k1 = _import(store, install, "k1.duckdb", "Keep Ring", 30, VM_REAR_ANTISWAY=3)
        k2 = _import(store, install, "k2.duckdb", "Keep Ring", 40, VM_REAR_ANTISWAY=5)
        _import(store, install, "k3.duckdb", "Keep Ring", 50, VM_REAR_ANTISWAY=5, VM_FRONT_ANTISWAY=6)
        store.set_rating("k1.duckdb", rating=4)
        store.set_rating("k2.duckdb", rating=2)
        found = {(c.source, c.winner, c.loser): c.weight for c in evidence(store, FUEL)}
        assert found[("revert", a, b)] == 1.0
        assert found[("keep", k2, k1)] == pytest.approx(0.3)
        assert found[("rating", k1, k2)] == 1.0
        assert len(found) == 3


def test_advisor_reports_the_learned_trade_off(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        _import(store, install, "a.duckdb", t=0)
        adv = Advisor(store, install)
        assert adv.preference().to_json()["comparisons"] == 0
        _import(store, install, "b.duckdb", t=10, VM_REAR_ANTISWAY=5)
        _import(store, install, "c.duckdb", t=20)
        assert adv.preference().to_json()["by_source"] == {"revert": 1}  # recomputed on new stints


# -- transfer between tracks ---------------------------------------------------------------
def test_gp_carries_an_effect_over_from_another_track():
    rng = np.random.default_rng(1)
    X_other = rng.uniform(0, 1, (12, 1))
    y_other = 2.0 * X_other[:, 0] + rng.normal(0, 0.05, 12)  # clear slope elsewhere
    X = np.vstack([[[0.5]], X_other])
    y = np.concatenate([[1.0], y_other])
    noise = np.full(13, 0.05 ** 2)
    groups = np.array([0] + [1] * 12)
    shared = GP.fit(X, y, noise, prior_spread=1.0, groups=groups)
    alone = GP.fit(X[:1], y[:1], noise[:1], prior_spread=1.0)
    lo, hi = shared.predict(np.array([[0.2], [0.8]]))[0]
    assert hi - lo > 0.6  # the slope seen at the other track
    assert 0.3 <= shared.rho < 1.0
    assert shared.predict(np.array([[0.8]]))[1][0] < alone.predict(np.array([[0.8]]))[1][0]


def test_dataset_can_bring_in_the_same_car_elsewhere_but_proposals_dont(tmp_path, install):
    with Store(tmp_path / "db.duckdb") as store:
        for i, rear in enumerate((2, 4, 6)):
            _import(store, install, f"o{i}.duckdb", "Other Ring", i * 10, VM_REAR_ANTISWAY=rear)
        rid = _import(store, install, "here.duckdb", t=100)
        ds = build_dataset(store, domain_of(store, rid), 0.03)
        assert len(ds.obs) == 1 and len(ds.extra) == 3 and ds.transfer_from == ["Other Ring"]
        assert ds.groups().tolist() == [0, 1, 1, 1]
        assert build_dataset(store, domain_of(store, rid), 0.03, transfer=False).extra == []

        # The app keeps each track to itself: what worked at another track isn't evidence here.
        pr = Advisor(store, install).proposal(rid)
        assert pr.model["transfer"] is None and pr.model["n_obs"] == 1
        assert pr.model["state"] == "learning"
        learning = [c for c in pr.changes if c.source == "learning"]
        assert learning and "other track" not in learning[0].reason


def test_transfer_beats_starting_alone_on_the_toy_car():
    t = bench.transfer(seeds=4)
    assert np.mean(t["transfer"][2:6]) < np.mean(t["alone"][2:6])
