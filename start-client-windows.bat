@echo off
if /i not "%~1"=="--minimized" (
  start "AS Client" /min "%ComSpec%" /d /c ""%~f0" --minimized"
  exit /b
)
where py >nul 2>nul
if errorlevel 1 (
  if exist "%~dp0AS-client.url" (
    start "" "%~dp0AS-client.url"
    exit /b
  )
  echo Run setup-server-windows.bat on the server PC first.
  pause
  exit /b 1
)
pushd "%~dp0"
py -3 launcher.py --client
if errorlevel 1 pause
popd
