@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install Python from https://www.python.org/downloads/
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8000"
py -3 app.py
pause
