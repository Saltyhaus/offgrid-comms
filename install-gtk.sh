#!/usr/bin/env bash
# Switch OffGrid Comms to the GTK 4 window (MSI and tablet). Safe to re-run.
#
#   1. GTK 4 + libadwaita for Python (system packages, seen by the venv)
#   2. check the venv's Python can load them
#   3. refresh the app-grid launcher (new window id for the dock icon)
#
# Rollback, any time:   OGC_UI=tk ~/offgrid-comms/run.sh
#   or permanently:      set "ui_toolkit": "tk" in ~/.config/offgrid-comms/config.json
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${VENV:-$HOME/reticulum-venv}"

echo "== 1/3 packages"
sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1

echo "== 2/3 venv check"
if grep -q '^include-system-site-packages = false' "$VENV/pyvenv.cfg"; then
    cp "$VENV/pyvenv.cfg" "$VENV/pyvenv.cfg.bak"
    sed -i 's/^include-system-site-packages = false/include-system-site-packages = true/' "$VENV/pyvenv.cfg"
    echo "   venv now includes system site-packages (backup: pyvenv.cfg.bak)"
fi
"$VENV/bin/python3" - << 'PY'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import cairo  # noqa: F401  (Level Check meter)
v = (Adw.get_major_version(), Adw.get_minor_version())
print(f"   GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, libadwaita {v[0]}.{v[1]}")
if v < (1, 4):
    raise SystemExit("   libadwaita is older than 1.4 -- the app will keep using the Tk window")
PY

echo "== 3/3 launcher"
chmod +x "$APP_DIR/run.sh" "$APP_DIR/main.py"
mkdir -p "$HOME/.local/share/applications"
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/offgrid-comms.desktop" \
    > "$HOME/.local/share/applications/offgrid-comms.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "   launcher updated"

echo
echo "Done. Quit the old window first (tray icon > Quit), then start it from a terminal once:"
echo "    $APP_DIR/run.sh"
echo "Back to the old window:  OGC_UI=tk $APP_DIR/run.sh"
