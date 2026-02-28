param(
  [string]$TaskName = "NanobotDesktop"
)

$ErrorActionPreference = "Stop"

Write-Host "Removing startup task '$TaskName'..."
& schtasks /Delete /TN $TaskName /F | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "Failed to delete scheduled task '$TaskName'."
}
Write-Host "Startup task removed."
