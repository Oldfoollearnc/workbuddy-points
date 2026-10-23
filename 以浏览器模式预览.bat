@echo off
rem Browser preview fallback: open the full panel in Edge app mode.
rem No tray / no local file access in this mode - data shown is demo data.
setlocal
cd /d "%~dp0"

set "PAGE=file:///%CD:\=/%/index.html"
set "EDGE1=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE2=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"

if exist "%EDGE1%" (
  start "" "%EDGE1%" --app="%PAGE%" --window-size=1440,920
  exit /b 0
)
if exist "%EDGE2%" (
  start "" "%EDGE2%" --app="%PAGE%" --window-size=1440,920
  exit /b 0
)

start "" "%PAGE%"
exit /b 0
