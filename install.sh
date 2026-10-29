#!/usr/bin/env bash
# Install OffGrid Comms on a Debian/Ubuntu machine. Safe to re-run.
#
#   MERCURY_CALL=N0CALL-2 ./install.sh
#
#  1. GTK 4 + libadwaita (and Tk for the fallback window)
#  2. the Python venv the app and Reticulum run from (~/reticulum-venv)
#  3. your callsign in ~/.config/offgrid-comms/config.json
#  4. a launcher in the app grid
#
# Mercury, mercury-connector, Hamlib (rigctld) and modem73 are separate
# installs -- see the README.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/reticulum-venv}"
CFG="$HOME/.config/offgrid-comms/config.json"

if [ -z "${MERCURY_CALL:-}" ]; then
    echo "Set your Mercury callsign for this machine first, with an SSID, e.g.:"
    echo "    MERCURY_CALL=N0CALL-2 $0"
    exit 1
fi
MERCURY_CALL=$(echo "$MERCURY_CALL" | tr '[:lower:]' '[:upper:]')
BASE_CALL=${MERCURY_CALL%%-*}

echo "== 1/4 packages"
sudo apt-get install -y python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-tk

echo "== 2/4 venv ($VENV)"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
if grep -q '^include-system-site-packages = false' "$VENV/pyvenv.cfg"; then
    cp "$VENV/pyvenv.cfg" "$VENV/pyvenv.cfg.bak"
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
    echo "   venv now sees system packages (GTK); backup: pyvenv.cfg.bak"
fi
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
"$VENV/bin/pip" install --quiet rns lxmf
"$VENV/bin/python3" -c 'import gi; gi.require_version("Adw", "1"); from gi.repository import Adw; print(f"   libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()} (1.4+ for the GTK window, otherwise Tk)")'

echo "== 3/4 config"
mkdir -p "$HOME/hf_spool/outgoing" "$HOME/hf_spool/incoming" "$HOME/hf_spool/sent"
"$VENV/bin/python3" - "$APP_DIR" "$CFG" "$BASE_CALL" "$MERCURY_CALL" << 'PY'
import json, os, sys
app_dir, path, base, call = sys.argv[1:]
sys.path.insert(0, app_dir)
import config
cfg = config.load_config()                      # writes the defaults the first time
if cfg.get("callsign") in (None, "", "N0CALL"):
    cfg["callsign"] = base                      # modem73: no SSID
merc = cfg.setdefault("mercury", {})
if not merc.get("mycall"):
    merc["mycall"] = call
config.save_config(cfg)
print(f"   callsign {cfg['callsign']}, Mercury {merc['mycall']} -> {path}")
PY

echo "== 4/4 launcher"
chmod +x "$APP_DIR/run.sh" "$APP_DIR/main.py"
mkdir -p "$HOME/.local/share/applications"
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/offgrid-comms.desktop" \
    > "$HOME/.local/share/applications/offgrid-comms.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo
echo "Done. Next: edit $CFG"
echo "  radios.<name>.device   your radio's /dev/serial/by-id/... path"
echo "  mercury.peers          the home station your connector calls"
echo "Then run it once from a terminal to see any errors:  $APP_DIR/run.sh"
