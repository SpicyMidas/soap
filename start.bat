@echo off
setlocal
rem One-click start for Windows: just double-click this file.
cd /d "%~dp0"
title Music Bot

rem ---- find Python 3.10+ ----
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython

rem ---- create the virtual environment (first run only) ----
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating a virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 goto :nopython
)

rem ---- settings file ----
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo  A settings file called .env was created and opened in Notepad.
    echo  Paste your bot token after DISCORD_TOKEN= then save and close Notepad.
    echo  After that, double-click start.bat again.
    echo.
    start "" notepad ".env"
    pause
    exit /b 0
)

echo [setup] Installing / updating requirements (quick after the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo  [!] Installing the requirements failed.
    echo      Check your internet connection and that you have Python 3.10 or newer.
    echo.
    pause
    exit /b 1
)

echo [run] Starting the bot. Close this window or press Ctrl+C to stop it.
echo.
".venv\Scripts\python.exe" bot.py
echo.
echo The bot has stopped.
pause
exit /b 0

:nopython
echo.
echo  [!] Python 3.10 or newer was not found.
echo      Download it from https://www.python.org/downloads/
echo      IMPORTANT: tick "Add python.exe to PATH" in the installer.
echo.
pause
exit /b 1
