@echo off
title Cafe POS - keep this window open while the cafe is running
cd /d "%~dp0"

set "PY="
python -c "" >nul 2>&1 && set "PY=python"
if not defined PY (py -3 -c "" >nul 2>&1 && set "PY=py -3")

if not defined PY (
  echo Python is not installed. Installing it now - one time only, needs internet...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set PY="%%D\python.exe"
)

if not defined PY (
  echo.
  echo Could not install Python automatically.
  echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then double-click this file again.
  pause
  exit /b 1
)

%PY% app.py %*
pause
