@echo off
setlocal
cd /d "%~dp0"

set "VPY=.venv\Scripts\python.exe"
set "PIPQUIET=--quiet"

REM startup.bat -v  /  --verbose  -> debug logging + unmuted pip output
if /i "%~1"=="-v" goto :verbose
if /i "%~1"=="--verbose" goto :verbose
goto :noverbose
:verbose
set "LOG_LEVEL=DEBUG"
set "PIPQUIET="
echo (verbose mode: LOG_LEVEL=DEBUG)
:noverbose

echo === WhereFrom Discord bot ===

if not exist ".env" (
    echo.
    echo ERROR: .env not found.
    echo Copy .env.example to .env and fill in DISCORD_BOT_TOKEN and SERPAPI_KEY.
    goto :error
)

if not exist "%VPY%" (
    echo Virtual environment not found - creating it...
    python -m venv .venv
)

REM Some Python installs (notably the Microsoft Store build) fail during
REM ensurepip and leave no interpreter behind. Retry without bundled pip.
if not exist "%VPY%" (
    echo Standard venv creation failed - retrying without bundled pip...
    python -m venv --without-pip .venv
)

if not exist "%VPY%" (
    echo.
    echo ERROR: could not create a virtual environment.
    echo.
    echo Check which Python you have:
    echo     python -c "import sys; print(sys.executable)"
    echo.
    echo If that path contains "WindowsApps", you are on the Microsoft Store
    echo build of Python, which cannot reliably create venvs. Install Python
    echo from https://www.python.org/downloads/ instead, ticking
    echo "Add python.exe to PATH" during setup.
    goto :error
)

REM Make sure pip actually works inside the venv.
"%VPY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Repairing pip inside the virtual environment...
    "%VPY%" -m ensurepip --upgrade >nul 2>&1
)

"%VPY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo Bootstrapping pip from bootstrap.pypa.io...
    powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri https://bootstrap.pypa.io/get-pip.py -OutFile get-pip.py } catch { exit 1 }"
    if errorlevel 1 (
        echo ERROR: could not download get-pip.py - check your internet connection.
        goto :error
    )
    "%VPY%" get-pip.py
    del /q get-pip.py >nul 2>&1
)

"%VPY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: pip is still unavailable in the virtual environment.
    goto :error
)

REM Install deps if discord.py is missing (cheap no-op on later runs).
"%VPY%" -c "import discord" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    "%VPY%" -m pip install %PIPQUIET% --upgrade pip
    "%VPY%" -m pip install %PIPQUIET% -r requirements.txt
    if errorlevel 1 (
        echo ERROR: dependency installation failed.
        goto :error
    )
)

echo Starting bot... (press Ctrl+C to stop)
echo.
"%VPY%" bot.py
if errorlevel 1 goto :error

echo.
echo Bot stopped normally.
pause
exit /b 0

:error
echo.
echo Startup failed - see the message above.
pause
exit /b 1
