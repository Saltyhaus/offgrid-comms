#!/usr/bin/env python3
"""OffGrid Comms entry point.

Picks the window toolkit:
  * GTK 4 / libadwaita (gtk_app.py) -- the default; native Wayland, touch
  * Tk (app.py)                       -- the old window, kept as a rollback

Choose with OGC_UI=gtk|tk in the environment, or "ui_toolkit": "gtk"|"tk"
in config.json. If GTK 4 / libadwaita >= 1.4 isn't installed the Tk window
starts instead, with a note on stdout.
"""

import os
import sys

# run relative to this file so assets/ and the venv-relative paths in
# config.py resolve the same way whether launched from a terminal or a
# .desktop file
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _wanted_toolkit():
    env = (os.environ.get("OGC_UI") or "").strip().lower()
    if env in ("gtk", "tk"):
        return env
    try:
        import config as cfgmod
        val = (cfgmod.load_config().get("ui_toolkit") or "").strip().lower()
        if val in ("gtk", "tk"):
            return val
    except Exception as e:      # a broken config is the app's problem to report, not ours
        print(f"[main] couldn't read ui_toolkit from config: {e}", flush=True)
    return "gtk"


def main():
    if _wanted_toolkit() == "gtk":
        try:
            import gtk_app
            if not gtk_app.adw_ok():
                raise ImportError("libadwaita %d.%d is older than 1.4" % (
                    gtk_app.Adw.get_major_version(), gtk_app.Adw.get_minor_version()))
        except (ImportError, ValueError) as e:
            print(f"[main] GTK 4 UI unavailable ({e}); starting the Tk UI. "
                  "Install: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1", flush=True)
        else:
            sys.exit(gtk_app.run())
    from app import run
    run()


if __name__ == "__main__":
    main()
