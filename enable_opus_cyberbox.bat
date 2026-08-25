@echo off
cd /d "%~dp0"
echo Enabling Windows Sandbox for Opus. Approve the UAC prompt.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0enable_windows_sandbox.ps1'"
echo.
type "%~dp0sandbox-enable.log"
echo.
pause

