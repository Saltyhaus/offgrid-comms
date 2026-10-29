"""
OffGrid Comms -- GTK 4 / libadwaita user interface.

Same backend as the Tk version (process_manager, modem_control, hf_mail,
bt_kiss, config, levelcheck -- none of them changed); only the window is
new. Why: every machine that runs this is on GNOME Wayland, and Tk runs
there through XWayland, where touch only arrives as XI2 touch events that
Tk never turns into clicks, Tk entries never raise the on-screen keyboard,
and there's no kinetic scrolling. GTK 4 is native Wayland: touch, swipe
scrolling, the GNOME on-screen keyboard and fractional scaling all just
work, on the tablet and the laptop alike.

What changed from the Tk window, on purpose:
  * pages (HF Mail / Radio / Mesh / Log) instead of one long scroll; on a
    narrow screen (tablet in portrait) the page switcher moves to the
    bottom, like a phone app
  * a status strip under the title bar that's always visible: Mercury,
    connector, link, TX, new message, modem73, rnsd
  * the message box is a chat with a compose bar; the GNOME keyboard
    pops up when you tap it -- no custom on-screen pads any more
  * no tray icon: GNOME on Wayland has no tray, and pystray's
    appindicator backend is GTK 3, which can't share a process with
    GTK 4. Closing the window while something is on the air asks whether
    to keep it running in the background; the launcher brings it back.
  * no "Always on top": Wayland doesn't let apps put themselves on top
    (that also removes the lid-close lock-screen problem the Tk version
    had to work around, so the sleep watcher is gone too)
  * starting Mercury with messages in the outbox asks first, because
    they go out on the air as soon as the connector is up

Rollback: run.sh starts this UI; `OGC_UI=tk ./run.sh` (or ui_toolkit:
"tk" in config.json) starts the old one. If GTK 4 / libadwaita aren't
installed, main.py falls back to Tk by itself.
"""

import os
import queue
import re
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import config as cfgmod  # noqa: E402
import hf_mail  # noqa: E402
from bt_kiss import AnytoneKissTnc  # noqa: E402
from modem_control import ModemControlClient  # noqa: E402
from process_manager import OffGridStack, SleepInhibitor  # noqa: E402
from systems_page import SystemsPage  # noqa: E402

APP_ID = "net.ag7jc.OffGridComms"

# Adw.ToolbarView / Adw.Breakpoint arrived in libadwaita 1.4 (Ubuntu 24.04
# ships 1.5). main.py checks this and falls back to the Tk UI if it's older.
MIN_ADW = (1, 4)

BG = "#12161a"
PANEL_BG = "#1a1f24"
FG = "#cfd8dc"
TEAL = "#00d9d9"
ORANGE = "#ff9500"
GREEN = "#33cc66"
RED = "#ff4444"
GRAY = "#5a6570"

LIGHT_CLASSES = ("gray", "green", "red", "orange", "teal")
CHANNEL_LIGHT = {"idle": "gray", "rx": "teal", "tx": "orange"}
LINK_STYLE = {"off": "gray", "idle": "teal", "calling": "orange", "connected": "green", "failed": "red"}

CSS = f"""
@define-color accent_color {TEAL};
@define-color accent_bg_color #009a9a;
@define-color accent_fg_color #ffffff;
@define-color window_bg_color {BG};
@define-color window_fg_color {FG};
@define-color view_bg_color #0c0f12;
@define-color view_fg_color {FG};
@define-color headerbar_bg_color {BG};
@define-color headerbar_fg_color {FG};
@define-color card_bg_color {PANEL_BG};
@define-color card_fg_color {FG};
@define-color popover_bg_color {PANEL_BG};
@define-color dialog_bg_color {PANEL_BG};
@define-color destructive_bg_color #c62828;
@define-color success_bg_color #1f8f4a;

.ogc-title {{ color: {ORANGE}; font-family: monospace; font-weight: bold; letter-spacing: 1px; }}
.ogc-call  {{ color: {TEAL}; font-family: monospace; font-weight: bold; }}
.mono      {{ font-family: monospace; }}
.readout   {{ font-family: monospace; color: {TEAL}; }}
.dimmed    {{ opacity: 0.65; }}
.c-gray    {{ color: {GRAY}; }}
.c-green   {{ color: {GREEN}; }}
.c-red     {{ color: {RED}; }}
.c-orange  {{ color: {ORANGE}; }}
.c-teal    {{ color: {TEAL}; }}

.light {{ min-width: 12px; min-height: 12px; border-radius: 999px; background: {GRAY}; }}
.light.green  {{ background: {GREEN};  box-shadow: 0 0 6px {GREEN}; }}
.light.red    {{ background: {RED};    box-shadow: 0 0 6px {RED}; }}
.light.orange {{ background: {ORANGE}; box-shadow: 0 0 6px {ORANGE}; }}
.light.teal   {{ background: {TEAL};   box-shadow: 0 0 6px {TEAL}; }}

.status-strip {{ background: {PANEL_BG}; padding: 4px 10px; border-bottom: 1px solid #262d33; }}
.status-strip label {{ font-size: 0.9em; }}
.linkstate {{ font-family: monospace; font-weight: bold; }}

.chat {{ background: #0c0f12; }}
.bubble {{ border-radius: 14px; padding: 6px 12px; }}
.bubble.in     {{ background: #0e3b3d; }}
.bubble.out    {{ background: #4a2f08; }}
.bubble.queued {{ background: #262c32; }}
.bubble .meta  {{ font-size: 0.8em; opacity: 0.7; }}
.bubble .who   {{ font-weight: bold; color: {TEAL}; }}
.compose {{ padding: 6px; background: {PANEL_BG}; border-top: 1px solid #262d33; }}

/* solid colors replace libadwaita's own hover/pressed shading, so give
   them their own -- otherwise a tap looks like nothing happened */
.start-btn, .stop-btn, .busy-btn {{ color: white; font-weight: bold; background-image: none;
                                    transition: background-color 90ms, box-shadow 90ms; }}
.start-btn         {{ background-color: #1f8f4a; }}
.start-btn:hover   {{ background-color: #28a85a; }}
.start-btn:active  {{ background-color: #136134; box-shadow: inset 0 3px 6px rgba(0,0,0,0.55); }}
.stop-btn          {{ background-color: #b71c1c; }}
.stop-btn:hover    {{ background-color: #d32626; }}
.stop-btn:active   {{ background-color: #7f1010; box-shadow: inset 0 3px 6px rgba(0,0,0,0.55); }}
.busy-btn, .busy-btn:disabled {{ background-color: #3a444d; color: #e0e6ea; }}
.start-btn:disabled, .stop-btn:disabled {{ opacity: 0.5; }}
.mode-btn:checked {{ background: #009a9a; color: white; }}
.setup-btn {{ font-weight: bold; min-width: 64px; }}
.setup-btn.current {{ background-color: #009a9a; color: white; }}
.setup-btn:active {{ background-color: #00797a; color: white; box-shadow: inset 0 3px 6px rgba(0,0,0,0.55); }}
.hf-label {{ font-weight: bold; color: {GRAY}; }}
.dest-btn:checked {{ background: #009a9a; color: white; }}

textview.log, textview.netstat {{ font-family: monospace; font-size: 0.85em; }}
.heard-head label {{ font-weight: bold; color: {GRAY}; }}
"""


def touchscreen_present():
    """udev's database is world-readable and already tags the tablet's
    panel as a touchscreen -- same check as touch_ui.py (copied so this
    module doesn't import tkinter)."""
    import glob
    for path in glob.glob("/run/udev/data/c13:*"):
        try:
            with open(path, errors="replace") as f:
                if "E:ID_INPUT_TOUCHSCREEN=1" in f.read():
                    return True
        except OSError:
            continue
    return False


def adw_ok():
    return (Adw.get_major_version(), Adw.get_minor_version()) >= MIN_ADW


# -- small widget helpers ---------------------------------------------------

class Light(Gtk.Box):
    def __init__(self, color="gray"):
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_css_class("light")
        self._color = None
        self.set_color(color)

    def set_color(self, color):
        if color == self._color:
            return
        for c in LIGHT_CLASSES:
            self.remove_css_class(c)
        if color != "gray":
            self.add_css_class(color)
        self._color = color


def set_text_color(widget, color):
    for c in LIGHT_CLASSES:
        widget.remove_css_class("c-" + c)
    if color:
        widget.add_css_class("c-" + color)


def lit(label_text, color="gray"):
    """A light with a caption, for the status strip / rows."""
    box = Gtk.Box(spacing=5)
    light = Light(color)
    box.append(light)
    box.append(Gtk.Label(label=label_text))
    return box, light


def button(label, cb, css=None, tooltip=None, icon=None):
    b = Gtk.Button()
    if icon:
        content = Adw.ButtonContent(icon_name=icon, label=label)
        b.set_child(content)
    else:
        b.set_label(label)
    if css:
        for c in css.split():
            b.add_css_class(c)
    if tooltip:
        b.set_tooltip_text(tooltip)
    b.connect("clicked", lambda _b: cb())
    return b


def wrap_label(text, css="dimmed", xalign=0.0):
    lbl = Gtk.Label(label=text, wrap=True, xalign=xalign)
    lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        for c in css.split():
            lbl.add_css_class(c)
    return lbl


def text_view(css, monospace=True, wrap=Gtk.WrapMode.NONE):
    tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=monospace, wrap_mode=wrap)
    tv.add_css_class(css)
    tv.set_left_margin(8)
    tv.set_right_margin(8)
    tv.set_top_margin(6)
    tv.set_bottom_margin(6)
    return tv


# -- the app -------------------------------------------------------------

class OffGridWindow:
    def __init__(self, app):
        self.app = app
        self.cfg = cfgmod.load_config()
        self.stack = OffGridStack(self.cfg)
        self.anytone = AnytoneKissTnc(self.cfg)
        self.inhibitor = SleepInhibitor()
        self.modem_client = None
        self._modem_connect_stop = threading.Event()
        self.event_q = queue.Queue()
        self._quitting = False

        ui = self.cfg.get("ui") or {}
        prof = (ui.get("profile") or "auto").lower()
        self.touch = (prof == "touch") or (prof == "auto" and touchscreen_present())
        print(f"[ui] GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
              f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}, "
              f"profile {'touch' if self.touch else 'desktop'}", flush=True)

        radios = list(self.cfg["radios"].keys())
        last = self.cfg.get("last_radio")
        self.radio_names = radios
        self._radio = last if last in radios else (radios[0] if radios else "")
        self._mode = self.cfg.get("last_mode", "Normal")
        if self._mode not in self.cfg["modes"]:
            self._mode = next(iter(self.cfg["modes"]), "Normal")

        self._rxmsg_until = 0.0
        self._link_state = "off"
        self._signal = {"snr": None, "bps": None}
        self._mail_sig = None
        self._last_queued = None
        self._heard_rows = []
        self._dialogs_open = 0

        self._install_css(ui)
        self._build_window()
        self._refresh_anytone_light()

        threading.Thread(target=self._start_rnsd_worker, daemon=True).start()
        GLib.timeout_add(200, self._poll_events)
        GLib.timeout_add(2000, self._poll_status)
        GLib.timeout_add(1500, self._poll_network_once)
        GLib.timeout_add(10000, self._poll_network_status)
        self._refresh_mail()

    # ---------------------------------------------------------------- setup

    def _install_css(self, ui):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = CSS
        # GTK already follows GNOME's display scaling and text-size setting.
        # On top of that the touch profile makes text and targets a bit
        # bigger; ui.gtk_scale in config.json overrides the factor.
        scale = ui.get("gtk_scale")
        try:
            scale = float(scale) if scale is not None else (1.2 if self.touch else 1.0)
        except (TypeError, ValueError):
            scale = 1.2 if self.touch else 1.0
        scale = min(max(scale, 0.7), 2.5)
        if abs(scale - 1.0) > 0.01:
            css += f"\nwindow.ogc {{ font-size: {11 * scale:.1f}pt; }}\n"
        if self.touch:
            css += ("\nwindow.ogc button { min-height: 40px; min-width: 40px; }"
                    "\nwindow.ogc .light { min-width: 14px; min-height: 14px; }\n")
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(css)          # GTK >= 4.12
        except AttributeError:
            provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_window(self):
        win = Adw.ApplicationWindow(application=self.app, title="OffGrid Comms")
        win.add_css_class("ogc")
        win.set_default_size(*((900, 1000) if not self.touch else (800, 1200)))
        win.set_size_request(360, 480)
        win.connect("close-request", self._on_close_request)
        self.win = win
        self._install_actions()

        self.view = Adw.ViewStack()
        self.view.set_vexpand(True)

        # header: title + page switcher (wide) / title only (narrow)
        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(stack=self.view, policy=Adw.ViewSwitcherPolicy.WIDE)
        title_box = Gtk.Box(spacing=10)
        t = Gtk.Label(label="OFFGRID COMMS")
        t.add_css_class("ogc-title")
        c = Gtk.Label(label=self.cfg.get("mercury", {}).get("mycall") or self.cfg.get("callsign", ""))
        c.add_css_class("ogc-call")
        title_box.append(t)
        title_box.append(c)
        header.pack_start(title_box)
        header.set_title_widget(self.switcher)

        menu = Gio.Menu()
        tools = Gio.Menu()
        tools.append("Level check…", "app.levelcheck")
        tools.append("List sound cards", "app.soundcards")
        tools.append("Save log to file", "app.savelog")
        menu.append_section(None, tools)
        win_sec = Gio.Menu()
        win_sec.append("Hide window (keep running)", "app.hide")
        win_sec.append("Stop everything and quit", "app.quit")
        menu.append_section(None, win_sec)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu")
        header.pack_end(mb)

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.view)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(self._build_status_strip())
        self.toasts = Adw.ToastOverlay(child=self.view)
        tv.set_content(self.toasts)
        tv.add_bottom_bar(self.switcher_bar)
        win.set_content(tv)

        # narrow (tablet portrait / small window): switcher goes to the bottom
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        bp.add_setter(self.switcher_bar, "reveal", True)
        bp.add_setter(self.switcher, "visible", False)
        win.add_breakpoint(bp)

        self.view.add_titled_with_icon(self._build_mail_page(), "mail", "HF Mail", "mail-unread-symbolic")
        self.view.add_titled_with_icon(self._build_radio_page(), "radio", "Radio", "network-wireless-symbolic")
        self.view.add_titled_with_icon(self._build_mesh_page(), "mesh", "Mesh", "network-workgroup-symbolic")
        self.systems = SystemsPage(self, Light)
        self.view.add_titled_with_icon(self.systems.build(), "systems", "Systems", "emblem-system-symbolic")
        self.view.add_titled_with_icon(self._build_log_page(), "log", "Log", "utilities-terminal-symbolic")
        self.mail_page = self.view.get_page(self.view.get_child_by_name("mail"))
        self.view.connect("notify::visible-child-name", self._on_page_changed)

        win.present()

    def _install_actions(self):
        for name, cb in (("levelcheck", self.open_level_check),
                         ("soundcards", self.list_sound_cards),
                         ("savelog", self.save_log),
                         ("hide", self.hide_window),
                         ("quit", self.quit_app)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.app.add_action(a)

    # ------------------------------------------------------------ status strip

    def _build_status_strip(self):
        strip = Gtk.Box(spacing=14)
        strip.add_css_class("status-strip")
        # one row; on a narrow screen it scrolls sideways (swipe) instead of
        # wrapping -- a FlowBox here made the top bar reserve its tallest
        # wrapped height
        flow = Gtk.Box(spacing=14)
        lights_scroll = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                           hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                           propagate_natural_height=True)
        self.lights = {}
        for key, text in (("mercury", "Mercury"), ("connector", "connector"), ("link", "link"),
                          ("tx", "TX"), ("rxmsg", "new msg"), ("modem73", "modem73"),
                          ("rnsd", "rnsd")):
            box, light = lit(text)
            self.lights[key] = light
            flow.append(box)
        lights_scroll.set_child(flow)
        strip.append(lights_scroll)
        right = Gtk.Box(spacing=10, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.mail_signal_label = Gtk.Label(label="")
        self.mail_signal_label.add_css_class("mono")
        self.mail_link_label = Gtk.Label(label="OFF", xalign=1)
        self.mail_link_label.add_css_class("linkstate")
        set_text_color(self.mail_link_label, "gray")
        right.append(self.mail_signal_label)
        right.append(self.mail_link_label)
        strip.append(right)
        return strip

    # --------------------------------------------------------------- HF mail

    def _build_mail_page(self):
        mc = self.cfg["mercury"]
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_start=10, margin_end=10, margin_top=8, margin_bottom=6)

        # Mercury on/off + radio in use
        row = Gtk.Box(spacing=8)
        hl = Gtk.Label(label="HF")
        hl.add_css_class("hf-label")
        row.append(hl)
        row.append(self._hf_selector())
        self.mail_radio_label = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.mail_radio_label.add_css_class("dimmed")
        row.append(self.mail_radio_label)
        top.append(row)

        # destination
        home = mc["peers"][0] if mc.get("peers") else "HOME"
        self.mail_dests = [(home, "home", None)]
        self.mail_dests += [(s, "via home", s) for s in mc.get("field_stations", [])
                            if s.upper() != (mc.get("mycall") or "").upper()]
        dest_row = Gtk.Box(spacing=6)
        dest_row.append(Gtk.Label(label="To:"))
        dest_box = Gtk.Box(spacing=0, hexpand=True, homogeneous=True)
        dest_box.add_css_class("linked")
        self.dest_buttons = []
        self.dest_notes = []
        first = None
        for call, note, _to in self.mail_dests:
            b = Gtk.ToggleButton()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            l1 = Gtk.Label(label=call)
            l1.add_css_class("heading")
            l2 = Gtk.Label(label=note)
            l2.add_css_class("caption")
            inner.append(l1)
            inner.append(l2)
            b.set_child(inner)
            b.add_css_class("dest-btn")
            b.connect("toggled", self._on_dest_toggled)
            self.dest_notes.append((l2, note))
            if first:
                b.set_group(first)
            else:
                first = b
            dest_box.append(b)
            self.dest_buttons.append(b)
        self._chat_cache = None
        if first:
            first.set_active(True)
        dest_row.append(dest_box)
        top.append(dest_row)

        # outbox
        ob = Gtk.Box(spacing=8)
        self.mail_queue_label = Gtk.Label(label="Outbox: empty", xalign=0, hexpand=True)
        ob.append(self.mail_queue_label)
        self.clear_outbox_btn = button("Clear outbox", self.clear_mail_queue, "flat destructive-action",
                                       icon="user-trash-symbolic")
        ob.append(self.clear_outbox_btn)
        ob.append(button("", self.open_mail_inbox, "flat", tooltip="Open the messages folder",
                         icon="folder-open-symbolic"))
        top.append(ob)
        page.append(top)

        # chat
        self.chat_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.chat_list.add_css_class("chat")
        self.chat_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.chat_scroll.set_child(Adw.Clamp(maximum_size=1100, tightening_threshold=900,
                                             child=self.chat_list))
        self.chat_scroll.add_css_class("chat")
        page.append(self.chat_scroll)

        # compose bar: quick messages, text, GPS, send
        comp = Gtk.Box(spacing=6)
        comp.add_css_class("compose")
        quick_pop = Gtk.Popover()
        qgrid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2,
                            min_children_per_line=2, homogeneous=True, column_spacing=6, row_spacing=6,
                            margin_start=6, margin_end=6, margin_top=6, margin_bottom=6)
        items = [(q, lambda q=q: self.queue_mail(q)) for q in mc.get("quick_messages", [])]
        items += [("Position report", lambda: self.queue_mail("Position report", need_gps=True)),
                  ("WX here", lambda: self.queue_mail(None, wx="WX")),
                  ("Marine WX", lambda: self.queue_mail(None, wx="WXM"))]

        def quick(cb):
            quick_pop.popdown()
            cb()
        for text, cb in items:
            b = button(text, lambda cb=cb: quick(cb))
            if text in ("Position report", "WX here", "Marine WX"):
                b.add_css_class("suggested-action")
            qgrid.append(b)
        quick_pop.set_child(qgrid)
        qb = Gtk.MenuButton(icon_name="view-list-bullet-symbolic", popover=quick_pop,
                            tooltip_text="Quick messages, position, weather", direction=Gtk.ArrowType.UP)
        comp.append(qb)

        self.compose_entry = Gtk.Entry(hexpand=True, placeholder_text="Message…")
        self.compose_entry.connect("activate", lambda _e: self._send_compose())
        comp.append(self.compose_entry)

        self.gps_toggle = Gtk.ToggleButton(icon_name="find-location-symbolic",
                                           tooltip_text="Attach my GPS position")
        self.gps_toggle.set_active(bool(mc.get("attach_gps", True)))
        comp.append(self.gps_toggle)
        comp.append(button("", self._send_compose, "suggested-action", tooltip="Queue for HF",
                           icon="mail-send-symbolic"))
        page.append(comp)
        self._update_mail_radio_label()
        return page

    def _selected_dest(self):
        for i, b in enumerate(self.dest_buttons):
            if b.get_active():
                return self.mail_dests[i]
        return self.mail_dests[0]

    def _update_mail_radio_label(self):
        self.mail_radio_label.set_label(
            f"{self._radio} · audio {self.stack.mercury_audio_device(self._radio)}"
            if self._radio else "no radio configured")

    # ----------------------------------------------------------------- radio

    def _build_radio_page(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Radio")
        self.radio_row = Adw.ComboRow(title="Rig", model=Gtk.StringList.new(self.radio_names))
        if self._radio in self.radio_names:
            self.radio_row.set_selected(self.radio_names.index(self._radio))
        self.radio_row.connect("notify::selected", self._on_radio_selected)
        g.add(self.radio_row)

        links = Adw.ActionRow(title="Rig link / audio", subtitle="from modem73 while it runs")
        rl_box, self.rig_light = lit("rig")
        au_box, self.audio_light = lit("audio")
        links.add_suffix(rl_box)
        links.add_suffix(au_box)
        g.add(links)

        self.freq_row = Adw.EntryRow(title="Set frequency (MHz)", show_apply_button=True)
        self.freq_row.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self.freq_row.connect("apply", lambda _r: self.set_frequency())
        self.freq_row.connect("entry-activated", lambda _r: self.set_frequency())
        g.add(self.freq_row)

        dial = Adw.ActionRow(title="Radio dial")
        self.freq_readout = Gtk.Label(label="-- . --- MHz")
        self.freq_readout.add_css_class("readout")
        self.freq_readout.add_css_class("title-3")
        dial.add_suffix(self.freq_readout)
        g.add(dial)
        page.add(g)

        # Reticulum HF stack
        g = Adw.PreferencesGroup(title="HF radio",
                                 description="Who has the HF radio. Mercury = HF mail. Reticulum = rigctld + "
                                             "modem73 (and MeshChatX, if it isn't running yet). They both need "
                                             "the radio, so only one runs at a time; OFF leaves MeshChatX up.")
        sel = self._hf_selector()
        sel.set_margin_top(4)
        sel.set_margin_bottom(4)
        g.add(sel)
        page.add(g)

        # mode
        g = Adw.PreferencesGroup(title="modem73 mode")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           max_children_per_line=4, min_children_per_line=2,
                           column_spacing=6, row_spacing=6)
        self.mode_buttons = {}
        first = None
        for name in self.cfg["modes"]:
            b = Gtk.ToggleButton(label=name)
            b.add_css_class("mode-btn")
            if first:
                b.set_group(first)
            else:
                first = b
            b.set_active(name == self._mode)
            b.connect("toggled", lambda b, n=name: b.get_active() and self.select_mode(n))
            flow.append(b)
            self.mode_buttons[name] = b
        g.add(flow)
        self.mode_desc = wrap_label("")
        self.mode_desc.set_margin_top(6)
        g.add(self.mode_desc)
        self._refresh_mode_desc()
        page.add(g)

        # link status
        g = Adw.PreferencesGroup(title="Link status (modem73)")
        ch = Adw.ActionRow(title="Channel")
        self.channel_light = Light()
        ch.add_prefix(self.channel_light)
        self.channel_state_label = Gtk.Label(label="IDLE")
        self.channel_state_label.add_css_class("readout")
        ch.add_suffix(self.channel_state_label)
        g.add(ch)
        self.readouts = {}
        for name, default in (("SNR", "-- dB"), ("BER", "--"), ("Population", "--"), ("Occupancy", "--%")):
            r = Adw.ActionRow(title=name)
            v = Gtk.Label(label=default)
            v.add_css_class("readout")
            r.add_suffix(v)
            g.add(r)
            self.readouts[name] = v
        page.add(g)

        # heard stations
        g = Adw.PreferencesGroup(title="Heard stations")
        self.heard_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.heard_list.add_css_class("boxed-list")
        self.heard_list.set_placeholder(wrap_label("Nothing heard yet.", xalign=0.5))
        head = self._heard_row(("TIME", "CALLSIGN", "MODE", "SNR"))
        head.add_css_class("heard-head")
        g.add(head)
        g.add(self.heard_list)
        page.add(g)
        return page

    def _heard_row(self, values):
        box = Gtk.Box(spacing=8, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        for v, width, expand in zip(values, (8, 10, 12, 6), (False, True, True, False)):
            lbl = Gtk.Label(label=v, xalign=0, width_chars=width, hexpand=expand,
                            ellipsize=Pango.EllipsizeMode.END)
            lbl.add_css_class("mono")
            box.append(lbl)
        return box

    # ------------------------------------------------------------------ mesh

    def _build_mesh_page(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="MeshChatX",
                                 description="On its own, without rigctld/modem73 -- for the anchor's "
                                             "TCP link, LAN or the Anytone TNC.")
        fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         max_children_per_line=4, min_children_per_line=2,
                         column_spacing=6, row_spacing=6)
        self.mcx_btn = Gtk.Button()
        self.mcx_btn.add_css_class("start-btn")
        mcx_box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.mcx_spinner = Gtk.Spinner(visible=False)
        self.mcx_label = Gtk.Label(label="Start MeshChatX")
        mcx_box.append(self.mcx_spinner)
        mcx_box.append(self.mcx_label)
        self.mcx_btn.set_child(mcx_box)
        self.mcx_btn.connect("clicked", lambda _b: self._toggle_meshchatx())
        self._mcx_busy = None            # None, "starting" or "stopping"
        fb.append(self.mcx_btn)
        fb.append(button("Open", self.open_meshchatx, "suggested-action", icon="web-browser-symbolic"))
        fb.append(button("Announce", self.announce_meshchatx, None, icon="mark-location-symbolic"))
        g.add(fb)
        page.add(g)

        g = Adw.PreferencesGroup(
            title="VHF/UHF -- Anytone (Bluetooth KISS)",
            description="Not part of the HF stack: binds the radio's Bluetooth TNC to /dev/rfcomm0 "
                        "for Reticulum. If Connect says \"already bound\" but the link is dead "
                        "(radio was out of range), use Reconnect.")
        r = Adw.ActionRow(title="TNC", subtitle=f"{self.anytone.mac}  ch{self.anytone.channel}")
        self.anytone_light = Light()
        r.add_prefix(self.anytone_light)
        self.anytone_status_label = Gtk.Label(label="not bound")
        r.add_suffix(self.anytone_status_label)
        g.add(r)
        bx = Gtk.Box(spacing=6, homogeneous=True, margin_top=6)
        bx.append(button("Connect", self.connect_anytone, "suggested-action"))
        bx.append(button("Disconnect", self.disconnect_anytone, "destructive-action"))
        bx.append(button("Reconnect", self.reconnect_anytone))
        g.add(bx)
        page.add(g)

        g = Adw.PreferencesGroup(title="Network status")
        hdr = Gtk.Box(spacing=6)
        hdr.append(button("", self.refresh_network_status, "flat", tooltip="Refresh now",
                          icon="view-refresh-symbolic"))
        g.set_header_suffix(hdr)
        r = Adw.ActionRow(title="Reticulum")
        b1, self.rnsd_light = lit("rnsd")
        b2, self.nomadnet_light = lit("propagation (nomadnet)")
        r.add_suffix(b1)
        r.add_suffix(b2)
        g.add(r)
        self.net_auto_row = Adw.SwitchRow(title="Auto-refresh every 10 s", active=True)
        g.add(self.net_auto_row)
        self.net_status_text = text_view("netstat")
        sw = Gtk.ScrolledWindow(min_content_height=220, margin_top=8)
        sw.set_child(self.net_status_text)
        sw.add_css_class("card")
        g.add(sw)
        page.add(g)
        return page

    # ------------------------------------------------------------------- log

    def _build_log_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.log_view = text_view("log", wrap=Gtk.WrapMode.WORD_CHAR if self.touch else Gtk.WrapMode.NONE)
        self.log_buf = self.log_view.get_buffer()
        self.log_end = self.log_buf.create_mark("end", self.log_buf.get_end_iter(), False)
        self.log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroll.set_child(self.log_view)
        page.append(self.log_scroll)
        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        self.log_follow = Gtk.CheckButton(label="Follow", active=True)
        bar.append(self.log_follow)
        bar.append(Gtk.Box(hexpand=True))
        bar.append(button("Copy", self.copy_log, None, icon="edit-copy-symbolic"))
        bar.append(button("Save to file", self.save_log, None, icon="document-save-symbolic"))
        page.append(bar)
        return page

    # -------------------------------------------------------------- dialogs

    def ask(self, heading, body, on_response, responses, default=None):
        """responses: [(id, label, style)] style in None/'suggested'/'destructive'.
        on_response(id) runs on the main loop. The first entry is the cancel/close one."""
        styles = {"suggested": Adw.ResponseAppearance.SUGGESTED,
                  "destructive": Adw.ResponseAppearance.DESTRUCTIVE}
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog(heading=heading, body=body)
        else:
            d = Adw.MessageDialog(transient_for=self.win, heading=heading, body=body)
        for rid, label, style in responses:
            d.add_response(rid, label)
            if style:
                d.set_response_appearance(rid, styles[style])
        d.set_close_response(responses[0][0])
        if default:
            d.set_default_response(default)
        d.set_body_use_markup(False)

        def done(_d, rid):
            self._dialogs_open -= 1
            on_response(rid)
        d.connect("response", done)
        self._dialogs_open += 1
        self.last_dialog = d          # for the test harness
        if hasattr(Adw, "AlertDialog"):
            d.present(self.win)
        else:
            d.present()

    def confirm(self, heading, body, on_yes, yes="OK", destructive=False):
        self.ask(heading, body, lambda rid: on_yes() if rid == "yes" else None,
                 [("no", "Cancel", None), ("yes", yes, "destructive" if destructive else "suggested")],
                 default="yes")

    def alert(self, heading, body):
        self.ask(heading, body, lambda _rid: None, [("ok", "OK", None)], default="ok")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ---------------------------------------------------------------- log

    def _log(self, line):
        self._log_lines([line])

    def _log_lines(self, lines):
        if not lines:
            return
        buf = self.log_buf
        buf.insert(buf.get_end_iter(), "\n".join(lines) + "\n")
        # keep it bounded -- Mercury logs a lot over a long session
        if buf.get_line_count() > 3000:
            start = buf.get_start_iter()
            cut = buf.get_iter_at_line(1000)
            cut = cut[1] if isinstance(cut, tuple) else cut
            buf.delete(start, cut)
        if self.log_follow.get_active():
            self.log_view.scroll_mark_onscreen(self.log_end)

    def _log_text(self):
        b = self.log_buf
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False)

    def save_log(self):
        path = Path.home() / "offgrid-comms-log.txt"
        try:
            path.write_text(self._log_text())
        except OSError as e:
            self.alert("Couldn't save the log", str(e))
            return
        self._log(f"[ui] log saved to {path}")
        self.toast(f"Log saved to {path}")

    def copy_log(self):
        Gdk.Display.get_default().get_clipboard().set(self._log_text())
        self.toast("Log copied")

    # ---------------------------------------------------------- window life

    def _anything_running(self):
        st = self.stack
        names = []
        if st.mercury_running:
            names.append("Mercury")
        if st.modem73_running:
            names.append("modem73")
        if st.meshchatx and st.meshchatx.is_running:
            names.append("MeshChatX")
        return names

    def _on_close_request(self, _win):
        if self._quitting:
            return False
        running = self._anything_running()
        if not running:
            self.quit_app()
            return True
        self.ask("Close OffGrid Comms?",
                 f"{', '.join(running)} {'is' if len(running) == 1 else 'are'} running. "
                 "Keep it going in the background (open OffGrid Comms again to get this window back), "
                 "or stop everything?",
                 lambda rid: {"bg": self.hide_window, "quit": self.quit_app}.get(rid, lambda: None)(),
                 [("cancel", "Cancel", None), ("quit", "Stop & quit", "destructive"),
                  ("bg", "Keep running", "suggested")], default="bg")
        return True

    def hide_window(self):
        self.app.hold_for_background()
        self.win.set_visible(False)

    def show_window(self):
        self.app.release_background()
        self.win.set_visible(True)
        self.win.present()

    def quit_app(self):
        self._quitting = True
        self._modem_connect_stop.set()
        self.inhibitor.release()
        try:
            self.systems.shutdown()
            self.stack.stop_mercury()
            self.stop_all()
            self.stack.stop_rnsd()
        finally:
            self.app.release_background()
            self.win.destroy()
            self.app.quit()

    def _on_page_changed(self, *_a):
        if self.view.get_visible_child_name() == "mail":
            self.mail_page.set_needs_attention(False)

    # -------------------------------------------------------------- Mercury

    # ------------------------------------------------- HF radio: who has it

    HF_CHOICES = (("off", "OFF"), ("mercury", "MERCURY"), ("reticulum", "RETICULUM"))

    def _hf_selector(self):
        """OFF | MERCURY | RETICULUM -- the same switch on HF Mail and Radio."""
        if not hasattr(self, "hf_selectors"):
            self.hf_selectors = []
            self._hf_busy = None
        box = Gtk.Box(homogeneous=True, hexpand=False)
        box.add_css_class("linked")
        btns = {}
        for key, label in self.HF_CHOICES:
            b = Gtk.Button(label=label)
            b.add_css_class("setup-btn")
            b.connect("clicked", lambda _b, k=key: self._hf_clicked(k))
            box.append(b)
            btns[key] = b
        self.hf_selectors.append(btns)
        return box

    def _hf_state(self):
        if self.stack.mercury_running:
            return "mercury"
        if self.stack.modem73_running:
            return "reticulum"
        return "off"

    def _update_hf_selector(self):
        cur, busy = self._hf_state(), self._hf_busy
        for btns in self.hf_selectors:
            for key, label in self.HF_CHOICES:
                b = btns[key]
                b.set_label("STARTING…" if busy == key else label)
                b.set_sensitive(not busy)
                if key == (busy or cur):
                    b.add_css_class("current")
                else:
                    b.remove_css_class("current")

    def _update_mercury_button(self):
        self._update_hf_selector()

    def _hf_clicked(self, target):
        if self._hf_busy:
            return
        if target == self._hf_state():
            self.toast({"off": "HF radio is already off", "mercury": "Mercury is already running",
                        "reticulum": "Reticulum is already on HF"}[target])
            return
        if target == "mercury":
            self.start_mercury()          # outbox warning + stops modem73 (asks first)
        elif target == "reticulum":
            self.start_all()              # stops Mercury (asks first)
        else:
            self.hf_off()

    def _radio_plugged(self, radio):
        dev = self.cfg["radios"].get(radio, {}).get("device", "")
        if dev and not os.path.exists(dev):
            others = [name for name, r in self.cfg["radios"].items()
                      if name != radio and r.get("device") and os.path.exists(r["device"])]
            hint = (f"\n\nPlugged in right now: {', '.join(others)} -- pick it on the Radio page."
                    if others else "\n\nCheck the USB cable and that the radio is on.")
            self.alert(f"The {radio} isn't connected", f"Its serial port isn't there:\n{dev}{hint}")
            return False
        return True

    def hf_off(self):
        stopped = []
        if self.stack.mercury_running:
            self.stop_mercury()
            stopped.append("Mercury")
        if self.stack.modem73_running:
            self._modem_connect_stop.set()
            if self.modem_client:
                self.modem_client.close()
                self.modem_client = None
            self.stack.stop_modem73_and_rigctld()
            self._log("[ui] Reticulum on HF stopped (modem73 + rigctld) -- MeshChatX keeps running")
            stopped.append("Reticulum")
        self._update_hf_selector()
        self.toast(f"HF radio off ({' and '.join(stopped)} stopped)" if stopped else "HF radio is off")

    def start_mercury(self):
        radio = self._radio
        if not self._radio_plugged(radio):
            return

        def go():
            self._remember_radio(radio)
            if self.stack.modem73_running:
                self.confirm("modem73 is using the radio",
                             "Stop the Reticulum HF stack and start Mercury instead? MeshChatX keeps running.",
                             lambda: self._start_mercury_now(radio, stop_modem=True), yes="Start Mercury")
            else:
                self._start_mercury_now(radio)

        # Anything in the outbox goes out on the air as soon as the
        # connector is up -- say so before keying the radio.
        mc = self.cfg["mercury"]
        n = sum(len(v) for v in hf_mail.queued(mc["outgoing_dir"], mc["peers"]).values())
        if n:
            self.confirm("Messages waiting in the outbox",
                         f"{n} message{'s' if n != 1 else ''} will be sent on the air as soon as "
                         f"Mercury is up on the {radio}.", go, yes="Start Mercury")
        else:
            go()

    def _start_mercury_now(self, radio, stop_modem=False):
        if stop_modem:
            self._modem_connect_stop.set()
            if self.modem_client:
                self.modem_client.close()
                self.modem_client = None
            self.stack.stop_modem73_and_rigctld()
            self._log("[ui] modem73 stopped to free the radio for Mercury")
        audio = self.stack.mercury_audio_device(radio)
        self._log(f"[ui] starting Mercury on {radio} (audio {audio})")
        self._set_mail_link("calling", "STARTING…")
        self._hf_busy = "mercury"
        self._update_hf_selector()
        threading.Thread(target=self._start_mercury_worker, args=(radio,), daemon=True).start()

    def _start_mercury_worker(self, radio):
        ok, msg = self.stack.start_mercury(radio)
        self.event_q.put(("mercury_result", (ok, msg)))

    def stop_mercury(self):
        self.stack.stop_mercury()
        self._set_mail_link("off", "OFF")
        self._log("[ui] Mercury and connector stopped")
        self._update_mercury_button()
        self._refresh_mail()

    def open_level_check(self):
        import levelcheck_gtk
        if getattr(self, "_levelcheck", None) and self._levelcheck.alive:
            self._levelcheck.win.present()
            return

        def busy():
            if self.stack.mercury_running:
                return "Stop Mercury first -- it holds the sound card and the radio."
            if self.stack.modem73_running:
                return "Stop the Reticulum HF stack (modem73) first -- it holds the sound card and the radio."
            return None

        self._levelcheck = levelcheck_gtk.LevelCheckWindow(
            presets=levelcheck_gtk.presets_from_offgrid_config(self.cfg),
            default=self._radio, busy_check=busy, transient_for=self.win, application=self.app)

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
            lines.append(f"[ui] {self._radio} is set to {self.stack.mercury_audio_device(self._radio)}")
            for line in lines:
                self.event_q.put(("log", line))
            self.event_q.put(("show_log", None))
        threading.Thread(target=work, daemon=True).start()

    def _set_mail_link(self, state, text):
        self.mail_link_label.set_label(text)
        set_text_color(self.mail_link_label, LINK_STYLE.get(state, None))
        self._link_state = state
        self.lights["link"].set_color({"connected": "green", "calling": "orange", "failed": "red"}.get(state, "gray"))
        if state in ("off", "idle", "failed"):
            self.lights["tx"].set_color("gray")
        if state != "connected":
            self._signal = {"snr": None, "bps": None}
            self.mail_signal_label.set_label("")

    def _apply_activity(self, act):
        kind, value = act
        if kind == "ptt":
            self.lights["tx"].set_color("red" if value else "gray")
        elif kind in ("snr", "bps"):
            self._signal[kind] = value
            snr, bps = self._signal["snr"], self._signal["bps"]
            parts = []
            if snr is not None:
                parts.append(f"SNR {snr:+.1f} dB")
            if bps is not None:
                parts.append(f"{bps} bps")
            color = "gray"
            if snr is not None:
                color = "green" if snr >= 8 else ("orange" if snr >= 0 else "red")
            self.mail_signal_label.set_label("  ".join(parts))
            set_text_color(self.mail_signal_label, color)
        elif kind == "rx_msg":
            self._flash_new_message()

    def _flash_new_message(self, seconds=60):
        self._rxmsg_until = time.time() + seconds
        self.lights["rxmsg"].set_color("green")
        if self.view.get_visible_child_name() != "mail":
            self.mail_page.set_needs_attention(True)

    # ------------------------------------------------------ composing / queue

    def _send_compose(self):
        text = self.compose_entry.get_text().strip()
        if not text:
            return
        self.queue_mail(text, from_compose=True)

    def queue_mail(self, body, need_gps=False, wx=None, from_compose=False):
        mc = self.cfg["mercury"]
        if not mc.get("mycall"):
            self.alert("No callsign", "Set mercury.mycall in config.json first.")
            return
        _call, _note, to = self._selected_dest()
        if wx:
            to = None   # home answers WX requests; it would relay one addressed elsewhere
        want_gps = bool(wx or need_gps or self.gps_toggle.get_active())
        self._log("[ui] getting a GPS fix…" if want_gps else "[ui] preparing message")
        if want_gps:
            self.toast("Getting a GPS fix…")

        def work():
            fix = hf_mail.gps_fix() if want_gps else None
            self.event_q.put(("mail_ready", (body, to, wx, need_gps, fix, from_compose)))
        threading.Thread(target=work, daemon=True).start()

    def _mail_ready(self, body, to, wx, need_gps, fix, from_compose):
        mc = self.cfg["mercury"]
        if (wx or need_gps) and not fix:
            self.alert("No GPS fix",
                       "gpsd has no position, so this message can't be built. Try near a window or outside.")
            return
        lat, lon = fix if fix else (None, None)
        if wx:
            body = f"{wx} {lat:.4f} {lon:.4f}"
        if not mc.get("peers") or not mc.get("mycall"):
            self.alert("Message not queued", "Set mercury.mycall and mercury.peers in config.json first.")
            return
        home = mc["peers"][0]
        try:
            content = hf_mail.build_message(mc["mycall"], body, category=mc.get("category", "status"),
                                            to=to, lat=lat, lon=lon)
        except ValueError as e:
            self.alert("Message not queued", str(e))
            return
        dest = f"{to} (relayed by {home})" if to else home
        when = ("The connector will call it on the air right away."
                if self.stack.connector_running else
                "It goes out on the air next time you start Mercury.")
        pos = f"\nPosition: {lat:.4f}, {lon:.4f}" if fix else ""

        def do_queue():
            path = hf_mail.queue_message(mc["outgoing_dir"], home, content)
            try:
                hf_mail.save_sent_copy(mc["sent_dir"], path, content)
            except OSError as e:
                self._log(f"[ui] couldn't keep a copy for the message list: {e}")
            self._log(f"[ui] queued {path.name} for {dest}: {body}")
            if from_compose:
                self.compose_entry.set_text("")
            self._refresh_mail(scroll=True)

        self.ask(f"Queue HF message to {dest}?", f"{body}{pos}\n\n{when}",
                 lambda rid: do_queue() if rid == "yes" else self._log("[ui] message not queued"),
                 [("no", "Cancel", None), ("yes", "Queue", "suggested")], default="yes")

    def clear_mail_queue(self):
        mc = self.cfg["mercury"]
        q = hf_mail.queued(mc["outgoing_dir"], mc["peers"])
        files = [p for paths in q.values() for p in paths]
        if not files:
            return

        def do_clear():
            for p in files:
                try:
                    p.unlink()
                except OSError as e:
                    self._log(f"[ui] could not delete {p}: {e}")
            hf_mail.delete_sent_copies(mc["sent_dir"], [p.name for p in files])
            self._log(f"[ui] outbox cleared ({len(files)} message(s))")
            self._refresh_mail()
        self.confirm("Clear the outbox?", f"Delete {len(files)} unsent message(s)? They won't go out.",
                     do_clear, yes="Delete", destructive=True)

    def open_mail_inbox(self):
        path = Path(self.cfg["mercury"]["incoming_dir"]).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(self.win, None, None, None) \
            if hasattr(Gtk, "FileLauncher") else \
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _refresh_mail(self, scroll=False):
        mc = self.cfg["mercury"]
        merc, conn = self.stack.mercury_running, self.stack.connector_running
        self.lights["mercury"].set_color("green" if merc else "gray")
        self.lights["connector"].set_color("green" if conn else "gray")
        if not merc and self.mail_link_label.get_label() not in ("OFF", "STARTING…"):
            self._set_mail_link("off", "OFF")
        if self._rxmsg_until and time.time() > self._rxmsg_until:
            self._rxmsg_until = 0.0
            self.lights["rxmsg"].set_color("gray")
        if not self._hf_busy:
            self._update_hf_selector()

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
            self.mail_queue_label.set_label(f"Outbox: {per}")
            set_text_color(self.mail_queue_label, "orange")
        else:
            self.mail_queue_label.set_label("Outbox: empty")
            set_text_color(self.mail_queue_label, None)
        self.clear_outbox_btn.set_sensitive(bool(n))

        msgs = hf_mail.inbox(mc["incoming_dir"])
        chat = hf_mail.conversation(mc["incoming_dir"], mc["sent_dir"], queued_names)
        sig = tuple((x["file"], x["time"], x.get("status")) for x in chat)
        if sig == self._mail_sig:
            if scroll:
                self._chat_to_end()
            return
        first = self._mail_sig is None
        old_in = {f for f, _t, s in (self._mail_sig or ()) if s is None}
        self._mail_sig = sig
        home = mc["peers"][0] if mc.get("peers") else "HOME"
        self._chat_cache = (chat, home)
        self._render_thread()
        new_in = [x for x in msgs if x["file"] not in old_in]
        if not first and new_in:
            self._log(f"[ui] HF mail: {hf_mail.format_inbox_line(new_in[0])}")
            self._flash_new_message()
            current = self._selected_dest()[0]
            for x in new_in:
                t = self._thread_of(x, home)
                if t != current:
                    self._mark_dest_new(t)

    # -- one conversation per destination ----------------------------------
    #
    # Home thread: what we sent home, home's replies/forecasts, and anything
    # from a station we don't have a button for. A field station's thread:
    # what we sent "to:" it (relayed by home) and what it sent us via home.

    def _thread_of(self, x, home):
        calls = [c.upper() for c, _n, _t in self.mail_dests]
        if x.get("direction") == "out":
            key = (x.get("to") or home).upper()
        elif x.get("relayed_by") and x.get("type") != "reply":
            key = (x.get("callsign") or home).upper()
        else:
            key = home.upper()
        return key if key in calls else home.upper()

    def _render_thread(self, scroll=True):
        if not self._chat_cache:
            return
        chat, home = self._chat_cache
        current = self._selected_dest()[0].upper()
        self._render_chat([x for x in chat if self._thread_of(x, home) == current], home, current)

    def _mark_dest_new(self, call):
        for i, (c, _n, _t) in enumerate(self.mail_dests):
            if c.upper() == call.upper():
                lbl, note = self.dest_notes[i]
                lbl.set_label(f"● new · {note}")
                set_text_color(lbl, "orange")

    def _on_dest_toggled(self, b):
        if not b.get_active():
            return
        i = self.dest_buttons.index(b)
        lbl, note = self.dest_notes[i]
        lbl.set_label(note)
        set_text_color(lbl, None)
        self._render_thread()

    def _render_chat(self, chat, home, thread=None):
        lb = self.chat_list
        child = lb.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            lb.remove(child)
            child = nxt
        if not chat:
            lb.append(wrap_label(
                f"No messages with {thread} yet." if thread and thread != home.upper() else
                "No messages yet. Reports you send and replies from home show up here.", xalign=0.5))
        last_day = None
        for x in chat:
            day = x["time"].date()
            if day != last_day:
                sep = Gtk.Label(label=x["time"].strftime("%a %b %d"))
                sep.add_css_class("caption")
                sep.add_css_class("dimmed")
                sep.set_margin_top(8)
                row = Gtk.ListBoxRow(activatable=False, selectable=False, child=sep)
                lb.append(row)
                last_day = day
            lb.append(self._bubble(x, home))
        self._chat_to_end()

    def _bubble(self, x, home):
        out = x.get("direction") == "out"
        body = x.get("body", "")
        t = x["time"].strftime("%H:%M")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("bubble")
        if out:
            sent = x.get("status") == "sent"
            box.add_css_class("out" if sent else "queued")
            dest = x.get("to") or home
            via = f" via {home}" if x.get("to") else ""
            meta = f"{t} · to {dest}{via} · " + ("✓ sent" if sent else "⏳ queued -- goes out next link")
        else:
            box.add_css_class("in")
            sender = "HOME" if x.get("type") == "reply" else x.get("callsign", "?")
            via = f" via {x['relayed_by']}" if x.get("relayed_by") else ""
            who = Gtk.Label(label=f"{sender}{via}", xalign=0)
            who.add_css_class("who")
            box.append(who)
            meta = t
        lbl = Gtk.Label(label=body, wrap=True, xalign=0, selectable=False, max_width_chars=42)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        box.append(lbl)
        m = Gtk.Label(label=meta, xalign=1 if out else 0, wrap=True)
        m.add_css_class("meta")
        box.append(m)
        holder = Gtk.Box(margin_start=10 if not out else 60, margin_end=10 if out else 60,
                         margin_top=3, margin_bottom=3)
        box.set_halign(Gtk.Align.END if out else Gtk.Align.START)
        box.set_hexpand(True)
        holder.append(box)
        return Gtk.ListBoxRow(activatable=False, selectable=False, child=holder)

    def _chat_to_end(self):
        def go():
            adj = self.chat_scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.timeout_add(60, go)

    # ----------------------------------------------------------- radio/mode

    def _remember_radio(self, radio):
        if self.cfg.get("last_radio") != radio:
            self.cfg["last_radio"] = radio
            cfgmod.save_config(self.cfg)

    def _on_radio_selected(self, row, _pspec):
        i = row.get_selected()
        if i == Gtk.INVALID_LIST_POSITION or i >= len(self.radio_names):
            return
        radio = self.radio_names[i]
        if radio == self._radio:
            return
        self._radio = radio
        self._update_mail_radio_label()
        self._remember_radio(radio)
        self.freq_readout.set_label("-- . --- MHz")
        if not (self.stack.modem73 and self.stack.modem73.is_running):
            return
        self._log(f"[ui] switching radio to {radio} -- this bounces modem73, MeshChatX stays up")
        threading.Thread(target=self._switch_radio_worker, args=(radio, self._mode), daemon=True).start()

    def _switch_radio_worker(self, radio, mode):
        if self.modem_client:
            self.modem_client.close()
            self.modem_client = None
        self.stack.switch_radio(radio, mode)
        self._connect_modem_client()

    def _refresh_mode_desc(self):
        self.mode_desc.set_label(self.cfg["modes"].get(self._mode, {}).get("description", ""))

    def select_mode(self, name):
        if name == self._mode:
            return
        self._mode = name
        self._refresh_mode_desc()
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
        raw = self.freq_row.get_text().strip().replace(",", ".")
        if not raw:
            return
        try:
            value = float(raw)
        except ValueError:
            self.alert("Not a frequency", f"'{raw}' isn't a number. Enter MHz, e.g. 7.104")
            return
        hz = int(round(value * 1_000_000)) if value < 1000 else int(value)
        if self.modem_client and self.modem_client.connected:
            ok = self.modem_client.set_freq_hz(hz)
            self._log(f"[ui] set frequency to {hz} Hz: {'ok' if ok else 'FAILED'}")
            self.toast(f"{hz / 1e6:.6f} MHz " + ("set" if ok else "FAILED"))
            return
        # Mercury mode (or nothing running): rigctld if it's up, else the serial port
        rig = self._rig_args()
        if not rig:
            self.alert("Radio not reachable", f"Can't reach the {self._radio} -- is it plugged in?")
            return

        def work():
            try:
                r = subprocess.run(["rigctl", *rig, "F", str(hz)], capture_output=True, text=True, timeout=10)
                ok = r.returncode == 0
                msg = "ok" if ok else (r.stderr.strip() or r.stdout.strip() or "FAILED")
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, msg = False, str(e)
            self.event_q.put(("log", f"[ui] set frequency to {hz} Hz: {msg}"))
            self.event_q.put(("toast", f"{hz / 1e6:.6f} MHz " + ("set" if ok else "FAILED -- see Log")))
            if ok:
                self._read_rig_freq()
        threading.Thread(target=work, daemon=True).start()

    def _rig_args(self):
        name = self._radio
        r = self.cfg["radios"].get(name, {})
        st = self.stack
        if st.rigctld and st.rigctld.is_running and st.rigctld_radio == name:
            return ["-m", "2", "-r", f"127.0.0.1:{r.get('rigctld_port', 4532)}"]
        dev = r.get("device") or ""
        if not dev or not os.path.exists(dev):
            return None
        return ["-m", str(r.get("hamlib_model", 2)), "-r", dev, "-s", str(r.get("baud", 38400))]

    def _read_rig_freq(self):
        """Background thread: read the dial through our rigctld (never fight it for the port)."""
        rig = self._rig_args()
        if not rig or rig[1] != "2":
            return
        try:
            r = subprocess.run(["rigctl", *rig, "f"], capture_output=True, text=True, timeout=5)
            hz = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            hz = 0
        if hz:
            self.event_q.put(("freq", hz))

    # ---------------------------------------------------------- HF stack

    def start_all(self):
        """Reticulum on HF: rigctld + modem73, and MeshChatX if it isn't up."""
        radio, mode = self._radio, self._mode
        if not self._radio_plugged(radio):
            return

        def go():
            self._log(f"[ui] starting Reticulum on HF: radio={radio} mode={mode}")
            self._hf_busy = "reticulum"
            self._update_hf_selector()
            threading.Thread(target=self._start_all_worker, args=(radio, mode), daemon=True).start()
        if self.stack.mercury_running:
            self.confirm("Mercury is using the radio",
                         "Stop Mercury and put Reticulum on HF instead? Anything in the outbox stays queued.",
                         lambda: (self.stop_mercury(), go()), yes="Switch")
            return
        go()

    def _start_all_worker(self, radio, mode):
        st = self.stack
        if st.rigctld and st.rigctld.is_running:
            st.rigctld.stop()
        ok = st.start_rigctld(radio) and st.start_modem73(radio, mode)
        if ok and not self._meshchatx_running():
            ok = st.start_meshchatx()
        self.event_q.put(("reticulum_result", ok))
        if not ok:
            self.event_q.put(("log", "[ui] one or more processes failed to start -- check the log above"))
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

    # ------------------------------------------------------------ MeshChatX

    def open_meshchatx(self):
        mc = self.cfg["meshchatx"]
        host = "127.0.0.1" if mc["host"] == "0.0.0.0" else mc["host"]
        webbrowser.open(f"{mc.get('scheme', 'https')}://{host}:{mc['port']}")

    def announce_meshchatx(self):
        # deliberately not an in-app RNS announce: MeshChatX owns its identity
        # and storage; a second process touching them caused the storage-lock
        # and ratchet-corruption bugs earlier on
        self.open_meshchatx()
        self._log("[ui] opened MeshChatX -- announce from its own Settings > Identity page")
        self.toast("Announce from MeshChatX's Settings › Identity page")

    def _meshchatx_running(self):
        mc = self.stack.meshchatx
        if mc and mc.is_running:
            return True
        # one this app didn't start (left over from before, or started by hand)
        return self._meshchatx_answers()

    def _stop_other_meshchatx(self):
        port = str(self.cfg["meshchatx"]["port"])
        try:
            out = subprocess.run(["pgrep", "-f", "meshchatx"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        for pid in out.split():
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            args = [a.decode(errors="replace") for a in cmdline]
            if any("meshchatx" in a for a in args[:2]) and port in args and int(pid) != os.getpid():
                self.event_q.put(("log", f"[ui] stopping MeshChatX the app didn't start (pid {pid})"))
                try:
                    os.kill(int(pid), 15)
                except OSError:
                    pass
        for _ in range(20):
            if not self._meshchatx_answers():
                return
            time.sleep(0.25)

    def _meshchatx_answers(self):
        mc = self.cfg["meshchatx"]
        host = "127.0.0.1" if mc["host"] in ("0.0.0.0", "") else mc["host"]
        try:
            with socket.create_connection((host, int(mc["port"])), timeout=0.3):
                return True
        except OSError:
            return False

    def _update_meshchatx_button(self):
        busy = self._mcx_busy
        on = self._meshchatx_running()
        for c in ("start-btn", "stop-btn", "busy-btn"):
            self.mcx_btn.remove_css_class(c)
        if busy:
            self.mcx_btn.add_css_class("busy-btn")
            self.mcx_label.set_label("Starting…" if busy == "starting" else "Stopping…")
        else:
            self.mcx_btn.add_css_class("stop-btn" if on else "start-btn")
            self.mcx_label.set_label("Stop MeshChatX" if on else "Start MeshChatX")
        self.mcx_spinner.set_visible(bool(busy))
        self.mcx_spinner.set_spinning(bool(busy))
        self.mcx_btn.set_sensitive(not busy)

    def _toggle_meshchatx(self):
        if self._mcx_busy:
            return
        if self._meshchatx_running():
            self.stop_meshchatx_only()
        else:
            self.start_meshchatx_only()

    def start_meshchatx_only(self):
        self._log("[ui] starting MeshChatX only (rigctld/modem73 not touched)")
        self._mcx_busy = "starting"
        self._update_meshchatx_button()

        def work():
            ok = self.stack.start_meshchatx()
            # "running" isn't "ready": wait until its web page actually answers
            deadline = time.time() + 90
            while ok and time.time() < deadline and not self._quitting:
                if self._meshchatx_answers():
                    break
                if not self._meshchatx_running():
                    ok = False
                    break
                time.sleep(1)
            else:
                ok = ok and self._meshchatx_answers()
            GLib.idle_add(self._meshchatx_started, ok)
        threading.Thread(target=work, daemon=True).start()

    def _meshchatx_started(self, ok):
        self._mcx_busy = None
        self._update_meshchatx_button()
        if ok:
            self._log("[ui] MeshChatX is up")
            self.toast("MeshChatX is up")
        elif self._meshchatx_running():
            self._log("[ui] MeshChatX is running but its page isn't answering yet")
            self.toast("MeshChatX started, page not answering yet -- see Log")
        else:
            self._log("[ui] MeshChatX failed to start -- check the log above")
            self.toast("MeshChatX didn't start -- see Log")
        return False

    def stop_meshchatx_only(self):
        self._mcx_busy = "stopping"
        self._update_meshchatx_button()

        def work():
            self.stack.stop_meshchatx()
            if self._meshchatx_answers():
                self._stop_other_meshchatx()
            GLib.idle_add(self._meshchatx_stopped)
        threading.Thread(target=work, daemon=True).start()

    def _meshchatx_stopped(self):
        self._mcx_busy = None
        self._update_meshchatx_button()
        self._log("[ui] MeshChatX stopped (rigctld/modem73 not touched)")
        self.toast("MeshChatX stopped")
        return False

    # -------------------------------------------------------------- Anytone

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
        self.anytone_light.set_color("green" if bound else "gray")
        self.anytone_status_label.set_label("bound" if bound else "not bound")

    # ------------------------------------------------------ modem73 control

    def _connect_modem_client(self):
        self._modem_connect_stop.clear()
        threading.Thread(target=self._modem_connect_worker, daemon=True).start()

    def _modem_connect_worker(self):
        m73 = self.cfg["modem73"]
        client = ModemControlClient(
            m73["control_host"], m73["control_port"],
            on_event=lambda frame: self.event_q.put(("rx_frame", frame)),
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

    def _start_rnsd_worker(self):
        ok = self.stack.start_rnsd()
        self.event_q.put(("log", "[ui] rnsd started" if ok else "[ui] rnsd FAILED to start -- check the log above"))

    # ------------------------------------------------------------ polling

    def _poll_events(self):
        if self._quitting:
            return False
        lines = []
        try:
            while True:
                kind, data = self.event_q.get_nowait()
                if kind == "log":
                    lines.append(data)
                    continue
                self._log_lines(lines)
                lines = []
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
                    if not ok:
                        self.toast(f"Anytone: {msg}")
                    self._refresh_anytone_light()
                elif kind == "network_status":
                    self._handle_network_status(data)
                elif kind == "reticulum_result":
                    self._hf_busy = None
                    self._update_hf_selector()
                    if data:
                        self.toast("Reticulum is on HF")
                    else:
                        self.alert("Reticulum didn't fully start on HF", "See the Log tab for which part failed.")
                elif kind == "mercury_result":
                    ok, msg = data
                    self._log(f"[ui] {msg}" if ok else f"[ui] Mercury FAILED: {msg}")
                    self._hf_busy = None
                    if ok:
                        self._set_mail_link("idle", "IDLE -- listening")
                    else:
                        self.stack.stop_mercury()
                        self._set_mail_link("failed", "FAILED -- see log")
                        self.alert("Mercury didn't start", msg)
                    self._update_mercury_button()
                    self._refresh_mail()
                elif kind == "mail_ready":
                    self._mail_ready(*data)
                elif kind == "freq":
                    self.freq_readout.set_label(f"{data / 1_000_000:.6f} MHz")
                elif kind == "status":
                    self._apply_status(data)
                elif kind == "toast":
                    self.toast(data)
                elif kind == "show_log":
                    self.view.set_visible_child_name("log")
        except queue.Empty:
            pass

        # tail the managed processes' output
        for proc in (self.stack.rnsd, self.stack.rigctld, self.stack.modem73, self.stack.meshchatx,
                     self.stack.mercury, self.stack.connector):
            if proc is None:
                continue
            total, new = proc.log.since(getattr(proc, "_last_logged_len", 0))
            lines.extend(new)
            if proc is self.stack.connector or proc is self.stack.mercury:
                for line in new:
                    state = hf_mail.link_state_from_line(line)
                    if state:
                        self._set_mail_link(*state)
                    act = hf_mail.activity_from_line(line)
                    if act:
                        self._apply_activity(act)
            proc._last_logged_len = total
        self._log_lines(lines)
        return True

    def _handle_rx_frame(self, frame):
        ts = datetime.now().strftime("%H:%M:%S")
        snr = frame.get("snr")
        snr_txt = f"{snr:.1f}" if isinstance(snr, (int, float)) else "?"
        row = Gtk.ListBoxRow(activatable=False, selectable=False,
                             child=self._heard_row((ts, str(frame.get("callsign", "?")),
                                                    str(frame.get("mode", "?")), snr_txt)))
        self.heard_list.prepend(row)
        self._heard_rows.insert(0, row)
        while len(self._heard_rows) > 50:
            self.heard_list.remove(self._heard_rows.pop())

    def _poll_status(self):
        if self._quitting:
            return False
        if self.modem_client and self.modem_client.connected:
            threading.Thread(target=self._status_worker, daemon=True).start()
        elif self.stack.mercury_running:
            threading.Thread(target=self._read_rig_freq, daemon=True).start()
        self.lights["modem73"].set_color("green" if self.stack.modem73_running else "gray")
        if not self._mcx_busy:
            self._update_meshchatx_button()     # also catches Start All / Stop All / a crash
        self.systems.refresh()
        self._refresh_anytone_light()
        try:
            self._refresh_mail()
        except OSError as e:
            print(f"[mail] refresh failed: {e}", flush=True)
        self._update_keep_awake()
        return True

    def _status_worker(self):
        client = self.modem_client
        if not client:
            return
        status = client.get_status()
        freq = client.get_freq_hz()
        if status:
            self.event_q.put(("status", status))
        if freq:
            self.event_q.put(("freq", freq))

    def _apply_status(self, status):
        state = status.get("channel_state", "idle")
        self.channel_light.set_color(CHANNEL_LIGHT.get(state, "gray"))
        self.channel_state_label.set_label(state.upper())
        snr = status.get("last_snr")
        self.readouts["SNR"].set_label(f"{snr:.1f} dB" if isinstance(snr, (int, float)) else "-- dB")
        ber = status.get("ber_ema")
        self.readouts["BER"].set_label(f"{ber:.4f}" if isinstance(ber, (int, float)) and ber >= 0 else "--")
        pop = status.get("population")
        self.readouts["Population"].set_label(str(pop) if pop is not None else "--")
        occ = status.get("occupancy_pct")
        self.readouts["Occupancy"].set_label(f"{occ}%" if occ is not None else "--%")
        self.rig_light.set_color("green" if status.get("rigctl_connected") else "red")
        self.audio_light.set_color("green" if status.get("audio_connected") else "red")

    def _update_keep_awake(self):
        hf = self.stack.modem73_running or self.stack.mercury_running
        if hf and self.cfg.get("keep_awake_while_hf", True) and not self.inhibitor.active:
            which = "Mercury" if self.stack.mercury_running else "modem73"
            ok, msg = self.inhibitor.acquire(f"{which} HF modem running")
            self._log(f"[ui] keeping the computer awake while {which} runs"
                      if ok else f"[ui] couldn't block suspend: {msg}")
        elif not hf and self.inhibitor.active:
            self.inhibitor.release()
            self._log("[ui] HF modem stopped -- normal suspend allowed again")

    # ------------------------------------------------------ network status

    def refresh_network_status(self):
        threading.Thread(target=self._network_status_worker, daemon=True).start()

    def _poll_network_once(self):
        self.refresh_network_status()
        return False

    def _poll_network_status(self):
        if self._quitting:
            return False
        if self.net_auto_row.get_active():
            self.refresh_network_status()
        return True

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
            "rnsd_alive": _check_proc_alive("rnsd"),
            "nomadnet_alive": _check_systemd_user_active("nomadnet.service"),
        }))

    def _handle_network_status(self, data):
        def color(v):
            return "green" if v is True else ("red" if v is False else "gray")
        self.rnsd_light.set_color(color(data["rnsd_alive"]))
        self.lights["rnsd"].set_color(color(data["rnsd_alive"]))
        self.nomadnet_light.set_color(color(data["nomadnet_alive"]))
        self.net_status_text.get_buffer().set_text(data["text"])


def _check_proc_alive(name):
    """True/False, or None if pgrep isn't there (gray, not a false red).
    -f because console scripts like rnsd show up as python3."""
    try:
        return subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _check_systemd_user_active(unit):
    """None (gray) if the unit isn't installed here at all -- e.g. the tablet."""
    try:
        result = subprocess.run(["systemctl", "--user", "show", "-p", "LoadState", "-p", "ActiveState", unit],
                                capture_output=True, text=True, timeout=5)
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return None
        return props.get("ActiveState") == "active"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


class OffGridApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
                         if hasattr(Gio.ApplicationFlags, "DEFAULT_FLAGS") else Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None
        self._held = False

    def do_activate(self):
        # second launch (dock icon, app grid) while running: bring the window back
        if self.window:
            self.window.show_window()
            return
        self.window = OffGridWindow(self)

    def hold_for_background(self):
        if not self._held:
            self.hold()
            self._held = True

    def release_background(self):
        if self._held:
            self._held = False
            self.release()


def run():
    app = OffGridApp()
    return app.run(None)


if __name__ == "__main__":
    run()
