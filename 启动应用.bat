@echo off
rem WorkBuddy Points Insight - launcher (tray only by default)
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PY%" (
  start "" "%PY%" "%~dp0desktop.py"
  exit /b 0
)

where pythonw >nul 2>nul
if errorlevel 1 (
  echo [WorkBuddy Points] Python not found.
  echo Install Python 3.9+ and run: pip install pywebview pillow
  echo Or double-click the browser preview script.
  pause
  exit /b 1
)

start "" pythonw "%~dp0desktop.py"
exit /b 0
