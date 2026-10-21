@echo off
setlocal
cd /d "%~dp0"
title Print Desk - install
echo.
echo   Print Desk - installing what it needs
echo   =====================================
echo.
call "%~dp0tools\find_python.bat"
if errorlevel 1 goto nopython
for /f "delims=" %%v in ('%PY% --version') do echo   Found %%v
echo   Installing the libraries Print Desk uses (aiohttp, Pillow, pywin32)...
echo.
%PY% -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pipfail
echo.
echo   All set. Double-click start.bat to run Print Desk.
echo.
pause
exit /b 0

:nopython
echo   Python 3.11 or newer isn't installed yet (or Windows can't find it).
echo.
where winget >nul 2>nul
if errorlevel 1 goto manual
choice /c YN /m "  Install Python 3.12 now with winget, Windows' own installer"
if errorlevel 2 goto manual
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 goto manual
echo.
echo   Python is installed. Close this window and double-click install.bat again:
echo   Windows only finds a newly installed Python in a new window.
echo.
pause
exit /b 1

:manual
echo.
echo   Install Python yourself:
echo     1. Go to https://www.python.org/downloads/windows/ (opening it now)
echo     2. Download the latest "Windows installer (64-bit)"
echo     3. In the installer, tick "Add python.exe to PATH", then click "Install Now"
echo     4. Double-click install.bat again
start "" https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:pipfail
echo.
echo   Installing the libraries didn't work. Check the internet connection and run
echo   install.bat again. If it keeps failing, copy the text above when asking for help.
echo.
pause
exit /b 1
