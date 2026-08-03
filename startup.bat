@echo off
setlocal
cd /d "%~dp0"

echo === WhereFrom Discord bot ===

if not exist ".env" (
    echo.
    echo ERROR: .env not found.
    echo Copy .env.example to .env and fill in DISCORD_BOT_TOKEN and SERPAPI_KEY.
    goto :error
)

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found - creating it...
    python -m venv .venv
    if errorlevel 1 goto :error
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :error
)

echo Starting bot... (press Ctrl+C to stop)
echo.
".venv\Scripts\python.exe" bot.py
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
