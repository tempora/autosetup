"""Which game build a recording was driven on.

Recordings don't say: their ``Version`` metadata is the file format and the channel list
is the same across updates. Steam's app manifest names the installed build and when it
was installed, so every build seen is remembered (``game_builds``) and a recording belongs
to the newest build installed before it was recorded. Only the current build's recordings
are learned from: an update can change the physics, the tyres and the setup ranges.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from autosetup.lmu.paths import LMU_APP_ID, LmuPaths

SCHEMA = """
create table if not exists game_builds (
    buildid     varchar primary key,
    updated_at  double                -- unix time Steam installed it
);
alter table recordings add column if not exists game_build varchar;
"""
RECORDING_TIME = "%Y-%m-%dT%H_%M_%SZ"


def manifest_of(paths: LmuPaths) -> Path:
    """<library>/steamapps/appmanifest_<id>.acf, next to steamapps/common/<LMU>."""
    return paths.root.parent.parent / f"appmanifest_{LMU_APP_ID}.acf"


def installed_build(paths: LmuPaths) -> tuple[str, float] | None:
    """(buildid, unix time it was installed) from Steam's manifest, or None."""
    f = manifest_of(paths)
    if not f.is_file():
        return None
    text = f.read_text(encoding="utf-8", errors="replace")
    build = re.search(r'"buildid"\s+"(\d+)"', text)
    updated = re.search(r'"LastUpdated"\s+"(\d+)"', text)
    if not (build and updated):
        return None
    return build.group(1), float(updated.group(1))


def recorded_unix(recorded_at: str | None) -> float | None:
    try:
        return datetime.strptime(recorded_at or "", RECORDING_TIME).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def note_build(store, paths: LmuPaths | None) -> str | None:
    """Remember the installed build and tag every recording with its build. Returns the
    current build (the newest known), or None when no build is known (no Steam manifest:
    then every recording counts)."""
    store.con.execute(SCHEMA)
    found = installed_build(paths) if paths is not None else None
    if found:
        store.con.execute("insert or ignore into game_builds values (?, ?)", list(found))
    builds = store.con.execute("select buildid, updated_at from game_builds order by updated_at").fetchall()
    if not builds:
        return None
    rows = store.con.execute("select id, recorded_at, file_mtime from recordings").fetchall()
    updates = []
    for rid, recorded_at, mtime in rows:
        t = recorded_unix(recorded_at) or mtime
        tag = None
        for buildid, updated_at in builds:
            if t >= updated_at:
                tag = buildid
        updates.append((rid, tag))
    if updates:
        store.con.execute(
            "update recordings set game_build = u.b from (select unnest(?::INTEGER[]) as id, "
            "unnest(?::VARCHAR[]) as b) u where recordings.id = u.id",
            [[u[0] for u in updates], [u[1] for u in updates]])
    return builds[-1][0]


def current_build(store) -> str | None:
    store.con.execute(SCHEMA)
    row = store.con.execute("select buildid from game_builds order by updated_at desc limit 1").fetchone()
    return row[0] if row else None


def current_filter(store, alias: str = "r") -> tuple[str, list]:
    """SQL condition (and its parameters) keeping only the current build's recordings;
    always true when no build is known."""
    build = current_build(store)
    if build is None:
        return "true", []
    return f"{alias}.game_build = ?", [build]
