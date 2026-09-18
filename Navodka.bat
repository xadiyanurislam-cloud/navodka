@echo off
rem Only ASCII here on purpose: Cyrillic inside a .bat depends on the
rem console code page and turns into garbage on some machines. All the
rem real work and all Russian messages live in launcher.py.
cd /d "%~dp0"
setlocal

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  where python3 >nul 2>&1 && set "PY=python3"
)

if not defined PY (
  echo.
  echo   Python not found / Python ne nayden.
  echo.
  echo   The download page will open now.
  echo   Install Python 3.11 or newer and TICK the checkbox
  echo   "Add python.exe to PATH" at the bottom of the first screen.
  echo   Then run this file again.
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

%PY% launcher.py
if errorlevel 1 (
  echo.
  pause
)
exit /b 0
