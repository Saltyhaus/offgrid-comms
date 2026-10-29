#!/usr/bin/env bash
# Wrapper script for the .desktop launcher -- activates the venv and starts
# the app, matching the pattern used for the amp monitor and Shack Services.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${VENV:-$HOME/reticulum-venv}/bin/activate"
exec python3 "$DIR/main.py"
