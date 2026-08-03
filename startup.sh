#!/usr/bin/env bash
# Start the WhereFrom Discord bot, creating the venv on first run.
set -euo pipefail

cd "$(dirname "$0")"

# ./startup.sh -v | --verbose  -> debug logging + unmuted pip output
PIP_QUIET="--quiet"
if [ "${1:-}" = "-v" ] || [ "${1:-}" = "--verbose" ]; then
    export LOG_LEVEL=DEBUG
    PIP_QUIET=""
    echo "(verbose mode: LOG_LEVEL=DEBUG)"
fi

echo "=== WhereFrom Discord bot ==="

if [ ! -f .env ]; then
    echo
    echo "ERROR: .env not found."
    echo "Copy .env.example to .env and fill in DISCORD_BOT_TOKEN and SERPAPI_KEY."
    exit 1
fi

# Windows venvs use Scripts/, POSIX venvs use bin/.
if [ -x ".venv/Scripts/python.exe" ]; then
    PYTHON=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
else
    echo "Virtual environment not found - creating it..."
    python3 -m venv .venv 2>/dev/null || python -m venv .venv
    if [ -x ".venv/Scripts/python.exe" ]; then
        PYTHON=".venv/Scripts/python.exe"
    else
        PYTHON=".venv/bin/python"
    fi
    echo "Installing dependencies..."
    "$PYTHON" -m pip install $PIP_QUIET --upgrade pip
    "$PYTHON" -m pip install $PIP_QUIET -r requirements.txt
fi

echo "Starting bot... (press Ctrl+C to stop)"
echo
exec "$PYTHON" bot.py
