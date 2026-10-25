#!/usr/bin/env bash
# FileHunter launcher for Linux (and macOS from a terminal).
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "Python 3.9+ is needed: install python3 from your package manager."; exit 1; }

if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "Tkinter is missing. Install it first, for example:"
    echo "  Ubuntu/Debian: sudo apt install python3-tk python3-venv"
    echo "  Fedora:        sudo dnf install python3-tkinter"
    echo "  Arch:          sudo pacman -S tk"
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "First start: preparing FileHunter (only once)..."
    "$PY" -m venv .venv || { echo "Install python3-venv first (sudo apt install python3-venv)."; exit 1; }
    .venv/bin/python -m pip install --quiet --upgrade pip || true
    .venv/bin/python -m pip install --quiet -r requirements.txt || echo "(Pillow not installed: photo previews limited to PNG/GIF)"
fi
exec .venv/bin/python filehunter.py "$@"
