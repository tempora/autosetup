# pitwall

Telemetry ingestion, four-wheel quasi-steady-state mechanical physics, and
holdout validation for Le Mans Ultimate (LMU) `.duckdb` recordings.

`pitwall` reads LMU recordings directly, parses flat `CarSetup` metadata into
normalized `[0, 1]` setup coordinates, and evaluates steady-state cornering
grip and balance from lateral load transfer distribution, nonlinear tyre load
sensitivity, aero balance shift, and phase-dependent differential locking.
Physics models are graded on holdout pairs `(stint A, stint B)` from the same
car and layout by calibrating on A and scoring predicted deltas against B's
measured telemetry.

## Conventions

Per-wheel channels and arrays follow LMU's `FL, FR, RL, RR` order.  Understeer
angle is in road-wheel degrees (`+` understeer, `-` oversteer), shaft velocity
in `mm/s` (`+` bump, `-` rebound), track curvature in `1/m` (`+` right-hand
turn), and rake as `rh_rear - rh_front` (`+` nose-down).  Discrete channels are
resampled with zero-order hold; continuous channels use linear interpolation.

## Testing

```powershell
cargo test -p pitwall --no-default-features
cargo test -p pitwall
```
