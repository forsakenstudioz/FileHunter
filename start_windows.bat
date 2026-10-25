@echo off
title FileHunter
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

if not exist ".venv\Scripts\pythonw.exe" (
    echo First start: preparing FileHunter. This takes a minute, only once...
    %PY% -m venv .venv || goto nopython
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
)
start "" ".venv\Scripts\pythonw.exe" filehunter.py
exit /b 0

:nopython
echo.
echo Python 3.9 or newer is needed.
echo Download it from https://www.python.org/downloads/
echo During setup, tick "Add python.exe to PATH", then double-click this file again.
echo.
pause
