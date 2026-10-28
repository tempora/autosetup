# PERMANENT ARCHITECTURAL DIRECTIVE: UI LAYOUT CONTRACT

> **Mandate.** This contract is agreed with the user and the agent follows it. The agent never rewrites it on its own. Audit every layout change, styling update and DOM modification against these rules before applying it.
>
> **Conflict protocol.** If a request conflicts with the contract, ask the user before deviating. Decisions the contract doesn't cover are the user's call: ask, don't guess.
>
> Approved by the user on 2026-10-22. Any change that breaks the rules below is an unauthorized regression.

## Screens

### 1. Nav
One slim bar across the top: **Lap Analysis · Setup · History** on the left; on the right, a thin SVG burger icon and then the theme toggle.

The burger opens a menu under the icon (fade about 150 ms; Esc or a click outside closes it):
**Driver · Settings · Keyboard shortcuts · About**

### 2. Welcome (no session open in LMU)
- A minimal centred screen: "Welcome back" and one fun fact from the user's own data (e.g. "You've done 312 laps at Sebring; your best got 1.4 s quicker over the last 3 stints"). Nothing else.
- When a session opens in LMU, the app moves to Lap Analysis for that car and track by itself.
- Welcome only shows when the app starts with no session open. When a session ends, the screen stays on it.

### 3. Lap Analysis (the drive screen)
**Left compartment:** fixed width of about 300px, with a hairline on its right. Top to bottom:
1. **Session:** track and car as plain text. Fixed; nothing to click.
2. **Lap:** lap time, sectors and key stats for the lap on screen. A B column and Δ appear when Compare is on.
3. **Stints:** this session's stints, newest first. Expanding a stint shows its laps; clicking a lap shows it. Default: the best clean lap of the newest stint.
4. **Pinned at the bottom:**
   - **Compare**, off by default. It picks lap B from the same list.
   - **Next setup · N**, which opens the setup pop-up.

**Centre:** the track map, always, taking most of the screen, with the car marker at the slider position. Under it:
- a one-line readout at the slider position: distance, speed, gear, throttle, brake, steering
- the slider across the lap
- thin traces aligned to the same distance

Dragging over the traces zooms in, and double-click resets. When Compare is on, B's line is added to the map along with a delta trace.

### 4. Setup
**Left compartment:**
- session (fixed)
- which stint the next setup is built from
- **Next setup · N**, which opens the setup pop-up

**Centre:** one large table.
- A **Stints / Laps** switch picks the columns, newest on the right. The header row stays visible.
- Row groups:
  - pace
  - balance: understeer entry, mid and exit; slow and fast corners; countersteer
  - tyres: inner, centre and outer per wheel; hot pressure; wear
  - brakes
  - platform
  - aids
  - driving
  - setup changes: the settings that changed, with each column's value

### 5. History
- A list of past sessions: car, track, date, laps, best lap.
- Picking one opens Lap Analysis and Setup on that data. The nav then reads **History: car · track**, with a way back to the live session.
- This is the only place where the data can be switched. The car and track are fixed everywhere else: there is no context picker and no follow toggle.

### 6. Setup Pop-up
Its own window over the screen, containing:
- the change rows: name, signed delta, `from → to`, one reason line, and a lock on each row
- change size
- **Car feel**, per car class: front **Lazier / Pointier**, rear **Safer / Looser**, each a step either way from what your ratings taught (picking the lit one unpicks it). Picking updates the proposal at once, so you can try a feel for a stint. Unpicked by default (the usual targets).
- **Rate stint**, collapsed (it never opens on its own), one section: Overall 1–5, then the **balance grid**: rows Low / Mid / High speed, columns Corner entry / Mid corner / Corner exit, each cell a slider from oversteer (left) to understeer (right), Neutral in the centre, nine stops (A lot of · Quite a lot of · plain · A little — oversteer, Neutral, the same towards understeer; half rating points: "A little" only teaches what you like, a point or more is fixed directly). Unrated cells sit greyed in the centre reading "Not rated"; × clears one. Saved on release. The speed rows teach the balance you like at each speed and are fixed with that speed's tools (mechanical when slow, aero when fast). On prototypes (no ABS) it also has a **Too much brake pressure** button: brake pressure rises a step each stint until you press it
- **Write setup**
- Close (Esc also closes it)

### 7. Analysis Sheet
Stays, opened by its icon: Pareto, laps, balance, tyres, platform, driving, model. Each has a table view.

## Behaviour

### 8. Desktop Only
Minimum window 1000×640. No phone layout, no phone tabs, no phone breakpoints.

### 9. Soft Animations
- The pop-up, the sheet and tab changes fade and scale over about 200 ms with ease-out.
- The marker and readout follow the slider smoothly.
- Hover and focus transitions take about 150 ms.
- `prefers-reduced-motion` turns them off.

### 10. Nothing Happens On Its Own
Except the Welcome → Lap Analysis move and new stints appearing in the list. The page never navigates, scrolls itself or opens a pop-up unasked.

### 11. No Clutter, No AI Fluff
- No trade-off sliders (the trade-off is learned).
- No helper text, tips, badges, pills, cards, callouts, avatars or explainer copy.
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
  - one line per finding on the Driver screen
  - real errors

### 12. No Screenshots
Only when the user asks for them. Layout is checked by measuring element boxes in a headless DOM dump.

## Look

### 13. Shape
- Square corners everywhere (`*{border-radius:0 !important}`); only the theme toggle and point markers are round.
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

- Lap A is always `--s1` and lap B always `--s2`, everywhere.
- Text never takes a data colour.
- Light applies under `prefers-color-scheme: light` or `data-theme="light"` on `<html>`.

## Serving

### 15. Serving Contract
- One Python server (`autosetup serve`) serves one self-contained `web/index.html` (plain JS, no framework) and a JSON API.
- The desktop app (`desktop/`, Tauri) is only a window around that server. It shows a splash in these tokens until `/api/health` answers, then the page. It has no UI of its own.
- `GET /api/health` is polled every 2s, and the page reloads when it recovers. `GET /api/live` is polled every 3s to detect the session open in LMU.
- **Stable API surface:** endpoints can be added, but existing ones are never renamed or reshaped without updating this file.
  - `GET /api/health`
  - `GET /api/state`: contexts, selected context, `weights` (learned, max = 1), `preference`, `change_size`, objectives, watcher status
  - `GET /api/stints?context=`: stints with summaries, laps (`seq`, `lap`, `time`, `complete`, `in_pits`, `off_track`, `clean`), ratings, setup changes vs the previous stint, Pareto flag
  - `GET /api/live`: the session open in LMU, `{state: off|menu|live, vehicle, track, session, context}`
  - `GET /api/trackmap?context=` (or `?layout=`): outline `{length, points: [[dist, x, y]], sectors: [d1, d2], origin}` plus the car's best clean lap (`reference`)
  - `GET /api/lap?stint=&seq=`: `{lap, time, clean, setup_name, metrics, trace: {step_m, distance[], time[], speed[], throttle[], brake[], steer[], gear[], x[], y[], sectors[]}}`, on a 5 m grid in the map's frame
  - `GET /api/setup-diff?a=&b=`: setting changes going from stint b to stint a
  - `GET /api/pareto?context=`
  - `GET /api/proposal?stint=` / `POST /api/propose {stint}`: the next-setup proposal (`changes`, `notes`, `signature`, `target`, `locks`, `model`, `error`)
  - `POST /api/write {stint, signature}`; 409 if the proposal changed in the meantime
  - `POST /api/locks {stint, keys, locked}`, `POST /api/context`, `POST /api/rating`, `POST /api/import`
  - `POST /api/change-size {size}`: `small` | `medium` | `large` (default)
  - `GET /api/driver`: the Driver screen, one section per car class (§16)
  - `GET /api/settings` / `POST /api/settings`: units, change size, folders, per-class rotation resets (§17)
  - `POST /api/recompute`: recompute lap metrics
  - `POST /api/open-folder {which}`: `install` | `telemetry` | `settings` | `database`
  - `GET /api/about`: app version, metrics version, database path, recordings, laps

## Menu screens (approved 2026-10-23)

### 16. Driver
Opens like a tab and replaces the left compartment and centre. The nav reads **Driver**, with a way back. One section per car class driven, newest first:
- **Header:** class · laps · ratings
- **Rotation:** entry, mid and exit. The angle you like, the usual angle, and one line ("Pointier than usual on entry").
- **Setups you go towards:** settings moved by 5 % of range or more, from the starting setup at each car/track to the setups you kept (drove again, didn't revert) or rated 4–5. Each shows Δ % of range, how many car/tracks, and one line.
- **Trade-off:** Pace · Drivability · Tyres (learned, shared across cars).
- **Empty:** "No ratings yet".

### 17. Settings
Opens like a tab. Label/value rows in sections:
- **Appearance:** theme System / Dark / Light (the nav toggle stays).
- **Units:** speed km/h | mph, temperature °C | °F, pressure kPa | psi. Applies everywhere.
- **Proposals:** default change size.
- **Data:** LMU install, telemetry folder, settings folder and database file, read-only in mono, each with "Open folder". Buttons: **Import now**, **Recompute metrics**.
- **Driver profile:** per car class, **Reset rotation learning**, behind a confirm. It ignores ratings from before the reset; the ratings themselves are kept.

### 18. Keyboard shortcuts
A pop-up listing: `1` `2` `3` switch tabs · `←` `→` move the slider 5 m (`Shift` 50 m) · `C` toggle Compare · `N` open Next setup · `Esc` close.

### 19. About
A pop-up showing app version, metrics version, database path, recordings and laps. No prose.
