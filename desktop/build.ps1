# Build the desktop installer: freeze the server, then build the Tauri shell around it.
#   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot
& "$root\.venv\Scripts\python.exe" -m PyInstaller "$PSScriptRoot\autosetup-server.spec" --distpath "$root\build" --workpath "$root\build\pyi" --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Push-Location $PSScriptRoot
try {
    npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    npx tauri build
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
} finally { Pop-Location }
Get-ChildItem "$PSScriptRoot\src-tauri\target\release\bundle\nsis\*.exe" | Select-Object FullName, Length
