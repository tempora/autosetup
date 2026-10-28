"""Dashboard server: ``autosetup serve``.

Standard-library HTTP server (same shape as the qwen_image phone server). It binds
0.0.0.0 so a phone on the LAN can open it, serves ``web/index.html`` and a small JSON API
(see UI_LAYOUT_CONTRACT.md §14), and runs a watcher thread that imports recordings once
the game has finished writing them.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autosetup import __version__, analysis, driver, screens, trackmap, units
from autosetup.live import LiveFeed
from autosetup.lmu.builds import note_build
from autosetup.lmu.paths import LmuPaths, game_running
from autosetup.lmu.telemetry import RecordingBusy, Session, in_progress
from autosetup.metrics import METRICS_VERSION
from autosetup.feel import fingerprint as fpm
from autosetup.knowledge import load_knowledge
from autosetup.opt.dampers import histograms
from autosetup.proposals import Advisor, ProposalConflict
from autosetup.setup_writer import WriteError
from autosetup.store import Store, read_recording

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_PORT = 8191
# A recording must be unchanged this long before it's imported. LMU deletes the .wal only
# after merging it, so this is a margin, not the main guard; a torn copy is retried.
SETTLE_S = 3.0


SLOW_S = 1.0  # requests slower than this are written to the log
# Recordings per watcher round, newest first. A first start can face gigabytes of old
# recordings: in small rounds the newest outings show up first, the links are learned and
# the page refreshes between rounds, and an outing finished meanwhile jumps the queue.
IMPORT_BATCH = 8
# Recordings read at once. Reading is mostly numpy holding the GIL, so threads give about
# 2x and more of them don't help; one while the game runs, to leave the CPU to it.
IMPORT_WORKERS = min(4, os.cpu_count() or 1)


def log(message: str) -> None:
    """One line in the server log (stdout; the desktop app sends it to server.log)."""
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def _stamp(path: Path) -> tuple[int, float]:
    st = path.stat()
    return st.st_size, st.st_mtime


class App:
    """Shared state: the store (each thread gets its own DuckDB cursor) and a lock that
    keeps a request from reading half an import, plus the watcher's status."""

    def __init__(self, store: Store, paths: LmuPaths | None):
        self.store = store
        self.paths = paths
        self.advisor = Advisor(store, paths)
        self.live = LiveFeed()
        self._traces: dict[tuple, dict] = {}  # (stint, seq, file size) -> lap json, small LRU
        self.lock = threading.RLock()
        self.status = {"importing": False, "last_import": None, "last_error": None, "imported": 0, "waiting": [],
                       "recomputing": False, "recomputed": None}
        self._seen: dict[str, tuple[int, float]] = {}  # name -> (size, first time seen at that size)
        self._unusable: dict[str, tuple[int, float]] = {}  # name -> (size, mtime) that gave nothing or failed
        self._driving: set[str] = set()  # recordings seen with a .wal (outing running)
        self._running: tuple[bool, float] | None = None  # game_running answer and when
        self.game_running = game_running  # injectable for tests
        self._stop = threading.Event()
        with self.lock:
            units.set_current(units.Units.from_json(store.get_setting("units")))
            note_build(store, paths)

    # -- watcher ---------------------------------------------------------------------
    def start_watcher(self, interval: float = 1.0) -> threading.Thread:
        t = threading.Thread(target=self._watch, args=(interval,), daemon=True, name="telemetry-watcher")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self.live.stop()

    def _watch(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:  # keep watching; surface the error in /api/state
                self.status["last_error"] = f"{type(e).__name__}: {e}"
            self._stop.wait(interval)

    def poll(self, now: float | None = None) -> int:
        """Import recordings whose size has been stable for SETTLE_S, at most IMPORT_BATCH
        of them, newest first. Returns the count."""
        if self.paths is None:
            return 0
        now = time.time() if now is None else now
        ready = []
        with self.lock:  # one query, not one per file: the folder can hold thousands
            imported = set(self.store.con.execute("select file_name, file_size from recordings").fetchall())
        for f in sorted(self.paths.telemetry.glob("*.duckdb"), key=lambda p: p.stat().st_mtime, reverse=True):
            size = f.stat().st_size
            seen = self._seen.get(f.name)
            if in_progress(f):
                self._driving.add(f.name)
            elif f.name in self._driving:
                # Its .wal just went: LMU deletes it only after merging and letting go, so
                # the outing is finished; no need to wait for the size to settle.
                self._driving.discard(f.name)
                self._seen[f.name] = (size, now)
                ready.append(f)
                continue
            if seen is None or seen[0] != size:
                self._seen[f.name] = (size, now)
                continue
            if now - seen[1] >= SETTLE_S and (in_progress(f) or (f.name, size) not in imported):
                if self._unusable.get(f.name) != _stamp(f):  # else it would hold a place in every round
                    ready.append(f)  # in-progress files are reported as waiting
        if not ready:
            self.status["waiting"] = []
            self.status["last_error"] = None
            return 0
        return self.import_files(ready[:IMPORT_BATCH])

    def import_files(self, files: list[Path]) -> int:
        """Import finished stints. Files LMU is still writing (locked, or the car hasn't
        come back to the pits yet) are listed in status["waiting"] and retried on the next
        poll; they're not errors. Errors from an earlier round clear once a round is clean.
        Files are read in parallel (the slow part, without the lock: the app stays
        responsive) and saved in the order given."""
        self.status["importing"] = True
        n = 0
        waiting, errors, new = [], [], []
        running = self._game_running() if len(files) > 1 or any(in_progress(f) for f in files) else False
        t0 = time.perf_counter()

        def failed(f: Path, stamp, e: Exception) -> None:  # one bad file mustn't block the rest, nor be retried every poll
            self._unusable[f.name] = stamp
            errors.append(f"{f.name}: {type(e).__name__}: {e}")
            log(f"import failed: {errors[-1]}")

        try:
            todo = []
            for f in files:
                stamp = None
                try:
                    stamp = _stamp(f)
                    if self._unusable.get(f.name) == stamp:
                        continue  # no usable data or failed before, and unchanged since
                    if running and in_progress(f):
                        raise RecordingBusy(f"{f.name}: outing still running")
                    with self.lock:
                        if self.store.has_recording(f):
                            continue
                    todo.append((f, stamp))
                except RecordingBusy:
                    waiting.append(f.name)
                except Exception as e:
                    failed(f, stamp, e)
            workers = 1 if running else IMPORT_WORKERS
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo))), thread_name_prefix="import") as ex:
                reads = [(f, stamp, ex.submit(read_recording, f)) for f, stamp in todo]
                for f, stamp, fut in reads:
                    try:
                        rec = fut.result()
                        if rec is None:
                            self._unusable[f.name] = stamp
                            continue
                        with self.lock:
                            self.store.save_recording(rec)
                        new.append(f.name)
                        n += 1
                    except RecordingBusy:
                        waiting.append(f.name)
                    except Exception as e:
                        failed(f, stamp, e)
            if n and self.paths:
                with self.lock:
                    self.store.learn_links(self.paths.telemetry, self.paths.settings, only=new)
                    note_build(self.store, self.paths)
            if n:
                log(f"imported {n} recording{'s' if n != 1 else ''} in {time.perf_counter() - t0:.1f} s")
        finally:
            self.status["importing"] = False
            self.status["waiting"] = waiting
            self.status["last_error"] = errors[-1] if errors else None
        if n:
            self.status["last_import"] = time.time()
            self.status["imported"] += n
        return n

    def _game_running(self) -> bool:
        """``game_running`` (a tasklist call, ~0.1 s), asked at most every 10 s: a paused
        outing keeps its .wal while the watcher polls every second."""
        now = time.monotonic()
        if self._running is None or now - self._running[1] > 10.0:
            self._running = (self.game_running(), now)
        return self._running[0]

    def import_now(self) -> int:
        if self.paths is None:
            return 0
        with self.lock:
            files = [f for f in self.paths.telemetry.glob("*.duckdb") if not self.store.has_recording(f)]
        return self.import_files(sorted(files, key=lambda p: p.stat().st_mtime, reverse=True))

    def recompute(self, force: bool = True) -> bool:
        """Recompute lap metrics in the background: every recording, or with ``force``
        off only those from an older METRICS_VERSION (at startup after an update). The
        app keeps answering meanwhile. False if one is already running."""
        if self.paths is None:
            raise ValueError("LMU install not found")
        if self.status["recomputing"]:
            return False
        self.status["recomputing"] = True

        def run() -> None:
            try:
                t0 = time.perf_counter()
                done, _ = self.store.recompute_metrics(
                    self.paths.telemetry, force=force, lock=self.lock,
                    workers=1 if self._game_running() else IMPORT_WORKERS)
                self.status["recomputed"] = done
                if done:
                    log(f"recomputed metrics of {done} recording{'s' if done != 1 else ''} in {time.perf_counter() - t0:.0f} s")
            except Exception as e:
                self.status["last_error"] = f"{type(e).__name__}: {e}"
            finally:
                self.status["recomputing"] = False

        threading.Thread(target=run, daemon=True, name="recompute").start()
        return True

    # -- menu screens ------------------------------------------------------------------
    def folders(self) -> dict[str, Path | None]:
        p = self.paths
        return {"install": p.root if p else None, "telemetry": p.telemetry if p else None,
                "settings": p.settings if p else None, "database": self.store.path}

    def settings(self) -> dict:
        with self.lock:
            classes = self.store.con.execute(
                """select r.car_class, count(g.file_name) from recordings r left join ratings g using (file_name)
                   where r.car_class is not null and exists (
                       select 1 from laps l where l.recording_id = r.id and l.complete and not l.in_pits)
                   group by r.car_class order by max(r.file_mtime) desc""").fetchall()
            fps = fpm.active(self.store)
        return {
            "units": units.current.to_json(), "unit_choices": {k: list(v) for k, v in units.CHOICES.items()},
            "folders": [{"which": k, "path": str(v) if v else None} for k, v in self.folders().items()],
            "classes": [{"car_class": c} for c, _ in classes],
            "fingerprints": [{"car_key": f.car_key, "car_class": f.car_class, "layout": f.layout,
                              "created_at": f.created_at} for f in fps],
            "recomputing": self.status["recomputing"], "recomputed": self.status["recomputed"],
        }

    def put_settings(self, body: dict) -> dict:
        with self.lock:
            if "units" in body:
                u = units.Units.from_json({**units.current.to_json(), **(body["units"] or {})})
                self.store.put_setting("units", u.to_json())
                units.set_current(u)
        return self.settings()

    def open_folder(self, which: str) -> str:
        path = self.folders().get(which)
        if path is None:
            raise ValueError(f"unknown folder {which!r}")
        folder = path.parent if path.is_file() else path
        if not folder.exists():
            raise ValueError(f"{folder} doesn't exist")
        if hasattr(os, "startfile"):
            os.startfile(folder)  # noqa: S606 - a fixed folder of ours, not user input
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        return str(folder)

    def about(self) -> dict:
        with self.lock:
            recordings, laps = self.store.con.execute(
                "select (select count(*) from recordings), (select count(*) from laps where complete)").fetchone()
        return {"version": __version__, "metrics_version": METRICS_VERSION, "database": str(self.store.path),
                "recordings": recordings, "laps": laps}

    def driver(self) -> dict:
        with self.lock:
            return driver.profile(self.store)

    # -- laps ------------------------------------------------------------------------
    def lap(self, rid: int, seq: int) -> dict:
        """One lap's trace (5 m grid in the track map's frame), sector times, metrics and
        clean-lap status. Traces are read from the recording and cached."""
        with self.lock:
            rec = self.store.recording(rid)
            layout = rec["track_layout"] or rec["track"]
            tmap = trackmap.track_map(self.store, layout, self.paths.telemetry if self.paths else None)
            laps = {lap["seq"]: lap for lap in analysis._laps(self.store, rid)}
            metrics = dict(self.store.con.execute(
                "select name, value from lap_metrics where recording_id = ? and seq = ?", [rid, seq]).fetchall())
            svm = self.store.con.execute(
                "select svm_path from recording_setups where file_name = ?", [rec["file_name"]]).fetchone()
        if seq not in laps:
            raise KeyError(f"stint {rid} has no lap {seq}")
        if self.paths is None:
            raise ValueError("LMU install not found")
        f = self.paths.telemetry / rec["file_name"]
        key = (rid, seq, f.stat().st_size if f.exists() else 0)
        trace = self._traces.get(key)
        if trace is None:
            with Session.open(f) as s:
                session_laps = s.laps()
                if seq >= len(session_laps):
                    raise KeyError(f"stint {rid} has no lap {seq}")
                trace = trackmap.lap_trace(s, session_laps[seq], tuple(tmap["origin"]) if tmap else None,
                                           tmap["length"] if tmap else None, tmap["sectors"] if tmap else None)
            self._traces[key] = trace
            while len(self._traces) > 48:
                self._traces.pop(next(iter(self._traces)))
        info = laps[seq]
        name = svm[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".svm") if svm else None
        return {"stint": rid, "seq": seq, "lap": info["lap"], "time": info["time"], "clean": info["clean"],
                "off_track": info["off_track"], "setup_name": name, "metrics": metrics, "trace": trace}

    def open_session(self) -> dict:
        """The session open in LMU and the car + layout context it belongs to (None while
        its first outing hasn't been imported yet)."""
        snap = self.live.snapshot()
        out = {"state": snap.get("state", "off"), "vehicle": None, "track": None, "session": None, "context": None}
        car = snap.get("car") or {}
        if out["state"] != "live":
            return out
        out.update(vehicle=car.get("vehicle"), track=car.get("track"), session=car.get("session"),
                   in_garage=car.get("in_garage"), laps_completed=car.get("laps_completed"), best_lap=car.get("best_lap"))
        with self.lock:
            for c in analysis.contexts(self.store):
                if c.car_name == car.get("vehicle") and car.get("track") in (c.track, c.layout):
                    out["context"] = c.key
                    break
            latest = screens.latest_session(self.store, out["context"]) if out["context"] else None
        out["session"] = latest["id"] if latest else None
        return out

    def setup_diff(self, a: int, b: int) -> list[dict]:
        """What changed in the setup going from stint b to stint a."""
        with self.lock:
            return analysis.setup_diff(self.store.setup_of(b), self.store.setup_of(a))

    # -- API -------------------------------------------------------------------------
    def context_key(self, requested: str | None) -> str | None:
        with self.lock:
            ctxs = analysis.contexts(self.store)
            keys = [c.key for c in ctxs]
            for key in (requested, self.store.get_setting("context")):
                if key in keys:
                    return key
            return keys[0] if keys else None

    def state(self) -> dict:
        with self.lock:
            ctxs = analysis.contexts(self.store)
        return {
            "contexts": [dict(key=c.key, car_name=c.car_name, car_class=c.car_class, track=c.track,
                              layout=c.layout, stints=c.stints, last_recorded=c.last_recorded) for c in ctxs],
            "context": self.context_key(None),
            "objectives": [dict(key=k, label=label, unit=unit) for k, label, unit in analysis.OBJECTIVES],
            "watcher": dict(self.status, watching=self.paths is not None),
        }

    def stints(self, key: str | None, session: int | None = None) -> dict:
        """Stints of a car + track; with ``session``, only that session's."""
        with self.lock:
            if session is not None:
                sess = screens.session(self.store, session)
                key = sess["context"]
            else:
                key = self.context_key(key)
            if key is None:
                return {"context": None, "stints": []}
            car_name, layout = key.split("|", 1)
            items = analysis.stints(self.store, car_name, layout)
        if session is not None:
            items = [st for st in items if st.id in set(sess["stint_ids"])]
        return {"context": key, "session": session, "stints": [st.to_json() for st in items]}

    def table(self, session: int, by: str) -> dict:
        with self.lock:
            sess = screens.session(self.store, session)
            return {"session": session, **screens.setup_table(self.store, sess["stint_ids"], by)}

    def pareto(self, key: str | None) -> dict:
        data = self.stints(key)
        points = [dict(id=st["id"], pareto=st["pareto"], **{k: st["summary"].get(k) for k, _, _ in analysis.OBJECTIVES})
                  for st in data["stints"]]
        return {"context": data["context"], "points": points}


class Handler(BaseHTTPRequestHandler):
    app: App  # set by serve()
    server_version = f"autosetup/{__version__}"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        dt = time.perf_counter() - getattr(self, "_t0", time.perf_counter())
        if dt > SLOW_S:
            log(f"slow request: {self.command} {self.path} took {dt:.1f} s")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        data = json.loads(self.rfile.read(n))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def do_GET(self):
        self._t0 = time.perf_counter()
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                body = (WEB_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/health":
                self._json({"ok": True})
            elif url.path == "/api/state":
                self._json(self.app.state())
            elif url.path == "/api/stints":
                self._json(self.app.stints(q.get("context"), int(q["session"]) if q.get("session") else None))
            elif url.path == "/api/pareto":
                self._json(self.app.pareto(q.get("context")))
            elif url.path == "/api/sessions":
                with self.app.lock:
                    self._json({"sessions": screens.sessions(self.app.store)})
            elif url.path == "/api/welcome":
                with self.app.lock:
                    self._json({"fact": screens.fun_fact(self.app.store)})
            elif url.path == "/api/table":
                self._json(self.app.table(int(q["session"]), q.get("by", "stint")))
            elif url.path == "/api/live":
                self._json(self.app.open_session())
            elif url.path == "/api/trackmap":
                key = q.get("context")
                layout = q.get("layout") or (self.app.context_key(key) or "|").split("|", 1)[1]
                with self.app.lock:
                    data = trackmap.track_map(self.app.store, layout, self.app.paths.telemetry if self.app.paths else None)
                    ref = None
                    ctx = self.app.context_key(key)
                    if q.get("car"):  # live: the car actually on track
                        ref = trackmap.best_clean_lap(self.app.store, q["car"], layout)
                    elif ctx and ctx.split("|", 1)[1] == layout:
                        ref = trackmap.best_clean_lap(self.app.store, *ctx.split("|", 1))
                self._json({"layout": layout, "map": data, "reference": ref})
            elif url.path == "/api/lap":
                self._json(self.app.lap(int(q["stint"]), int(q["seq"])))
            elif url.path == "/api/setup-diff":
                self._json({"changes": self.app.setup_diff(int(q["a"]), int(q["b"]))})
            elif url.path == "/api/proposal":
                with self.app.lock:
                    self._json(self.app.advisor.proposal_json(int(q["stint"])))
            elif url.path == "/api/target":
                with self.app.lock:
                    self._json(self.app.advisor.target_json(int(q["stint"])))
            elif url.path == "/api/histograms":
                kerbs = {"on": True, "off": False, "both": None}[q.get("kerbs", "off")]
                with self.app.lock:
                    rec = self.app.store.recording(int(q["stint"]))
                    self._json(histograms(self.app.store, int(q["stint"]), kerbs, float(q.get("bin", 25)),
                                          load_knowledge(rec["car_class"])))
            elif url.path == "/api/driver":
                self._json(self.app.driver())
            elif url.path == "/api/settings":
                self._json(self.app.settings())
            elif url.path == "/api/about":
                self._json(self.app.about())
            else:
                self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request: {e}"}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        self._t0 = time.perf_counter()
        try:
            body = self._body()
            app = self.app
            if self.path == "/api/context":
                key = app.context_key(body.get("context"))
                if key != body.get("context"):
                    return self._json({"error": "unknown context"}, 400)
                with app.lock:
                    app.store.put_setting("context", key)
                self._json({"context": key})
            elif self.path == "/api/feedback":
                # {stint, button: pointier|lazier|looser|safer} | {stint, undo: true} | {stint, reset: true}
                stint = int(body["stint"])
                with app.lock:
                    if body.get("undo"):
                        self._json(app.advisor.undo(stint))
                    elif body.get("reset"):
                        self._json(app.advisor.reset(stint))
                    else:
                        self._json(app.advisor.press(stint, str(body["button"])))
            elif self.path == "/api/lock":
                with app.lock:
                    self._json(app.advisor.lock(int(body["stint"])))
            elif self.path == "/api/forget":
                with app.lock:
                    self._json({"forgotten": fpm.forget(app.store, str(body["car_key"]))})
            elif self.path == "/api/rating":
                # {file_name, lockups?, note?}: only the fields in the request change.
                name = str(body["file_name"])
                with app.lock:
                    r = {k: v for k, v in (app.store.rating(name) or {}).items() if k in ("note", "lockups")}
                    if "lockups" in body:
                        r["lockups"] = bool(body["lockups"])
                    if "note" in body:
                        r["note"] = body["note"]
                    app.store.set_rating(name, **r)
                    self._json({"rating": app.store.rating(name)})
            elif self.path == "/api/import":
                self._json({"imported": app.import_now()})
            elif self.path == "/api/settings":
                self._json(app.put_settings(body))
            elif self.path == "/api/recompute":
                self._json({"started": app.recompute()})
            elif self.path == "/api/open-folder":
                self._json({"opened": app.open_folder(str(body.get("which")))})
            elif self.path == "/api/propose":
                with app.lock:
                    self._json(app.advisor.proposal_json(int(body["stint"])))
            elif self.path == "/api/write":
                with app.lock:
                    self._json(app.advisor.write(int(body["stint"]), str(body["signature"])))
            elif self.path == "/api/locks":
                keys = body["keys"]
                if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                    raise ValueError("keys must be a list of setting names")
                with app.lock:
                    context = analysis.context_of(app.store, int(body["stint"]))
                    locks = app.advisor.set_locks(context, keys, bool(body.get("locked", True)))
                self._json({"context": context, "locks": sorted(locks)})
            else:
                self._json({"error": "not found"}, 404)
        except ProposalConflict as e:
            self._json({"error": str(e)}, 409)
        except (KeyError, ValueError, TypeError, WriteError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def make_server(app: App, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def serve(store: Store, paths: LmuPaths | None, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    app = App(store, paths)
    if paths is not None:
        app.recompute(force=False)  # after an update that changed the metrics: in the background
    app.start_watcher()
    app.live.start()
    httpd = make_server(app, host, port)
    print(f"autosetup dashboard: http://127.0.0.1:{port}  (phone: http://{lan_ip()}:{port})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        httpd.server_close()
