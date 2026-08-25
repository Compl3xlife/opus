@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.ps1 first.
  exit /b 1
)
start "Opus" ".venv\Scripts\pythonw.exe" -m opus
