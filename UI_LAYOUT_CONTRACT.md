# PERMANENT ARCHITECTURAL DIRECTIVE: UI LAYOUT CONTRACT

> **Mandate.** This contract is agreed with the user and the agent follows it. The agent never rewrites it on its own. Audit every layout change, styling update and DOM modification against these rules before applying it.
>
> **Conflict protocol.** If a request conflicts with the contract, ask the user before deviating. Decisions the contract doesn't cover are the user's call: ask, don't guess.
>
> Approved by the user on 2026-10-22; amended with the user on 2026-10-23 (menu screens) 2026-10-28 (0.3: visual Setup page, feel buttons, fingerprint, Driver plot) 2026-10-28 (feel toggles instead of press counts) and 2026-10-29 (0.3.1 UX rework: start and waiting lines, tooltips, map zoom, Compare time sync, no Analysis sheet, no shortcuts, wording). Any change that breaks the rules below is an unauthorized regression.

## Screens

### 1. Nav
One slim bar across the top: **Lap Analysis · Setup · History** on the left; on the right, a thin SVG burger icon.

The burger opens a menu under the icon (fade about 150 ms; Esc or a click outside closes it):
**Driver · Settings · About**

### 2. Welcome (no session open in LMU)
- A minimal centred screen: "Welcome back" and one fun fact from the user's own data (e.g. "You've done 312 laps at Sebring; your best got 1.4 s quicker over the last 3 stints").
- Under it, one muted line: **"Launch a session in LMU to set up"**; while LMU is in its menus, **"Waiting for a session"**. Nothing else.
- When a session opens in LMU, the app moves to Lap Analysis for that car and track by itself.
- Welcome only shows when the app starts with no session open. When a session ends, the screen stays on it.

### 3. Lap Analysis (the drive screen)
**Left compartment:** fixed width of about 300px, with a hairline on its right. Top to bottom:
1. **Session:** track and car as plain text. Fixed; nothing to click.
2. **Lap:** lap time, sectors, top and min speed for the lap on screen. A B column and Δ appear when Compare is on.
3. **Stints:** this session's stints, newest first. Expanding a stint shows its laps; clicking a lap shows it. Default: the best clean lap of the newest stint.
4. **Pinned at the bottom:**
   - **Compare**, off by default. It picks lap B from the same list.
   - **Next setup · N**, which opens the setup pop-up.

**Centre:** the track map, always, taking most of the screen, with the car marker at the slider position. Under it:
- a one-line readout at the slider position: distance, speed, gear, throttle, brake, steering
- the slider across the lap
- thin traces aligned to the same distance

Dragging over the traces zooms in, and double-click resets. The mouse wheel zooms the map at the cursor and dragging pans it; double-click or **Whole lap** resets the view. The map's view is its own; dragging over the traces still zooms them and fits the map to that stretch.

When Compare is on, B's line is added to the map along with a delta trace, and a **Distance · Time** switch appears next to Compare (Distance by default):
- **Distance:** both markers at the same point of the lap.
- **Time:** the slider runs over elapsed lap time and each marker sits where its lap was at that time, like a ghost. The readout shows each lap's own values plus the gap in metres between the markers. The traces keep their distance axis, with one cursor line per lap.
- Turning Compare off goes back to Distance.

While LMU is live and no stint of this session has been imported yet, the centre shows one line instead of the empty map: **"Laps load when you're back in the garage"**.

### 4. Setup
**Left compartment:**
- session (fixed)
- which stint the next setup is built from
- **Next setup · N**, which opens the setup pop-up

**Centre:** a visual page of the stint the next setup is built from. An **Advanced** switch at the top right swaps it for the table (below); off by default.
- **Balance map:** a grid, rows Fast corners / Slow corners, columns Turn-in / Apex / Exit. Each cell shows where the stint is against its target band (colour: `--us` understeer, `--os` oversteer, neutral inside the band) and, small, the value predicted after the proposed changes. Cells that don't count at this track (too little of the lap) are dimmed.
- **Side view:** the car from the side with its ride heights at speed and its rake, current and proposed.
- **Top view:** the car from above, marking each setting the proposal changes where it sits on the car, with an arrow for its direction.
- **Lap Δ:** the predicted lap-time change with its doubt (`−0.20 ± 0.13 s`), and one bar per sector.
- **Suspension:** four shaft-velocity histograms, one per wheel, laid out like the car (2×2).
  - Rebound left (−), bump right (+). The slow region (under the knee) is shaded and the knee is marked.
  - Overlays: current (`--s1`), previous stint (`--s2`), and a target shape (a `--muted` outline: the current shape made symmetric).
  - Bins are 10 mm/s. Control: kerbs `Off | On | Both`.
  - Under each: bump / rebound %, slow / fast %, skew, load variation.

**Advanced:** one large table.
- A **Stints / Laps** switch picks the columns, newest on the right. The header row stays visible.
- Row groups:
  - pace
  - balance: understeer turn-in, apex and exit; slow and fast corners; countersteer
  - tyres: inner, centre and outer per wheel; hot pressure; wear
  - brakes
  - platform
  - aids
  - driving
  - setup changes: the settings that changed, with each column's value

### 5. History
- A list of past sessions: car, track, date, laps, best lap.
- Picking one opens Lap Analysis and Setup on that data. The nav then shows **● Back to live** after a hairline, set apart from the tabs. The dot (`--live`) is red when LMU has a session open and `--muted` otherwise. The left column already names the car and track, so the nav doesn't.
- This is the only place where the data can be switched. The car and track are fixed everywhere else: there is no context picker and no follow toggle.

### 6. Setup Pop-up
Its own window over the screen, containing:
- the change rows: name, signed delta, `from → to`, one reason line, and a lock on each row
- **Feel**, two toggles, each against the last stint: front **Pointier · As it was · Lazier**, rear **Looser · As it was · Safer**. A toggle asks for one step from how the last stint felt, and a new stint starts back at *As it was*. **This is it** locks the balance of this stint as the car's fingerprint. Toggles update the proposal at once.
- the gap line, when the band is what costs time ("0.3 s available with more rotation")
- on prototypes (no ABS), **Too much brake pressure**: brake pressure rises a step each stint until you press it
- **Write setup**: writes the file and closes the pop-up
- While a new proposal is worked out, the rows dim, Write setup is disabled and the status reads **"Working out changes…"**
- Close (Esc also closes it)

## Behaviour

### 8. Desktop Only
Minimum window 1000×640. No phone layout, no phone tabs, no phone breakpoints.

### 9. Soft Animations
- The pop-up and tab changes fade and scale over about 200 ms with ease-out.
- The marker and readout follow the slider smoothly.
- Hover and focus transitions take about 150 ms.
- `prefers-reduced-motion` turns them off.

### 10. Nothing Happens On Its Own
Except the Welcome → Lap Analysis move and new stints appearing in the list. The page never navigates, scrolls itself or opens a pop-up unasked.

### 11. No Clutter, No AI Fluff
- No trade-off sliders and no balance sliders: lap time is the objective, and how the car should feel is the two toggles and the fingerprint.
- No helper text, badges, pills, cards, callouts, avatars or explainer copy.
- **No decorative text anywhere.** That means no sentence that explains nothing:
  - chart or table subtitles
  - "how to read this" lines
  - taglines
  - chatty empty states or status messages
  - descriptions of what a section is
- Labels are short nouns ("Stints", "Understeer entry"). Numbers carry their units.
- An empty state is at most a few words ("No laps yet").
- The only sentences allowed:
  - the one-line reason on each change row
  - the fun fact on Welcome
  - one line per car on the Driver screen
  - the gap line in the pop-up ("0.3 s available with more rotation")
  - "Launch a session in LMU to set up" / "Waiting for a session" on Welcome
  - "Laps load when you're back in the garage" in Lap Analysis
  - "Working out changes…" in the pop-up
  - real errors
- **Tooltips:** labels and units may have a hover tooltip of one or two short plain sentences saying what it is and which way is good (§20). They show on hover only, never unasked. The feel controls have none.

### 12. No Screenshots
Only when the user asks for them. Layout is checked by measuring element boxes in a headless DOM dump.

## Look

### 13. Shape
- Square corners everywhere (`*{border-radius:0 !important}`); only point markers are round.
- Hairlines (`1px solid var(--line)`) separate areas.
- No shadows (`box-shadow` or `drop-shadow`, chart marks included).
- No boxes inside boxes.
- Icons are thin inline SVG (`stroke="currentColor"`, `stroke-width="1.75"`). No emoji or unicode glyphs as buttons.
- Type: `"IBM Plex Sans","Segoe UI Variable","Segoe UI",system-ui,sans-serif` for text; `--mono: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace` for numbers, units, file names and lap data. Deltas are signed text (`+0.21 s`).

### 14. Palette
Neutral graphite, not Gruvbox yellow.

| Token | Dark (default) | Light |
|---|---|---|
| `--bg` | `#111214` | `#f5f5f3` |
| `--surface` | `#18191c` | `#eaeae7` |
| `--line` | `#26282b` | `#d9d9d6` |
| `--text` | `#e4e4e2` | `#1d1e21` |
| `--muted` | `#8a8d93` | `#686b71` |
| `--s1` (lap A) | `#4a90d9` | `#2563ad` |
| `--s2` (lap B) | `#e08a3c` | `#b05a10` |
| `--us` (understeer) | `#3aa6a0` | `#1f7f79` |
| `--os` (oversteer) | `#d9534f` | `#b93a36` |
| `--live` (Back to live dot) | `#e5484d` | `#c62f34` |

- Lap A is always `--s1` and lap B always `--s2`, everywhere; in the suspension histograms the current stint is `--s1` and the previous one `--s2`.
- `--us` / `--os` are only for the balance map; `--live` only for the Back to live dot.
- Text never takes a data colour.
- Light applies under `prefers-color-scheme: light` or `data-theme="light"` on `<html>`.

## Serving

### 15. Serving Contract
- One Python server (`autosetup serve`) serves one self-contained `web/index.html` (plain JS, no framework) and a JSON API.
- The desktop app (`desktop/`, Tauri) is only a window around that server. It shows a splash in these tokens until `/api/health` answers, then the page. It has no UI of its own.
- `GET /api/health` is polled every 2s, and the page reloads when it recovers. `GET /api/live` is polled every 3s to detect the session open in LMU.
- **Stable API surface:** endpoints can be added, but existing ones are never renamed or reshaped without updating this file.
  - `GET /api/health`
  - `GET /api/state`: contexts, selected context, objectives, watcher status
  - `GET /api/stints?context=`: stints with summaries, laps (`seq`, `lap`, `time`, `complete`, `in_pits`, `off_track`, `clean`), what you said (lock-ups, note), setup changes vs the previous stint, Pareto flag
  - `GET /api/live`: the session open in LMU, `{state: off|menu|live, vehicle, track, session, context}`
  - `GET /api/trackmap?context=` (or `?layout=`): outline `{length, points: [[dist, x, y]], sectors: [d1, d2], origin}` plus the car's best clean lap (`reference`)
  - `GET /api/lap?stint=&seq=`: `{lap, time, clean, setup_name, metrics, trace: {step_m, distance[], time[], speed[], throttle[], brake[], steer[], gear[], x[], y[], sectors[]}}`, on a 5 m grid in the map's frame
  - `GET /api/setup-diff?a=&b=`: setting changes going from stint b to stint a
  - `GET /api/pareto?context=`
  - `GET /api/proposal?stint=` / `POST /api/propose {stint}`: the next-setup proposal (`changes`, `notes`, `signature`, `target`, `locks`, `plan`, `error`). `plan`: the balance target, measured and predicted cells, lap Δ with sectors, gap, wing sensitivity, platform current / proposed, dampers
  - `GET /api/target?stint=`: the target, the measured balance and the feel events
  - `POST /api/feedback {stint, axis: front|rear, value: -1|0|1}`: the stint's toggle (−1 pointier / looser, 1 lazier / safer)
  - `POST /api/lock {stint}`: "This is it"
  - `GET /api/histograms?stint=&kerbs=off|on|both&bin=`: per wheel `{edges, counts, stats}`, plus the knee
  - `POST /api/write {stint, signature}`; 409 if the proposal changed in the meantime
  - `POST /api/locks {stint, keys, locked}`, `POST /api/context`, `POST /api/rating {file_name, lockups?, note?}`, `POST /api/import`
  - `GET /api/driver`: the Driver screen, one section per car class (§16)
  - `GET /api/settings` / `POST /api/settings`: units, folders, fingerprints per car (§17)
  - `POST /api/forget {car_key}`: forget a car's fingerprint
  - `POST /api/recompute`: recompute lap metrics
  - `POST /api/open-folder {which}`: `install` | `telemetry` | `settings` | `database`
  - `GET /api/about`: app version, metrics version, database path, recordings, laps

## Menu screens (approved 2026-10-23)

### 16. Driver
Opens like a tab and replaces the left compartment and centre. A **Close** button at the bottom right returns to where you were; Esc does the same. One section per car class driven, newest first:
- **Header:** class · laps
- **Plot:** Looser ↔ Safer across, Pointier ↔ Lazier up, in toggle steps from the class's usual balance (the centre). One point per car with a fingerprint, labelled with the car.
- **Cars:** one line per car ("Pointy entry, stable exit, slightly more rake than usual").
- **Empty:** "No fingerprint yet".

### 17. Settings
Opens like a tab, closed the same way as Driver. Label/value rows in sections:
- **Appearance:** theme System / Dark / Light (the only theme control).
- **Units:** speed km/h | mph, temperature °C | °F, pressure kPa | psi. Applies everywhere.
- **Data:** LMU install, telemetry folder, settings folder and database file, read-only in mono, each with "Open folder". Buttons: **Import now**, **Recompute metrics**.
- **Driver profile:** per car with a fingerprint, **Forget fingerprint**, behind a confirm.

### 18. About
A pop-up showing app version, metrics version, database path, recordings and laps. No prose.

### 19. Keys
No keyboard shortcuts. Esc closes the menu, pop-ups and the Driver and Settings screens.

### 20. Tooltips
Agreed texts; edit here first.
- **Balance:** "Understeer angle against your target band, in degrees of steering. + more understeer than target, − more oversteer. Small number: after the proposed changes."
- **Side:** "Ride height front and rear at speed, and rake. Current and proposed."
- **Changes:** "Where each proposed change sits on the car; the arrow shows its direction."
- **Lap Δ:** "Predicted lap-time change with these changes. ± is how unsure the prediction is."
- **Suspension:** "Damper shaft speed in mm/s. Left of zero is rebound, right is bump. Shaded: slow (below the knee)."
- **Readout:** distance, speed, gear, throttle, brake, steering (steering wheel angle), Δ (time to lap A).
- **Change row lock:** "Keep this setting as it is."
- **Next setup:** "The changes proposed from the last stint."
- **Advanced table rows:** the server's `help` text per row, when it has one.
