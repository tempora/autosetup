"""Which session is open in LMU, from its local REST API (http://localhost:6397).

The dashboard follows the car and track you have open. ``/rest/watch/standings`` lists
every car in the session; the player's entry names the car (``vehicleName``, e.g.
"DKR Engineering #3:ELMS26" — the part before ':' is the car name recordings use) and
``/rest/watch/sessionInfo`` the track. Fields are read defensively: the endpoints aren't
documented. Only GET requests are made; nothing in the game is changed.
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request

BASE = "http://localhost:6397"
INTERVAL = 2.0


def _get(path: str, timeout: float = 0.8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "null")


def _num(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and math.isfinite(v):
            return float(v)
    return None


def _speed(entry: dict) -> float | None:
    v = entry.get("carVelocity")
    if isinstance(v, dict):
        if isinstance(v.get("velocity"), (int, float)):
            return float(v["velocity"])
        comps = [v.get(c) for c in ("x", "y", "z")]
        if all(isinstance(c, (int, float)) for c in comps):
            return math.sqrt(sum(c * c for c in comps))
    return _num(entry, "speed")


def parse_player(standings, session: dict | None) -> dict | None:
    """The player's car from a standings payload, normalised; None if not found."""
    entries = standings if isinstance(standings, list) else (standings or {}).get("standings") or []
    player = next((e for e in entries if isinstance(e, dict) and e.get("player")), None)
    if player is None:
        return None
    session = session if isinstance(session, dict) else {}
    pos = player.get("carPosition") if isinstance(player.get("carPosition"), dict) else {}
    sector = player.get("sector")
    sector_no = {"SECTOR1": 1, "SECTOR2": 2, "SECTOR3": 3}.get(str(sector).upper(), None)
    if sector_no is None and isinstance(sector, (int, float)):
        sector_no = 3 if int(sector) == 0 else int(sector)
    return {
        "lap_distance": _num(player, "lapDistance"),
        "time_into_lap": _num(player, "timeIntoLap"),
        "laps_completed": _num(player, "lapsCompleted"),
        "last_lap": _positive(_num(player, "lastLapTime")),
        "best_lap": _positive(_num(player, "bestLapTime")),
        "estimated_lap": _positive(_num(player, "estimatedLapTime")),
        "sector": sector_no,
        "in_pits": bool(player.get("pitting") or player.get("inGarageStall")),
        "in_garage": bool(player.get("inGarageStall")),
        "speed_kmh": (lambda v: v * 3.6 if v is not None else None)(_speed(player)),
        "position": {k: pos.get(k) for k in ("x", "y", "z")} if pos else None,
        "vehicle": str(player.get("vehicleName") or "").split(":")[0]
        or (f"{player.get('fullTeamName')} #{player.get('carNumber')}" if player.get("fullTeamName") else None),
        "track": session.get("trackName"),
        "session": session.get("session"),
        "track_length": _num(session, "lapDistance", "trackLength"),
    }


def _positive(v: float | None) -> float | None:
    return v if v is not None and v > 0 else None


class LiveFeed:
    """Background poller. ``snapshot()`` returns {"state": "off"|"menu"|"live", ...}."""

    def __init__(self, game_running=None, fetch=_get):
        from autosetup.lmu.paths import game_running as _running

        self.game_running = game_running or _running
        self.fetch = fetch
        self._state = {"state": "off", "updated": None}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running_checked = 0.0
        self._running = False

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True, name="lmu-live")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> dict:
        now = time.time()
        if now - self._running_checked > 10.0:
            self._running, self._running_checked = self.game_running(), now
        if not self._running:
            state = {"state": "off"}
        else:
            try:
                standings = self.fetch("/rest/watch/standings")
                try:
                    session = self.fetch("/rest/watch/sessionInfo")
                except (OSError, ValueError):
                    session = None
                car = parse_player(standings, session)
                state = {"state": "live", "car": car} if car else {"state": "menu"}
            except (OSError, ValueError, urllib.error.URLError):
                state = {"state": "menu"}
        state["updated"] = now
        with self._lock:
            self._state = state
        return state

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self.poll_once()
            except Exception:  # never let the poller die
                state = {"state": "off"}
            self._stop.wait(INTERVAL)
