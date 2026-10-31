"""The next setup for a stint: rules, then the optimiser, then the dampers; and writing it.

  rules      tyres, brake cooling, brake pressure, platform safety (rules.py)
  main       the fastest setup predicted to feel the way you asked (opt/bo.py): target from
             your fingerprint and presses (feel/), balance model (opt/balance_model.py),
             lap model of this car at this layout with its aero map and residual GP
             (opt/laptime.py), platform safety; the drag level first
  dampers    a better contact patch inside the same band, kerbs no worse (opt/dampers.py)

Only the current game build's recordings are learned from. Proposals that are written
are kept (``proposals``), so the next stint can be linked back to what was asked for and
the trust region can grow or shrink with how they paid off.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from autosetup import analysis, templates
from autosetup.feel import fingerprint as fpm
from autosetup.feel import target as feel
from autosetup.feel.cars import recording_car
from autosetup.feel.measure import measured, same_setup_stints
from autosetup.knowledge import load_knowledge
from autosetup.lmu.builds import current_build
from autosetup.lmu.paths import LmuPaths
from autosetup.lmu.setup_space import parse_number
from autosetup.lmu.svm import read_settings
from autosetup.opt import bo, dampers
from autosetup.opt.balance_model import BalanceModel, Evidence
from autosetup.opt.laptime import UNKNOWN_SD, lap_function
from autosetup.opt.platform import platform_of
from autosetup.opt.priors import sensitivities
from autosetup.opt.space import Dim, aero_wing, damper_dims, main_dims, setup_fingerprint
from autosetup.rules import Change, Proposal, _index_dir, manages_brake_pressure, propose
from autosetup.scales import CarScales, build_scales
from autosetup.setup_writer import WriteError, write_setup
from autosetup.sim.aero import Platform
from autosetup.sim.model import LayoutModel, aero_map
from autosetup.sim.residual import with_trap_drag
from autosetup.sim.sensitivity import aero_sensitivity
from autosetup.store import Store

SCHEMA = """
create sequence if not exists proposal_id;
create table if not exists proposals (
    id            integer primary key default nextval('proposal_id'),
    created_at    timestamp,
    recording_id  integer,              -- the stint the proposal was built from
    base_file     varchar,
    context       varchar,
    signature     varchar,
    changes       json,                 -- [{keys, before, after, reason, group}]
    svm_path      varchar
);
"""
NAMES = {"wing": "Rear wing", "front_wing": "Front wing", "rh_rear": "Rear ride height", "rh_front": "Front ride height",
         "arb_front": "Front anti-roll bar", "arb_rear": "Rear anti-roll bar", "diff_preload": "Diff preload",
         "spring_front": "Front springs", "spring_rear": "Rear springs", "brake_bias": "Brake bias",
         "toe_front": "Front toe", "toe_rear": "Rear toe", "tender_front": "Front tender springs",
         "tender_rear": "Rear tender springs", "third_spring_front": "Front third spring",
         "third_spring_rear": "Rear third spring", "diff_power": "Diff power ramp", "diff_coast": "Diff coast ramp",
         "front_diff_power": "Front diff power", "front_diff_coast": "Front diff coast",
         "front_diff_preload": "Front diff preload"}
DAMPER_BAND_LOSS = 0.2
MAX_RH_MM = 5.0  # a ride height moves at most this much in one proposal
SLOW_BALANCE = ("slow_bump_front", "slow_rebound_front", "slow_bump_rear", "slow_rebound_rear")  # priors.py
SLOW_REACH = 2  # clicks a slow damper moves for balance in one proposal
TC_KEYS = ("VM_TRACTIONCONTROLMAP", "VM_TRACTIONCONTROLPOWERCUTMAP", "VM_TRACTIONCONTROLSLIPANGLEMAP")
THIRD_REACH = 2  # clicks a third spring moves in one proposal
TOE_COST_S = 0.05  # per toe setting moved: toe only when it's clearly better
REAR_TOE_OUT_S = 0.15  # a setup with rear toe-out (below 0°) pays this, unless the car already runs it
# What moving a setting costs (s), by kind: of settings that do the same job, rake first,
# bars and toe last. Anything else costs bo.CHANGE_COST_S.
CHANGE_COST = {"rh_": 0.005, "wing": 0.01, "front_wing": 0.01, "spring_": 0.01, "third_spring_": 0.01,
               "slow_": 0.01, "arb_": 0.02, "diff_": 0.02, "front_diff_": 0.02, "brake_bias": 0.02,
               "toe_": TOE_COST_S}
# Clicks a setting may move in one proposal (a backstop: the model sizes the moves).
REACH = {"arb_": 1, "wing": 1, "front_wing": 1, "toe_": 1, "diff_power": 1, "diff_coast": 1, "front_diff_": 1,
         "brake_bias": 2}


def _by_kind(table: dict, name: str):
    return next((v for k, v in table.items() if name == k or (k.endswith("_") and name.startswith(k))), None)
PHASE = {"entry": "at turn-in", "mid": "at the apex", "exit": "on exit"}
BAND = {"low": "slow", "high": "fast"}
SMALL_ALONE = "small on its own · works with the other changes"


class ProposalConflict(Exception):
    """The proposal changed between showing it and writing it (new data, locks, presses)."""


@dataclass
class Plan:
    """Everything a proposal was decided from, for the Setup page."""

    stint: int
    car_key: str
    car_class: str
    layout: str
    target: feel.Target
    measured: dict
    predicted: dict[str, list[float]] = field(default_factory=dict)  # cell → [mean, sd] after the changes
    lap: dict | None = None  # base, best, delta, sd, sectors
    gap: list[dict] = field(default_factory=list)
    reason: str = "stay"
    feasible: bool = True
    wing: dict | None = None
    balance_source: str = "none"
    dampers: dict | None = None
    platform: dict | None = None  # current / proposed: ride heights at speed and rake (mm), wing
    short: dict | None = None  # {"stint", "undrivable"}: the last stint had no full lap
    notes: list[str] = field(default_factory=list)
    searched: int | None = None  # settings the solver could move (not locked, adjustable)

    def to_json(self) -> dict:
        return {"stint": self.stint, "car_key": self.car_key, "car_class": self.car_class, "layout": self.layout,
                "target": self.target.to_json(), "measured": self.measured, "predicted": self.predicted,
                "lap": self.lap, "gap": self.gap, "reason": self.reason, "feasible": self.feasible,
                "wing": self.wing, "balance_source": self.balance_source, "dampers": self.dampers,
                "platform": self.platform, "short": self.short}


NO_CORNERS = "No corner passes yet"
SOURCE_TEXT = {"layout": "stints here", "other layouts": "other tracks", "fingerprint": "the fingerprint",
               "none": "nothing"}


def why_nothing(pr: Proposal, plan: Plan, locked: int) -> dict | None:
    """Why a proposal has no changes: one plain line, and the facts behind it (for the
    hover, and for debugging). None when there are changes."""
    if pr.changes:
        return None
    asked = bool(plan.target.front or plan.target.rear)
    facts = [f"solver: {plan.reason}", f"balance from: {SOURCE_TEXT.get(plan.balance_source, plan.balance_source)}",
             f"lap model: {'yes' if plan.lap else 'none here yet'}"]
    if plan.searched is not None:
        facts.append(f"settings searched: {plan.searched}")
    if locked:
        facts.append(f"locked: {locked}")
    facts += [n for n in pr.notes if n not in plan.notes]  # the rules' own ("… is already at its limit")
    if NO_CORNERS in plan.notes:
        text = "No corners measured on this setup yet, so there's no balance to work from"
    elif plan.searched == 0:
        text = "Every setting is locked or fixed on this car"
    elif plan.balance_source == "none":
        text = "No balance measured for this car yet, so the feel buttons have nothing to work from"
    elif not plan.feasible:
        text = "Nothing within reach moves the balance towards what you asked" + (
            "; unlocking settings widens the search" if locked else "")
    elif asked:
        text = "Your setup already sits where you asked, and nothing within reach is predicted faster"
    elif not plan.lap:
        text = "The balance is on target, and there's no lap-time model here yet to find time with"
    else:
        text = f"The balance is on target, and nothing within reach is predicted at least {bo.MIN_GAIN_S:.2f} s faster"
    return {"text": text, "facts": facts}


def _traction(setup, scales: CarScales, summary: dict, tgt: feel.Target, m_now, k: dict,
              locks: set[str]) -> Change | None:
    """TC a step a stint, map, cut and slip together: down while it works more than
    tc_s_per_lap with exits in the band or understeering, up when the exits oversteer past
    the band or the rear needs catching on exit. The rear feel moves the band, so it's in
    here through the band."""
    keys = [key for key in TC_KEYS if key in setup.params and setup[key].adjustable and key not in locks
            and setup[key].text.strip().lower() != "linked"]  # a linked slip map follows the TC map
    if not keys:
        return None
    exits = [c for c in tgt.cells if c.startswith("exit_") and c in m_now.cells
             and tgt.weights.get(c, 0.5) >= bo.MIN_WEIGHT]
    over = [c for c in exits if m_now.cells[c].mean < tgt.cells[c] - tgt.tolerance[c]]
    cs, tc_s = summary.get("cs_exit"), summary.get("tc_s")
    if over or (cs is not None and cs > float(k["countersteer_per_lap"])):
        step, reason = 1, "oversteer on exit"
    elif exits and tc_s is not None and tc_s > float(k["tc_s_per_lap"]):
        step, reason = -1, f"TC working {tc_s:.1f} s a lap with stable exits"
    else:
        return None
    before = {key: setup[key].value for key in keys}
    after = {key: setup[key].clamp(before[key] + step * _index_dir(key, scales, numeric_up=True)) for key in keys}
    moved = [key for key in keys if after[key] != before[key]]
    if not moved:
        return None

    def text(key, i):
        return (scales.label(key, i)[0] or str(i)) if i != before[key] else setup[key].text.strip()
    return Change("aids", moved, {key: before[key] for key in moved}, {key: after[key] for key in moved}, reason,
                  "TC", " / ".join(setup[key].text.strip() for key in moved),
                  " / ".join(text(key, after[key]) for key in moved), source="telemetry")


def toe_deg(d: Dim, setup, scales: CarScales, index: int) -> float | None:
    """Toe at an index of a toe dim, deg (toe-in +): its label where it's known, else
    the current label moved by the label's step."""
    key = d.keys[0]
    label, _ = scales.label(key, index)
    v = parse_number(label) if label else None
    if v is not None:
        return v
    now, step = parse_number(setup[key].text), scales[key].per_step
    if now is None or step is None:
        return None
    return now + step * (index - d.index(setup))


def _front_toe_limit(dims: list[Dim], setup, scales: CarScales) -> list[Dim]:
    """Front toe never goes positive (toe-in): its range is cut to the indices at or under
    0°, or under where the car is if it already runs toe-in (it can only come back)."""
    out = []
    for d in dims:
        if d.name == "toe_front":
            now = d.index(setup)
            here = toe_deg(d, setup, scales, now)
            deg = {i: toe_deg(d, setup, scales, i) for i in range(d.lo, d.hi + 1)}
            if here is not None and all(v is not None for v in deg.values()):
                ok = [i for i, v in deg.items() if v <= max(0.0, here) + 1e-9]
                d = dataclasses.replace(d, lo=min(ok), hi=max(ok), span=d.span or (d.lo, d.hi))
        out.append(d)
    return out


def _rear_toe_out(d: Dim | None, setup, scales: CarScales):
    """Penalty for rear toe-out: REAR_TOE_OUT_S for a setup below 0° at the rear, nothing
    for a car that already runs it."""
    if d is None:
        return None
    now = toe_deg(d, setup, scales, d.index(setup))
    if now is None or now < 0:
        return None
    deg = {i: toe_deg(d, setup, scales, i) for i in range(d.lo, d.hi + 1)}

    def penalty(idx: dict[str, int]) -> float:
        v = deg.get(idx.get(d.name))
        return REAR_TOE_OUT_S if v is not None and v < 0 else 0.0
    return penalty


class Advisor:
    def __init__(self, store: Store, paths: LmuPaths | None):
        self.store = store
        self.paths = paths
        self.store.con.execute(SCHEMA)
        self._scales: dict[tuple[str, int, int], CarScales] = {}
        self._models: dict[tuple, tuple] = {}  # (car, layout, data stamp) -> (lap model, aero map)
        self._laps: dict[tuple, tuple] = {}  # (stint, settings searched) -> (lap model, lap function)

    # -- caches ----------------------------------------------------------------------
    def scales(self, car_signature: str) -> CarScales:
        n = self.store.con.execute("select count(*) from recordings where car_signature = ?", [car_signature]).fetchone()[0]
        svms = read_settings(self.paths.settings) if self.paths is not None else {}
        key = (car_signature, n, len(svms))
        if key not in self._scales:
            self._scales = {k: v for k, v in self._scales.items() if k[0] != car_signature}
            self._scales[key] = build_scales(self.store, car_signature, self.paths.settings if self.paths else None, svms)
        return self._scales[key]

    def invalidate(self) -> None:
        """New or recomputed stints: drop what was built from them."""
        self._models.clear()
        self._laps.clear()

    def layout_model(self, signature: str, layout: str, k: dict) -> tuple:
        """(lap model with trap drag, aero map), or (None, None). Fitting it is most of a
        proposal's time and it doesn't depend on the feel or the locks, so it is kept
        until this car's stints change."""
        stamp = self.store.con.execute(
            "select count(*), max(id), max(metrics_version) from recordings where car_signature = ?",
            [signature]).fetchone()
        key = (signature, layout, *stamp)
        if key not in self._models:
            model = LayoutModel.build(self.store, signature, layout, k)
            amap = aero_map(self.store, signature, k) if model is not None else None
            if model is not None:
                model = with_trap_drag(model)
            self._models = {kk: v for kk, v in self._models.items() if kk[:2] != (signature, layout)}
            self._models[key] = (model, amap)
        return self._models[key]

    # -- locks -----------------------------------------------------------------------
    def locks(self, context: str) -> set[str]:
        return set(self.store.get_setting(f"locks:{context}", []))

    def set_locks(self, context: str, keys: list[str], locked: bool) -> set[str]:
        current = self.locks(context)
        current = current | set(keys) if locked else current - set(keys)
        self.store.put_setting(f"locks:{context}", sorted(current))
        return current

    def brake_ceiling(self, car_signature: str) -> int | None:
        """Brake pressure index one step under the least pressure you've flagged too many
        lock-ups at on this car model; None when you never have."""
        rows = self.store.con.execute(
            """select r.setup_json from recordings r join ratings g on g.file_name = r.file_name
               where r.car_signature = ? and g.lockups""", [car_signature]).fetchall()
        flagged = [json.loads(sj).get("VM_BRAKE_PRESSURE", {}).get("value") for (sj,) in rows]
        flagged = [int(v) for v in flagged if v is not None]
        return min(flagged) - 1 if flagged else None

    # -- feel ------------------------------------------------------------------------
    def toggle(self, stint: int, axis: str, value: int) -> dict:
        stint = self.base_of(stint)[0]
        key, cls = recording_car(self.store, stint)
        feel.toggle(self.store, key, cls, stint, axis, value)
        return self.target_json(stint)

    def lock(self, stint: int) -> dict:
        stint = self.base_of(stint)[0]
        rec = self.store.recording(stint)
        fp = fpm.lock(self.store, stint, load_knowledge(rec["car_class"]))
        return {"fingerprint": fp.to_json(), **self.target_json(stint)}

    def target(self, stint: int) -> feel.Target:
        key, cls = recording_car(self.store, stint)
        k = load_knowledge(cls)
        return feel.target(self.store, key, cls, k, stint, self._mix(stint))

    def target_json(self, stint: int) -> dict:
        stint = self.base_of(stint)[0]
        t = self.target(stint)
        m = measured(self.store, same_setup_stints(self.store, stint))
        return {"target": t.to_json(), "measured": m.to_json(), "events": feel.events_json(self.store, t.car_key)}

    def _mix(self, stint: int) -> dict[str, float]:
        """Cornering time per speed band at this layout, over the car's stints there."""
        return measured(self.store, self._layout_stints(stint)).mix

    def _layout_stints(self, stint: int, any_car: bool = False) -> list[int]:
        """Every build's: only the lap-time model is limited to the installed build (sim/model.py)."""
        rec = self.store.recording(stint)
        sql = "select id from recordings r where coalesce(track_layout, track) = ?"
        args = [rec["track_layout"] or rec["track"]]
        if not any_car:
            sql += " and car_signature = ?"
            args.append(rec["car_signature"])
        return [r for (r,) in self.store.con.execute(sql, args).fetchall()]

    # -- the plan ----------------------------------------------------------------------
    def proposal(self, stint_id: int) -> Proposal:
        return self.plan(stint_id)[0]

    def base_of(self, stint_id: int) -> tuple[int, dict | None]:
        """(the stint a proposal is built from, the question about a short stint). A stint
        without a complete racing lap is asked about; until you say it was fine, the
        proposal is built from the last stint here with one."""
        rec = self.store.recording(stint_id)
        stints = analysis.stints(self.store, rec["car_name"], rec["track_layout"] or rec["track"])
        order = [st.id for st in stints]
        if stint_id not in order or any(lap["complete"] and not lap["in_pits"] for lap in stints[order.index(stint_id)].laps):
            return stint_id, None
        answer = (self.store.rating(rec["file_name"]) or {}).get("undrivable")
        question = {"stint": stint_id, "undrivable": answer}
        if answer is False:
            return stint_id, question
        good = next((st.id for st in reversed(stints[:order.index(stint_id)])
                     if any(lap["complete"] and not lap["in_pits"] for lap in st.laps)), None)
        return (stint_id if good is None else good), question

    def plan(self, stint_id: int) -> tuple[Proposal, Plan]:
        base, question = self.base_of(stint_id)
        pr, plan = self._plan(base, undone=stint_id if base != stint_id else None)
        plan.short = question
        pr.model = plan.to_json()
        return pr, plan

    def _plan(self, stint_id: int, undone: int | None = None) -> tuple[Proposal, Plan]:
        """``undone``: a stint you couldn't drive after this one; its moves aren't repeated."""
        store = self.store
        rec = store.recording(stint_id)
        context = analysis.context_of(store, stint_id)
        layout = context.split("|", 1)[1]
        locks = self.locks(context)
        scales = self.scales(rec["car_signature"])
        setup = store.setup_of(stint_id)
        k = load_knowledge(rec["car_class"])
        summary = analysis.stint_summary(store, stint_id)
        pr = propose(stint_id, summary, setup, rec["car_class"], scales, rating=store.rating(rec["file_name"]),
                     locks=locks, knowledge=k, brake_ceiling=self.brake_ceiling(rec["car_signature"]))
        car_key, car_class = recording_car(store, stint_id)
        tgt = feel.target(store, car_key, car_class, k, stint_id, self._mix(stint_id))
        m_now = measured(store, same_setup_stints(store, stint_id))
        plan = Plan(stint_id, car_key, car_class, layout, tgt, m_now.to_json())
        if not m_now.cells:
            plan.notes.append(NO_CORNERS)
            pr.notes += plan.notes
            return pr, plan
        taken = set(locks) | {key for c in pr.changes for key in c.keys}
        ddims = damper_dims(setup, taken)
        # The slow dampers with a balance effect are searched with the main settings; the
        # damper block refines them afterwards for tyre load.
        slow = [d for d in ddims if d.name in SLOW_BALANCE]
        dims = _front_toe_limit(main_dims(setup, taken), setup, scales) + slow
        kept = [d for d in ddims if d.name not in SLOW_BALANCE]  # dampers the main block keeps where they are
        base = {d.name: d.index(setup) for d in dims}
        plan.searched = len(dims)
        dbase = {d.name: d.index(setup) for d in ddims}

        balance = self._balance_model(rec, setup, dims + kept, k, scales, layout, car_key, car_class)
        plan.balance_source = balance.source
        if tgt.anchor in ("current", "toggled") and tgt.anchor_stint is not None and balance.offset:
            self._model_anchor(tgt, balance, dims + kept, k)
        tc = _traction(setup, scales, summary, tgt, m_now, k, locks)
        if tc is not None:
            pr.changes.append(tc)
            taken |= set(tc.keys)
        model, amap = self.layout_model(rec["car_signature"], layout, k)
        platform = platform_of(summary, dims, k, scales)
        ref_platform = None
        if model is not None:
            ref_platform = next((s.platform for s in model.stints if s.rid == stint_id and s.platform), model.platform)
        # The lap function remembers every setup it simulated; feel toggles search the same
        # setups again, so it is kept per stint and searched settings (locks change those).
        lkey = (stint_id, tuple(d.name for d in dims))
        held = self._laps.get(lkey)
        if held is None or held[0] is not model:  # rebuilt model: new data
            self._laps = {kk: v for kk, v in self._laps.items() if kk[0] != stint_id}
            held = self._laps[lkey] = (model, lap_function(store, model, amap, dims, base, ref_platform,
                                                           platform.mm_per_click)[0])
        lap = held[1]

        dmodel = self._damper_model(rec, setup, ddims + [d for d in dims if "spring" in d.name and "tender" not in d.name],
                                    k, layout,
                                    stint_id)
        band = (bo.TargetBand(tgt.cells, tgt.tolerance, tgt.weights, float(k["feel_step_deg"])) if tgt.cells else None)
        x_damp = {d.name: d.x(dbase[d.name]) for d in kept}

        def with_dampers(idx: dict[str, int]) -> dict[str, float]:
            return {**bo.x_of(dims, idx), **x_damp}

        class _Balance:  # the main block's view: dampers held where they are
            offset = balance.offset

            @staticmethod
            def predict(x):
                return balance.predict({**x, **x_damp})

        extra = []
        if dmodel is not None:
            extra.append(lambda idx: dmodel.kerb_ok(with_dampers(idx), with_dampers(base)))
        # On a track the car hasn't been driven at, the fingerprint's own setup is a candidate
        # however far it is (the first proposal should already be close); once there are
        # stints here, the trust region and the ride-height reach hold.
        candidates = []
        fp = fpm.own(store, car_key)
        if fp is not None and fp.setup and balance.source != "layout":
            candidates.append({n: v["index"] for n, v in fp.setup.items() if n in base})
        reach = {d.name: max(1, round(MAX_RH_MM / abs(platform.mm_per_click.get(d.name, 1.0))))
                 for d in dims if d.name.startswith("rh_")}
        toe = [d for d in dims if d.name in ("toe_front", "toe_rear")]
        reach.update({d.name: _by_kind(REACH, d.name) for d in dims if _by_kind(REACH, d.name) is not None})
        reach.update({d.name: SLOW_REACH for d in slow})
        reach.update({d.name: THIRD_REACH for d in dims if d.name.startswith("third_spring")})
        avoid = {}
        if undone is not None:
            bad = store.setup_of(undone)
            for d in dims:
                if all(key in bad.params for key in d.keys) and d.index(bad) != base[d.name]:
                    step = d.index(bad) - base[d.name]
                    avoid[d.name] = (1 if step > 0 else -1, abs(step))
        prob = bo.Problem(dims, base, lap, _Balance(), band, platform, extra,
                          (lambda idx: dmodel.contact(with_dampers(idx))[0]) if dmodel is not None else None,
                          self._radius(stint_id, rec, tgt), candidates, seed=stint_id, reach=reach,
                          cost={d.name: _by_kind(CHANGE_COST, d.name) for d in dims
                                if _by_kind(CHANGE_COST, d.name) is not None},
                          penalty=_rear_toe_out(next((d for d in toe if d.name == "toe_rear"), None), setup, scales),
                          avoid=avoid)
        choice = bo.solve(prob)
        plan.reason, plan.feasible, plan.gap, plan.wing = choice.reason, choice.feasible, choice.gap, choice.wing
        plan.predicted = {c: [m, float(np.sqrt(v))] for c, (m, v) in choice.best.balance.items()}
        if choice.base.lap[1] < UNKNOWN_SD:
            plan.lap = {"base": choice.base.lap[0], "best": choice.best.lap[0],
                        "delta": choice.best.lap[0] - choice.base.lap[0], "sd": choice.best.lap[1]}
            if model is not None and amap is not None and ref_platform is not None:
                plan.lap["sectors"] = self._sector_delta(model, amap, dims, base, choice.best.indices, ref_platform,
                                                         platform.mm_per_click)
                wing = aero_wing(dims)
                if wing is not None:
                    s = aero_sensitivity(model, amap, ref_platform, wing.clicks)
                    plan.wing = {**(plan.wing or {}), "s_per_click": s.wing_s_per_click, "rake_s_per_mm": s.rake_s_per_mm}
        main_rows = {}
        for name, (a, b) in choice.moves.items():
            d = next(x for x in dims if x.name == name)
            main_rows[name] = len(pr.changes)
            pr.changes.append(self._change(d, setup, scales, a, b, self._reason(prob, d, b)))
        if ref_platform is not None:
            plan.platform = self._platforms(ref_platform, base, choice.best.indices, platform.mm_per_click, setup,
                                            scales, dims)

        # Dampers, around the main choice, inside the same band: a damper move may not cut
        # the chance of being in the band by more than DAMPER_BAND_LOSS.
        if dmodel is not None and ddims:
            x_main = bo.x_of(dims, choice.best.indices)
            dbase = {**dbase, **{d.name: choice.best.indices[d.name] for d in slow}}  # refine, not undo
            p_main = bo.p_in_band(choice.best.balance, band)

            def balance_ok(idx: dict[str, int]) -> float:
                if band is None:
                    return 1.0
                pred = balance.predict({**x_main, **{d.name: d.x(idx[d.name]) for d in ddims}})
                return 1.0 if bo.p_in_band(pred, band) >= (1 - DAMPER_BAND_LOSS) * p_main else 0.0

            dc = dampers.choose(dmodel, ddims, dbase, x_main, balance_ok)
            plan.dampers = {"contact": list(dc.contact), "contact_base": list(dc.contact_base),
                            "exploring": dc.exploring, "stints": dmodel.stints,
                            "settings": dmodel.settings}
            gone = []
            for name, (a, b) in dc.moves.items():
                d = next(x for x in ddims if x.name == name)
                if name in main_rows:  # moved by both blocks: one row, driven → final, the balance reason
                    i = main_rows[name]
                    driven = d.index(setup)
                    if b == driven:
                        gone.append(i)
                    else:
                        pr.changes[i] = self._change(d, setup, scales, driven, b, pr.changes[i].reason)
                    continue
                pr.changes.append(self._change(d, setup, scales, a, b, dc.reason, group="dampers",
                                               source="learning" if dc.exploring else "model"))
            pr.changes = [c for i, c in enumerate(pr.changes) if i not in gone]
        pr.notes += plan.notes
        pr.model = plan.to_json()
        return pr, plan

    def _model_anchor(self, tgt: feel.Target, balance: BalanceModel, dims: list[Dim], k: dict) -> None:
        """A target anchored on a stint (not a locked fingerprint) starts from the balance
        model's estimate at that stint's setup: its own measurement pooled with every other
        setup driven here. One short outing alone is noisy; aiming at its noise would
        chase it. The toggles are put back on top; the band is the estimate's own doubt,
        narrowed where a toggle moved it."""
        s = self.store.setup_of(tgt.anchor_stint)
        x = {d.name: d.x(d.index(s)) for d in dims if all(key in s.params for key in d.keys)}
        pred = balance.predict(x)
        floor = float(k["feel_tolerance_deg"])
        tgt.cells = feel.shifted({c: m for c, (m, _) in pred.items()}, tgt.front, tgt.rear, float(k["feel_step_deg"]))
        step = float(k["feel_step_deg"])
        tgt.tolerance = feel.narrowed({c: max(floor, 2.0 * float(np.sqrt(v))) for c, (_, v) in pred.items()},
                                      tgt.front, tgt.rear, step)
        mix = {c.split("_")[1]: w for c, w in tgt.weights.items()}
        tgt.weights = {c: mix.get(c.split("_")[1], 0.5) for c in tgt.cells}

    def _balance_model(self, rec, setup, dims: list[Dim], k, scales, layout, car_key, car_class) -> BalanceModel:
        slopes = sensitivities(dims, k, scales)
        # Every build's stints: an update (BoP, physics) shifts the balance a little, so older
        # builds' count with more doubt (OLDER_BUILD_SD), but what a setup did then still says
        # where to start from.
        build = current_build(self.store)
        rows = self.store.con.execute(
            """select id, coalesce(track_layout, track), setup_json, game_build from recordings r
               where car_signature = ?
               and file_name not in (select file_name from ratings where undrivable) order by file_mtime""",
            [rec["car_signature"]]).fetchall()
        from autosetup.lmu.setup_space import CarSetup

        # One piece of evidence per setup driven at a layout (its stints pooled), as the
        # target pools them: the setup you're on is then measured the same way by both.
        groups: dict[tuple, list[int]] = {}
        setups = {}
        for rid, lay, sj, gb in rows:
            s = CarSetup.from_json(json.loads(sj))
            key = (lay, setup_fingerprint(s), build is not None and gb != build)
            groups.setdefault(key, []).append(rid)
            setups[key] = s
        evidence = []
        for (lay, fp_key, older), rids in groups.items():
            m = measured(self.store, rids)
            if not m.cells:
                continue
            s = setups[(lay, fp_key, older)]
            x = {d.name: d.x(d.index(s)) for d in dims if all(key in s.params for key in d.keys)}
            evidence.append(Evidence(lay, x, {c: (v.mean, v.se) for c, v in m.cells.items()}, rec["id"] in rids,
                                     older_build=older))
        fp = fpm.anchor_for(self.store, car_key, car_class, k)
        fp_pair = None
        if fp is not None and fp.setup:
            fx = {d.name: d.x(fp.setup[d.name]["index"]) for d in dims if d.name in fp.setup}
            fp_pair = (fx, fp.cells)
        return BalanceModel.fit(slopes, layout, evidence, fp_pair)

    def _damper_model(self, rec, setup, dims: list[Dim], k, layout, stint_id) -> dampers.DamperModel | None:
        if not dims:
            return None
        data = []
        for rid in self._layout_stints(stint_id):
            vals = dampers.stint_metrics(self.store, rid, k)
            if not vals:
                continue
            s = self.store.setup_of(rid)
            sm = dampers.StintMetrics(rid, {d.name: d.x(d.index(s)) for d in dims if all(key in s.params for key in d.keys)},
                                      vals)
            data.append(sm)
        return dampers.DamperModel.fit(dims, data) if data else None

    def _platforms(self, ref: Platform, base: dict[str, int], best: dict[str, int], mm: dict[str, float], setup,
                   scales, dims: list[Dim]) -> dict:
        """Ride heights at speed and rake (mm) and the wing, now and with the proposal."""
        def at(idx):
            f = ref.rh_front + (idx.get("rh_front", base.get("rh_front", 0)) - base.get("rh_front", 0)) * mm.get("rh_front", 1.0)
            r = ref.rh_rear + (idx.get("rh_rear", base.get("rh_rear", 0)) - base.get("rh_rear", 0)) * mm.get("rh_rear", 1.0)
            wing = aero_wing(dims)
            text = None
            if wing is not None:
                text = scales.label(wing.keys[0], idx[wing.name])[0] or setup[wing.keys[0]].text.strip()
            else:
                key = next((k for k in ("VM_REAR_WING", "VM_FRONT_WING") if k in setup.params
                            and setup[k].adjustable), "VM_REAR_WING")
                text = setup[key].text.strip() if key in setup.params else None
            return {"rh_front": round(f, 1), "rh_rear": round(r, 1), "rake": round(r - f, 1), "wing": text}
        return {"current": at(base), "proposed": at(best)}

    def _sector_delta(self, model, amap, dims, base, best, ref_platform, mm) -> list[float]:
        def plat(idx):
            def rh(name, ref_mm):
                return ref_mm + (idx.get(name, base.get(name, 0)) - base.get(name, 0)) * mm.get(name, 1.0)
            wing = aero_wing(dims)
            return Platform(wing.x(idx[wing.name]) if wing else ref_platform.wing, rh("rh_front", ref_platform.rh_front),
                            rh("rh_rear", ref_platform.rh_rear))
        a = model.simulate(model.car_at(amap, plat(base))).sectors
        b = model.simulate(model.car_at(amap, plat(best))).sectors
        return [y - x for x, y in zip(a, b)]

    def _reason(self, prob: bo.Problem, d: Dim, b: int) -> str:
        """One line: what this move does alone. The time is a prediction with its doubt,
        not a promise: "predicted −0.16 ± 0.10 s · more rotation at turn-in in slow corners"."""
        alone = {**prob.base, d.name: b}
        (lap0, sd0), (lap1, sd1) = prob.lap(prob.base), prob.lap(alone)
        pred0 = prob.balance.predict(bo.x_of(prob.dims, prob.base))
        pred1 = prob.balance.predict(bo.x_of(prob.dims, alone))
        parts = []
        if abs(lap1 - lap0) >= 0.01 and sd0 < UNKNOWN_SD:
            parts.append(f"predicted {lap1 - lap0:+.2f} ± {sd1:.2f} s".replace("-", "−"))
        moved = sorted(((pred1[c][0] - pred0[c][0], c) for c in pred1 if c in pred0), key=lambda t: -abs(t[0]))
        if moved and abs(moved[0][0]) >= 0.05:
            delta, cell = moved[0]
            ph, band = cell.split("_")
            parts.append(f"{'more rotation' if delta < 0 else 'more stability'} {PHASE[ph]} in {BAND[band]} corners")
        return " · ".join(parts) or SMALL_ALONE

    def _change(self, d: Dim, setup, scales, a: int, b: int, reason: str, group: str = "balance",
                source: str = "model") -> Change:
        keys = list(d.keys)
        before = {key: setup[key].value for key in keys}
        after = {key: b for key in keys}
        label, exact = scales.label(keys[0], b)
        if label is None:
            label = f"{setup[keys[0]].text.strip()} {'+' if b > a else '−'}{abs(b - a)} step{'s' if abs(b - a) > 1 else ''}"
        name = NAMES.get(d.name) or analysis.pretty_name(keys[0]) + (
            "/" + "/".join(key[-2:] for key in keys[1:]) if len(keys) > 1 else "")
        return Change(group, keys, before, after, reason, name, setup[keys[0]].text.strip(), label, approx=not exact,
                      source=source)

    def _radius(self, stint_id: int, rec: dict, tgt: feel.Target) -> float:
        """Trust region from history: each written proposal that was then driven grows it
        if that stint was quicker (median clean lap) and inside the band, and shrinks it if not."""
        context = analysis.context_of(self.store, stint_id)
        car, layout = context.split("|", 1)
        rows = self.store.con.execute(
            """select p.recording_id, p.changes from proposals p join recordings r on r.id = p.recording_id
               where r.car_name = ? and coalesce(r.track_layout, r.track) = ? order by p.id""", [car, layout]).fetchall()
        if not rows:
            return bo.RADIUS_DEFAULT
        stints = [st for st in analysis.stints(self.store, car, layout)]
        order = [st.id for st in stints]
        radius = bo.RADIUS_DEFAULT
        setups: dict[int, CarSetup] = {}

        def setup_of(rid: int) -> CarSetup:
            if rid not in setups:
                setups[rid] = self.store.setup_of(rid)
            return setups[rid]

        for rid, changes in rows:
            if rid not in order:
                continue
            values = {key: v for c in json.loads(changes) for key, v in c["after"].items()}
            nxt = next((st for st in stints[order.index(rid) + 1:]
                        if all(key in setup_of(st.id).params and setup_of(st.id)[key].value == v
                               for key, v in values.items())), None)
            prev = stints[order.index(rid)]
            if nxt is None or prev.summary.get("median") is None or nxt.summary.get("median") is None:
                continue
            inside = True
            if tgt.cells:
                m = measured(self.store, [nxt.id]).mean()
                inside = all(abs(m[c] - tgt.cells[c]) <= tgt.tolerance[c] for c in m if c in tgt.cells
                             and tgt.weights.get(c, 0.5) >= bo.MIN_WEIGHT)
            radius = bo.next_radius(radius, nxt.summary["median"] < prev.summary["median"] and inside)
        return radius

    # -- json / writing -------------------------------------------------------------------
    def proposal_json(self, stint_id: int) -> dict:
        pr, plan = self.plan(stint_id)
        stint_id = pr.stint_id  # the stint it's built from
        out = pr.to_json()
        out["context"] = analysis.context_of(self.store, stint_id)
        out["locks"] = [{"key": key, "name": analysis.pretty_name(key)} for key in sorted(self.locks(out["context"]))]
        rec = self.store.recording(stint_id)
        out["brake_pressure_by_feel"] = manages_brake_pressure(self.store.setup_of(stint_id),
                                                               load_knowledge(rec["car_class"]))
        out["car_class"] = rec["car_class"]
        out["plan"] = plan.to_json()
        out["target"] = None
        out["error"] = None
        out["why"] = why_nothing(pr, plan, len(out["locks"]))
        if pr.changes and self.paths is not None:
            try:
                res = write_setup(self.store, self.paths, stint_id, pr.values, dry_run=True, names=self.templates(),
                                  fields=self._fields(pr, plan))
                out["target"] = {"file": res.path.name, "folder": res.path.parent.name, "notes": res.notes}
            except WriteError as e:
                out["error"] = str(e)
        return out

    def write(self, stint_id: int, signature: str) -> dict:
        if self.paths is None:
            raise WriteError("LMU install not found; can't write setups")
        pr, plan = self.plan(stint_id)
        stint_id = pr.stint_id  # the stint it's built from
        if pr.signature != signature:
            raise ProposalConflict("The proposal changed since it was shown; check it and write again.")
        if not pr.changes:
            raise WriteError("Nothing to change")
        res = write_setup(self.store, self.paths, stint_id, pr.values, names=self.templates(),
                          fields=self._fields(pr, plan))
        rec = self.store.recording(stint_id)
        self.store.con.execute(
            "insert into proposals (created_at, recording_id, base_file, context, signature, changes, svm_path) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            [datetime.now(timezone.utc).replace(tzinfo=None), stint_id, rec["file_name"],
             analysis.context_of(self.store, stint_id), pr.signature,
             json.dumps([{key: getattr(c, key) for key in ("group", "keys", "before", "after", "reason")} for c in pr.changes]),
             str(res.path)],
        )
        return {"file": res.path.name, "folder": res.path.parent.name, "path": str(res.path),
                "changes": len(pr.changes)}

    def templates(self) -> tuple[str, str]:
        """(file name, description) templates from Settings, the defaults where unset."""
        t = self.store.get_setting("templates", {}) or {}
        return t.get("name") or templates.NAME, t.get("notes") or templates.NOTES

    def _fields(self, pr: Proposal, plan: Plan) -> dict:
        """Template fields of a proposal: the stint it's built from (its number in the list),
        its best lap, the feel asked for, the changes in plain words."""
        rec = self.store.recording(pr.stint_id)
        stints = analysis.stints(self.store, rec["car_name"], rec["track_layout"] or rec["track"])
        order = [st.id for st in stints]
        n = order.index(pr.stint_id) + 1 if pr.stint_id in order else ""
        laps = [lap["time"] for st in stints if st.id == pr.stint_id for lap in st.laps
                if lap["complete"] and not lap["in_pits"] and lap["time"]]
        best = min(laps) if laps else None
        return {"stint": n, "lap": templates.lap_text(best, True), "lap_notes": templates.lap_text(best, False),
                "feel": templates.feel_text(plan.target.front, plan.target.rear),
                "changes": "; ".join(f"{c.name} {c.before_text} → {'≈ ' if c.approx else ''}{c.after_text}"
                                     + (f": {c.reason}" if c.reason else "") for c in pr.changes)}

    def written(self, stint_id: int) -> list[dict]:
        cur = self.store.con.execute(
            "select signature, svm_path, created_at from proposals where recording_id = ? order by id", [stint_id])
        return [dict(signature=s, file=Path(p).name, created_at=str(t)) for s, p, t in cur.fetchall()]

