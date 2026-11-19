@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -B app.py --open
) else (
  python -B app.py --open
)
if errorlevel 1 pause
