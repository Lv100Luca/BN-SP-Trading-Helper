#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment and installing dependencies (one-time)..."
  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python run.py "$@"
