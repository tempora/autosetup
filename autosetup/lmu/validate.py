"""Check the VM_* ↔ .svm mapping against a real install.

For each recording, find the saved .svm that best matches its CarSetup (searching every
Settings folder, because folders aren't named after TrackName: 'Fuji Speedway' → 'Fuji').
A setting matches when both index and display text agree; text equality is an
independent check that both names refer to the same setting. Over well-matched pairs,
"same text but different index" flags a wrong table entry or an index offset.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from autosetup.lmu.mapping import NOT_IN_SVM, VM_TO_SVM
from autosetup.lmu.setup_space import CarSetup
from autosetup.lmu.svm import SvmFile, best_match, read_settings
from autosetup.lmu.telemetry import Session, list_recordings


@dataclass
class KeyStats:
    compared: int = 0
    index_equal: int = 0
    text_equal: int = 0
    both_equal: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def offset_suspects(self) -> int:
        """Pairs where the label matched but the index didn't."""
        return self.text_equal - self.both_equal


@dataclass
class Match:
    recording: Path
    svm: Path
    score: float
    track: str
    car_name: str
    signature: str
    vehicle_class: str


@dataclass
class MappingReport:
    matches: list[Match]
    keys: dict[str, KeyStats]
    unmapped_setup_keys: set[str]
    missing_in_svm: set[str]

    def suspects(self) -> dict[str, KeyStats]:
        return {k: s for k, s in self.keys.items() if s.offset_suspects}


def best_matches(telemetry_dir: Path, settings_dir: Path, min_score: float = 0.85) -> list[tuple[Match, CarSetup, SvmFile]]:
    """Every recording in ``telemetry_dir``, read from its file, against the saved setups."""
    def recordings():
        for rec in list_recordings(telemetry_dir):
            try:
                with Session.open(rec) as s:
                    yield rec, CarSetup.from_json(s.setup_json), s.track, s.car_name
            except Exception:
                continue

    return match_recordings(recordings(), read_settings(settings_dir), min_score)


def match_recordings(recordings: Iterable[tuple[Path, CarSetup, str, str]], svms: dict[Path, SvmFile],
                     min_score: float = 0.85) -> list[tuple[Match, CarSetup, SvmFile]]:
    """(recording, its setup, track, car name) → the saved setup each one matches best."""
    out = []
    for rec, setup, track, car_name in recordings:
        if not setup.params:
            continue
        score, best = best_match(setup, svms, min_score)
        if best is not None:
            svm = svms[best]
            out.append((Match(rec, best, score, track, car_name, setup.signature, svm.vehicle_class), setup, svm))
    return out


def validate(telemetry_dir: Path, settings_dir: Path, min_score: float = 0.85) -> MappingReport:
    stats: dict[str, KeyStats] = collections.defaultdict(KeyStats)
    unmapped: set[str] = set()
    missing: set[str] = set()
    matches = []
    for match, setup, svm in best_matches(telemetry_dir, settings_dir, min_score):
        matches.append(match)
        unmapped |= {k for k in setup.params if k not in VM_TO_SVM and k not in NOT_IN_SVM}
        idx, labels = svm.vm_values(), svm.vm_labels()
        for p in setup:
            if p.key not in VM_TO_SVM:
                continue
            if p.key not in idx:
                if p.available:
                    missing.add(p.key)
                continue
            st = stats[p.key]
            ie = idx[p.key] == p.value
            te = labels.get(p.key, "").strip() == p.text.strip()
            st.compared += 1
            st.index_equal += ie
            st.text_equal += te
            st.both_equal += ie and te
            if ie != te and len(st.examples) < 3:
                st.examples.append(f"svm {idx[p.key]}//{labels.get(p.key)!r} vs setup {p.value} {p.text!r}")
    return MappingReport(matches, dict(stats), unmapped, missing)
