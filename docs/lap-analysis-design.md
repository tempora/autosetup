# Lap Analysis: single-page design

Lap Analysis answers three questions without scrolling or opening anything:

1. **How far apart are the two laps, and where did the time go?**
2. **What did I do differently there?** Brake point, brake shape, minimum speed, throttle pickup.
3. **What does it look like on track?** Line and braking.

Everything sits on one fixed page, and one cursor (a time in lap A, `S.t`) drives all of it. This document describes the page as built in `web/index.html`. §8 lists the parts of the original proposal that were left out.

---

## 1. Principles

- **One screen, no moving parts.** Nothing floats, nothing overlaps the map, and no panel needs dragging.
- **One cursor.** The car on the map, the cursor line in the deck, the values in the transport and the playhead all follow the same position.
- **Answer first, detail second.** The gap comes first, then the map (where), then the deck (why).
- **Numbers only where they help.** No hints, legend or captions.
- **Colour on data only:** gain is `--pri`, loss is `--ter`, throttle is `--green`, brake is `--red`, and everything else uses greys from the existing tokens.
- **No corner detection.** Zones come from brake onsets (brake above 10 %). Nothing is guessed from the track shape.

---

## 2. Layout

```
┌──────────┬─────────────────────────────────────────────────────────────────────┐
│ SESSION  │ BAR     A −0.420 s to B                         Line [Pedals|Gain/loss] │
│ card     ├──────────────────────────────────────────┬──────────────────────────┤
│          │ MAP  (60 %)                              │ TRACE DECK  (40 %)       │
│ STINTS   │  flat top view, real road width,         │  Δ A to B                │
│ lap list │  paint, both lines, brake-point lines,   │  Speed                   │
│ (picks   │  sector names with Δ, cars A and B,      │  Throttle                │
│  A / B)  │  gap tag over car A              [+][−]  │  Brake                   │
│          │                                  [fit]   │  Gear                    │
│          │                                  [follow]│  distance axis           │
│          ├──────────────────────────────────────────┴──────────────────────────┤
│          │ TRANSPORT  A  thr ▮▮  brk ▮▮  gear  speed  [▶]  speed  gear  brk  thr  B │
│          │            0:31.204 ├──S1──┼────S2────┼──S3──┤ 1:02.060               │
└──────────┴─────────────────────────────────────────────────────────────────────┘
```

- The centre column is a grid of bar, stage and transport, sized to `100vh − 64px` (minimum 560 px).
- The stage splits map to deck at 60 : 40 with an 8 px gutter.
- Below 1100 px wide, the stage stacks with the map at 56vh and the deck at 600 px, and the page scrolls.

---

## 3. Regions

### 3.1 Bar

- **Total gap:** `A −0.420 s to B` at 20/500, coloured gain or loss. It's hidden when there's no lap B.
- **Line pill:** Pedals or Gain/loss. It changes the paint on the map only. Gain/loss is disabled without lap B.
- Laps are chosen in the stint list on the left, not in the bar.

### 3.2 Map: where

- **View:** a flat top view, with a dot grid every 100 m once it's legible.
- **Road:** drawn at real width. The centre comes from Path Lateral and the half-width from Track Edge (5–14 m, 6 m where the channel is missing). Both laps are averaged.
- **Paint on the tarmac:**
  - Pedals: lap A's braking, in red by brake amount.
  - Gain/loss: where the gap changes, using the Δ rate over ±20 m.
  - Each 5 m piece blends from its start shade to its end shade.
- **Driven lines:**
  - Lap A is coloured by pedals: brake red, throttle green, coasting grey.
  - Lap B is grey.
  - Positions are smoothed with a binomial 1-4-6-4-1 filter, two passes. Speeds and pedals are never smoothed.
- **Brake-point lines** run across the road for both laps.
- **Sector lines** are drawn too, with each sector's name and lap A's sector time against lap B's.
- **Cars:** a circle each, lettered A and B and coloured with `--lapA` and `--lapB`. Car A carries the live gap tag.
- **Controls:**
  - Buttons: zoom +/−, fit, follow.
  - Wheel: zooms at the pointer.
  - Drag: pans.
  - Right-drag or Shift-drag: turns the view.
  - Double-click: fits the whole track.
  - Follow: keeps car A pointing up.

### 3.3 Trace deck: why

- **Lanes:** one column of lanes on a shared distance axis, centred on the cursor.
  - Order and base heights: Δ 100, speed 170, throttle 110, brake 110, gear 66. They scale to fit.
  - Gear goes first on short screens.
  - Each lane has a small title and fixed gridlines.
- **Δ:** uses one fixed scale for the whole lap. It's coloured by trend (gain, loss or even) and hidden without lap B.
- **Speed:** lap A is light and lap B grey dashed. Each braking zone's minimum is labelled as A · B in km/h.
- **Throttle and brake:** separate lanes, lines only, lap B grey dashed. Brake points are ticked on the brake lane, white for A and grey for B, so you can see which lap braked deeper.
- **Gear:** a stepped line. Gear 0, a shift in progress, carries the previous gear.
- **Distance axis:**
  - Zoomed in: shows metres from the cursor.
  - Whole lap: shows sector names.
- **Cursor:** one solid line through every lane. Hovering adds a dashed line and a value tag per lane, with A and B side by side.
- **Interaction:**
  - Click or drag: moves the cursor and pauses playback.
  - Wheel: sets the window, from ±50 m to the whole lap. The default is ±200 m.

### 3.4 Transport

- **Live values:** lap A's on the left and lap B's mirrored on the right. Each side shows throttle and brake bars, gear and speed.
- **Play/pause** sits in the middle, and playback runs at real time.
- **Timeline:** time so far, the lap as one bar with S1–S3 marked where lap A crossed each line, then the lap time. Drag it to scrub.

---

## 4. Keys

| Key | Result |
|---|---|
| Space | play / pause |
| ← / → | ∓1 s (Shift ∓0.1 s) |
| 1 · 2 · 3 | deck window ±75 m · ±150 m · whole lap |
| F | follow camera on/off |

---

## 5. Derived data

All of it is computed in the page from the 5 m traces: time, speed, throttle, brake, gear, x/y, Path Lateral and Track Edge.

- **Zones:** each zone starts at lap A's brake onset and ends where lap A is back on full throttle (at least 90 % throttle and under 5 % brake), searching at most 380 m ahead.
  - The zone is padded from 60 m before the onset to 40 m after the pickup.
  - Zones that overlap merge.
- **Brake points:** the 10 % crossing, interpolated between samples. Lap B's onset is matched to lap A's if it lies within 80 m.
- **Δ rate:** the gap's slope over ±20 m, scaled to its 95th percentile. It's used for colour only.
- **No GPS:** lines fall back to the track map's points.

---

## 6. States

- **One lap only:** the Δ lane, the gap, the gap tag, lap B's line and car, and the Gain/loss paint all disappear.
- **Live, before the first lap:** the map shows the text "Laps load when you're back in the garage".

---

## 7. Acceptance checks

- At 1280 × 800, the bar, map, deck and transport are all visible with no scrolling and no overlap.
- Moving the cursor anywhere updates every region in the same frame.
- No text on the page explains how to use it.

---

## 8. Left out of the original proposal

These were in the first proposal and aren't built. Each is a choice to make before it's added.

- **Time ledger:** one row per braking zone, showing brake point, peak, minimum, pickup, exit and Δ, with the biggest effect in bold, sorting by Δ, and click to jump.
- **Lap strip** above the deck, with a draggable window bracket.
- **Mini-sector bars** (24 equal mini-sectors), and a mini-sector timeline in place of the sector bar.
- **Tilted 3D map** with 3D cars and a Tilt pill. The build uses a flat top view instead.
- **Highlights:** a ledger row lighting its stretch of road, and the camera easing to a zone.
- **Keys:** `[` / `]` for the previous and next zone.
- **Steering** in the gear lane.
- **Width breakpoints** (1280 / 1440 / 1920) that change the ledger columns and deck window.
- **Unlabelled lanes.** The build gives each lane a small title instead.
