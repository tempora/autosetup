"""Command line: ``python -m autosetup <command>`` (or ``autosetup`` once installed)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autosetup.lmu.paths import find_install
from autosetup.store import Store


def _fmt_time(t: float | None) -> str:
    if t is None:
        return "-"
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:06.3f}"


def cmd_info(args, store: Store) -> int:
    paths = find_install()
    print(f"LMU install : {paths.root}")
    print(f"Telemetry   : {paths.telemetry}")
    print(f"Setups      : {paths.settings}")
    print(f"Database    : {store.path}")
    return 0


def cmd_import(args, store: Store) -> int:
    paths = find_install()
    added, errors, busy = store.import_all(paths.telemetry)
    print(f"Imported {len(added)} new recording(s).")
    for f in busy:
        print(f"  waiting for {f.name}: LMU is still writing it; it's imported once the stint ends")
    for f, err in errors:
        print(f"  skipped {f.name}: {err}", file=sys.stderr)
    n = store.learn_links(paths.telemetry, paths.settings)
    cars = store.con.execute("select count(*) from car_templates").fetchone()[0]
    tracks = store.con.execute("select count(*) from track_folders").fetchone()[0]
    print(f"Matched {n} recording(s) to saved setups: templates for {cars} car(s), folders for {tracks} track(s).")
    return 0


def cmd_list(args, store: Store) -> int:
    rows = store.recordings()
    if not rows:
        print("No recordings imported yet; run `autosetup import`.")
        return 0
    print(f"{'id':>3}  {'type':<11} {'class':<6} {'car':<32} {'track':<32} {'dur':>6} {'laps':>4}  best")
    for r in rows:
        print(f"{r['id']:>3}  {r['session_type'][:11]:<11} {r['car_class'][:6]:<6} {r['car_name'][:32]:<32} "
              f"{r['track'][:32]:<32} {r['duration']:>6.0f} {r['clean_laps']:>4}  {_fmt_time(r['best_lap'])}")
    return 0


def cmd_show(args, store: Store) -> int:
    rec = store.recording(args.id)
    setup = store.setup_of(args.id)
    print(f"{rec['car_name']} ({rec['car_class']}) at {rec['track']}, {rec['session_type']}, {rec['file_name']}")
    print(f"car model {rec['car_signature']}, template: {store.template_for(rec['car_signature']) or 'none'}")
    print()
    for p in setup.adjustable():
        print(f"  {p.key:<34} {p.value:>4} / {p.min}..{p.max - 1:<4} {p.text}")
    laps = store.con.execute(
        "select seq, lap_number, lap_time, complete, in_pits from laps where recording_id = ? order by seq",
        [args.id],
    ).fetchall()
    print()
    for seq, num, t, complete, pits in laps:
        flag = "" if complete and not pits else ("  pit" if pits else "  partial")
        print(f"  lap {num:>3}  {_fmt_time(t)}{flag}")
    return 0


def cmd_validate(args, store: Store) -> int:
    from autosetup.lmu.validate import validate

    paths = find_install()
    report = validate(paths.telemetry, paths.settings, args.min_score)
    print(f"{len(report.matches)} recording(s) matched a saved setup at score >= {args.min_score}")
    for m in report.matches:
        print(f"  {m.score:.2f}  {m.recording.name[:48]:<48} -> {m.svm.parent.name}\\{m.svm.name}")
    if report.unmapped_setup_keys:
        print("Setup keys with no mapping:", ", ".join(sorted(report.unmapped_setup_keys)))
    if report.missing_in_svm:
        print("Mapped keys missing from matched files:", ", ".join(sorted(report.missing_in_svm)))
    suspects = report.suspects()
    for k, s in sorted(suspects.items()):
        print(f"  same label, different index: {k} ({s.offset_suspects}/{s.compared}) {s.examples}")
    print(f"{len(report.keys)} mapped settings compared; {len(suspects)} with index disagreements.")
    return 0


def cmd_write(args, store: Store) -> int:
    from autosetup.setup_writer import WriteError, parse_change, write_setup

    try:
        setup = store.setup_of(args.id)
        changes = dict(parse_change(spec, setup) for spec in args.changes)
        res = write_setup(store, find_install(), args.id, changes, args.template, args.dry_run)
    except WriteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    verb = "Would write" if args.dry_run else "Wrote"
    print(f"{verb} {res.path}")
    print(f"  template: {res.template}")
    for k, (a, b) in res.changes.items():
        print(f"  {k}: {a} -> {b}")
    return 0


def cmd_recompute(args, store: Store) -> int:
    done, missing = store.recompute_metrics(find_install().telemetry, force=args.all)
    print(f"Recomputed metrics for {done} recording(s).")
    if missing:
        print(f"  {len(missing)} recording file(s) no longer exist; kept their old metrics.")
    return 0


def _exit_with_stdin() -> None:
    """Exit as soon as stdin closes: the desktop shell holds the other end of the pipe, so
    the server never outlives its window, even if the shell crashes."""
    import os
    import threading

    def watch() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError, AttributeError):
            pass
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="stdin-watch").start()


def cmd_serve(args, store: Store) -> int:
    from autosetup.server import serve

    if args.exit_with_stdin:
        _exit_with_stdin()
    try:
        paths = find_install()
    except FileNotFoundError as e:
        print(f"warning: {e}; serving the database without watching for new telemetry", file=sys.stderr)
        paths = None
    serve(store, paths, args.host, args.port)  # recomputes outdated metrics in the background
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autosetup", description="Le Mans Ultimate setup optimiser")
    ap.add_argument("--db", type=Path, help="database file (default: %%LOCALAPPDATA%%\\autosetup\\autosetup.duckdb)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show install and database locations").set_defaults(fn=cmd_info)
    sub.add_parser("import", help="import new telemetry and learn car templates / track folders").set_defaults(fn=cmd_import)
    sub.add_parser("list", help="list imported recordings").set_defaults(fn=cmd_list)
    p = sub.add_parser("show", help="show a recording's setup and laps")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("validate-mapping", help="check telemetry ↔ .svm key mapping against saved setups")
    p.add_argument("--min-score", type=float, default=0.85)
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("write", help="write a new .svm from a recording's setup, with optional changes")
    p.add_argument("id", type=int, help="recording id (see `autosetup list`)")
    p.add_argument("changes", nargs="*", help="KEY=index, KEY+=steps or KEY-=steps, e.g. VM_REAR_ANTISWAY-=1")
    p.add_argument("--template", type=Path, help=".svm of the same car to take the header from")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_write)
    p = sub.add_parser("recompute", help="recompute lap metrics after a metrics update")
    p.add_argument("--all", action="store_true", help="recompute every recording, not just outdated ones")
    p.set_defaults(fn=cmd_recompute)
    p = sub.add_parser("serve", help="run the dashboard (and import new telemetry as it appears)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8191)
    p.add_argument("--exit-with-stdin", action="store_true", help="stop when stdin closes (used by the desktop app)")
    p.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    with Store(args.db) as store:
        return args.fn(args, store)
