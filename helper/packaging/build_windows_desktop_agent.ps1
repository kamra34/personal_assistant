param(
  [string]$ProjectPath = ""
)

if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
  $ProjectPath = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}

Set-Location $ProjectPath

$python = Join-Path $ProjectPath ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  Write-Host "Missing Python venv at $python" -ForegroundColor Red
  exit 1
}

& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

& $python -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --name "MeetingAssistantDesktopAgent" `
  --paths "$ProjectPath" `
  --hidden-import "helper.ui_agent" `
  --hidden-import "helper.audio_capture_windows" `
  "$ProjectPath\helper\desktop_agent.py"

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$distExe = Join-Path $ProjectPath "dist\MeetingAssistantDesktopAgent.exe"
$targetDir = Join-Path $ProjectPath "web\public\downloads"
New-Item -ItemType Directory -Force $targetDir | Out-Null

if (Test-Path $distExe) {
  $legacyTarget = Join-Path $targetDir "MeetingAssistantDesktopAgent.exe"
  $versionedTarget = Join-Path $targetDir "MeetingAssistantDesktopAgent-standalone.exe"
  Copy-Item $distExe $legacyTarget -Force
  Copy-Item $distExe $versionedTarget -Force
  Write-Host "Copied exe to web/public/downloads/MeetingAssistantDesktopAgent-standalone.exe" -ForegroundColor Green
} else {
  Write-Host "Built output not found at $distExe" -ForegroundColor Yellow
}
