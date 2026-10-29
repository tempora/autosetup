autosetup 0.3.1
===============

Setup optimiser for Le Mans Ultimate: the fastest setup you can actually drive, from a
few laps per run. You say how the car should feel; it finds the settings.

Install
  1. Run autosetup_0.3.1_x64-setup.exe. It installs for your Windows user only (no
     admin rights). Windows SmartScreen may warn because the build isn't signed:
     "More info" -> "Run anyway".
  2. Start autosetup from the Start menu.
  3. In LMU, turn telemetry recording on and drive. Finished stints show up a few
     seconds after you return to the garage.

Use
  - Setup tab: how the car is balanced against your target, what the next setup
    changes, the predicted lap time, the suspension histograms.
  - Next setup: the changes, then the feel buttons. Looser / Safer for the rear,
    Pointier / Lazier for the front. They move the target, not the settings. When the
    car feels right, press "This is it". Write setup saves the file into LMU's Settings
    folder; load it in the garage, no restart needed.

Needs
  - Windows 10/11. Microsoft Edge WebView2 is downloaded by the installer if missing.
  - Le Mans Ultimate installed through Steam (found automatically).

Where things go
  - The app and your data (database, server.log): %LOCALAPPDATA%\autosetup
  - Setups autosetup writes: your LMU Settings folder, named autosetup_*.svm. It never
    overwrites a setup it didn't write.

Updating from 0.2
  Install over it. Your laps are kept and recomputed on the first start (about a
  minute, in the background). Ratings and car-feel choices from 0.2 aren't used any
  more: the feel buttons replace them.

Remove
  Settings -> Apps -> autosetup -> Uninstall. Delete %LOCALAPPDATA%\autosetup if you
  want your data gone too.

Problems
  Send %LOCALAPPDATA%\autosetup\server.log along with what you did, before restarting
  the app (it starts a fresh log every launch).
