@echo off
rem Print Desk commands from a command prompt in this folder, e.g.:
rem   printdesk test all
rem   printdesk picture photo.jpg --compare
rem   printdesk ofp
setlocal
cd /d "%~dp0"
call "%~dp0tools\find_python.bat"
if errorlevel 1 (
  echo Python isn't installed yet: double-click install.bat first.
  exit /b 1
)
%PY% printdesk.py %*
