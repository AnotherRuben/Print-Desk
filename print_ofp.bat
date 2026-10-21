@echo off
setlocal
cd /d "%~dp0"
call "%~dp0tools\find_python.bat"
if errorlevel 1 (
  echo Python isn't installed yet: double-click install.bat first.
  pause
  exit /b 1
)
%PY% printdesk.py ofp
timeout /t 5
