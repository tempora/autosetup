<div align="center">

<img src="desktop/icon.png" width="112" alt="autosetup">

# autosetup

**Setup optimiser for Le Mans Ultimate.**<br>
Drive a few laps, say how the car should feel, and it writes the next setup into your garage.

![version](https://img.shields.io/badge/version-0.3.0-e08a3c?style=flat-square)
![platform](https://img.shields.io/badge/platform-Windows-4a90d9?style=flat-square)
![game](https://img.shields.io/badge/game-Le%20Mans%20Ultimate-121314?style=flat-square)
![license](https://img.shields.io/badge/license-Apache%202.0-555?style=flat-square)

<br>

<img src="docs/screenshots/setup.png" alt="Setup" width="900">

<table>
<tr>
<td><img src="docs/screenshots/lap-analysis.png" alt="Lap Analysis"></td>
<td><img src="docs/screenshots/next-setup.png" alt="Next setup"></td>
</tr>
<tr>
<td align="center"><sub>Lap Analysis</sub></td>
<td align="center"><sub>Next setup</sub></td>
</tr>
</table>

</div>

<br>

## Install

1. Download the installer from [Releases](https://github.com/tempora/autosetup/releases/latest) and run it (Windows 10/11; LMU through Steam is found automatically).
2. In LMU, turn telemetry recording on and drive. Stints show up when you're back in the garage.
3. **Write setup** saves `autosetup_*.svm` to LMU's Settings folder. Load it in the garage, no restart needed.

## DAM plugin (optional)

Adds tyre load and wheel slip from the rF2 DAM MoTeC plugin.

1. Put `DAMPlugin.dll` in `<LMU>\Plugins\`.
2. With LMU closed, enable it in `<LMU>\UserData\player\CustomPluginVariables.JSON`:
   ```json
   "DAMPlugin.dll": { " Enabled": 1 }
   ```
3. Turn logging on in the plugin. `<LMU>\DAMPlugin_Output.txt` should say `Active on startup = true`.

Logs in `<LMU>\LOG\` are matched to your stints automatically.

## License

Apache 2.0
