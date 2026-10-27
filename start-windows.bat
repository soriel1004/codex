@echo off
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install Python from https://www.python.org/downloads/
  pause
  exit /b 1
)
if /i not "%~1"=="--minimized" (
  start "AS Slip Manager" /min "%ComSpec%" /d /c ""%~f0" --minimized"
  exit /b
)
py -3 launcher.py --open-browser
if errorlevel 1 pause
popd
