@echo off
setlocal
cd /d "%~dp0"
title Print Desk
call "%~dp0tools\find_python.bat"
if errorlevel 1 (
  call "%~dp0install.bat"
  exit /b
)
%PY% -c "import aiohttp, PIL" >nul 2>nul
if errorlevel 1 (
  echo   First start: installing the libraries Print Desk needs...
  %PY% -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo   That didn't work. Run install.bat to see what went wrong.
    pause
    exit /b 1
  )
)
%PY% printdesk.py run
pause
