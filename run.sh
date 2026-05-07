#!/usr/bin/env bash
# One-shot launcher: creates .venv on first run, then starts uvicorn.
set -euo pipefail
cd "$(dirname "$0")"
PYBIN="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "[setup] creating .venv with $PYBIN"
  "$PYBIN" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -e .
fi
exec ./.venv/bin/python -m copilot_dashboard "$@"
