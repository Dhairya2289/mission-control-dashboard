#!/usr/bin/env bash
#
# Mission Control — one-shot installer.
#
# Creates a self-contained virtualenv for the web server (so it survives any
# update to a separate Hermes install) and installs the (tiny) dependency set.
# Idempotent: safe to re-run.
#
# Usage:
#     ./install.sh
#     # then copy .env.example to .env and edit it, and:
#     . .venv/bin/activate && uvicorn main:app --host "${MC_HOST:-127.0.0.1}" --port "${MC_PORT:-51763}"
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV_DIR=".venv"

echo "==> Mission Control installer"
echo "    repo:   $(pwd)"
echo "    python: $("$PYTHON" --version 2>&1)"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating virtualenv at $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
else
  echo "==> Reusing existing virtualenv at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> Upgrading pip"
python -m pip install --upgrade pip >/dev/null

echo "==> Installing requirements"
python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "==> Wrote a starter .env (copied from .env.example) — edit it to taste."
  fi
fi

cat <<EOF

==> Done.

Next steps:
  1. (optional) edit .env to point at your data — see .env.example for every knob.
  2. start the server:
       source $VENV_DIR/bin/activate
       uvicorn main:app --host "\${MC_HOST:-127.0.0.1}" --port "\${MC_PORT:-51763}"
  3. open http://127.0.0.1:51763/

To run it as a background service, see mission-control.service.example.

Note: data-backed tabs (agents, tracker, research, etc.) read from a local
Hermes install. Without one the server still runs; those tabs just show empty
states. Point \$HERMES_HOME at your install to light them up.
EOF
