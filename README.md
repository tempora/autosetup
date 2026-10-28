# LMU autosetup

Telemetry-driven setup optimiser for Le Mans Ultimate. It looks for Pareto trade-offs between pace, drivability and tyre life, and learns from how you drive. UI rules are in [UI_LAYOUT_CONTRACT.md](UI_LAYOUT_CONTRACT.md).

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
- **Setup:** one large table of the session with a Stints / Laps switch. It covers pace, balance (understeer by phase and corner speed, countersteer), tyres per wheel, brakes, platform, aids, driving, and the settings that changed. **Next setup** opens the pop-up with the change rows, locks, change size, the collapsed rating, and **Write setup**.
- **History:** past sessions (one car at one track in one sitting). Picking one opens it in Lap Analysis and Setup until you go back to live.
- **Chart icon:** the analysis sheet (Pareto, laps, balance, tyres, platform, driving, model).

There are no trade-off sliders: the trade-off is learned (see below).

### Next setup (phase 3)

The setup bar proposes the next setup from the most recent stint that has at least 2 full laps. Each change comes with its reason. **Write setup** saves it as `autosetup_<car>_<track>_<n>.svm` in that track's Settings folder, ready to load in the garage without restarting. The lock icon on a row keeps that setting fixed; the advice then moves on to the next option.

| Group | Decided by |
|---|---|
| Tyres | Pressure per wheel from the tread crown (centre vs edges), camber per axle from the inner–outer spread |
| Brakes | Front/rear ducts from peak disc temperature |
| Platform | Ride height from bottoming; lowering a car with room to spare when pace weight ≥ 60 %. On cars with a third spring, bottoming while hardly on the third packers (< 5 % of the lap) adds a packer step instead of ride height |
| Balance | Entry / mid / exit, from your rating; otherwise only clear oversteer (countersteer, heavy TC). At most 2 moves, small steps, never detaching a bar |

Targets live in [`autosetup/knowledge/classes.toml`](autosetup/knowledge/classes.toml). They're calibrated on this install's GT3 data (mostly pro setups), so they flag outliers rather than restate textbook values. Tune them as you learn.

### Third springs (prototypes)

On cars with an adjustable third (heave) element on an axle, such as LMP2 and Hypercar, each lap gets three numbers per axle from `Front3rdDeflection` / `Rear3rdDeflection`: the travel used, where the packers start, and the share of the lap spent on them. They show in the Platform group of the Setup table.

LMU's packers are a stiff rate, not a wall. So contact shows as a knee in deflection against speed²: past it, more aero load hardly compresses the third spring any further. The knee is fitted once per recording, with all laps pooled and pit exit included. On a fast track a racing lap may never be slow enough to leave the packers, so one lap alone can't show the knee.

GT3s log these channels with unrelated values, so the numbers only appear for axles that have a third element.

The model learns the third spring, its packers and its four dampers like the corner settings, changing at most one third-spring setting per axle per proposal. The rules only touch packers for bottoming (see the table above). Running on the packers can be deliberate (aero platform), so whether you're on them too much is left to the model, which learns it from your laps.

### Change per stint

**Change per stint**, in the setup bar's *Changes* panel, sets how bold proposals are:

| Size | Settings changed | Step |
|---|---|---|
| Small | 1 | one click |
| Medium | 3 | about 8 % of each setting's range |
| Large (default) | 5 | about 15 % of the range |

The rules' balance moves also scale with the size: up to 4 moves and double steps on Large. The model fills the remaining slots with settings the rules don't touch, including all eight dampers, springs, wing, TC and ABS. Each slot goes to a different group, so you won't get three TC maps at once. On the toy-car benchmark with ~18 settings, after 20 stints (start 0, lower is better):

| Size | Cost |
|---|---|
| Large | −0.17 |
| Medium | −0.18 |
| Small | +0.04 |
| Random | +0.26 |

Understeer and oversteer are fixed by phase with the bars, brake bias, diff, front ride height and rear wing. If the same complaint comes back, it gets a different tool than the one changed in the last two stints. Without a rating, telemetry counts as understeer when the understeer angle is above what GT3 laps usually show (classes.toml).

### Learning what works for you (phase 4)

Balance settings are also learned from your own stints. Each car + track layout gets its own model, a small Gaussian process (numpy only) for each objective:

- **Pace:** the mean clean lap, corrected to a reference fuel load (`fuel_s_per_l` in the knowledge file) and to the 3rd lap of a stint. The second correction uses a within-stint trend fitted on stint-demeaned laps, which covers tyre wear and settling in.
- **Tyres:** wear per lap.
- **Drivability:** lap-to-lap spread, plus countersteer moments, plus your rating when you give one.

Your experience on the car/track and the track temperature are inputs to the model, so getting faster yourself isn't credited to the setup.

The model goes through two states:

- **Learning:** fewer than 4 stints with 3+ clean laps, or fewer than 3 different setups. It asks for one click at a time on the setting it knows least about. That only happens when the rules aren't already fixing a balance problem.
- **Ready:** up to two moves, picked by expected improvement under your trade-off weights, inside a trust region around the setup you're used to. The region grows after proposals that paid off and shrinks after ones that didn't.

Rated balance complaints still get their direct fix. Telemetry-only rule moves give way to the model once it's ready. The **Model** tab shows what it has learned: the corrections, your improvement since stint 1, and each setting's expected effect with its uncertainty.

To check that the loop converges:

```
.venv\Scripts\python -m autosetup.opt.bench
```

It runs a simulated car with known optimum, a driver who improves, and 0.25 s lap noise. Random clicking makes things worse, and staying put stays at 0 (numbers above). It also runs the transfer check below.

### Learning what you prefer (phase 5)

There are no trade-off sliders. How much pace, tyre wear and drivability matter to you is learned from what you do with the setups, with no extra input:

| Evidence | Read as | Weight |
|---|---|---|
| You tried a new setup and went back to one you had before | the one you went back to won | 1 |
| You built the next setup on the new one instead of going back | the new one won, weakly | 0.3 |
| Two rated stints of the same car and track | the higher rating won | ½ per rating point |

Each comparison sets the two stints' objectives against each other (corrected pace, wear, drivability), each scaled by how much a setup plausibly moves it. A Bradley–Terry model over the weights starts from pace 1, tyres 0.5, drivability 0.75; it takes about a dozen comparisons to move far from there. Preference is yours, not the car's, so evidence from every car and track is pooled. The Model tab shows the learned weights, their uncertainty and where they came from (`autosetup/opt/preference.py`).

### Transfer between tracks (phase 5)

The model for a car at a track also uses that car model's stints at other tracks. Each track keeps its own corrections and its own learning curve. Its objectives are shifted to this track's level, and the GP links tracks with a correlation ρ that it fits (prior around 0.7). So a new track starts from what the bars, dampers and diff did elsewhere, and still learns where it differs.

The model counts as ready with 2 stints at this track, 4 in all and 3 different setups; reasons then read e.g. "Model of 2 stints + 3 at 1 other track". In the toy benchmark, track B wants more wing and a stiffer front bar than track A. After 12 stints at A, the first 6 stints at B score (mean true cost of stints 3–6, 8 seeds):

| | Cost |
|---|---|
| Starting alone at B | +0.15 |
| With A's stints | −0.10 |

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
- **Templates:** a car model is identified by its parameter ranges, which change with game updates. A saved `.svm` of the same car supplies the file header. Every setting is then overwritten from the recording.

## License

Apache License 2.0, see [LICENSE](LICENSE).
