"""Locate the Le Mans Ultimate install and its user-data folders."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LMU_APP_ID = "2399420"
LMU_DIR_NAME = "Le Mans Ultimate"
LMU_EXE = "Le Mans Ultimate.exe"
ENV_OVERRIDE = "AUTOSETUP_LMU_DIR"


@dataclass(frozen=True)
class LmuPaths:
    root: Path

    @property
    def user_data(self) -> Path:
        return self.root / "UserData"

    @property
    def telemetry(self) -> Path:
        return self.user_data / "Telemetry"

    @property
    def settings(self) -> Path:
        """Per-track setup folders: Settings/<Track>/*.svm."""
        return self.user_data / "player" / "Settings"


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            roots.append(Path(winreg.QueryValueEx(key, "SteamPath")[0]))
    except OSError:
        pass
    roots.append(Path(r"C:\Program Files (x86)\Steam"))
    return roots


def _library_folders(steam_root: Path) -> list[Path]:
    """Library paths from libraryfolders.vdf, LMU's library first if it is listed."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return []
    text = vdf.read_text(encoding="utf-8", errors="replace")
    libs: list[tuple[bool, Path]] = []
    # Each library block: "path" "X:\\..." followed by an "apps" block listing app ids.
    for block in re.split(r'\n\t"\d+"\s*\n', text):
        m = re.search(r'"path"\s+"([^"]+)"', block)
        if m:
            path = Path(m.group(1).replace("\\\\", "\\"))
            libs.append((f'"{LMU_APP_ID}"' in block, path))
    libs.sort(key=lambda item: not item[0])
    return [p for _, p in libs]


def game_running() -> bool:
    """Whether Le Mans Ultimate is running (Windows only; False elsewhere)."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {LMU_EXE}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return LMU_EXE.lower() in out.lower()


def find_install() -> LmuPaths:
    """Find the LMU install: $AUTOSETUP_LMU_DIR, then every Steam library."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        root = Path(override)
        if not (root / "UserData").is_dir():
            raise FileNotFoundError(f"{ENV_OVERRIDE}={override} has no UserData folder")
        return LmuPaths(root)
    for steam in _steam_roots():
        for lib in _library_folders(steam):
            root = lib / "steamapps" / "common" / LMU_DIR_NAME
            if (root / "UserData").is_dir():
                return LmuPaths(root)
    raise FileNotFoundError(f"Le Mans Ultimate not found; set {ENV_OVERRIDE} to the install folder")
