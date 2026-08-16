#!/bin/bash
# Set up the card cataloguer on macOS.
# Double-click this file in Finder, or run: bash setup_mac.command

set -u
cd "$(dirname "$0")"

echo
echo "  Card Cataloguer - macOS setup"
echo "  =============================="
echo

PY=""
for candidate in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version=$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
        major=${version%%.*}
        minor=${version##*.}
        if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 9 ]; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    echo "  No Python 3.9+ found."
    echo "  Install it from https://www.python.org/downloads/ and run this again."
    echo
    read -r -p "  Press return to close. "
    exit 1
fi

echo "  Using $PY ($("$PY" --version 2>&1))"
echo

echo "  Installing dependencies..."
"$PY" -m pip install --upgrade pip >/dev/null 2>&1
if ! "$PY" -m pip install -r requirements.txt; then
    echo
    echo "  Dependency install failed. If it is a permissions problem, try:"
    echo "     $PY -m pip install --user -r requirements.txt"
    echo
    read -r -p "  Press return to close. "
    exit 1
fi

# tkinter is not always bundled with the python.org build
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo
    echo "  tkinter is missing - the graphical interface needs it."
    if command -v brew >/dev/null 2>&1; then
        echo "  Installing with Homebrew..."
        brew install python-tk || true
    else
        echo "  Install Homebrew (https://brew.sh) then run:  brew install python-tk"
        echo "  The terminal version (tools/catalogue.py) works without it."
    fi
fi

echo
echo "  Checking everything..."
echo
"$PY" tools/doctor.py

echo
echo "  To start:   $PY tools/gui.py"
echo
read -r -p "  Press return to close. "
