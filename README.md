<div align="center">

<img src="desktop/icon.png" width="112" alt="autosetup">

# autosetup

**Setup optimiser for Le Mans Ultimate.**<br>
Drive a few laps and say how the car should feel. 

![version](https://img.shields.io/badge/version-0.4.1-e08a3c?style=flat-square)
![platform](https://img.shields.io/badge/platform-Windows-4a90d9?style=flat-square)
![license](https://img.shields.io/badge/license-Apache%202.0-555?style=flat-square)

<br>

<table>
<tr>
<td><img src="docs/screenshots/setup.png" alt="Setup"></td>
<td><img src="docs/screenshots/lap-analysis.png" alt="Lap Analysis"></td>
</tr>
</table>

</div>

<br>

## Install

1. Download the installer from [Releases](https://github.com/tempora/autosetup/releases/latest) and run it (Windows 10/11; LMU through Steam is found automatically).
2. In LMU, turn telemetry recording on and drive. Stints show up when you're back in the garage.

## DAM plugin (optional)

Adds tyre load and wheel slip from the rF2 DAM MoTeC plugin.

1. Put `DAMPlugin.dll` in `<LMU>\Plugins\`.
2. With LMU closed, enable it in `<LMU>\UserData\player\CustomPluginVariables.JSON`:
   ```json
   "DAMPlugin.dll": { " Enabled": 1 }
   ```
3. Turn logging on in the plugin. `<LMU>\DAMPlugin_Output.txt` should say `Active on startup = true`.

Logs in `<LMU>\LOG\` are matched to your stints automatically.
