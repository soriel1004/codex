@echo off
if /i not "%~1"=="--minimized" (
  start "AS Server" /min "%ComSpec%" /d /c ""%~f0" --minimized"
  exit /b
)
pushd "%~dp0"
py -3 launcher.py --server --open-browser
if errorlevel 1 pause
popd
