param(
  [string]$TaskName = "NanobotDesktop",
  [string]$ExePath = "",
  [string]$Arguments = ""
)

$ErrorActionPreference = "Stop"

function Resolve-DesktopExe {
  param([string]$UserProvided)

  if ($UserProvided) {
    return (Resolve-Path $UserProvided).Path
  }

  $candidates = @(
    ".\desktop-app\src-tauri\target\release\nanobot-desktop.exe",
    "$env:LOCALAPPDATA\Programs\Nanobot Desktop\nanobot-desktop.exe",
    "$env:LOCALAPPDATA\Programs\nanobot-desktop\nanobot-desktop.exe",
    "$env:ProgramFiles\Nanobot Desktop\nanobot-desktop.exe"
  )

  foreach ($p in $candidates) {
    if (Test-Path $p) {
      return (Resolve-Path $p).Path
    }
  }

  throw "nanobot-desktop.exe not found. Please pass -ExePath explicitly."
}

$resolvedExe = Resolve-DesktopExe -UserProvided $ExePath
$command = "`"$resolvedExe`""
if ($Arguments) {
  $command = "$command $Arguments"
}

Write-Host "Registering startup task '$TaskName'..."
Write-Host "Command: $command"

& schtasks /Create /TN $TaskName /SC ONLOGON /TR $command /RL LIMITED /F | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "Failed to create scheduled task '$TaskName'."
}

Write-Host "Startup task created."
& schtasks /Query /TN $TaskName
