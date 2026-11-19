#!/usr/bin/env bash
# One-click start for macOS / Linux:  ./start.sh
set -e
cd "$(dirname "$0")"

PY=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[!] Python 3.10 or newer was not found."
  echo "    macOS: https://www.python.org/downloads/   Linux: sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[setup] Creating a virtual environment with $PY..."
  "$PY" -m venv .venv || {
    echo "[!] Couldn't create the virtual environment. On Debian/Ubuntu run: sudo apt install python3-venv"
    exit 1
  }
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "  A settings file called .env was created."
  echo "  Open it, paste your bot token after DISCORD_TOKEN= and run ./start.sh again."
  echo
  exit 0
fi

echo "[setup] Installing / updating requirements (quick after the first time)..."
.venv/bin/python -m pip install --upgrade pip --quiet --disable-pip-version-check
.venv/bin/python -m pip install --upgrade -r requirements.txt --quiet --disable-pip-version-check

echo "[run] Starting the bot. Press Ctrl+C to stop it."
exec .venv/bin/python bot.py
