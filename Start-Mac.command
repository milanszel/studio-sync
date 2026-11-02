#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3.12 >/dev/null; then
  echo 'Studio Sync needs Python 3.12 for its prebuilt MIDI dependency.'
  echo 'Install the macOS universal2 installer from:'
  echo 'https://www.python.org/downloads/release/python-31210/'
  read -r -p 'Press Return to close.'
  exit 1
fi
if [ ! -x .venv312/bin/python ]; then
  python3.12 -m venv .venv312 || exit 1
fi
# Install only on first launch or when requirements change, so normal launches work offline.
if ! cmp -s requirements.txt .venv312/requirements-installed.txt; then
  if ! .venv312/bin/python -m pip install --only-binary=:all: --no-binary=proxy-tools -r requirements.txt; then
    echo 'Dependency installation failed. Copy the error above for troubleshooting.'
    read -r -p 'Press Return to close.'
    exit 1
  fi
  cp requirements.txt .venv312/requirements-installed.txt
fi
.venv312/bin/python web_app.py
read -r -p 'Press Return to close.'
