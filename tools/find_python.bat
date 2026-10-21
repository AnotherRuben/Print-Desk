@echo off
rem Finds Python 3.11 or newer and puts the command in PY. Exit code 1 when there is none.
rem The "py" launcher comes with the python.org installer; "python" is the fallback.
rem (The Microsoft Store shortcut that only opens the Store fails the check, as it should.)
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
if not defined PY exit /b 1
exit /b 0
