"""
OffGrid Comms - main application window + system tray integration.

Dark theme with teal/orange digital-readout styling, matching the
DN-AUTO amp monitor and Shack Services apps.
"""

import os
import queue
import re
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

# Force pystray onto the appindicator (StatusNotifierItem) backend rather
# than letting it auto-detect. Root cause of the lid-close freeze: with no
# PyGObject in the venv, pystray silently fell back to its legacy X11
# "_xorg" systray backend, which depends on a GNOME Shell extension
# bridging the old XEmbed protocol into Mutter's own single-threaded main
# loop -- and that bridge hung re-registering our icon on resume, freezing
# the whole compositor (not just our window). Fixed by installing
# python3-gi + enabling system-site-packages in the venv (see bt_kiss.py-
# style history for the full diagnosis). Forcing the backend here means
# any future regression (venv rebuilt without system-site-packages, the
# GNOME appindicator extension disabled, etc.) throws at startup instead
# of silently reverting to the buggy fallback and freezing the desktop
# again days later.
os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")

import pystray
from PIL import Image

import config as cfgmod
from process_manager import OffGridStack, SleepInhibitor
from modem_control import ModemControlClient
from bt_kiss import AnytoneKissTnc
from touch_ui import UIMetrics, DragScroller, FreqKeypad, TextKeyboard
import hf_mail

# -- theme --------------------------------------------------------------

BG = "#12161a"
PANEL_BG = "#1a1f24"
FG = "#cfd8dc"
TEAL = "#00d9d9"
ORANGE = "#ff9500"
GREEN = "#33cc66"
RED = "#ff4444"
GRAY = "#5a6570"
# fonts come from UIMetrics.font() (touch_ui.py) so they scale with the
# screen profile; desktop sizes are unchanged from the original constants

CHANNEL_COLORS = {"idle": GRAY, "rx": TEAL, "tx": ORANGE}

# Confirmed root cause (2026-09-27): pystray was silently using its legacy
# X11 "_xorg" systray backend (no PyGObject in the venv to reach the
# appindicator backend instead), which hung GNOME Shell's whole compositor
# on lid-close/resume -- disabling the tray icon entirely made suspend/
# resume work perfectly. Fixed at the source (python3-gi installed +
# venv's pyvenv.cfg set to include-system-site-packages, plus the
# PYSTRAY_BACKEND override above), confirmed via
# `python3 -c "import pystray; print(pystray.Icon.__module__)"` ->
# pystray._appindicator. Tray re-enabled; leave this True unless the
# freeze comes back, in which case check pystray's backend again first.
ENABLE_TRAY = True


class StatusLight(tk.Canvas):
    def __init__(self, parent, size=14, **kwargs):
        super().__init__(parent, width=size, height=size, bg=PANEL_BG,
                          highlightthickness=0, **kwargs)
        self.size = size
        self._oval = self.create_oval(2, 2, size - 2, size - 2, fill=GRAY, outline="")

    def set_color(self, color):
        self.itemconfig(self._oval, fill=color)


class OffGridCommsApp:
    def __init__(self, root):
        self.root = root
        self.cfg = cfgmod.load_config()
        self.stack = OffGridStack(self.cfg)
        self.anytone = AnytoneKissTnc(self.cfg)
        self.inhibitor = SleepInhibitor()
        self.modem_client = None
        self._modem_connect_stop = threading.Event()

        self.event_q = queue.Queue()
        self.heard_stations = []  # list of dicts, newest first

        self.radio_var = tk.StringVar(value=self.cfg.get("last_radio") or next(iter(self.cfg["radios"])))
        self.mode_var = tk.StringVar(value=self.cfg.get("last_mode", "Normal"))
        self.freq_var = tk.StringVar(value="")
        self.always_on_top_var = tk.BooleanVar(value=self.cfg["window"].get("always_on_top", False))
        self.net_status_auto_var = tk.BooleanVar(value=True)
        self._topmost_before_sleep = False
        self._sleep_monitor_proc = None

        self.m = UIMetrics(self.cfg.get("ui"))
        self.m.scale_named_fonts(self.root)
        print(f"[ui] screen profile: {self.m.profile} (scale {self.m.s})", flush=True)

        self._build_ui()
        if self.m.touch:
            self._maximize()
        self._apply_always_on_top()
        self._refresh_anytone_light()

        threading.Thread(target=self._start_rnsd_worker, daemon=True).start()

        self.tray_icon = None
        if ENABLE_TRAY:
            self._build_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray if ENABLE_TRAY else self.quit_app)
        self.root.after(200, self._poll_events)
        self.root.after(2000, self._poll_status)
        self.root.after(1500, self._poll_network_status)
        self._start_sleep_watcher()

    # -- UI construction --------------------------------------------------

    def _build_ui(self):
        m = self.m
        touch = m.touch
        self.root.title("OffGrid Comms")
        self.root.configure(bg=BG)
        if touch:
            self.root.minsize(m.px(300), m.px(300))
        else:
            self.root.geometry("780x700")
            self.root.minsize(620, 420)

        # combobox drop-down list font must be set before any combobox exists
        self.root.option_add("*TCombobox*Listbox.font", m.font(11))

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", fieldbackground=PANEL_BG, background=PANEL_BG,
                         foreground=FG, arrowcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_BG)])
        style.configure("Vertical.TScrollbar", background=PANEL_BG, troughcolor=BG,
                         arrowcolor=FG, bordercolor=BG)
        style.configure("Horizontal.TScrollbar", background=PANEL_BG, troughcolor=BG,
                         arrowcolor=FG, bordercolor=BG)
        if touch:
            style.configure("TCombobox", padding=m.px(6), arrowsize=m.px(14))
            style.configure("Vertical.TScrollbar", arrowsize=m.px(16))
            style.configure("Horizontal.TScrollbar", arrowsize=m.px(10))
            style.configure("Treeview", rowheight=m.px(22), font=m.font(10))
            style.configure("Treeview.Heading", font=m.font(10, bold=True))

        # -- scrollable body -- the panel list keeps growing as features get
        # added, and a fixed-height window just clips whatever doesn't fit
        # on a given screen. Everything below attaches to self.body (inside
        # a canvas+scrollbar) instead of self.root directly, so the window
        # can be resized/scrolled to reach anything regardless of how tall
        # the content gets or how short the screen is.
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.body = tk.Frame(canvas, bg=BG)

        self.body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.canvas = canvas

        # mousewheel: Button-4/5 for Linux (this app's target platform),
        # MouseWheel for anywhere else it might get run
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # finger-drag scrolling on the panel backgrounds (touch profile only;
        # on the laptop a click-drag on a panel shouldn't move the view)
        if touch:
            self._drag_scroller = DragScroller(self.root, canvas)

        header = tk.Frame(self.body, bg=BG)
        header.pack(fill="x", padx=m.px(12), pady=(m.px(12), m.px(6)))
        big = m.font(20 if not touch else 16, bold=True, mono=True)
        tk.Label(header, text="OFFGRID COMMS", bg=BG, fg=ORANGE, font=big).pack(side="left")
        tk.Label(header, text=self.cfg["callsign"], bg=BG, fg=TEAL, font=big).pack(side="right")

        # On the tablet the primary controls go right under the header so
        # Start/Stop All are reachable without scrolling; on the laptop
        # they stay at the bottom, where they've always been.
        if touch:
            self._build_controls()

        # -- radio / frequency panel --
        radio_panel = self._panel("RADIO")
        row = tk.Frame(radio_panel, bg=PANEL_BG)
        row.pack(fill="x", pady=m.px(4))

        tk.Label(row, text="Rig:", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        radio_combo = ttk.Combobox(row, textvariable=self.radio_var, state="readonly",
                                    values=list(self.cfg["radios"].keys()), width=12,
                                    font=m.font(11))
        radio_combo.pack(side="left", padx=m.px(6))
        radio_combo.bind("<<ComboboxSelected>>", self._on_radio_selected)

        self.rig_light = StatusLight(row, size=m.px(14))
        self.rig_light.pack(side="left", padx=(m.px(10), m.px(2)))
        tk.Label(row, text="rig link", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")

        self.audio_light = StatusLight(row, size=m.px(14))
        self.audio_light.pack(side="left", padx=(m.px(16), m.px(2)))
        tk.Label(row, text="audio", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")

        freq_row = tk.Frame(radio_panel, bg=PANEL_BG)
        freq_row.pack(fill="x", pady=m.px(4))
        tk.Label(freq_row, text="Freq (MHz):", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        freq_entry = tk.Entry(freq_row, textvariable=self.freq_var, bg="#0c0f12", fg=TEAL,
                               insertbackground=TEAL, font=m.font(11, mono=True), width=10 if touch else 12)
        freq_entry.pack(side="left", padx=m.px(6), ipady=m.px(4) if touch else 0)
        freq_entry.bind("<Return>", lambda e: self.set_frequency())
        if touch:
            # Tk text fields never raise GNOME's on-screen keyboard, so a
            # tap opens our own number pad instead.
            freq_entry.bind("<ButtonRelease-1>", lambda e: self.open_freq_keypad())
        self._button(freq_row, "Set", self.set_frequency, ORANGE).pack(side="left")
        readout_parent = freq_row
        if touch:
            # in portrait the readout doesn't fit on the same line
            readout_parent = tk.Frame(radio_panel, bg=PANEL_BG)
            readout_parent.pack(fill="x")
        self.freq_readout = tk.Label(readout_parent, text="-- . --- MHz", bg=PANEL_BG, fg=TEAL,
                                     font=m.font(11, mono=True))
        self.freq_readout.pack(side="right")

        # -- mode panel --
        mode_panel = self._panel("MODE")
        mode_row = tk.Frame(mode_panel, bg=PANEL_BG)
        mode_row.pack(fill="x", pady=m.px(4))
        self.mode_buttons = {}
        cols = 2 if touch else len(self.cfg["modes"])
        for i, name in enumerate(self.cfg["modes"]):
            b = self._button(mode_row, name, lambda n=name: self.select_mode(n), FG,
                             bold=True, active=TEAL, width=None if touch else 11)
            if touch:
                b.grid(row=i // cols, column=i % cols, sticky="ew", padx=m.px(3), pady=m.px(3))
            else:
                b.pack(side="left", padx=4)
            self.mode_buttons[name] = b
        if touch:
            for c in range(cols):
                mode_row.grid_columnconfigure(c, weight=1, uniform="modes")
        self.mode_desc = tk.Label(mode_panel, text="", bg=PANEL_BG, fg=GRAY, font=m.font(10))
        self.mode_desc.pack(anchor="w", pady=(m.px(2), 0))
        self._refresh_mode_buttons()

        self._build_mercury_panel()

        # -- VHF/UHF (Anytone Bluetooth KISS TNC) panel --
        anytone_panel = self._panel("VHF/UHF -- ANYTONE (BLUETOOTH KISS)")
        a_row = tk.Frame(anytone_panel, bg=PANEL_BG)
        a_row.pack(fill="x", pady=m.px(4))

        self.anytone_light = StatusLight(a_row, size=m.px(14))
        self.anytone_light.pack(side="left", padx=(0, m.px(6)))
        self.anytone_status_label = tk.Label(a_row, text="not bound", bg=PANEL_BG, fg=FG, font=m.font(10))
        self.anytone_status_label.pack(side="left")

        tk.Label(a_row, text=f"  ({self.anytone.mac} ch{self.anytone.channel})", bg=PANEL_BG,
                 fg=GRAY, font=m.font(10 if not touch else 9)).pack(side="left")

        btn_parent = a_row
        if touch:
            btn_parent = tk.Frame(anytone_panel, bg=PANEL_BG)
            btn_parent.pack(fill="x", pady=(m.px(4), 0))
        buttons = [("Connect", self.connect_anytone, GREEN),
                   ("Disconnect", self.disconnect_anytone, RED),
                   ("Reconnect", self.reconnect_anytone, ORANGE)]
        if touch:
            for i, (text, cmd, color) in enumerate(buttons):
                self._button(btn_parent, text, cmd, color).grid(
                    row=0, column=i, sticky="ew", padx=m.px(3))
                btn_parent.grid_columnconfigure(i, weight=1, uniform="anytone")
        else:
            for text, cmd, color in buttons:
                self._button(btn_parent, text, cmd, color).pack(side="right", padx=4)

        self._wrapping_label(anytone_panel,
                 "Not part of the HF stack -- this just binds the radio's Bluetooth SPP TNC to "
                 "/dev/rfcomm0 for Reticulum's KISSInterface. No modem73, no rigctld. "
                 "If Connect says \"already bound\" but the link is actually dead (radio was out "
                 "of range), use Reconnect to force a clean release + rebind.",
                 size=8).pack(anchor="w", fill="x", pady=(m.px(4), 0))

        # -- readout panel --
        readout_panel = self._panel("LINK STATUS")
        grid = tk.Frame(readout_panel, bg=PANEL_BG)
        grid.pack(fill="x")

        self.channel_light = StatusLight(grid, size=m.px(16))
        self.channel_state_label = tk.Label(grid, text="IDLE", bg=PANEL_BG, fg=GRAY,
                                            font=m.font(11, mono=True))
        self.channel_light.grid(row=0, column=0, padx=(0, m.px(6)))
        self.channel_state_label.grid(row=0, column=1, sticky="w")

        self.readouts = {}
        fields = [("SNR", "-- dB"), ("BER", "--"), ("Population", "--"), ("Occupancy", "--%")]
        for i, (label, default) in enumerate(fields):
            if touch:
                # 2 x 2 below the channel state instead of one long row
                r, col = 1 + i // 2, (i % 2) * 2
                padx = (0 if col == 0 else m.px(16), m.px(6))
            else:
                r, col = 0, 2 + i * 2
                padx = (16, 4)
            tk.Label(grid, text=label, bg=PANEL_BG, fg=FG, font=m.font(10)).grid(
                row=r, column=col, padx=padx, pady=m.px(2) if touch else 0, sticky="w" if touch else "")
            val = tk.Label(grid, text=default, bg=PANEL_BG, fg=TEAL, font=m.font(11, mono=True))
            val.grid(row=r, column=col + 1, sticky="w" if touch else "")
            self.readouts[label] = val

        # -- heard stations --
        heard_panel = self._panel("HEARD STATIONS")
        columns = ("time", "callsign", "mode", "snr")
        self.heard_tree = ttk.Treeview(heard_panel, columns=columns, show="headings", height=6)
        for col, w in zip(columns, (80, 100, 140, 70)):
            self.heard_tree.heading(col, text=col.upper())
            self.heard_tree.column(col, width=m.px(w), anchor="w")
        self.heard_tree.pack(fill="x")

        # -- network status (rnstatus at a glance + rnsd/nomadnet health) --
        net_panel = self._panel("NETWORK STATUS")
        net_row = tk.Frame(net_panel, bg=PANEL_BG)
        net_row.pack(fill="x", pady=(0, m.px(6)))

        self.rnsd_light = StatusLight(net_row, size=m.px(14))
        self.rnsd_light.pack(side="left", padx=(0, m.px(6)))
        tk.Label(net_row, text="rnsd", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")

        self.nomadnet_light = StatusLight(net_row, size=m.px(14))
        self.nomadnet_light.pack(side="left", padx=(m.px(16), m.px(6)))
        tk.Label(net_row, text="propagation (nomadnet)", bg=PANEL_BG, fg=FG,
                 font=m.font(10)).pack(side="left")

        net_btns = net_row
        if touch:
            net_btns = tk.Frame(net_panel, bg=PANEL_BG)
            net_btns.pack(fill="x", pady=(0, m.px(6)))
        self._toggle(net_btns, "Auto-refresh (10s)", self.net_status_auto_var,
                     bg=PANEL_BG).pack(side="right", padx=(m.px(4), 0))
        self._button(net_btns, "Refresh", self.refresh_network_status, TEAL).pack(
            side="left" if touch else "right")

        self.net_status_text = tk.Text(net_panel, bg="#0c0f12", fg=FG,
                                        font=m.font(7 if touch else 8, mono=True), height=9,
                                        wrap="none", state="disabled")
        self.net_status_text.pack(fill="x")
        if touch:
            xs = ttk.Scrollbar(net_panel, orient="horizontal", command=self.net_status_text.xview)
            self.net_status_text.configure(xscrollcommand=xs.set)
            xs.pack(fill="x")

        # -- log panel --
        log_panel = self._panel("LOG", expand=True)
        self.log_text = tk.Text(log_panel, bg="#0c0f12", fg=FG,
                                 font=m.font(8 if touch else 9, mono=True),
                                 height=10, wrap="char" if touch else "none")
        self.log_text.pack(fill="both", expand=True)
        self._button(log_panel, "Save Log to File", self.save_log, TEAL).pack(
            anchor="e", pady=(m.px(4), 0))

        if not touch:
            self._build_controls()

    def _build_controls(self):
        m = self.m
        touch = m.touch
        controls = tk.Frame(self.body, bg=BG)
        controls.pack(fill="x", padx=m.px(12), pady=m.px(10) if not touch else (m.px(2), m.px(6)))

        hf_row = tk.Frame(controls, bg=BG)
        hf_row.pack(fill="x")
        tk.Label(hf_row, text="HF stack:", bg=BG, fg=GRAY, font=m.font(10)).pack(
            side="left", padx=(0, m.px(6)))
        self._button(hf_row, "Start All", self.start_all, BG, bg=GREEN, bold=True,
                     width=10, active=GREEN).pack(side="left", padx=m.px(4), fill="x",
                                                  expand=touch)
        self._button(hf_row, "Stop All", self.stop_all, BG, bg=RED, bold=True,
                     width=10, active=RED).pack(side="left", padx=m.px(4), fill="x",
                                                expand=touch)

        mc_row = tk.Frame(controls, bg=BG)
        mc_row.pack(fill="x", pady=(m.px(6), 0))
        tk.Label(mc_row, text="MeshChatX:", bg=BG, fg=GRAY, font=m.font(10)).pack(
            side="left" if not touch else "top", anchor="w", padx=(0, m.px(6)))
        mc_buttons = [("Start", self.start_meshchatx_only, GREEN, 10),
                      ("Stop", self.stop_meshchatx_only, RED, 10),
                      ("Open MeshChatX", self.open_meshchatx, TEAL, None),
                      ("Announce", self.announce_meshchatx, ORANGE, None)]
        if touch:
            grid = tk.Frame(mc_row, bg=BG)
            grid.pack(fill="x")
            for i, (text, cmd, color, _w) in enumerate(mc_buttons):
                self._button(grid, text, cmd, color, bold=True).grid(
                    row=i // 2, column=i % 2, sticky="ew", padx=m.px(3), pady=m.px(3))
            for c in range(2):
                grid.grid_columnconfigure(c, weight=1, uniform="mc")
        else:
            for text, cmd, color, w in mc_buttons:
                self._button(mc_row, text, cmd, color, bold=True, width=w).pack(side="left", padx=4)

        opts_row = tk.Frame(controls, bg=BG)
        opts_row.pack(fill="x", pady=(m.px(8), 0))
        self._toggle(opts_row, "Always on top", self.always_on_top_var,
                     command=self._apply_always_on_top, bg=BG).pack(side="left")
        self._button(opts_row, "Minimize to tray", self.hide_to_tray, FG).pack(
            side="right", padx=m.px(4))

    # -- Mercury HF mail panel ---------------------------------------------

    def _build_mercury_panel(self):
        m = self.m
        touch = m.touch
        mc = self.cfg["mercury"]
        mycall = mc.get("mycall") or "callsign not set"
        panel = self._panel(f"HF MAIL -- MERCURY ({mycall})")

        # status: mercury / connector lights + link state
        st = tk.Frame(panel, bg=PANEL_BG)
        st.pack(fill="x", pady=m.px(2))
        self.mercury_light = StatusLight(st, size=m.px(14))
        self.mercury_light.pack(side="left", padx=(0, m.px(4)))
        tk.Label(st, text="mercury", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        self.connector_light = StatusLight(st, size=m.px(14))
        self.connector_light.pack(side="left", padx=(m.px(14), m.px(4)))
        tk.Label(st, text="connector", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        # live activity: link to another station, transmitting, new message
        self.link_light = StatusLight(st, size=m.px(14))
        self.link_light.pack(side="left", padx=(m.px(14), m.px(4)))
        tk.Label(st, text="link", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        self.tx_light = StatusLight(st, size=m.px(14))
        self.tx_light.pack(side="left", padx=(m.px(14), m.px(4)))
        tk.Label(st, text="TX", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        self.rxmsg_light = StatusLight(st, size=m.px(14))
        self.rxmsg_light.pack(side="left", padx=(m.px(14), m.px(4)))
        tk.Label(st, text="new msg", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(side="left")
        self._rxmsg_until = 0.0
        self._link_state = "off"

        lk = tk.Frame(panel, bg=PANEL_BG)
        lk.pack(fill="x", pady=(m.px(2), m.px(4)))
        self.mail_link_label = tk.Label(lk, text="OFF", bg=PANEL_BG, fg=GRAY,
                                        font=m.font(11, mono=True), anchor="w")
        self.mail_link_label.pack(side="left", fill="x", expand=True)
        # signal quality while linked: what Mercury reports (SNR, speed)
        self.mail_signal_label = tk.Label(lk, text="", bg=PANEL_BG, fg=GRAY,
                                          font=m.font(10, mono=True), anchor="e")
        self.mail_signal_label.pack(side="right")
        self._signal = {"snr": None, "bps": None}

        btns = tk.Frame(panel, bg=PANEL_BG)
        btns.pack(fill="x")
        for i, (text, cmd, color) in enumerate([("Start Mercury", self.start_mercury, GREEN),
                                                ("Stop Mercury", self.stop_mercury, RED),
                                                ("Sound cards", self.list_sound_cards, TEAL),
                                                ("Level check", self.open_level_check, ORANGE)]):
            b = self._button(btns, text, cmd, color, bold=True)
            if touch:
                b.grid(row=i // 2, column=i % 2, sticky="ew", padx=m.px(3), pady=m.px(3))
                btns.grid_columnconfigure(i % 2, weight=1, uniform="merc")
            else:
                b.pack(side="left", padx=(0, 6))

        # destination
        tk.Label(panel, text="Send to:", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(
            anchor="w", pady=(m.px(8), m.px(2)))
        home = mc["peers"][0] if mc["peers"] else "HOME"
        self.mail_dests = [(f"{home}\nhome", None)]
        self.mail_dests += [(f"{s}\nvia home", s) for s in mc.get("field_stations", [])
                            if s.upper() != (mc.get("mycall") or "").upper()]
        self.mail_dest_var = tk.IntVar(value=0)
        dest_row = tk.Frame(panel, bg=PANEL_BG)
        dest_row.pack(fill="x")
        self.mail_dest_buttons = []
        for i, (label, _to) in enumerate(self.mail_dests):
            b = self._button(dest_row, label, lambda i=i: self._select_mail_dest(i), FG,
                             bold=True, active=TEAL)
            b.grid(row=0, column=i, sticky="ew", padx=m.px(3), pady=m.px(3))
            dest_row.grid_columnconfigure(i, weight=1, uniform="dest")
            self.mail_dest_buttons.append(b)
        self._select_mail_dest(0)

        # quick messages
        tk.Label(panel, text="Quick messages:", bg=PANEL_BG, fg=FG, font=m.font(10)).pack(
            anchor="w", pady=(m.px(8), m.px(2)))
        quick = tk.Frame(panel, bg=PANEL_BG)
        quick.pack(fill="x")
        items = [(q, lambda q=q: self.queue_mail(q)) for q in mc.get("quick_messages", [])]
        items += [("Position", lambda: self.queue_mail("Position report", need_gps=True)),
                  ("WX here", lambda: self.queue_mail(None, wx="WX")),
                  ("Marine WX", lambda: self.queue_mail(None, wx="WXM")),
                  ("Write message…", self.compose_mail)]
        cols = 2 if touch else 4
        for i, (label, cmd) in enumerate(items):
            color = ORANGE if label == "Write message…" else (TEAL if i >= len(items) - 4 else FG)
            self._button(quick, label, cmd, color).grid(
                row=i // cols, column=i % cols, sticky="ew", padx=m.px(3), pady=m.px(3))
        for c in range(cols):
            quick.grid_columnconfigure(c, weight=1, uniform="quick")

        self.mail_gps_var = tk.BooleanVar(value=bool(mc.get("attach_gps", True)))
        opt = tk.Frame(panel, bg=PANEL_BG)
        opt.pack(fill="x", pady=(m.px(4), 0))
        self._toggle(opt, "Attach GPS position", self.mail_gps_var, bg=PANEL_BG).pack(side="left")

        # queue
        q = tk.Frame(panel, bg=PANEL_BG)
        q.pack(fill="x", pady=(m.px(8), 0))
        self.mail_queue_label = tk.Label(q, text="Outbox: empty", bg=PANEL_BG, fg=FG,
                                         font=m.font(10), anchor="w")
        self.mail_queue_label.pack(side="left", fill="x", expand=True)
        self._button(q, "Clear outbox", self.clear_mail_queue, RED).pack(side="right")

        # inbox
        ib = tk.Frame(panel, bg=PANEL_BG)
        ib.pack(fill="x", pady=(m.px(8), m.px(2)))
        self.mail_inbox_title = tk.Label(ib, text="Messages", bg=PANEL_BG, fg=FG, font=m.font(10))
        self.mail_inbox_title.pack(side="left")
        self._button(ib, "Open folder", self.open_mail_inbox, TEAL).pack(side="right")
        self.mail_inbox_text = tk.Text(panel, bg="#0c0f12", fg=FG, height=10,
                                       font=m.font(9 if touch else 9, mono=True),
                                       wrap="word", state="disabled", padx=m.px(6), pady=m.px(4))
        self.mail_inbox_text.pack(fill="both", expand=True)
        # received on the left, ours on the right -- like a chat
        self.mail_inbox_text.tag_configure("in", foreground=TEAL, lmargin1=0, lmargin2=0,
                                           rmargin=m.px(60), spacing3=m.px(6))
        self.mail_inbox_text.tag_configure("out", foreground=ORANGE, justify="right",
                                           lmargin1=m.px(60), lmargin2=m.px(60), spacing3=m.px(6))
        self.mail_inbox_text.tag_configure("outq", foreground=GRAY, justify="right",
                                           lmargin1=m.px(60), lmargin2=m.px(60), spacing3=m.px(6))
        self._mail_inbox_sig = None
        self._last_queued = None

        self._wrapping_label(panel,
            "Mercury and modem73 both need the radio, so only one runs at a time. "
            "Anything in the outbox goes out on the air: the connector calls home as soon as "
            "Mercury is up, and again every couple of minutes until it gets through. "
            "\"WX here\"/\"Marine WX\" send your GPS position and home replies with a "
            "forecast. While Mercury runs it holds port 8300, so Pat can't use Mercury.",
            size=8).pack(anchor="w", fill="x", pady=(m.px(4), 0))

    def _select_mail_dest(self, i):
        self.mail_dest_var.set(i)
        for j, b in enumerate(self.mail_dest_buttons):
            b.configure(bg=TEAL if j == i else PANEL_BG, fg=BG if j == i else FG)

    def _colors(self):
        return {"BG": BG, "PANEL_BG": PANEL_BG, "TEAL": TEAL, "ORANGE": ORANGE,
                "GREEN": GREEN, "RED": RED, "GRAY": GRAY}

    def _remember_radio(self, radio):
        if self.cfg.get("last_radio") != radio:
            self.cfg["last_radio"] = radio
            cfgmod.save_config(self.cfg)

    def start_mercury(self):
        radio = self.radio_var.get()
        dev = self.cfg["radios"].get(radio, {}).get("device", "")
        if dev and not os.path.exists(dev):
            others = [name for name, r in self.cfg["radios"].items()
                      if name != radio and r.get("device") and os.path.exists(r["device"])]
            hint = (f"\n\nPlugged in right now: {', '.join(others)} -- pick it in the radio list."
                    if others else "\n\nCheck the USB cable and that the radio is on.")
            messagebox.showerror("OffGrid Comms",
                                 f"The {radio} isn't connected (its serial port isn't there):\n{dev}{hint}")
            return
        self._remember_radio(radio)
        if self.stack.modem73_running:
            if not messagebox.askyesno(
                    "OffGrid Comms",
                    "modem73 (Reticulum HF) is using the radio.\n\n"
                    "Stop it and start Mercury instead? MeshChatX keeps running."):
                return
            self._modem_connect_stop.set()
            if self.modem_client:
                self.modem_client.close()
                self.modem_client = None
            self.stack.stop_modem73_and_rigctld()
            self._log("[ui] modem73 stopped to free the radio for Mercury")
        audio = self.stack.mercury_audio_device(radio)
        self._log(f"[ui] starting Mercury on {radio} (audio {audio})")
        self._set_mail_link("calling", "STARTING…")
        threading.Thread(target=self._start_mercury_worker, args=(radio,), daemon=True).start()

    def _start_mercury_worker(self, radio):
        ok, msg = self.stack.start_mercury(radio)
        self.event_q.put(("mercury_result", (ok, msg)))

    def stop_mercury(self):
        self.stack.stop_mercury()
        self._set_mail_link("off", "OFF")
        self._log("[ui] Mercury and connector stopped")

    def open_level_check(self):
        """Test-tone / level meter window (levelcheck_gui.py) for setting audio levels."""
        import levelcheck_gui
        if getattr(self, "_levelcheck_win", None) and self._levelcheck_win.winfo_exists():
            self._levelcheck_win.lift()
            return

        def busy():
            if self.stack.mercury_running:
                return "Stop Mercury first -- it holds the sound card and the radio."
            if self.stack.modem73_running:
                return "Stop the Reticulum HF stack (modem73) first -- it holds the sound card and the radio."
            return None

        top = tk.Toplevel(self.root)
        self._levelcheck_win = top
        levelcheck_gui.LevelCheckWindow(
            top, presets=levelcheck_gui.presets_from_offgrid_config(self.cfg),
            default=self.radio_var.get(), scale=self.m.px(100) / 100, busy_check=busy)

    def list_sound_cards(self):
        def work():
            try:
                r = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
                text = r.stdout or r.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                text = f"arecord failed: {e}"
            cards = re.findall(r"^card (\d+): (\S+) \[(.*?)\], device (\d+)", text, re.M)
            lines = ["[ui] sound cards (use plughw:CARD=<id>,DEV=<n> as mercury_audio):"]
            lines += [f"    plughw:CARD={cid},DEV={dev}   ({name}, card {num})"
                      for num, cid, name, dev in cards] or ["    " + text.strip()]
            radio = self.radio_var.get()
            lines.append(f"[ui] {radio} is set to {self.stack.mercury_audio_device(radio)}")
            for line in lines:
                self.event_q.put(("log", line))
        threading.Thread(target=work, daemon=True).start()

    def _set_mail_link(self, state, text):
        colors = {"off": GRAY, "idle": TEAL, "calling": ORANGE, "connected": GREEN, "failed": RED}
        self.mail_link_label.configure(text=text, fg=colors.get(state, FG))
        self._link_state = state
        self.link_light.set_color({"connected": GREEN, "calling": ORANGE, "failed": RED}.get(state, GRAY))
        if state in ("off", "idle", "failed"):
            self.tx_light.set_color(GRAY)
        if state != "connected":
            self._signal = {"snr": None, "bps": None}
            self.mail_signal_label.configure(text="")

    def _apply_activity(self, act):
        kind, value = act
        if kind == "ptt":
            self.tx_light.set_color(RED if value else GRAY)
        elif kind in ("snr", "bps"):
            self._signal[kind] = value
            snr, bps = self._signal["snr"], self._signal["bps"]
            parts = []
            if snr is not None:
                parts.append(f"SNR {snr:+.1f} dB")
            if bps is not None:
                parts.append(f"{bps} bps")
            color = GRAY
            if snr is not None:
                color = GREEN if snr >= 8 else (ORANGE if snr >= 0 else RED)
            self.mail_signal_label.configure(text="  ".join(parts), fg=color)
        elif kind == "rx_msg":
            self._flash_new_message()

    def _flash_new_message(self, seconds=60):
        self._rxmsg_until = time.time() + seconds
        self.rxmsg_light.set_color(GREEN)

    # composing / queueing

    def compose_mail(self):
        TextKeyboard(self.root, self.m, self._colors(), "Message to home",
                     lambda text: self.queue_mail(text))

    def queue_mail(self, body, need_gps=False, wx=None):
        mc = self.cfg["mercury"]
        if not mc.get("mycall"):
            messagebox.showwarning("OffGrid Comms", "Set mercury.mycall in config.json first.")
            return
        label, to = self.mail_dests[self.mail_dest_var.get()]
        if wx:
            to = None   # home answers WX requests; it would relay one addressed elsewhere
        want_gps = bool(wx or need_gps or self.mail_gps_var.get())
        self._log("[ui] getting a GPS fix…" if want_gps else "[ui] preparing message")

        def work():
            fix = hf_mail.gps_fix() if want_gps else None
            self.event_q.put(("mail_ready", (body, to, wx, need_gps, fix)))
        threading.Thread(target=work, daemon=True).start()

    def _mail_ready(self, body, to, wx, need_gps, fix):
        mc = self.cfg["mercury"]
        if (wx or need_gps) and not fix:
            messagebox.showwarning("OffGrid Comms",
                                   "No GPS fix from gpsd, so this message can't be built. "
                                   "Try near a window or outside.")
            return
        lat, lon = fix if fix else (None, None)
        if wx:
            body = f"{wx} {lat:.4f} {lon:.4f}"
        if not mc.get("peers") or not mc.get("mycall"):
            messagebox.showwarning("OffGrid Comms", "Set mercury.mycall and mercury.peers in config.json first.")
            return
        home = mc["peers"][0]
        try:
            content = hf_mail.build_message(mc["mycall"], body, category=mc.get("category", "status"),
                                            to=to, lat=lat, lon=lon)
        except ValueError as e:
            messagebox.showerror("OffGrid Comms", str(e))
            return
        dest = f"{to} (relayed by {home})" if to else home
        when = ("The connector will call it on the air right away."
                if self.stack.connector_running else
                "It goes out on the air next time you start Mercury.")
        pos = f"\nPosition: {lat:.4f}, {lon:.4f}" if fix else ""
        if not messagebox.askyesno("Queue HF message",
                                   f"To: {dest}\n\n{body}{pos}\n\n{when}"):
            self._log("[ui] message not queued")
            return
        path = hf_mail.queue_message(mc["outgoing_dir"], home, content)
        try:
            hf_mail.save_sent_copy(mc["sent_dir"], path, content)
        except OSError as e:
            self._log(f"[ui] couldn't keep a copy for the message list: {e}")
        self._log(f"[ui] queued {path.name} for {dest}: {body}")
        self._refresh_mail()

    def clear_mail_queue(self):
        mc = self.cfg["mercury"]
        q = hf_mail.queued(mc["outgoing_dir"], mc["peers"])
        files = [p for paths in q.values() for p in paths]
        if not files:
            return
        if not messagebox.askyesno("OffGrid Comms",
                                   f"Delete {len(files)} unsent message(s) from the outbox?"):
            return
        for p in files:
            try:
                p.unlink()
            except OSError as e:
                self._log(f"[ui] could not delete {p}: {e}")
        hf_mail.delete_sent_copies(mc["sent_dir"], [p.name for p in files])
        self._log(f"[ui] outbox cleared ({len(files)} message(s))")
        self._refresh_mail()

    def open_mail_inbox(self):
        path = Path(self.cfg["mercury"]["incoming_dir"]).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _refresh_mail(self):
        mc = self.cfg["mercury"]
        merc, conn = self.stack.mercury_running, self.stack.connector_running
        self.mercury_light.set_color(GREEN if merc else GRAY)
        self.connector_light.set_color(GREEN if conn else GRAY)
        if not merc and self.mail_link_label.cget("text") not in ("OFF", "STARTING…"):
            self._set_mail_link("off", "OFF")
        if self._rxmsg_until and time.time() > self._rxmsg_until:
            self._rxmsg_until = 0.0
            self.rxmsg_light.set_color(GRAY)

        q = hf_mail.queued(mc["outgoing_dir"], mc["peers"])
        queued_names = {p.name for paths in q.values() for p in paths}
        if self._last_queued is not None:
            for name in self._last_queued - queued_names:
                if hf_mail.mark_delivered(mc["sent_dir"], name):
                    self._log(f"[ui] HF mail delivered: {name}")
        self._last_queued = queued_names
        n = sum(len(v) for v in q.values())
        if n:
            per = ", ".join(f"{len(v)} for {k}" for k, v in q.items() if v)
            self.mail_queue_label.configure(text=f"Outbox: {per}", fg=ORANGE)
        else:
            self.mail_queue_label.configure(text="Outbox: empty", fg=FG)

        msgs = hf_mail.inbox(mc["incoming_dir"])
        chat = hf_mail.conversation(mc["incoming_dir"], mc["sent_dir"], queued_names)
        sig = tuple((x["file"], x["time"], x.get("status")) for x in chat)
        if sig != self._mail_inbox_sig:
            first = self._mail_inbox_sig is None
            old_in = {f for f, _t, s in (self._mail_inbox_sig or ()) if s is None}
            self._mail_inbox_sig = sig
            home = mc["peers"][0] if mc["peers"] else "HOME"
            t = self.mail_inbox_text
            t.configure(state="normal")
            t.delete("1.0", "end")
            if not chat:
                t.insert("end", "(no messages yet)")
            for x in chat:
                if x["direction"] == "out":
                    tag = "out" if x["status"] == "sent" else "outq"
                else:
                    tag = "in"
                t.insert("end", hf_mail.format_chat_line(x, home) + "\n", tag)
            t.configure(state="disabled")
            t.see("end")
            self.mail_inbox_title.configure(text=f"Messages ({len(msgs)} in)")
            new_in = [x for x in msgs if x["file"] not in old_in]
            if not first and new_in:
                self._log(f"[ui] HF mail: {hf_mail.format_inbox_line(new_in[0])}")
                self._flash_new_message()

    # -- widget helpers ---------------------------------------------------

    def _button(self, parent, text, command, fg, bg=PANEL_BG, bold=False, width=None,
                active=None):
        """Flat themed button. On the touch profile it gets a larger font and
        padding so it's a comfortable finger target (~9 mm on the tablet)."""
        m = self.m
        active = active or fg
        kw = dict(text=text, command=command, bg=bg, fg=fg,
                  activebackground=active, activeforeground=BG if bg == PANEL_BG else bg,
                  relief="flat", font=m.font(10, bold=bold))
        if width:
            kw["width"] = width
        if m.touch:
            kw.update(padx=m.px(8), pady=m.px(7))
        return tk.Button(parent, **kw)

    def _toggle(self, parent, text, variable, bg, command=None):
        """Checkbox on the desktop; a large on/off toggle button on touch
        (the stock checkbox indicator is too small to hit with a finger)."""
        m = self.m
        if m.touch:
            return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                                  indicatoron=False, bg=PANEL_BG, fg=FG,
                                  selectcolor="#0e4f4f", activebackground=PANEL_BG,
                                  activeforeground=FG, relief="flat", offrelief="flat",
                                  font=m.font(10), padx=m.px(8), pady=m.px(9))
        return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                              bg=bg, fg=FG, selectcolor=PANEL_BG if bg == BG else BG,
                              activebackground=bg, activeforeground=FG)

    def _wrapping_label(self, parent, text, size=8):
        """Gray help text that re-wraps to the panel width (fixed line
        breaks would run off the side in portrait on the tablet)."""
        label = tk.Label(parent, text=text, bg=PANEL_BG, fg=GRAY, font=self.m.font(size),
                         justify="left", anchor="w")
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(50, e.width - 4)))
        return label

    def _panel(self, title, expand=False):
        m = self.m
        outer = tk.Frame(self.body, bg=BG)
        outer.pack(fill="both" if expand else "x", padx=m.px(12), pady=m.px(6), expand=expand)
        tk.Label(outer, text=title, bg=BG, fg=GRAY, font=m.font(10, bold=True)).pack(anchor="w")
        inner = tk.Frame(outer, bg=PANEL_BG, padx=m.px(10), pady=m.px(8))
        inner.pack(fill="both" if expand else "x", expand=expand)
        return inner

    def open_freq_keypad(self):
        colors = {"BG": BG, "PANEL_BG": PANEL_BG, "TEAL": TEAL, "ORANGE": ORANGE,
                  "GREEN": GREEN, "RED": RED, "GRAY": GRAY}

        def on_set(value):
            self.freq_var.set(value)
            self.set_frequency()

        FreqKeypad(self.root, self.m, colors, self.freq_var.get(), on_set)

    def _refresh_mode_buttons(self):
        current = self.mode_var.get()
        for name, btn in self.mode_buttons.items():
            if name == current:
                btn.configure(bg=TEAL, fg=BG)
            else:
                btn.configure(bg=PANEL_BG, fg=FG)
        desc = self.cfg["modes"].get(current, {}).get("description", "")
        self.mode_desc.configure(text=desc)

    # -- rnsd (base Reticulum transport, runs for the app's whole lifetime) --

    def _start_rnsd_worker(self):
        ok = self.stack.start_rnsd()
        self.event_q.put(("log", "[ui] rnsd started" if ok else "[ui] rnsd FAILED to start -- check the log above"))

    # -- tray ---------------------------------------------------------------

    def _build_tray(self):
        image = Image.open("assets/icon.png")
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Start All", lambda: self.root.after(0, self.start_all)),
            pystray.MenuItem("Stop All", lambda: self.root.after(0, self.stop_all)),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self.tray_icon = pystray.Icon("offgrid-comms", image, "OffGrid Comms", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None):
        self.root.after(0, self.show_from_tray)

    def _tray_quit(self, icon=None, item=None):
        self.root.after(0, self.quit_app)

    def hide_to_tray(self):
        self.root.withdraw()

    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()

    def _update_keep_awake(self):
        """Hold the sleep inhibitor exactly while an HF modem is running."""
        hf = self.stack.modem73_running or self.stack.mercury_running
        if hf and self.cfg.get("keep_awake_while_hf", True) and not self.inhibitor.active:
            which = "Mercury" if self.stack.mercury_running else "modem73"
            ok, msg = self.inhibitor.acquire(f"{which} HF modem running")
            self._log(f"[ui] keeping the computer awake while {which} runs"
                      if ok else f"[ui] couldn't block suspend: {msg}")
        elif not hf and self.inhibitor.active:
            self.inhibitor.release()
            self._log("[ui] HF modem stopped -- normal suspend allowed again")

    def quit_app(self):
        self.inhibitor.release()
        self.stack.stop_mercury()
        self.stop_all()
        self.stack.stop_rnsd()
        if self.tray_icon:
            self.tray_icon.stop()
        if self._sleep_monitor_proc:
            self._sleep_monitor_proc.terminate()
        self.root.destroy()

    def _maximize(self):
        # fill the 7" screen; follows rotation since GNOME re-maximizes
        try:
            self.root.attributes("-zoomed", True)
        except tk.TclError:
            w, h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.geometry(f"{w}x{h}+0+0")

    def _apply_always_on_top(self):
        on = self.always_on_top_var.get()
        self.root.attributes("-topmost", on)
        self.cfg["window"]["always_on_top"] = on
        cfgmod.save_config(self.cfg)

    # -- suspend/resume handling ------------------------------------------
    #
    # Root cause of the "lid close -> screen frozen, mouse moves but nothing
    # responds, only pkill -f offgrid-comms fixes it" bug: GNOME's lock
    # screen (the ScreenShield surface that grabs all keyboard/pointer input
    # on suspend) can end up *underneath* an X11 window that has the
    # "-topmost" attribute set. The lock screen still owns the input grab
    # (so clicks/keys go nowhere), but this window's pixels stay composited
    # on top of it (so the screen visually "stays as is") -- the cursor
    # still moves because the X server tracks pointer position independent
    # of any grab. Disconnecting Bluetooth or stopping MeshChatX doesn't
    # touch window stacking, so neither fixes it; killing the whole app
    # (which drops the topmost window) does.
    #
    # Fix: listen for logind's PrepareForSleep D-Bus signal and force
    # -topmost off right before suspend, restoring it after resume if the
    # "Always on top" checkbox was on. Uses `dbus-monitor` (ships with the
    # `dbus`/`dbus-x11` package on essentially every desktop distro) as a
    # subprocess instead of a Python D-Bus binding, so this has no extra
    # dependency to install in the venv.
    def _start_sleep_watcher(self):
        threading.Thread(target=self._sleep_watcher_worker, daemon=True).start()

    def _sleep_watcher_worker(self):
        # IMPORTANT: print()/stdout here, not just self.event_q -> self._log().
        # The Tkinter Log widget dies the instant the process is killed to
        # recover from a freeze, same blind spot that made the original
        # ratchet-crash traceback invisible until the "Save Log" button was
        # added. print() goes to the parent's real stdout, so it survives in
        # a `python3 -u main.py 2>&1 | tee ...` capture even through a kill.
        cmd = [
            "dbus-monitor", "--system",
            "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
        ]
        print("[sleep-watcher] starting: " + " ".join(cmd), flush=True)
        try:
            self._sleep_monitor_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except FileNotFoundError:
            msg = ("[sleep-watcher] disabled: dbus-monitor not found "
                   "(install the 'dbus' package to auto-fix the topmost/lid-close freeze)")
            print(msg, flush=True)
            self.event_q.put(("log", msg))
            return
        except OSError as e:
            msg = f"[sleep-watcher] failed to start: {e}"
            print(msg, flush=True)
            self.event_q.put(("log", msg))
            return

        print(f"[sleep-watcher] dbus-monitor running as pid {self._sleep_monitor_proc.pid}, watching for PrepareForSleep", flush=True)
        proc = self._sleep_monitor_proc
        for line in proc.stdout:
            # Echo every raw line so we can see in the tee'd log whether the
            # signal is even being emitted at all on this system, not just
            # whether our "boolean true/false" parsing matched it.
            print(f"[sleep-watcher] {line.rstrip()}", flush=True)
            if "boolean true" in line:
                self.event_q.put(("sleep_prepare", True))
            elif "boolean false" in line:
                self.event_q.put(("sleep_prepare", False))
        print(f"[sleep-watcher] dbus-monitor exited (code {proc.poll()})", flush=True)

    def _handle_sleep_prepare(self, going_to_sleep):
        if going_to_sleep:
            self._topmost_before_sleep = self.always_on_top_var.get()
            print(f"[sleep-watcher] PrepareForSleep(true) received -- always_on_top_var={self._topmost_before_sleep}", flush=True)
            if self._topmost_before_sleep:
                self.root.attributes("-topmost", False)
                self._log("[ui] suspend detected -- temporarily dropping always-on-top "
                          "so the lock screen can take input")
        else:
            print("[sleep-watcher] PrepareForSleep(false) received (resume)", flush=True)
            if self._topmost_before_sleep:
                self.root.attributes("-topmost", True)
                self._log("[ui] resumed -- restored always-on-top")

    # -- actions --------------------------------------------------------

    def _log(self, line):
        self.log_text.insert("end", line + "\n")
        # keep the widget bounded -- Mercury logs a lot over a long session
        if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.see("end")

    def save_log(self):
        path = Path.home() / "offgrid-comms-log.txt"
        with open(path, "w") as f:
            f.write(self.log_text.get("1.0", "end"))
        self._log(f"[ui] log saved to {path}")

    def start_all(self):
        radio = self.radio_var.get()
        mode = self.mode_var.get()
        if self.stack.mercury_running:
            if not messagebox.askyesno("OffGrid Comms",
                                       "Mercury is using the radio.\n\n"
                                       "Stop Mercury and start the Reticulum HF stack?"):
                return
            self.stop_mercury()
        self._log(f"[ui] starting stack: radio={radio} mode={mode}")
        threading.Thread(target=self._start_all_worker, args=(radio, mode), daemon=True).start()

    def _start_all_worker(self, radio, mode):
        ok = self.stack.start_all(radio, mode)
        if not ok:
            self.root.after(0, lambda: self._log("[ui] one or more processes failed to start -- check the log above"))
        self._connect_modem_client()
        self.cfg["last_radio"] = radio
        self.cfg["last_mode"] = mode
        cfgmod.save_config(self.cfg)

    def stop_all(self):
        self._modem_connect_stop.set()
        if self.modem_client:
            self.modem_client.close()
            self.modem_client = None
        self.stack.stop_all()
        self._log("[ui] stack stopped")

    def _on_radio_selected(self, event=None):
        radio = self.radio_var.get()
        mode = self.mode_var.get()
        self._remember_radio(radio)
        if not (self.stack.modem73 and self.stack.modem73.is_running):
            return  # nothing running yet, nothing to switch
        self._log(f"[ui] switching radio to {radio} -- this bounces modem73, MeshChatX stays up")
        threading.Thread(target=self._switch_radio_worker, args=(radio, mode), daemon=True).start()

    def _switch_radio_worker(self, radio, mode):
        if self.modem_client:
            self.modem_client.close()
            self.modem_client = None
        self.stack.switch_radio(radio, mode)
        self._connect_modem_client()
        self.cfg["last_radio"] = radio
        cfgmod.save_config(self.cfg)

    def select_mode(self, name):
        self.mode_var.set(name)
        self._refresh_mode_buttons()
        mode = self.cfg["modes"][name]
        self.cfg["last_mode"] = name
        cfgmod.save_config(self.cfg)
        if self.modem_client and self.modem_client.connected:
            fields = {k: v for k, v in mode.items() if k != "description"}
            resp = self.modem_client.set_config(**fields)
            ok = bool(resp and resp.get("ok"))
            self._log(f"[ui] live mode change to {name}: {'ok' if ok else 'FAILED'}")
        else:
            self._log(f"[ui] mode set to {name} (will apply once modem73 is running)")

    def set_frequency(self):
        raw = self.freq_var.get().strip()
        if not raw:
            return
        try:
            value = float(raw)
        except ValueError:
            messagebox.showerror("OffGrid Comms", f"'{raw}' isn't a number")
            return
        hz = int(value * 1_000_000) if value < 1000 else int(value)
        if self.modem_client and self.modem_client.connected:
            ok = self.modem_client.set_freq_hz(hz)
            self._log(f"[ui] set frequency to {hz} Hz: {'ok' if ok else 'FAILED'}")
            return
        # Mercury mode (or nothing running): talk to the radio through
        # rigctld if it's up, otherwise straight to its serial port.
        rig = self._rig_args()
        if not rig:
            messagebox.showwarning("OffGrid Comms",
                                   f"Can't reach the {self.radio_var.get()} -- is it plugged in?")
            return

        def work():
            try:
                r = subprocess.run(["rigctl", *rig, "F", str(hz)], capture_output=True, text=True, timeout=10)
                ok = r.returncode == 0
                msg = "ok" if ok else (r.stderr.strip() or r.stdout.strip() or "FAILED")
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, msg = False, str(e)
            self.event_q.put(("log", f"[ui] set frequency to {hz} Hz: {msg}"))
            if ok:
                self._read_rig_freq()
        threading.Thread(target=work, daemon=True).start()

    def _rig_args(self):
        """rigctl arguments for the selected radio: via our rigctld when it's
        running for this radio, else direct to the serial port. None if the
        radio isn't plugged in."""
        name = self.radio_var.get()
        r = self.cfg["radios"].get(name, {})
        st = self.stack
        if st.rigctld and st.rigctld.is_running and st.rigctld_radio == name:
            return ["-m", "2", "-r", f"127.0.0.1:{r.get('rigctld_port', 4532)}"]
        dev = r.get("device") or ""
        if not dev or not os.path.exists(dev):
            return None
        return ["-m", str(r.get("hamlib_model", 2)), "-r", dev, "-s", str(r.get("baud", 38400))]

    def _read_rig_freq(self):
        """Background: read the radio's dial via rigctld and show it."""
        rig = self._rig_args()
        if not rig or rig[1] != "2":        # only through our rigctld, never fight it for the port
            return
        try:
            r = subprocess.run(["rigctl", *rig, "f"], capture_output=True, text=True, timeout=5)
            hz = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            hz = 0
        if hz:
            self.root.after(0, lambda: self.freq_readout.configure(text=f"{hz / 1_000_000:.6f} MHz"))

    def open_meshchatx(self):
        mc = self.cfg["meshchatx"]
        host = "127.0.0.1" if mc["host"] == "0.0.0.0" else mc["host"]
        scheme = mc.get("scheme", "https")
        webbrowser.open(f"{scheme}://{host}:{mc['port']}")

    def announce_meshchatx(self):
        # Deliberately NOT a one-click in-app RNS announce: that would mean a
        # second process reaching into MeshChatX's identity/storage files
        # while it's already running and holding them open -- exactly the
        # class of bug that caused the storage-lock and ratchet-corruption
        # issues earlier in this project. MeshChatX owns its identity safely;
        # the announce control lives on its own Settings/Identity page.
        self.open_meshchatx()
        self._log("[ui] opened MeshChatX -- announce from its own Settings > Identity page")

    # -- MeshChatX standalone (no rigctld/modem73/TNC) --------------------
    # For when you're using other means to get on the mesh (the anchor's
    # TCP link, LAN, the Anytone KISS TNC) and just want the chat client.

    def start_meshchatx_only(self):
        self._log("[ui] starting MeshChatX only (rigctld/modem73 not touched)")
        threading.Thread(target=self._start_meshchatx_only_worker, daemon=True).start()

    def _start_meshchatx_only_worker(self):
        ok = self.stack.start_meshchatx()
        if not ok:
            self.root.after(0, lambda: self._log("[ui] MeshChatX failed to start -- check the log above"))

    def stop_meshchatx_only(self):
        self.stack.stop_meshchatx()
        self._log("[ui] MeshChatX stopped (rigctld/modem73 not touched)")

    # -- Anytone Bluetooth KISS TNC ---------------------------------------
    # Independent of Start All/Stop All -- this radio isn't always with the
    # laptop, so binding is a manual on-demand action, not tied to the HF
    # stack's lifecycle. No modem73/rigctld involved.

    def connect_anytone(self):
        self._log("[ui] binding Anytone Bluetooth KISS TNC...")
        threading.Thread(target=self._anytone_worker, args=(self.anytone.connect,), daemon=True).start()

    def disconnect_anytone(self):
        self._log("[ui] releasing Anytone Bluetooth KISS TNC...")
        threading.Thread(target=self._anytone_worker, args=(self.anytone.disconnect,), daemon=True).start()

    def reconnect_anytone(self):
        self._log("[ui] reconnecting Anytone Bluetooth KISS TNC (release + rebind)...")
        threading.Thread(target=self._anytone_worker, args=(self.anytone.reconnect,), daemon=True).start()

    def _anytone_worker(self, action):
        ok, msg = action()
        self.event_q.put(("anytone_result", (ok, msg)))

    def _refresh_anytone_light(self):
        bound = self.anytone.is_bound()
        self.anytone_light.set_color(GREEN if bound else GRAY)
        self.anytone_status_label.configure(text="bound" if bound else "not bound")

    # -- modem control port connection ----------------------------------

    def _connect_modem_client(self):
        self._modem_connect_stop.clear()
        threading.Thread(target=self._modem_connect_worker, daemon=True).start()

    def _modem_connect_worker(self):
        m73 = self.cfg["modem73"]
        client = ModemControlClient(
            m73["control_host"], m73["control_port"],
            on_event=self._on_modem_event,
            on_disconnect=lambda: self.event_q.put(("disconnect", None)),
        )
        for _ in range(30):
            if self._modem_connect_stop.is_set():
                return
            if client.connect():
                self.modem_client = client
                self.event_q.put(("connected", None))
                return
            time.sleep(1.0)
        self.event_q.put(("connect_failed", None))

    def _on_modem_event(self, frame):
        # called on the reader thread -- never touch Tkinter widgets here
        self.event_q.put(("rx_frame", frame))

    # -- polling loops (run on the Tk main thread) -----------------------

    def _poll_events(self):
        try:
            while True:
                kind, data = self.event_q.get_nowait()
                if kind == "rx_frame":
                    self._handle_rx_frame(data)
                elif kind == "disconnect":
                    self._log("[ui] lost connection to modem73 control port")
                elif kind == "connected":
                    self._log("[ui] connected to modem73 control port")
                elif kind == "connect_failed":
                    self._log("[ui] could not connect to modem73 control port after 30s")
                elif kind == "anytone_result":
                    ok, msg = data
                    self._log(f"[ui] anytone: {msg}" if ok else f"[ui] anytone FAILED: {msg}")
                    self._refresh_anytone_light()
                elif kind == "sleep_prepare":
                    self._handle_sleep_prepare(data)
                elif kind == "log":
                    self._log(data)
                elif kind == "network_status":
                    self._handle_network_status(data)
                elif kind == "mercury_result":
                    ok, msg = data
                    self._log(f"[ui] {msg}" if ok else f"[ui] Mercury FAILED: {msg}")
                    if ok:
                        self._set_mail_link("idle", "IDLE -- listening")
                    else:
                        self.stack.stop_mercury()
                        self._set_mail_link("failed", "FAILED -- see log")
                    self._refresh_mail()
                elif kind == "mail_ready":
                    self._mail_ready(*data)
        except queue.Empty:
            pass

        # tail process logs
        for proc in (self.stack.rnsd, self.stack.rigctld, self.stack.modem73, self.stack.meshchatx,
                     self.stack.mercury, self.stack.connector):
            if proc is None:
                continue
            total, lines = proc.log.since(getattr(proc, "_last_logged_len", 0))
            for line in lines:
                self._log(line)
                if proc is self.stack.connector or proc is self.stack.mercury:
                    state = hf_mail.link_state_from_line(line)
                    if state:
                        self._set_mail_link(*state)
                    act = hf_mail.activity_from_line(line)
                    if act:
                        self._apply_activity(act)
            proc._last_logged_len = total

        self.root.after(200, self._poll_events)

    def _handle_rx_frame(self, frame):
        ts = datetime.now().strftime("%H:%M:%S")
        callsign = frame.get("callsign", "?")
        mode = frame.get("mode", "?")
        snr = frame.get("snr")
        snr_txt = f"{snr:.1f}" if isinstance(snr, (int, float)) else "?"
        self.heard_tree.insert("", 0, values=(ts, callsign, mode, snr_txt))
        children = self.heard_tree.get_children()
        if len(children) > 50:
            self.heard_tree.delete(children[-1])

    def _poll_status(self):
        if self.modem_client and self.modem_client.connected:
            threading.Thread(target=self._status_worker, daemon=True).start()
        elif self.stack.mercury_running:
            # Mercury mode: show the radio's dial from our rigctld
            threading.Thread(target=self._read_rig_freq, daemon=True).start()
        self._refresh_anytone_light()
        try:
            self._refresh_mail()
        except OSError as e:
            print(f"[mail] refresh failed: {e}", flush=True)
        self._update_keep_awake()
        self.root.after(2000, self._poll_status)

    def _status_worker(self):
        status = self.modem_client.get_status()
        freq = self.modem_client.get_freq_hz()
        if status:
            self.root.after(0, lambda: self._apply_status(status))
        if freq:
            self.root.after(0, lambda: self.freq_readout.configure(text=f"{freq / 1_000_000:.6f} MHz"))

    def _apply_status(self, status):
        state = status.get("channel_state", "idle")
        self.channel_light.set_color(CHANNEL_COLORS.get(state, GRAY))
        self.channel_state_label.configure(text=state.upper())

        snr = status.get("last_snr")
        self.readouts["SNR"].configure(text=f"{snr:.1f} dB" if isinstance(snr, (int, float)) else "-- dB")

        ber = status.get("ber_ema")
        self.readouts["BER"].configure(text=f"{ber:.4f}" if isinstance(ber, (int, float)) and ber >= 0 else "--")

        pop = status.get("population")
        self.readouts["Population"].configure(text=str(pop) if pop is not None else "--")

        occ = status.get("occupancy_pct")
        self.readouts["Occupancy"].configure(text=f"{occ}%" if occ is not None else "--%")

        self.rig_light.set_color(GREEN if status.get("rigctl_connected") else RED)
        self.audio_light.set_color(GREEN if status.get("audio_connected") else RED)

    # -- network status (rnstatus + rnsd/nomadnet health) ------------------
    #
    # rnsd runs as a plain background job on this laptop (not systemd), and
    # the nomadnet propagation node runs as a systemd --user service, so
    # neither is visible through OffGridStack's own subprocess management --
    # this shells out read-only to check on them, the same way you'd type
    # `rnstatus` or `systemctl --user status nomadnet` by hand.

    def refresh_network_status(self):
        threading.Thread(target=self._network_status_worker, daemon=True).start()

    def _poll_network_status(self):
        if self.net_status_auto_var.get():
            threading.Thread(target=self._network_status_worker, daemon=True).start()
        self.root.after(10000, self._poll_network_status)

    def _network_status_worker(self):
        rnstatus_bin = self.stack._venv_bin("rnstatus")
        try:
            result = subprocess.run([rnstatus_bin], capture_output=True, text=True, timeout=8)
            text = (result.stdout or result.stderr or "(no output)").strip()
        except FileNotFoundError:
            text = f"(rnstatus not found at {rnstatus_bin})"
        except subprocess.TimeoutExpired:
            text = "(rnstatus timed out after 8s)"
        except OSError as e:
            text = f"(rnstatus failed: {e})"

        self.event_q.put(("network_status", {
            "text": text,
            "rnsd_alive": self._check_proc_alive("rnsd"),
            "nomadnet_alive": self._check_systemd_user_active("nomadnet.service"),
        }))

    @staticmethod
    def _check_proc_alive(name):
        """True/False if we could tell, None if pgrep itself isn't available --
        distinguished so the light can show gray (unknown) instead of a false red.

        Uses -f (full command line) rather than -x (exact process name):
        a console-script like rnsd is a shebang-invoked Python script, so the
        kernel's actual process name (comm) is usually the interpreter itself
        ("python3"), not "rnsd" -- an exact-name match would never find it
        even while it's running. pgrep excludes its own PID from results by
        default, so this doesn't self-match the way `ps aux | grep` can."""
        try:
            result = subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5)
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    @staticmethod
    def _check_systemd_user_active(unit):
        """True/False if the unit exists, None (gray light) if it isn't
        installed on this machine at all -- e.g. the tablet, which has no
        NomadNet propagation node -- so a missing unit isn't shown as a
        failure."""
        try:
            result = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState",
                                     "-p", "ActiveState", unit],
                                     capture_output=True, text=True, timeout=5)
            props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if props.get("LoadState") == "not-found":
                return None
            return props.get("ActiveState") == "active"
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def _handle_network_status(self, data):
        def light_color(v):
            return GREEN if v is True else (RED if v is False else GRAY)

        self.rnsd_light.set_color(light_color(data["rnsd_alive"]))
        self.nomadnet_light.set_color(light_color(data["nomadnet_alive"]))

        self.net_status_text.configure(state="normal")
        self.net_status_text.delete("1.0", "end")
        self.net_status_text.insert("1.0", data["text"])
        self.net_status_text.configure(state="disabled")


def run():
    # className sets WM_CLASS so the .desktop launcher's StartupWMClass can
    # match this window for GNOME dock pinning, same pattern as the amp
    # monitor and Shack Services apps. Tk reshapes this string rather than
    # using it verbatim: confirmed via `xprop WM_CLASS` that "OffGridComms"
    # here produces instance="offGridComms" (first letter lowercased) and
    # class="Offgridcomms" (first letter kept, rest lowercased). GNOME
    # matches on the class component, so offgrid-comms.desktop's
    # StartupWMClass is set to "Offgridcomms" -- exactly, case-sensitive --
    # to match. If this className string ever changes, re-check with
    # xprop rather than assuming the same transform applies.
    root = tk.Tk(className="OffGridComms")
    # Belt-and-suspenders: set the window's own icon hint directly too,
    # rather than relying entirely on WM_CLASS/.desktop matching (which
    # needs the .desktop file actually installed into a location GNOME
    # scans -- see offgrid-comms.desktop's install notes).
    try:
        icon_img = tk.PhotoImage(file="assets/icon.png")
        root.iconphoto(True, icon_img)
    except tk.TclError:
        pass
    OffGridCommsApp(root)
    root.mainloop()
