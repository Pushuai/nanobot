param(
  [string]$PythonExe = ".\.venv\Scripts\python.exe",
  [string]$OutDir = ".\desktop-app\resources\backend"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path $PythonExe)) {
  throw "Python not found: $PythonExe"
}

Write-Host "Using python: $PythonExe"
& $PythonExe -m pip install --upgrade pip pyinstaller

$workDir = ".\.pyi\work"
$specDir = ".\.pyi\spec"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path $workDir | Out-Null
New-Item -ItemType Directory -Force -Path $specDir | Out-Null

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name nanobot-desktop-backend `
  --distpath $OutDir `
  --workpath $workDir `
  --specpath $specDir `
  scripts\desktop_sidecar_entry.py

$backendExe = Join-Path $OutDir "nanobot-desktop-backend.exe"
if (!(Test-Path $backendExe)) {
  throw "Sidecar build failed: $backendExe not found"
}

Write-Host "Sidecar built: $backendExe"
