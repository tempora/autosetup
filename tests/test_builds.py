from datetime import datetime, timezone

from autosetup.lmu.builds import current_build, current_filter, installed_build, note_build
from autosetup.lmu.paths import LmuPaths
from autosetup.store import Store

MANIFEST = '''"AppState"
{
\t"appid"\t\t"2399420"
\t"buildid"\t\t"%s"
\t"LastUpdated"\t\t"%d"
}
'''


def _install(tmp_path, build: str, updated: datetime) -> LmuPaths:
    root = tmp_path / "steamapps" / "common" / "Le Mans Ultimate"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "steamapps" / "appmanifest_2399420.acf").write_text(MANIFEST % (build, updated.timestamp()))
    return LmuPaths(root)


def _rec(store: Store, name: str, recorded_at: str) -> None:
    store.con.execute("insert into recordings (file_name, file_size, file_mtime, recorded_at) values (?, 1, 0, ?)",
                      [name, recorded_at])


def test_installed_build_from_steam_manifest(tmp_path):
    paths = _install(tmp_path, "25661166", datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert installed_build(paths) == ("25661166", datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp())
    assert installed_build(LmuPaths(tmp_path / "nowhere")) is None


def test_recordings_are_tagged_with_the_build_they_were_driven_on(tmp_path):
    store = Store(tmp_path / "db.duckdb")
    _rec(store, "old.duckdb", "2026-08-08T11_49_09Z")
    _rec(store, "new.duckdb", "2026-10-22T14_55_11Z")
    assert note_build(store, None) is None  # no build known: everything counts
    assert current_filter(store) == ("true", [])

    paths = _install(tmp_path, "100", datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert note_build(store, paths) == "100"
    tags = dict(store.con.execute("select file_name, game_build from recordings").fetchall())
    assert tags == {"old.duckdb": None, "new.duckdb": "100"}
    cond, params = current_filter(store)
    kept = store.con.execute(f"select file_name from recordings r where {cond}", params).fetchall()
    assert kept == [("new.duckdb",)]

    # The next update: the build before it is remembered, today's recordings move on.
    _rec(store, "newer.duckdb", "2026-11-02T10_00_00Z")
    paths = _install(tmp_path, "200", datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert note_build(store, paths) == "200" == current_build(store)
    tags = dict(store.con.execute("select file_name, game_build from recordings").fetchall())
    assert tags == {"old.duckdb": None, "new.duckdb": "100", "newer.duckdb": "200"}
