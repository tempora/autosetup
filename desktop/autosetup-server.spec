# PyInstaller spec for the server the desktop app bundles. One folder, not one file: it
# starts in a second instead of unpacking ~100 MB on every launch, and stays one process.
#
#   .venv\Scripts\python -m PyInstaller desktop\autosetup-server.spec --distpath build --workpath build\pyi --noconfirm
from pathlib import Path

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "desktop" / "server_main.py")],
    pathex=[str(root)],
    datas=[
        (str(root / "web" / "index.html"), "web"),  # server.WEB_DIR: <bundle>/web
        (str(root / "autosetup" / "knowledge" / "classes.toml"), "autosetup/knowledge"),
    ],
    excludes=["tkinter", "_tkinter", "pytest", "IPython", "matplotlib", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="autosetup-server",
          console=True,  # stdio works; the shell hides the console window
          upx=False)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="autosetup-server")
