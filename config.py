"""
OffGrid Comms - configuration

All persistent settings live in ~/.config/offgrid-comms/config.json.
A default config is written the first time the app runs. Edit the JSON
by hand (or through the app's Settings, once you add one) to fill in
your real serial device paths -- the placeholders below WILL NOT WORK
until you point them at your actual radios.

To find a stable device path for each radio, plug it in one at a time and run:

    ls -l /dev/serial/by-id/

Use the by-id path (not /dev/ttyUSB0) so it doesn't shift around if you
plug things in a different order.
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "offgrid-comms"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Hamlib model numbers, confirmed current (Hamlib 4.7.1):
#   FT-991A  -> 1035
#   KX3      -> 2045
#   TX-500   -> 2050
DEFAULT_CONFIG = {
    "callsign": "N0CALL",     # your callsign, no SSID (modem73 won't take one)
    "venv_bin": str(Path.home() / "reticulum-venv" / "bin"),
    "meshchatx": {
        "host": "0.0.0.0",
        "port": 8000,
        "storage_dir": str(Path.home() / ".local" / "share" / "meshchatx"),
        # MeshChatX serves its own self-signed TLS cert by default -- the
        # "Open MeshChatX" button needs https:// or the browser just gets
        # a connection reset. Set to "http" if you've reconfigured it plain.
        "scheme": "https",
    },
    "modem73": {
        "binary": "modem73",
        "control_host": "127.0.0.1",
        "control_port": 8073,
        "kiss_port": 8001,
    },
    # The Anytone AT-D890UV's built-in Bluetooth SPP KISS TNC. This does NOT
    # go through modem73 or rigctld -- the radio does its own AFSK/KISS
    # framing in firmware. Reticulum's KISSInterface (already defined in
    # ~/.reticulum/config) just needs /dev/rfcommN to exist; rnsd's own
    # interface-reconnect logic picks it up once it's bound, no restart
    # needed. `rfcomm bind`/`release` require root -- see the sudoers note
    # in bt_kiss.py for running this without a password prompt each time.
    "anytone": {
        "mac": "00:00:00:00:00:00",      # your radio's Bluetooth address: bluetoothctl devices
        "channel": 2,
        "device": "rfcomm0",
        "rfcomm_bin": "/usr/bin/rfcomm",
    },
    "radios": {
        "FT-991A": {
            "hamlib_model": 1035,
            "device": "/dev/serial/by-id/CHANGE_ME_FT991A",
            "baud": 38400,
            "rigctld_port": 4532,
            "audio_device": None,
            # the FT-991A's built-in USB sound card ("USB Audio CODEC")
            "mercury_audio": "plughw:CARD=CODEC,DEV=0",
            # Matches the proven hamshack rigctld.service for this same radio --
            # the FT-991A's CP2105 CAT interface needs hardware handshake.
            "rigctld_extra_args": ["-C", "stop_bits=1,serial_handshake=Hardware"],
        },
        "KX3": {
            "hamlib_model": 2045,
            "device": "/dev/serial/by-id/CHANGE_ME_KX3",
            "baud": 38400,
            "rigctld_port": 4534,
            "audio_device": None,
            "rigctld_extra_args": [],
        },
        "TX-500": {
            "hamlib_model": 2050,
            "device": "/dev/serial/by-id/CHANGE_ME_TX500",
            "baud": 38400,
            "rigctld_port": 4536,
            "audio_device": None,
            # No precedent for this rig yet -- starting plain like the KX3.
            # Add stop_bits/serial_handshake tuning here if CAT proves flaky.
            "rigctld_extra_args": [],
        },
    },
    # Mode presets sent live over modem73's control port via set_config.
    # modem_type: 0=OFDM, 1=MFSK, 2=ROBUST(RDM)
    # robust_mode numbering (see CONTROL_PORT.md get_config):
    #   0 RDM-1200  1 RDM-600  2 RDM-300  3 RDMN-300  4 RDMN-150  10 RDM-800
    "modes": {
        "Fast": {
            "description": "Best conditions, highest throughput",
            "modem_type": 0,
            "modulation": "QAM64",
            "code_rate": "3/4",
        },
        "Normal": {
            "description": "Everyday default",
            "modem_type": 0,
            "modulation": "QPSK",
            "code_rate": "1/2",
        },
        "Robust": {
            "description": "NVIS / fading HF path",
            "modem_type": 2,
            "robust_mode": 1,  # RDM-600
        },
        "Emergency": {
            "description": "Weakest signal, narrowest bandwidth",
            "modem_type": 2,
            "robust_mode": 4,  # RDMN-150
        },
    },
    # Mercury HF modem + mercury-connector: HF mail to/from the hamshack.
    # mycall is this machine's Mercury identity (e.g. N0CALL-2;
    # install.sh fills it in from MERCURY_CALL). It can carry an SSID because
    # Mercury, unlike modem73, accepts one. peers = who the connector
    # calls when there's mail queued (home). field_stations = the
    # portable stations that home relays to via the "to:" header.
    # audio_device is the ALSA device used unless a radio sets its own
    # "mercury_audio" -- use "Sound cards" in the panel to list them.
    "mercury": {
        "mycall": None,
        "peers": [],                  # home station(s) the connector calls, e.g. ["N0CALL"]
        "field_stations": [],         # your other portables, e.g. ["N0CALL-2", "N0CALL-3"]
        "binary": "mercury",
        "connector_binary": str(Path.home() / "mercury-connector" / "mercury-connector"),
        # home's published status (e.g. the hf-contact.json its website serves),
        # to check this Mercury matches home's version. Empty = no check.
        "home_status_url": "",
        "audio_device": "plughw:CARD=Device,DEV=0",
        "base_port": 8300,
        "extra_args": ["-G"],
        "retry_wait": None,
        "outgoing_dir": str(Path.home() / "hf_spool" / "outgoing"),
        "incoming_dir": str(Path.home() / "hf_spool" / "incoming"),
        # copies of what we queued, so the panel shows both sides of the chat
        "sent_dir": str(Path.home() / "hf_spool" / "sent"),
        "workdir": str(CONFIG_DIR / "mercury"),
        "startup_timeout": 20,
        # hf_sync's dashboard only accepts known categories, so keep this
        # "status" unless the hamshack database gains new ones
        "category": "status",
        "attach_gps": True,
        "quick_messages": ["All well", "On the move", "Arrived at camp", "Running late, all OK"],
    },
    # While Mercury or modem73 is running, block automatic suspend so a
    # transmission or ARQ session isn't cut off. Lid close still suspends.
    "keep_awake_while_hf": True,
    "last_radio": None,
    "last_mode": "Normal",
    "window": {"always_on_top": False},
    # Screen profile. "auto" picks "touch" when a touchscreen is present
    # (the FZ-M1 tablet) and "desktop" otherwise (the MSI laptop), so the
    # same code and config shape work on both. "scale" overrides the
    # touch profile's size multiplier (null = 1.75); bump it up or down
    # if text is too small/large on a given screen.
    # gtk_scale: extra text size for the GTK window on top of GNOME's own
    # scaling (null = 1.2 on touch, 1.0 on desktop). scale is the Tk one.
    "ui": {"profile": "auto", "scale": None, "gtk_scale": None},
    # Which window to open: "gtk" (GTK 4, native Wayland + touch) or "tk"
    # (the original). OGC_UI=gtk|tk in the environment overrides this.
    "ui_toolkit": "gtk",
}


def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))

    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)

    # backfill any keys added in a newer version of this app
    def merge(defaults, current):
        for k, v in defaults.items():
            if k not in current:
                current[k] = v
            elif isinstance(v, dict) and isinstance(current[k], dict):
                merge(v, current[k])
        return current

    return merge(json.loads(json.dumps(DEFAULT_CONFIG)), cfg)


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)
