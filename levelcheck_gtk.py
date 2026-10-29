#!/usr/bin/env python3
"""
Level Check (GTK 4) -- the same tool as levelcheck_gui.py, native on Wayland
so it works with touch on the tablet.

  * METER        watch the receive level while you turn AF gain / DATA OUT
  * SEND TONE    key the radio and send a clean 1000 Hz tone for 30 s
  * ANALYZE      record 12 s and say in plain words what's wrong

Stop Mercury / modem73 first -- they hold the sound card and the radio.

  standalone:          python3 levelcheck_gtk.py [--device plughw:CARD=CODEC,DEV=0]
                                                 [--rig "-m 2 -r 127.0.0.1:4532"]
  from OffGrid Comms:  menu > Level check…
"""
import argparse
import math
import queue
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import levelcheck as lc  # noqa: E402
from levelcheck_gui import presets_from_offgrid_config, sound_cards  # noqa: E402,F401  (pure helpers)

TEAL, ORANGE, GREEN, RED, GRAY = "#00d9d9", "#ff9500", "#33cc66", "#ff4444", "#5a6570"
METER_MIN, METER_MAX = -60.0, 0.0


def _rgb(hexcol):
    h = hexcol.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class LevelCheckWindow:
    def __init__(self, presets=None, default=None, device=None, rig=None, busy_check=None,
                 transient_for=None, application=None):
        self.busy_check = busy_check
        self.presets = list(presets or [])
        self.presets.append(("rigctld on this computer (port 4532)", device or "", "-m 2 -r 127.0.0.1:4532"))
        self.q = queue.Queue()
        self.meter_proc = None
        self.tone_stop = None
        self.tone_thread = None
        self.analyzing = False
        self.level = None
        self.peak_hold = (METER_MIN, 0.0)
        self.alive = True

        self.win = Adw.Window(title="Level Check", default_width=620, default_height=760)
        if application:
            self.win.set_application(application)
        if transient_for:
            self.win.set_transient_for(transient_for)
        self.win.add_css_class("ogc")
        self.win.connect("close-request", self._on_close)
        self._build(default, device, rig)
        GLib.timeout_add(100, self._poll)
        self.win.present()

    # ---------- layout ----------

    def _build(self, default, device, rig):
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(
            title="Level Check", subtitle="Stop Mercury first")))
        self.toasts = Adw.ToastOverlay()
        tv.set_content(self.toasts)
        self.win.set_content(tv)

        page = Adw.PreferencesPage()
        self.toasts.set_child(page)

        g = Adw.PreferencesGroup(
            title="Radio",
            description="Receiving station: METER or ANALYZE. Sending station: SEND TONE.")
        self.preset_row = Adw.ComboRow(title="Radio",
                                       model=Gtk.StringList.new([p[0] for p in self.presets]))
        g.add(self.preset_row)

        self.device_row = Adw.EntryRow(title="Sound card")
        self.device_row.set_text(device or "")
        cards = sound_cards()
        if cards:
            pop = Gtk.Popover()
            lb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=6,
                         margin_end=6, margin_top=6, margin_bottom=6)
            for label, dev in cards:
                b = Gtk.Button(label=f"{dev}   {label}")
                b.add_css_class("flat")
                b.connect("clicked", lambda _b, d=dev: (self.device_row.set_text(d), pop.popdown()))
                lb.append(b)
            pop.set_child(lb)
            mb = Gtk.MenuButton(icon_name="audio-card-symbolic", popover=pop, valign=Gtk.Align.CENTER,
                                tooltip_text="Sound cards on this computer")
            mb.add_css_class("flat")
            self.device_row.add_suffix(mb)
        g.add(self.device_row)

        self.rig_row = Adw.EntryRow(title="Rig control (rigctl arguments)")
        self.rig_row.set_text(rig or "")
        g.add(self.rig_row)
        page.add(g)

        names = [p[0] for p in self.presets]
        if default in names:
            self.preset_row.set_selected(names.index(default))
            if not device:
                self._apply_preset()
        elif not device and names:
            self._apply_preset()
        self.preset_row.connect("notify::selected", lambda *_a: self._apply_preset())

        # meter
        g = Adw.PreferencesGroup(title="Receive level",
                                 description="Aim for the green band while the tone is on -- never red.")
        self.level_lbl = Gtk.Label(label="meter off")
        self.level_lbl.add_css_class("readout")
        g.set_header_suffix(self.level_lbl)
        self.meter = Gtk.DrawingArea(content_height=44, hexpand=True)
        self.meter.set_draw_func(self._draw_meter)
        frame = Gtk.Frame(child=self.meter)
        g.add(frame)
        page.add(g)

        # buttons
        g = Adw.PreferencesGroup()
        fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                         min_children_per_line=2, max_children_per_line=4,
                         column_spacing=6, row_spacing=6)
        self.meter_btn = Gtk.Button(label="START METER")
        self.meter_btn.connect("clicked", lambda _b: self.toggle_meter())
        self.meter_btn.add_css_class("suggested-action")
        self.analyze_btn = Gtk.Button(label="ANALYZE 12 s")
        self.analyze_btn.connect("clicked", lambda _b: self.analyze())
        self.tone_btn = Gtk.Button(label="SEND TONE 30 s")
        self.tone_btn.connect("clicked", lambda _b: self.toggle_tone())
        self.tone_btn.add_css_class("destructive-action")
        copy = Gtk.Button(label="COPY")
        copy.connect("clicked", lambda _b: self.copy_results())
        for b in (self.meter_btn, self.analyze_btn, self.tone_btn, copy):
            fb.append(b)
        g.add(fb)
        page.add(g)

        # results
        g = Adw.PreferencesGroup(title="Results")
        self.out = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                wrap_mode=Gtk.WrapMode.WORD_CHAR)
        for m in ("left", "right", "top", "bottom"):
            getattr(self.out, f"set_{m}_margin")(8)
        buf = self.out.get_buffer()
        for tag, color in (("ok", GREEN), ("bad", RED), ("note", ORANGE), ("dim", GRAY), ("head", TEAL)):
            buf.create_tag(tag, foreground=color)
        self.out_end = buf.create_mark("end", buf.get_end_iter(), False)
        sw = Gtk.ScrolledWindow(min_content_height=240, child=self.out)
        sw.add_css_class("card")
        g.add(sw)
        page.add(g)
        self._say("Ready. Make sure Mercury is stopped on this machine.\n", "dim")

    def _apply_preset(self):
        i = self.preset_row.get_selected()
        if i == Gtk.INVALID_LIST_POSITION or i >= len(self.presets):
            return
        _name, audio, rig = self.presets[i]
        if audio:
            self.device_row.set_text(audio)
        self.rig_row.set_text(rig)

    # ---------- output ----------

    def _say(self, text, tag=None):
        buf = self.out.get_buffer()
        if tag:
            buf.insert_with_tags_by_name(buf.get_end_iter(), text, tag)
        else:
            buf.insert(buf.get_end_iter(), text)
        self.out.scroll_mark_onscreen(self.out_end)

    def copy_results(self):
        buf = self.out.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title="Copied to the clipboard", timeout=2))

    def _report(self, r):
        self._say("\n── analysis ─────────────────────────\n", "head")
        if r.get("error"):
            self._say(r["error"] + "\n", "bad")
            return
        self._say(f"tone   {r['freq']:.0f} Hz ({r['offset']:+.0f})   level {r['rms_db']:.1f} dBFS"
                  f"   peak {r['peak_db']:.1f}\n")
        self._say(f"harmonics {r['h2_dbc']:.0f} / {r['h3_dbc']:.0f} dBc   quality {r['clean_db']:.0f} dB\n", "dim")
        for ok, text in r["verdicts"]:
            self._say(("✓ " if ok else ("• " if ok is None else "✗ ")) + text + "\n",
                      "ok" if ok else ("note" if ok is None else "bad"))

    def _alert(self, text):
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog(heading="Level Check", body=text)
            d.add_response("ok", "OK")
            d.present(self.win)
        else:
            d = Adw.MessageDialog(transient_for=self.win, heading="Level Check", body=text)
            d.add_response("ok", "OK")
            d.present()

    # ---------- meter ----------

    def _draw_meter(self, _area, cr, wdt, hgt):
        def x(dbv):
            return (min(max(dbv, METER_MIN), METER_MAX) - METER_MIN) / (METER_MAX - METER_MIN) * wdt

        cr.set_source_rgb(*_rgb("#0c0f12"))
        cr.paint()
        for lo, hi, col in ((-60, -30, "#1d2228"), (-30, -20, "#26303a"), (-20, -10, "#173d26"),
                            (-10, -3, "#3d3417"), (-3, 0, "#3d1717")):
            cr.set_source_rgb(*_rgb(col))
            cr.rectangle(x(lo), 0, x(hi) - x(lo), hgt)
            cr.fill()
        cr.set_source_rgb(*_rgb(GRAY))
        cr.set_font_size(10)
        for dbv in (-50, -40, -30, -20, -10, 0):
            cr.move_to(x(dbv), hgt - 6)
            cr.line_to(x(dbv), hgt)
            cr.stroke()
            cr.move_to(min(x(dbv) + 2, wdt - 14), 11)
            cr.show_text(str(dbv))
        if self.level is None:
            return
        rms, _pk, clip = self.level
        color = RED if clip or rms > -3 else (ORANGE if rms > -10 else (GREEN if rms >= -20 else TEAL))
        cr.set_source_rgb(*_rgb(color))
        cr.rectangle(0, hgt * 0.35, x(rms), hgt * 0.5)
        cr.fill()
        ph, _t = self.peak_hold
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(2)
        cr.move_to(x(ph), hgt * 0.25)
        cr.line_to(x(ph), hgt)
        cr.stroke()

    def toggle_meter(self):
        if self.meter_proc:
            self._stop_meter()
            return
        if self.analyzing or not self._ready(need_rig=False):
            return
        dev = self.device_row.get_text().strip()
        try:
            self.meter_proc = subprocess.Popen(
                ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(lc.RATE), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self._say(f"couldn't start arecord: {e}\n", "bad")
            return
        self.meter_btn.set_label("STOP METER")
        threading.Thread(target=self._meter_worker, args=(self.meter_proc,), daemon=True).start()

    def _meter_worker(self, proc):
        chunk = lc.RATE // 10 * 2          # 0.1 s
        while True:
            data = proc.stdout.read(chunk)
            if not data:
                err = proc.stderr.read().decode(errors="replace").strip()
                self.q.put(("meter_end", err))
                return
            self.q.put(("level", lc.level_of(data)))

    def _stop_meter(self):
        p, self.meter_proc = self.meter_proc, None
        if p:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        self.meter_btn.set_label("START METER")
        self.level_lbl.set_label("meter off")
        self.level = None
        self.meter.queue_draw()

    # ---------- analyze ----------

    def analyze(self):
        if self.analyzing:
            return
        if self.meter_proc:
            self._stop_meter()
            time.sleep(0.2)
        if not self._ready(need_rig=False):
            return
        self.analyzing = True
        self.analyze_btn.set_label("RECORDING…")
        self.analyze_btn.set_sensitive(False)
        self._say("\nrecording 12 s -- start the tone on the other station now\n", "note")
        dev = self.device_row.get_text().strip()

        def work():
            x, err = lc.record(dev, 12)
            self.q.put(("analysis", {"error": err} if err else lc.analyze(x)))
        threading.Thread(target=work, daemon=True).start()

    # ---------- tone ----------

    def toggle_tone(self):
        if self.tone_thread and self.tone_thread.is_alive():
            self.tone_stop.set()
            return
        if not self._ready(need_rig=True):
            return
        body = ("This keys the radio and transmits a 1000 Hz tone for 30 seconds.\n\n"
                "Low power, clear frequency or dummy load?")
        if hasattr(Adw, "AlertDialog"):
            d = Adw.AlertDialog(heading="Send test tone?", body=body)
        else:
            d = Adw.MessageDialog(transient_for=self.win, heading="Send test tone?", body=body)
        d.add_response("no", "Cancel")
        d.add_response("yes", "Transmit")
        d.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("no")
        d.connect("response", lambda _d, rid: rid == "yes" and self._start_tone())
        d.present(self.win) if hasattr(Adw, "AlertDialog") else d.present()

    def _start_tone(self):
        dev, rig = self.device_row.get_text().strip(), self.rig_row.get_text().strip()
        self.tone_stop = threading.Event()
        self.tone_btn.set_label("STOP TONE")

        def work():
            ok, msg = lc.send_tone(dev, rig, seconds=30, stop_event=self.tone_stop,
                                   log=lambda t: self.q.put(("log", t + "\n")))
            self.q.put(("tone_done", (ok, msg)))
        self.tone_thread = threading.Thread(target=work, daemon=True)
        self.tone_thread.start()

    # ---------- plumbing ----------

    def _ready(self, need_rig):
        if self.busy_check:
            reason = self.busy_check()
            if reason:
                self._alert(reason)
                return False
        if not self.device_row.get_text().strip():
            self._alert("Pick the radio's sound card first.")
            return False
        if need_rig and not self.rig_row.get_text().strip():
            self._alert("Rig control is empty -- pick a radio.")
            return False
        return True

    def _poll(self):
        if not self.alive:
            return False
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "level":
                    rms, pk, clip = data
                    ph, t = self.peak_hold
                    now = time.time()
                    if pk >= ph or now - t > 1.5:
                        self.peak_hold = (pk, now)
                    self.level = data
                    self.meter.queue_draw()
                    tag = "CLIP!" if clip else ("good" if -20 <= rms <= -10 else ("hot" if rms > -10 else
                                                                               ("low" if rms < -30 else "")))
                    self.level_lbl.set_label(f"{rms:6.1f} dBFS  {tag}" if not math.isinf(rms) else "silence")
                elif kind == "meter_end":
                    if self.meter_proc:
                        self._say(f"meter stopped: {data or 'arecord ended'} (is Mercury holding the card?)\n", "bad")
                        self._stop_meter()
                elif kind == "analysis":
                    self.analyzing = False
                    self.analyze_btn.set_label("ANALYZE 12 s")
                    self.analyze_btn.set_sensitive(True)
                    self._report(data)
                elif kind == "tone_done":
                    ok, msg = data
                    self.tone_btn.set_label("SEND TONE 30 s")
                    if not ok:
                        self._say(msg + "\n", "bad")
                elif kind == "log":
                    self._say(data, "dim")
        except queue.Empty:
            pass
        return True

    def _on_close(self, _w):
        if self.tone_stop:
            self.tone_stop.set()           # send_tone always unkeys on its way out
        if self.meter_proc:
            self._stop_meter()
        if self.tone_thread and self.tone_thread.is_alive():
            self.tone_thread.join(timeout=3)
        self.alive = False
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="sound card, e.g. plughw:CARD=CODEC,DEV=0")
    ap.add_argument("--rig", help='rigctl arguments, e.g. "-m 2 -r 127.0.0.1:4532"')
    a = ap.parse_args()
    presets = []
    try:
        import config as cfgmod          # inside the OffGrid Comms folder: offer its radios
        presets = presets_from_offgrid_config(cfgmod.load_config())
    except Exception:
        pass

    def busy():
        # hamshack: Mercury / modem73 run as services and hold the codec
        for unit, user in (("mercury", False), ("modem73", True)):
            cmd = ["systemctl"] + (["--user"] if user else []) + ["is-active", "--quiet", unit]
            try:
                if subprocess.run(cmd, timeout=5).returncode == 0:
                    return (f"{unit} is running and holds the sound card.\n\n"
                            "On the hamshack run:  shack-mode apps\n(and  shack-mode mercury  when you're done)")
            except (OSError, subprocess.TimeoutExpired):
                pass
        return None

    app = Adw.Application(application_id="net.ag7jc.LevelCheck",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        LevelCheckWindow(presets=presets, device=a.device, rig=a.rig,
                         busy_check=None if presets else busy, application=app)
    app.connect("activate", activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
