# LMU autosetup

Telemetry-driven setup optimiser for Le Mans Ultimate: the fastest setup you can actually drive, from a few laps per run. You say how the car should feel (four buttons, a locked fingerprint); it finds the settings. UI rules are in [UI_LAYOUT_CONTRACT.md](UI_LAYOUT_CONTRACT.md).

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

The install is found through Steam. Set `AUTOSETUP_LMU_DIR` to override it.

## Commands

```
.venv\Scripts\python -m autosetup info                 # install + database locations
.venv\Scripts\python -m autosetup import               # import new telemetry, learn car templates / track folders
.venv\Scripts\python -m autosetup list                 # imported recordings with clean-lap counts
.venv\Scripts\python -m autosetup show 22              # a recording's adjustable settings and laps
.venv\Scripts\python -m autosetup write 22 VM_REAR_ANTISWAY-=1 --dry-run
.venv\Scripts\python -m autosetup validate-mapping     # check telemetry <-> .svm names against your setups
```

```
.venv\Scripts\python -m autosetup serve                # dashboard on :8191; prints the LAN URL for your phone
.venv\Scripts\python -m autosetup recompute [--all]    # recompute lap metrics after a metrics update
```

`serve` watches the telemetry folder and imports each outing a few seconds after you're back in the garage. It also follows the session open in LMU (through the game's local REST API on port 6397), so the page shows the car and track you're driving.

The page follows [UI_LAYOUT_CONTRACT.md](UI_LAYOUT_CONTRACT.md). It's desktop only, with three tabs: **Lap Analysis · Setup · History**.

- **Welcome:** shown when the app starts with no session open in LMU, with one fact from your own laps. Opening a session in LMU moves the app to Lap Analysis for that car and track.
- **Lap Analysis:** the car and track are fixed to the session.
  - **Left:** session, the lap's time, sectors and key stats, and the session's stints. Expand a stint to pick a lap.
  - **Centre:** the track map. Under it are a slider that moves the car marker along the lap, a readout at that point, and thin traces. Drag over the traces to zoom; double-click or "Whole lap" resets.
  - **Compare** (off by default) adds a lap B: a second line, B and Δ in the stats, and a delta trace.
- **Setup:** the stint the next setup is built from, as pictures:
  - a balance map (Low / High speed × Entry / Mid / Exit, against your target band);
  - side and top views of the car (ride heights and rake; what the proposal changes);
  - the predicted lap Δ with sectors;
  - a shaft-velocity histogram per wheel (bin width, kerbs on/off, against the previous stint).

  **Advanced** shows the full table instead. **Next setup** opens the pop-up: the change rows with locks, the four feel buttons with Undo, **This is it**, the gap line, and **Write setup**.
- **History:** past sessions (one car at one track in one sitting). Picking one opens it in Lap Analysis and Setup until you go back to live.
- **Chart icon:** the analysis sheet (laps, balance, tyres, platform, driving).
- **Menu → Driver:** per car class, where your fingerprints sit on Looser ↔ Safer × Pointier ↔ Lazier, with one line per car. **Settings** can forget a car's fingerprint.


### Next setup (0.3)

The goal is the fastest setup you can actually drive, from a few laps per run. **Write setup** saves the proposal as `autosetup_<car>_<track>_<n>.svm` in that track's Settings folder, ready to load in the garage without restarting. The lock icon on a row keeps that setting fixed.

| Step | What it does |
|---|---|
| Rules | **Tyres:** pressure from the tread crown, only ever down; camber from the inner–outer spread. **Brakes:** ducts from peak disc temperature; brake pressure by feel on cars without ABS. **Platform:** ride height (or third packers) up when the car bottoms. |
| Target | **Your balance per phase (entry, mid, exit) × speed band (low, high):** your locked fingerprint for the car, plus your button presses. Without a fingerprint, the balance model's estimate at the stint you're on. |
| Main block | **Up to 8 settings, in clicks** (rear wing, ride heights, bars, diff preload, springs, brake bias). The objective is predicted lap time; the constraints are every cell inside the target band, no bottoming or ride height under the class minimum, and kerbs no worse. The drag level (wing) is chosen first, then balance is solved with the rest. |
| Dampers | **Slow/fast bump and rebound per axle, third-element dampers and packers, in clicks:** a steadier contact patch inside the same band, kerbs no worse. |

**Balance model.** Where the car sits at this layout, measured, plus each setting's known direction at a fixed size (`opt/priors.py`); the sizes aren't learned from stints. At a layout the car hasn't been driven at, the car's other layouts (with more doubt) or the fingerprint stand in.

**Lap-time model.** See *Lap-time model* below, plus a residual GP per sector and the aero map's doubt about any platform you haven't driven. Candidates are charged one sd of lap-time doubt, so an uncertain gain doesn't send you out on a guess.

**The band wins.** If more rotation would be faster, the gap says so ("0.3 s available with more rotation"). A trust region around your setup grows after proposals that paid off and shrinks after ones that didn't. Ride heights move at most 5 mm per proposal, dampers at most 3 clicks (packers 2).

`python -m autosetup.opt.bench` runs the loop on a toy car whose true balance slopes are 0.5–1.5× the priors. From 0.9° outside the band it gets inside in one run, holds it (≤ 0.06°), and gets 0.2–0.35 s faster in a few runs.

### Car feel and the fingerprint

Four buttons on two axes move the target by a fixed step (`feel_step_deg`); they never touch a setting.

- **Looser / Safer:** mid-corner and exit rotation against rear grip margin (and how much countersteer is acceptable).
- **Pointier / Lazier:** entry rotation and turn-in response.

Presses add up, combine, and can be undone.

**This is it** freezes the balance you're driving as the car's fingerprint, with a tolerance band (no more rotation, no less). It keeps:

- the aero platform: wing, ride heights static and at speed, rake, aero balance;
- the track's speed-band split;
- the setup.

The fingerprint belongs to the car (by model, across liveries and game updates). On another track it's the target, with the cells weighted by that track's corner mix. A car of the same class without one gets the class's fingerprints pooled, with a wider band.

### Third springs (prototypes)

On cars with an adjustable third (heave) element on an axle, such as LMP2 and Hypercar, each lap gets three numbers per axle from `Front3rdDeflection` / `Rear3rdDeflection`: the travel used, where the packers start, and the share of the lap spent on them. They show in the Platform group of the Setup table.

LMU's packers are a stiff rate, not a wall. So contact shows as a knee in deflection against speed²: past it, more aero load hardly compresses the third spring any further. The knee is fitted once per recording, with all laps pooled and pit exit included. On a fast track a racing lap may never be slow enough to leave the packers, so one lap alone can't show the knee.

GT3s log these channels with unrelated values, so the numbers only appear for axles that have a third element.

The third element's four dampers and its packers belong to the damper block (below); the third spring itself stays where you set it. The rules only touch packers for bottoming. Running on the packers can be deliberate (aero platform), so whether you're on them too much is left to the damper block's contact-patch and kerb numbers.

### Desktop app

`desktop/` holds a minimal Tauri (Rust) app.
- **Shell:** a WebView2 window that starts the bundled server, shows a splash until it answers, then shows the same page. Closing the window stops the server: it runs with `--exit-with-stdin` on a pipe from the shell.
- **Server:** the backend frozen by PyInstaller (`autosetup-server.exe`).
- **Phone:** the server still listens on the LAN, so the phone works. Windows asks once to allow it through the firewall.
- **Already running:** if an `autosetup serve` is up on port 8191, the app uses it.

Build (needs Rust, Node, and `pip install -e ".[desktop]"`):

```
.venv\Scripts\python -m PyInstaller desktop\autosetup-server.spec --distpath build --workpath build\pyi --noconfirm
cd desktop
npm install
npx tauri build      # → src-tauri\target\release\bundle\nsis\autosetup_0.3.0_x64-setup.exe
```

`desktop\build.ps1` runs all three steps. The installer installs per user and downloads WebView2 if it's missing. The server log goes to `%LOCALAPPDATA%\autosetup\server.log`, next to the database.

`write` (the CLI command) takes `KEY=index`, `KEY+=steps` or `KEY-=steps`, using the key names `show` prints. It creates `autosetup_<car>_<track>_<n>.svm` in the track's Settings folder. It never overwrites a setup it didn't create.

### Lap-time model (0.3)

A quasi-steady-state point-mass lap (`autosetup/sim/`) over the curvature of your fastest lap at the layout. The car is your own measured envelope: lateral grip (99th percentile per 5 m/s, never falling with speed), braking (95th) and net full-throttle acceleration (median, away from gear changes), pooled over the car's stints at the layout. One driver-efficiency factor per car and layout makes the simulated laps match yours. Leave-one-out on the current build's stints it predicts a stint's best lap within 0.58 % rms (0.2–0.7 % per layout, no bias).

What a setup does is measured between stints at the same layout:

- **Drag:** a per-stint v² term on the full-throttle straights, with the track's slopes shared, corrected by top speeds against the other stints.
- **Downforce:** the lateral limit per speed bin against the other stints, weighted by how close the bin is to the limit. It reads low rather than wrong.
- **Aero balance:** high- minus low-speed understeer.

The car's aero map (`aero_maps`, per car model and game build) is the slope of each against rear wing, front ride height and rake at speed, so it carries across tracks. Sector times the point mass misses are left to a residual GP per sector. `sim/sensitivity.py` gives what a click of wing and a mm of rake are worth at a layout.

## Tests

```
.venv\Scripts\python -m pytest              # fast, synthetic data only
.venv\Scripts\python -m pytest -m install   # also check against the local LMU install
```

## Data sources (phase 1 findings)

- **Telemetry:** `UserData\Telemetry\*.duckdb`, one table per channel. Fixed-rate channels have no timestamps; their rate comes from `channelsList`, and it differs between game versions. Event tables carry `ts` on the `GPS Time` clock. `metadata.CarSetup` holds every setting as `{value, minValue, maxValue (exclusive), stringValue}`.
- **Live outings:** while you drive, LMU writes the outing through `<file>.duckdb.wal` and keeps both files locked; reading them gives "permission denied". When you return to the garage (pit lane or straight back), it merges the log, deletes it and releases the lock. autosetup therefore treats a `.wal` as "outing still running" and imports right after it disappears. If the game exits leaving a `.wal` behind, the log is replayed into a temp copy.
- **Setups:** `UserData\player\Settings\<Folder>\*.svm` stores the same step indices as `CarSetup`. Folder names differ from TrackName (`Circuit de la Sarthe` → `Lemans`), so they're learned from matched history. Tyre compound is the one setting whose index doesn't match, so it's never written.
- **Channels that can't be trusted (phase 2 findings):**
  - `G Force Lat/Long` (10 Hz) don't correlate with anything. Accelerations are derived instead: speed × GPS yaw rate, and d(speed)/dt.
  - `Wheel Speed` is contact-patch speed, so it carries no slip or lock-up information.
  - `TyresTempLeft/Right` are car-frame sides: the inner edge is `Right` on the left-hand wheels.
  - `Lap Dist` is 10 Hz in older files, so it's unwrapped before resampling.
- **Game builds:** recordings don't say which build they're from (`Version` is the file format). Steam's `appmanifest_2399420.acf` names the installed build and when it was installed; autosetup remembers each build it sees and learns only from the current one's recordings. Older ones still show in History.
- **Corner passes (0.3):** each lap's pass through each corner (one map per layout, from the median lap) gives one balance observation per phase (entry / mid / exit) and speed band (low / high, by apex speed). Balance is the understeer angle, the yaw-rate error against v·δ/L in steering degrees, signed by the way the car turns, with steering and throttle rates normalised out per corner. Partial laps outside the pits count; nothing waits for a number of stints.
- **Suspension:** `Susp Pos` (100 Hz) is spring compression; shaft velocity is its Savitzky–Golay derivative over 40 ms. Load variation uses the spring compression as a proxy (the game logs spring settings as indices, not rates).
- **Kerbs:** `SurfaceTypes` code 5 per wheel; without it, shaft-velocity spikes at the track edge. Each event records peak load, airtime, pitch and heave transients and the yaw-rate disturbance after it.
- **DAM (optional):** a MoTeC log from the rF2 DAM plugin (`<LMU>\LOG\*.ld`) of the same outing adds tyre load and per-wheel slip. Its session clock is whole seconds, so it's aligned by Ground Speed.
- **Templates:** a car model is identified by its parameter ranges, which change with game updates. A saved `.svm` of the same car supplies the file header. Every setting is then overwritten from the recording.

## License

Apache License 2.0, see [LICENSE](LICENSE).
