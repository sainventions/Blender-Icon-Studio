@echo off
rem Blender Icon Studio launcher: prepares dependencies, starts the local server and opens the app.
rem Options are passed through to scripts\start.ps1 (e.g. -Port 8430, -NoBrowser, -Rebuild, -Fake).
setlocal
title Blender Icon Studio
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
if errorlevel 1 (
  echo.
  echo Blender Icon Studio could not start - see the messages above.
  pause
)
endlocal
