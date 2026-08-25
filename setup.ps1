param(
  [switch]$Startup
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) {
  throw "Python 3.12 was not found at $Python"
}

Write-Host "Creating virtual environment..."
& $Python -m venv .venv
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt
& $VenvPython -c "from opus.ui.icons import ensure_icons; ensure_icons()"

if ($Startup) {
  $StartupFolder = [Environment]::GetFolderPath("Startup")
  $ShortcutPath = Join-Path $StartupFolder "Opus.lnk"
  $Wsh = New-Object -ComObject WScript.Shell
  $Shortcut = $Wsh.CreateShortcut($ShortcutPath)
  $Shortcut.TargetPath = Join-Path $Root "run_opus.bat"
  $Shortcut.WorkingDirectory = $Root
  $Shortcut.Save()
  Write-Host "Added Opus to Windows startup."
}

Write-Host "Done. Start Opus with .\run_opus.bat"
