# Build the desktop installer: freeze the server, then build the Tauri shell around it.
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1 -Flavour debug
# "debug" builds "autosetup debug" from whatever is checked out: it installs and runs beside
# autosetup with its own port (8192) and its own copy of the database, and About names the
# branch and commit it was built from. It runs its server from this repo's .venv while the
# repo is there, so edits to web/ show within seconds and Python edits on the app's next start.
param([ValidateSet("release", "debug")][string]$Flavour = "release")
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot
& "$root\.venv\Scripts\python.exe" -m PyInstaller "$PSScriptRoot\autosetup-server.spec" --distpath "$root\build" --workpath "$root\build\pyi" --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$tauriArgs = @()
$name = "autosetup"
if ($Flavour -eq "debug") {
    $branch = git -C $root rev-parse --abbrev-ref HEAD
    $commit = git -C $root rev-parse --short HEAD
    $dirty = if (git -C $root status --porcelain) { " + uncommitted changes" } else { "" }
    $env:AUTOSETUP_BUILD = "debug $branch $commit$dirty"
    $env:AUTOSETUP_REPO = $root  # it runs the server live from here while the repo exists
    $tauriArgs = @("--features", "debug-build", "--config", "src-tauri\tauri.debug.conf.json")
    $name = "autosetup debug"
}
Push-Location $PSScriptRoot
try {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    npx tauri build @tauriArgs
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
} finally {
    Pop-Location
    Remove-Item Env:AUTOSETUP_BUILD, Env:AUTOSETUP_REPO -ErrorAction SilentlyContinue
}
Get-ChildItem "$PSScriptRoot\src-tauri\target\release\bundle\nsis\$($name)_*.exe" | Select-Object FullName, Length
