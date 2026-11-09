# Changelog

## 0.4.1

### Fixes
- The Lexus RC F GT3 and Mercedes-AMG GT3 now use their own wheelbases for understeer, instead of the GT3 class's rough figure. Existing recordings are recomputed on the next start.
- If reading the game's live state fails, the app now shows the game as off instead of keeping the last state it saw.
- Removed unused code and a duplicate test.

## 0.4.0

### New look
- Reworked interface: a home screen to continue where you left off, Lap Analysis, Setup and History tabs, and a menu for your car fingerprints, settings and About.
- The Setup screen fits the window and never scrolls: balance, side view, changes on the car and damper charts on the left, Next setup on the right with Write setup always in view.
- Damper charts are drawn to fit and keep readable text at any window size, including fullscreen.
- The feel buttons now read "Front should be" and "Rear should be": Pointier / Lazier and Looser / Safer ask for how the next setup should feel.
- All interface text comes from a language file, ready for translations.

### Setup
- Dampers: moves are scored on kerbs too, and each damper is explored on its own.
- TC map, TC power cut and slip angle each move on their own signal.
- Settings the game fixes for a car are told apart from ones you locked.
- More track names recognised.

### Fixes
- No more double scrollbar next to the Next setup panel.
- Car names on the fingerprint page no longer wrap one word per line.
