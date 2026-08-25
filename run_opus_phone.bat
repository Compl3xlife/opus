@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.ps1 first.
  exit /b 1
)
echo Starting Opus phone preview. After GitHub Pages is on, the iPhone app does not need this PC.
".venv\Scripts\python.exe" -m opus.phone
