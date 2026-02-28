param(
  [switch]$SkipSidecar
)

$ErrorActionPreference = "Stop"

if (!(Test-Path ".\desktop-app\package.json")) {
  throw "desktop-app not found."
}

if (-not $SkipSidecar) {
  .\scripts\build_backend_sidecar.ps1
}

Push-Location .\desktop-app
try {
  npm install
  npm run build
  npm run tauri:build

  $msiDir = ".\src-tauri\target\release\bundle\msi"
  $msi = Get-ChildItem -Path $msiDir -Filter "*.msi" -ErrorAction SilentlyContinue
  if (-not $msi) {
    throw "Desktop bundle failed: no MSI found under $msiDir"
  }
}
finally {
  Pop-Location
}

Write-Host "Desktop app build completed. MSI: $($msi[0].FullName)"
