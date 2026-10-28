autosetup - portable test build
===============================

Setup optimiser for Le Mans Ultimate, driven by the game's own telemetry.

Run
  1. Unzip anywhere (keep autosetup.exe next to the "server" folder).
  2. Start autosetup.exe. Windows SmartScreen may warn because the build isn't signed:
     "More info" -> "Run anyway".
  3. In LMU, turn telemetry recording on and drive. Finished stints show up in the app
     a few seconds after you return to the garage.

Needs
  - Windows 10/11 with Microsoft Edge WebView2 (built into Windows 11 and up-to-date
    Windows 10; otherwise install "WebView2 Runtime" from Microsoft).
  - Le Mans Ultimate installed through Steam (found automatically).

Where things go
  - Your data (database, server.log): %LOCALAPPDATA%\autosetup
  - Setups autosetup writes: your LMU Settings folder, named autosetup_*.svm. It never
    overwrites a setup it didn't write.

Remove
  Delete this folder, and %LOCALAPPDATA%\autosetup if you want your data gone too.

Problems
  Send %LOCALAPPDATA%\autosetup\server.log along with what you did, before restarting
  the app (it starts a fresh log every launch). It lists slow requests and imports.
