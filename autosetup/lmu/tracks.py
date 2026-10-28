"""Resolve a telemetry TrackName to the Settings folder the garage reads setups from.

The folder names come from the track's own data, not from TrackName ('Circuit de la
Sarthe' → 'Lemans', 'Lusail International Circuit' → 'Qatar'). The store learns the
pairs from matched history; this module is the fallback for tracks with no history.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Normalised TrackName fragment → normalised folder name.
_ALIASES = {
    "sarthe": "lemans",
    "lusail": "qatar",
    "josecarlospace": "interlagos",
    "enzoedinoferrari": "imola",
    "algarve": "portimao",
    "catalunya": "circuitdebarcelona",
    "barcelona": "circuitdebarcelona",
    "paulricard": "paulricard",
    "circuitoftheamericas": "circuitoftheamericas",
    "spafrancorchamps": "spa",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def guess_folder(track_name: str, settings_dir: Path) -> Path | None:
    """Best existing Settings folder for ``track_name``, or None when nothing fits."""
    if not settings_dir.is_dir():
        return None
    folders = {_norm(f.name): f for f in settings_dir.iterdir() if f.is_dir()}
    name = _norm(track_name)
    if name in folders:
        return folders[name]
    for fragment, folder in _ALIASES.items():
        if fragment in name and folder in folders:
            return folders[folder]
    # A folder whose name is a prefix of the track name ('Fuji' for 'Fuji Speedway');
    # the longest one wins ('Silverstonewec' isn't a prefix of 'silverstonecircuit').
    candidates = [k for k in folders if k and name.startswith(k)]
    return folders[max(candidates, key=len)] if candidates else None
