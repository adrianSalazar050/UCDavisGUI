# Build the Windows installer for Bambu Monitor.
#
# Run from the REPO ROOT in PowerShell:
#     powershell -ExecutionPolicy Bypass -File desktop\build-windows.ps1
#
# Produces: desktop\release\Bambu Monitor Setup <version>.exe
#
# Prereqs on this machine: Python 3.11+, Node 18+, npm. No admin needed.
$ErrorActionPreference = "Stop"

# Resolve the repo root as this script's parent directory, so the script works
# regardless of the caller's current directory.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "== Repo root: $RepoRoot" -ForegroundColor Cyan

# 1. Build the React frontend -> frontend/dist
Write-Host "`n== [1/4] Building frontend" -ForegroundColor Cyan
Push-Location frontend
npm ci
npm run build
Pop-Location

# 2. Install the frozen-build Python deps + PyInstaller
Write-Host "`n== [2/4] Installing backend build dependencies" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements-desktop.txt pyinstaller

# 3. Freeze the backend -> dist/bambu-backend/
Write-Host "`n== [3/4] Freezing backend with PyInstaller" -ForegroundColor Cyan
if (Test-Path build\bambu-backend) { Remove-Item -Recurse -Force build\bambu-backend }
if (Test-Path dist\bambu-backend)  { Remove-Item -Recurse -Force dist\bambu-backend }
pyinstaller --noconfirm --clean desktop\bambu-backend.spec

if (-not (Test-Path "dist\bambu-backend\bambu-backend.exe")) {
    throw "PyInstaller did not produce dist\bambu-backend\bambu-backend.exe"
}

# 4. Package the Electron app -> desktop/release/*.exe
Write-Host "`n== [4/4] Building Electron installer" -ForegroundColor Cyan
Push-Location desktop
npm ci
npx electron-builder --win
Pop-Location

Write-Host "`n== Done. Installer(s):" -ForegroundColor Green
Get-ChildItem desktop\release\*.exe | ForEach-Object { Write-Host "   $($_.FullName)" }
