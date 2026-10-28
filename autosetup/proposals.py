"""Glue between the store, the advisor and the .svm writer: build the proposal for a
stint, keep per-context locks, and write a proposal out (recording what was written so
phase 4 can link the next stint back to it).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from autosetup import analysis
from autosetup.advisor import (MIN_RACING_LAPS, SEMANTIC, Change, Proposal, load_knowledge, manages_brake_pressure,
                               propose, understeer_signs)
from autosetup.lmu.paths import LmuPaths
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import SvmFile, read_settings
from autosetup.opt.balance import Balance
from autosetup.opt.balance import learn as learn_balance
from autosetup.opt.balance import feel_key, rated_stints, reset_key
from autosetup.opt.dataset import OBJECTIVES, UNITS, Dataset, build_dataset, domain_of
from autosetup.opt.drag import full_throttle_shares, wing_effect
from autosetup.opt.optimizer import (CHANGE_SIZES, ChangeSize, ModelResult, next_radius, optimise, prior_spread,
                                     weights_vector)
from autosetup.opt.preference import DEFAULT_WEIGHTS, Preference, evidence, learn
from autosetup.scales import CarScales, build_scales
from autosetup.setup_writer import WriteError, write_setup
from autosetup.store import Store

MODEL_CONFIDENCE = 0.7  # chance of beating the current setup before the model overrides the rules
DEFAULT_CHANGE_SIZE = "large"  # the toy-car benchmark converges fastest with large steps

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


class ProposalConflict(Exception):
    """The proposal changed between showing it and writing it (new data, locks, ratings)."""


class Advisor:
    def __init__(self, store: Store, paths: LmuPaths | None):
        self.store = store
        self.paths = paths
        self.store.con.execute(SCHEMA)
        self._scales: dict[tuple[str, int, int], CarScales] = {}
        self._pref: tuple[tuple, Preference] | None = None
        self._balance: dict[str, tuple[tuple, Balance]] = {}

    # -- caches ----------------------------------------------------------------------
    def _svm_cache(self) -> dict[Path, SvmFile]:
        """Saved setups, shared with the link matching (each file read once until it changes)."""
        return read_settings(self.paths.settings) if self.paths is not None else {}

    def scales(self, car_signature: str) -> CarScales:
        n = self.store.con.execute(
            "select count(*) from recordings where car_signature = ?", [car_signature]).fetchone()[0]
        svms = self._svm_cache()
        key = (car_signature, n, len(svms))
        if key not in self._scales:
            self._scales = {k: v for k, v in self._scales.items() if k[0] != car_signature}
            self._scales[key] = build_scales(self.store, car_signature,
                                             self.paths.settings if self.paths else None, svms)
        return self._scales[key]

    # -- what you prefer ---------------------------------------------------------------------
    def preference(self) -> Preference:
        """The trade-off learned from your reverts, keeps and ratings (no sliders).
        Recomputed when stints or ratings change."""
        stamp = self.store.con.execute(
            "select (select count(*) from recordings), (select max(id) from recordings), "
            "(select count(*) from ratings), (select max(rated_at) from ratings)").fetchone()
        if self._pref is None or self._pref[0] != stamp:
            pref = learn(evidence(self.store, lambda cls: load_knowledge(cls)["fuel_s_per_l"]))
            self._pref = (stamp, pref)
        return self._pref[1]

    def balance(self, car_class: str, knowledge: dict) -> Balance:
        """The rotation you like per corner phase, from your ratings of this car class.
        Recomputed when stints or ratings change."""
        stamp = self.store.con.execute(
            "select (select count(*) from recordings), (select max(id) from recordings), "
            "(select count(*) from ratings), (select max(rated_at) from ratings)").fetchone() + (
            self.store.get_setting(reset_key(car_class)),)
        cached = self._balance.get(car_class)
        if cached is None or cached[0] != stamp:
            angles, countersteer = rated_stints(self.store, car_class)
            cached = (stamp, learn_balance(angles, knowledge, countersteer))
            self._balance[car_class] = cached
        return cached[1].with_feel(**self.feel(car_class))

    def feel(self, car_class: str) -> dict[str, int | None]:
        """The car feel you picked for this class: front −1 Pointier / +1 Lazier, rear −1
        Looser / +1 Safer, None where you haven't picked."""
        out = {}
        for side in ("front", "rear"):
            v = self.store.get_setting(feel_key(car_class, side))
            out[side] = int(v) if v is not None else None
        return out

    def set_feel(self, car_class: str, side: str, step: int | None) -> None:
        if side not in ("front", "rear"):
            raise ValueError("side must be front or rear")
        if step not in (None, -1, 1):
            raise ValueError("feel must be -1, 1 or null")
        self.store.put_setting(feel_key(car_class, side), step)

    # -- locks -----------------------------------------------------------------------
    def locks(self, context: str) -> set[str]:
        return set(self.store.get_setting(f"locks:{context}", []))

    def set_locks(self, context: str, keys: list[str], locked: bool) -> set[str]:
        current = self.locks(context)
        current = current | set(keys) if locked else current - set(keys)
        self.store.put_setting(f"locks:{context}", sorted(current))
        return current

    # -- proposals ---------------------------------------------------------------------
    def proposal(self, stint_id: int) -> Proposal:
        rec = self.store.recording(stint_id)
        context = analysis.context_of(self.store, stint_id)
        weights = self.preference().weights
        locks = self.locks(context)
        scales = self.scales(rec["car_signature"])
        setup = self.store.setup_of(stint_id)
        knowledge = load_knowledge(rec["car_class"])
        summary = analysis.stint_summary(self.store, stint_id)
        size = self.change_size()
        balance = self.balance(rec["car_class"], knowledge)
        shares = full_throttle_shares(self.store, rec["car_class"])
        pr = propose(stint_id, summary, setup, rec["car_class"], scales,
                     rating=self.store.rating(rec["file_name"]), weights=weights, locks=locks, knowledge=knowledge,
                     size=size, recent=self.recent_keys(stint_id), targets=balance.targets,
                     countersteer=balance.tolerance(knowledge, context.split("|", 1)[1]),
                     brake_ceiling=self.brake_ceiling(rec["car_signature"]),
                     wing_effect=wing_effect(shares, context.split("|", 1)[1], knowledge))
        if (summary.get("racing_laps") or 0) >= MIN_RACING_LAPS:
            self._add_model(pr, stint_id, setup, scales, weights, locks, knowledge, CHANGE_SIZES[size], balance,
                            shares)
        return pr

    def brake_ceiling(self, car_signature: str) -> int | None:
        """Brake pressure index one step under the least pressure you've flagged too many
        lock-ups at on this car model; None when you never have."""
        rows = self.store.con.execute(
            """select r.setup_json from recordings r join ratings g on g.file_name = r.file_name
               where r.car_signature = ? and g.lockups""", [car_signature]).fetchall()
        flagged = [json.loads(sj).get("VM_BRAKE_PRESSURE", {}).get("value") for (sj,) in rows]
        flagged = [int(v) for v in flagged if v is not None]
        return min(flagged) - 1 if flagged else None

    def change_size(self) -> str:
        size = self.store.get_setting("change_size", DEFAULT_CHANGE_SIZE)
        return size if size in CHANGE_SIZES else DEFAULT_CHANGE_SIZE

    def recent_keys(self, stint_id: int, stints: int = 2) -> set[str]:
        """Settings that changed going into this stint and the one before it."""
        context = analysis.context_of(self.store, stint_id)
        car_name, layout = context.split("|", 1)
        rows = self.store.con.execute(
            """select id, setup_json from recordings
               where car_name = ? and coalesce(track_layout, track) = ? and duration > 30
                 and file_mtime <= (select file_mtime from recordings where id = ?)
               order by file_mtime desc limit ?""", [car_name, layout, stint_id, stints + 1]).fetchall()
        setups = [CarSetup.from_json(json.loads(r[1])) for r in rows]
        keys: set[str] = set()
        for newer, older in zip(setups, setups[1:]):
            keys |= {k for k, p in newer.params.items() if k in older.params and older[k].value != p.value}
        return keys

    # -- optimiser ----------------------------------------------------------------------
    def dataset(self, stint_id: int, knowledge: dict, balance: Balance | None = None) -> Dataset:
        """Stints of this car at this track only. Other tracks' stints don't come along:
        every track asks different things of a setup, so what worked there isn't evidence
        here. (What you like, the balance and feel, does carry over: it's yours.)"""
        tolerance = (lambda layout: balance.tolerance(knowledge, layout)) if balance else None
        return build_dataset(self.store, domain_of(self.store, stint_id), float(knowledge["fuel_s_per_l"]),
                             tolerance=tolerance, transfer=False)

    def _add_model(self, pr: Proposal, stint_id: int, setup, scales: CarScales, weights: dict,
                   locks: set[str], knowledge: dict, size: ChangeSize, balance: Balance | None = None,
                   shares: dict[str, float] | None = None) -> None:
        """Balance moves from the model of this car + layout, filling what the rules left
        of the change size (``size.moves`` settings per proposal):
        * nothing until at least one stint qualifies (3+ clean laps);
        * learning (too few stints or setups): settings chosen to learn the most —
          dampers, springs, wing and the rest the rules don't touch — at the profile's
          step size;
        * ready: by expected improvement; when the model is confident its pick beats the
          current setup, telemetry-driven rule moves are replaced (rated complaints keep
          their direct fix). Not confident: the rules lead and the model only learns.
        """
        ds = self.dataset(stint_id, knowledge, balance)
        ready = (len(ds.obs) >= int(knowledge["model_min_stints"])
                 and ds.distinct_setups >= int(knowledge["model_min_setups"]))
        state = "off" if not ds.all_obs or not ds.dims else ("ready" if ready else "learning")
        radius = self._radius(ds, weights, size.radius)
        info = {"state": state, "n_obs": len(ds.obs), "n_setups": ds.distinct_setups,
                "min_stints": int(knowledge["model_min_stints"]), "min_setups": int(knowledge["model_min_setups"]),
                "fuel_s_per_l": ds.fuel_s_per_l, "trend_s_per_lap": ds.trend_s_per_lap, "lap_sigma": ds.lap_sigma,
                "ref_fuel": ds.ref_fuel, "radius": radius, "excluded": ds.excluded, "exploring": False,
                "predicted": None, "confidence": None, "experience_effect": None, "effects": [],
                "transfer": {"tracks": ds.transfer_from, "stints": len(ds.extra), "setups": ds.distinct_setups_all,
                             "rho": None} if ds.extra else None,
                "weights": weights, "balance": balance.to_json() if balance else None}
        pr.model = info
        if state == "off":
            return
        last = ds.obs[-1] if ds.obs else None
        now = (_experience_now(self.store, stint_id), last.track_temp if last else None)
        # Where the pace model starts on the rear wing, per layout (drag.py), in index direction.
        more_wing = SEMANTIC["up"]("VM_REAR_WING", scales)
        wing = {lay: wing_effect(shares or {}, lay, knowledge) for lay in [ds.target, *ds.transfer_from]}
        pace_prior = {lay: {"VM_REAR_WING": more_wing * s} for lay, s in wing.items() if s}
        info["wing_effect"] = wing[ds.target]

        def run(changes: list[Change], learning: bool) -> tuple[int, ModelResult]:
            budget = max(0, size.moves - len([c for c in changes if c.group == "balance"]))
            if weights.get("drivability", 0.75) < 0.3:
                budget = min(budget, 1)
            taken = set(locks) | {k for c in changes for k in c.keys}
            return budget, optimise(ds, setup, weights, now, locks=taken, radius=radius,
                                    size=ChangeSize(size.name, max(budget, 1), size.step_frac, size.radius),
                                    learning=learning, seed=stint_id, balance=balance,
                                    understeer=understeer_signs(scales), pace_prior=pace_prior)

        # Telemetry-driven rule moves give way to the model only when it is actually
        # sure of its pick. Enough stints doesn't mean it has learned anything: with
        # ~20 settings and lap times inside the noise, "ready" would otherwise replace
        # "fix the understeer" with a random walk.
        use_model = False
        if ready:
            kept = [c for c in pr.changes if not (c.group == "balance" and c.source == "telemetry")]
            budget, res = run(kept, learning=False)
            info["confidence"] = res.confidence
            use_model = bool(res.moves) and res.confidence >= MODEL_CONFIDENCE
            if use_model:
                pr.changes = kept
        if not use_model:
            rated_or_other = [c for c in pr.changes if not (c.group == "balance" and c.source == "telemetry")]
            telemetry_rules = [c for c in pr.changes if c.group == "balance" and c.source == "telemetry"]
            max_telem = max(0, size.moves - 2) if size.moves >= 3 else max(0, size.moves - 1)
            pr.changes = rated_or_other + telemetry_rules[:max_telem]
            budget, res = run(pr.changes, learning=True)
        info["experience_effect"] = list(res.experience_effect) if res.experience_effect else None
        if info["transfer"] and res.rho:
            info["transfer"]["rho"] = res.rho["pace"]
        info["effects"] = [_effect_json(e, scales) for e in res.effects]
        if budget <= 0 or not res.moves:
            return
        moves = res.moves[:budget]
        info["exploring"] = not use_model
        info["predicted"] = {n: list(v) for n, v in res.predicted.items()}
        for m, d, bal in zip(moves, res.per_move, res.per_move_balance or [{}] * len(moves)):
            before = {k: setup[k].value for k in m.dim.keys}
            after = {k: m.after for k in m.dim.keys}
            label, exact = scales.label(m.dim.keys[0], m.after)
            if label is None:
                label = f"{setup[m.dim.keys[0]].text.strip()} {'+' if m.steps > 0 else '−'}{abs(m.steps)} step{'s' if abs(m.steps) > 1 else ''}"
            if m.dim.name == "VM_CASTER":
                name = "Caster"
            elif m.dim.name.startswith("WM_CAMBER"):
                name = f"Camber {'Front' if m.dim.name.endswith(':F') else 'Rear'}"
            else:
                name = analysis.pretty_name(m.dim.keys[0]) + ("/" + "/".join(k[-2:] for k in m.dim.keys[1:]) if len(m.dim.keys) > 1 else "")
            reason = _model_reason(ds, d, bal, balance) if use_model else _learning_reason(ds, d)
            pr.changes.append(Change("balance", list(m.dim.keys), before, after, reason, name,
                                     setup[m.dim.keys[0]].text.strip(), label, approx=not exact,
                                     source="model" if use_model else "learning"))

    def _radius(self, ds: Dataset, weights: dict, start: float) -> float:
        """Trust region from history: each written proposal that was then driven grows it
        if that stint beat the one it was built from, and shrinks it if not."""
        if len(ds.obs) < 2:
            return start
        rows = self.store.con.execute(
            "select recording_id, changes from proposals where recording_id in (select unnest(?::INTEGER[])) order by id",
            [[o.stint_id for o in ds.obs]]).fetchall()
        written = {}
        for rid, changes in rows:
            written.setdefault(rid, []).append({k: v for c in json.loads(changes) for k, v in c["after"].items()})
        w = weights_vector(weights)

        def cost(o):
            z = np.array([(o.y[n] - ds.obs[0].y[n]) / prior_spread(n, ds) for n in OBJECTIVES])
            return float((w * z).max() + 0.05 * (w * z).sum())

        radius = start
        for prev, cur in zip(ds.obs, ds.obs[1:]):
            for values in written.get(prev.stint_id, []):
                if all(k in cur.setup and cur.setup[k].value == v for k, v in values.items()):
                    radius = next_radius(radius, cost(cur) < cost(prev))
                    break
        return radius

    def model_json(self, stint_id: int) -> dict | None:
        return self.proposal(stint_id).model

    def proposal_json(self, stint_id: int) -> dict:
        pr = self.proposal(stint_id)
        out = pr.to_json()
        out["context"] = analysis.context_of(self.store, stint_id)
        out["locks"] = [{"key": k, "name": analysis.pretty_name(k)} for k in sorted(self.locks(out["context"]))]
        rec = self.store.recording(stint_id)
        out["brake_pressure_by_feel"] = manages_brake_pressure(self.store.setup_of(stint_id),
                                                               load_knowledge(rec["car_class"]))
        out["car_class"] = rec["car_class"]
        out["feel"] = self.feel(rec["car_class"])
        out["target"] = None
        out["error"] = None
        if pr.changes and self.paths is not None:
            try:
                res = write_setup(self.store, self.paths, stint_id, pr.values, dry_run=True)
                out["target"] = {"file": res.path.name, "folder": res.path.parent.name}
            except WriteError as e:
                out["error"] = str(e)
        return out

    def write(self, stint_id: int, signature: str) -> dict:
        if self.paths is None:
            raise WriteError("LMU install not found; can't write setups")
        pr = self.proposal(stint_id)
        if pr.signature != signature:
            raise ProposalConflict("The proposal changed since it was shown; check it and write again.")
        if not pr.changes:
            raise WriteError("Nothing to change")
        res = write_setup(self.store, self.paths, stint_id, pr.values)
        rec = self.store.recording(stint_id)
        self.store.con.execute(
            "insert into proposals (created_at, recording_id, base_file, context, signature, changes, svm_path) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            [datetime.now(timezone.utc).replace(tzinfo=None), stint_id, rec["file_name"],
             analysis.context_of(self.store, stint_id), pr.signature,
             json.dumps([{k: getattr(c, k) for k in ("group", "keys", "before", "after", "reason")} for c in pr.changes]),
             str(res.path)],
        )
        return {"file": res.path.name, "folder": res.path.parent.name, "path": str(res.path),
                "changes": len(pr.changes)}

    def written(self, stint_id: int) -> list[dict]:
        cur = self.store.con.execute(
            "select signature, svm_path, created_at from proposals where recording_id = ? order by id", [stint_id])
        return [dict(signature=s, file=Path(p).name, created_at=str(t)) for s, p, t in cur.fetchall()]


def _experience_now(store: Store, stint_id: int) -> float:
    """Racing laps on this car + layout up to and including this stint."""
    row = store.con.execute(
        """select count(*) from laps l join recordings r on r.id = l.recording_id
           where r.car_signature = (select car_signature from recordings where id = $1)
             and coalesce(r.track_layout, r.track) = (select coalesce(track_layout, track) from recordings where id = $1)
             and r.file_mtime <= (select file_mtime from recordings where id = $1)
             and l.complete and not l.in_pits""", [stint_id]).fetchone()
    return float(row[0])


def _phrase(name: str, mean: float, sd: float) -> str:
    better, worse, unit, dec = {
        "pace": ("faster", "slower", "s", 2), "tyres": ("less wear", "more wear", "%/lap", 2),
        "drivability": ("easier to drive", "harder to drive", "s", 2)}[name]
    if abs(mean) < max(0.5 * sd, 10 ** -dec):
        return {"pace": "same pace", "tyres": "similar wear", "drivability": "similar drivability"}[name]
    if name == "pace":
        return f"about {abs(mean):.{dec}f} s {better if mean < 0 else worse}"
    return better if mean < 0 else worse


def _stints_phrase(ds: Dataset) -> str:
    n = len(ds.obs)
    own = f"{n} stint{'s' if n != 1 else ''}"
    if not ds.extra:
        return own
    tracks = len(ds.transfer_from)
    return f"{own} + {len(ds.extra)} at {tracks} other track{'s' if tracks != 1 else ''}"


def _model_reason(ds: Dataset, d: dict[str, tuple[float, float]], bal: dict[str, tuple[float, float]] | None = None,
                  balance: Balance | None = None) -> str:
    parts = [_phrase(k, *d[k]) for k in OBJECTIVES]
    reason = f"Model of {_stints_phrase(ds)}: {parts[0]} (±{d['pace'][1]:.2f} s), {parts[1]}, {parts[2]}"
    rotation = _rotation_phrase(bal or {}, balance)
    return f"{reason}; {rotation}" if rotation else reason


def _rotation_phrase(bal: dict[str, tuple[float, float]], balance: Balance | None) -> str:
    """'more rotation on entry' for phases the move clearly changes (half a rating point)."""
    if balance is None:
        return ""
    where = {f"{sp}_{ph}": f"{s} {p}" for sp, s in (("slow", "in low-speed"), ("medium", "in mid-speed"),
                                                    ("fast", "in high-speed"))
             for ph, p in (("entry", "corner entries"), ("mid", "mid-corner"), ("exit", "corner exits"))}
    out = []
    for ph, (mean, sd) in bal.items():
        pp = balance.targets[ph].per_point
        if abs(mean) >= 0.5 * pp and abs(mean) > sd:
            out.append(f"{'more' if mean < 0 else 'less'} rotation {where[ph]} ({mean:+.2f}°)")
    return ", ".join(out)


def _learning_reason(ds: Dataset, d: dict[str, tuple[float, float]]) -> str:
    return (f"Learning ({_stints_phrase(ds)} so far): changed to measure what this setting does; "
            f"expected within ±{2 * d['pace'][1]:.2f} s")


def _effect_json(e: dict, scales: CarScales) -> dict:
    keys = e["keys"]
    if e.get("dim") == "VM_CASTER":
        name = "Caster"
    elif e.get("dim", "").startswith("WM_CAMBER"):
        name = f"Camber {'Front' if e['dim'].endswith(':F') else 'Rear'}"
    else:
        name = analysis.pretty_name(keys[0]) + ("/" + "/".join(k[-2:] for k in keys[1:]) if len(keys) > 1 else "")
    return {"name": name, "keys": keys, "unit": UNITS, **{n: list(e["delta"][n]) for n in OBJECTIVES},
            "balance": {ph: list(v) for ph, v in e.get("balance", {}).items()}}
