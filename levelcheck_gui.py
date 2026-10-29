#!/usr/bin/env python3
"""
Level Check -- a small window around levelcheck.py for setting HF data audio
levels in the field, without a terminal.

  * LIVE METER   watch the receive level while you turn the AF gain / DATA OUT
  * SEND TONE    key the radio and send a clean 1000 Hz tone (the other station
                 measures it)
  * ANALYZE      record 12 s and say in plain words what's wrong: clipping,
                 level, distortion, frequency offset

Stop Mercury / modem73 first -- they hold the sound card and the radio.

  standalone:      python3 levelcheck_gui.py
                   python3 levelcheck_gui.py --device plughw:CARD=CODEC,DEV=0 \\
                                             --rig "-m 2 -r 127.0.0.1:4532"
  from OffGrid Comms: the "Level check" button in the Mercury panel.
"""
import argparse
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import levelcheck as lc

BG, PANEL_BG, FG = "#12161a", "#1a1f24", "#cfd8dc"
TEAL, ORANGE, GREEN, RED, GRAY = "#00d9d9", "#ff9500", "#33cc66", "#ff4444", "#5a6570"

METER_MIN, METER_MAX = -60.0, 0.0


def sound_cards():
    """[(label, plughw-name)] for every capture device arecord knows."""
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    cards = re.findall(r"^card (\d+): (\S+) \[(.*?)\], device (\d+)", out, re.M)
    return [(f"{name} ({cid})", f"plughw:CARD={cid},DEV={dev}") for _num, cid, name, dev in cards]


def presets_from_offgrid_config(cfg):
    """[(name, audio_device, rig_args)] from an OffGrid Comms config dict."""
    out = []
    mc = cfg.get("mercury", {})
    for name, r in cfg.get("radios", {}).items():
        dev = r.get("device") or ""
        if not dev or "CHANGE_ME" in dev:
            continue
        audio = r.get("mercury_audio") or mc.get("audio_device") or ""
        rig = f"-m {r.get('hamlib_model', 2)} -r {dev} -s {r.get('baud', 38400)}"
        out.append((name, audio, rig))
    return out


class LevelCheckWindow:
    def __init__(self, master, presets=None, default=None, device=None, rig=None,
                 scale=1.0, busy_check=None):
        """master: a Tk root or Toplevel to build into.
        presets: [(name, audio_device, rig_args)]; default: preset name to select.
        busy_check: callable returning a reason string if the card/radio is in use."""
        self.win = master
        self.scale = scale
        self.busy_check = busy_check
        self.presets = list(presets or [])
        self.presets.append(("rigctld on this computer (port 4532)", device or "", "-m 2 -r 127.0.0.1:4532"))
        self.q = queue.Queue()
        self.meter_proc = None
        self.tone_stop = None
        self.tone_thread = None
        self.analyzing = False
        self.peak_hold = (METER_MIN, 0.0)

        master.title("Level Check")
        master.configure(bg=BG)
        master.protocol("WM_DELETE_WINDOW", self.close)
        self._build(default, device, rig)
        self._poll()

    # ---------- layout ----------

    def f(self, size, bold=False, mono=False):
        fam = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        return (fam, int(round(size * self.scale)), "bold" if bold else "normal")

    def px(self, n):
        return int(round(n * self.scale))

    def _button(self, parent, text, cmd, color):
        b = tk.Label(parent, text=text, fg=color, bg=PANEL_BG, font=self.f(10, bold=True),
                     padx=self.px(12), pady=self.px(6), cursor="hand2",
                     highlightthickness=1, highlightbackground="#2a323a")
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def _build(self, default, device, rig):
        w = self.win
        pad = self.px(10)
        outer = tk.Frame(w, bg=BG)
        outer.pack(fill="both", expand=True, padx=pad, pady=pad)

        tk.Label(outer, text="LEVEL CHECK", fg=TEAL, bg=BG, font=self.f(15, bold=True, mono=True)).pack(anchor="w")
        tk.Label(outer, text="Stop Mercury first. Receiving station: METER or ANALYZE.  Sending station: SEND TONE.",
                 fg=GRAY, bg=BG, font=self.f(9)).pack(anchor="w", pady=(0, pad))

        # radio / sound card / rig
        box = tk.Frame(outer, bg=PANEL_BG)
        box.pack(fill="x")
        grid = tk.Frame(box, bg=PANEL_BG)
        grid.pack(fill="x", padx=pad, pady=pad)
        tk.Label(grid, text="Radio", fg=FG, bg=PANEL_BG, font=self.f(10)).grid(row=0, column=0, sticky="w")
        self.preset_var = tk.StringVar()
        self.preset_box = ttk.Combobox(grid, textvariable=self.preset_var, state="readonly",
                                       values=[p[0] for p in self.presets], font=self.f(10))
        self.preset_box.grid(row=0, column=1, sticky="ew", padx=(pad, 0), pady=2)
        self.preset_box.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())

        tk.Label(grid, text="Sound card", fg=FG, bg=PANEL_BG, font=self.f(10)).grid(row=1, column=0, sticky="w")
        self.cards = sound_cards()
        self.device_var = tk.StringVar(value=device or "")
        self.device_box = ttk.Combobox(grid, textvariable=self.device_var,
                                       values=[c[1] for c in self.cards], font=self.f(10))
        self.device_box.grid(row=1, column=1, sticky="ew", padx=(pad, 0), pady=2)
        names = "  ".join(f"{d} = {l}" for l, d in self.cards)
        tk.Label(grid, text=names, fg=GRAY, bg=PANEL_BG, font=self.f(8), wraplength=self.px(520),
                 justify="left").grid(row=2, column=1, sticky="w", padx=(pad, 0))

        tk.Label(grid, text="Rig control", fg=FG, bg=PANEL_BG, font=self.f(10)).grid(row=3, column=0, sticky="w")
        self.rig_var = tk.StringVar(value=rig or "")
        tk.Entry(grid, textvariable=self.rig_var, bg=BG, fg=FG, insertbackground=FG, relief="flat",
                 font=self.f(9, mono=True)).grid(row=3, column=1, sticky="ew", padx=(pad, 0), pady=2, ipady=3)
        grid.grid_columnconfigure(1, weight=1)

        names = [p[0] for p in self.presets]
        if default in names:
            self.preset_var.set(default)
            if not device:
                self._apply_preset()
        elif not device and names:
            self.preset_var.set(names[0])
            self._apply_preset()

        # meter
        mbox = tk.Frame(outer, bg=PANEL_BG)
        mbox.pack(fill="x", pady=(pad, 0))
        top = tk.Frame(mbox, bg=PANEL_BG)
        top.pack(fill="x", padx=pad, pady=(pad, 0))
        tk.Label(top, text="Receive level", fg=FG, bg=PANEL_BG, font=self.f(10)).pack(side="left")
        self.level_lbl = tk.Label(top, text="meter off", fg=GRAY, bg=PANEL_BG, font=self.f(11, bold=True, mono=True))
        self.level_lbl.pack(side="right")
        self.canvas = tk.Canvas(mbox, height=self.px(34), bg="#0c0f12", highlightthickness=0)
        self.canvas.pack(fill="x", padx=pad, pady=self.px(6))
        self.canvas.bind("<Configure>", lambda e: self._draw_meter(None))
        tk.Label(mbox, text="aim for the green band while the tone is on -- never red", fg=GRAY, bg=PANEL_BG,
                 font=self.f(8)).pack(anchor="w", padx=pad, pady=(0, pad))

        # buttons
        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill="x", pady=pad)
        self.meter_btn = self._button(btns, "START METER", self.toggle_meter, TEAL)
        self.meter_btn.pack(side="left", padx=(0, pad))
        self.analyze_btn = self._button(btns, "ANALYZE 12 s", self.analyze, GREEN)
        self.analyze_btn.pack(side="left", padx=(0, pad))
        self.tone_btn = self._button(btns, "SEND TONE 30 s", self.toggle_tone, ORANGE)
        self.tone_btn.pack(side="right")
        self._button(btns, "COPY", self.copy_results, GRAY).pack(side="right", padx=(0, pad))

        # results
        self.out = tk.Text(outer, height=12, bg="#0c0f12", fg=FG, font=self.f(9, mono=True),
                           wrap="word", relief="flat", padx=self.px(8), pady=self.px(6))
        self.out.pack(fill="both", expand=True)
        for tag, color in (("ok", GREEN), ("bad", RED), ("note", ORANGE), ("dim", GRAY), ("head", TEAL)):
            self.out.tag_configure(tag, foreground=color)
        self._say("Ready. Make sure Mercury is stopped on this machine.\n", "dim")
        self._draw_meter(None)

    def copy_results(self):
        """Put the results text on the clipboard (the text box itself is read-only)."""
        text = self.out.get("1.0", "end").strip()
        self.win.clipboard_clear()
        self.win.clipboard_append(text)
        self.win.update()                  # keep it on the clipboard after the window closes
        self._say("\n(copied to the clipboard)\n", "dim")

    def _apply_preset(self):
        for name, audio, rig in self.presets:
            if name == self.preset_var.get():
                if audio:
                    self.device_var.set(audio)
                self.rig_var.set(rig)

    # ---------- output ----------

    def _say(self, text, tag=None):
        self.out.configure(state="normal")
        self.out.insert("end", text, tag)
        self.out.see("end")
        self.out.configure(state="disabled")

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

    # ---------- meter ----------

    def _draw_meter(self, level):
        c = self.canvas
        c.delete("all")
        wdt = max(c.winfo_width(), 10)
        hgt = max(c.winfo_height(), 10)

        def x(dbv):
            return (min(max(dbv, METER_MIN), METER_MAX) - METER_MIN) / (METER_MAX - METER_MIN) * wdt

        # zones: too low | ok-ish | target | hot | clip
        for lo, hi, col in ((-60, -30, "#1d2228"), (-30, -20, "#26303a"), (-20, -10, "#173d26"),
                            (-10, -3, "#3d3417"), (-3, 0, "#3d1717")):
            c.create_rectangle(x(lo), 0, x(hi), hgt, fill=col, outline="")
        for dbv in (-50, -40, -30, -20, -10, 0):
            c.create_line(x(dbv), hgt - 6, x(dbv), hgt, fill=GRAY)
            c.create_text(x(dbv) + 2, 2, text=str(dbv), anchor="nw", fill=GRAY, font=self.f(7))
        if level is None:
            return
        rms, pk, clip = level
        color = RED if clip or rms > -3 else (ORANGE if rms > -10 else (GREEN if rms >= -20 else TEAL))
        c.create_rectangle(0, hgt * 0.35, x(rms), hgt * 0.85, fill=color, outline="")
        ph, _t = self.peak_hold
        c.create_line(x(ph), hgt * 0.25, x(ph), hgt, fill="#ffffff", width=2)

    def toggle_meter(self):
        if self.meter_proc:
            self._stop_meter()
            return
        if self.analyzing:
            return
        if not self._ready(need_rig=False):
            return
        dev = self.device_var.get().strip()
        try:
            self.meter_proc = subprocess.Popen(
                ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(lc.RATE), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self._say(f"couldn't start arecord: {e}\n", "bad")
            return
        self.meter_btn.configure(text="STOP METER", fg=RED)
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
        self.meter_btn.configure(text="START METER", fg=TEAL)
        self.level_lbl.configure(text="meter off", fg=GRAY)
        self._draw_meter(None)

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
        self.analyze_btn.configure(text="RECORDING…", fg=GRAY)
        self._say("\nrecording 12 s -- start the tone on the other station now\n", "note")
        dev = self.device_var.get().strip()

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
        if not messagebox.askyesno("Send test tone",
                                   "This keys the radio and transmits a 1000 Hz tone for 30 seconds.\n\n"
                                   "Low power, clear frequency or dummy load?", parent=self.win):
            return
        dev, rig = self.device_var.get().strip(), self.rig_var.get().strip()
        self.tone_stop = threading.Event()
        self.tone_btn.configure(text="STOP TONE", fg=RED)

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
                messagebox.showwarning("Level Check", reason, parent=self.win)
                return False
        if not self.device_var.get().strip():
            messagebox.showwarning("Level Check", "Pick the radio's sound card first.", parent=self.win)
            return False
        if need_rig and not self.rig_var.get().strip():
            messagebox.showwarning("Level Check", "Rig control is empty -- pick a radio.", parent=self.win)
            return False
        return True

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "level":
                    rms, pk, clip = data
                    ph, t = self.peak_hold
                    now = time.time()
                    if pk >= ph or now - t > 1.5:
                        self.peak_hold = (pk, now)
                    self._draw_meter(data)
                    tag = "CLIP!" if clip else ("good" if -20 <= rms <= -10 else ("hot" if rms > -10 else
                                                                               ("low" if rms < -30 else "")))
                    col = RED if clip else (GREEN if tag == "good" else (ORANGE if tag == "hot" else FG))
                    self.level_lbl.configure(text=f"{rms:6.1f} dBFS  {tag}", fg=col)
                elif kind == "meter_end":
                    if self.meter_proc:
                        self._say(f"meter stopped: {data or 'arecord ended'} (is Mercury holding the card?)\n", "bad")
                        self._stop_meter()
                elif kind == "analysis":
                    self.analyzing = False
                    self.analyze_btn.configure(text="ANALYZE 12 s", fg=GREEN)
                    self._report(data)
                elif kind == "tone_done":
                    ok, msg = data
                    self.tone_btn.configure(text="SEND TONE 30 s", fg=ORANGE)
                    if not ok:
                        self._say(msg + "\n", "bad")
                elif kind == "log":
                    self._say(data, "dim")
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(100, self._poll)

    def close(self):
        if self.tone_stop:
            self.tone_stop.set()           # send_tone always unkeys on its way out
        self._stop_meter() if self.meter_proc else None
        if self.tone_thread and self.tone_thread.is_alive():
            self.tone_thread.join(timeout=3)
        self.win.destroy()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", help="sound card, e.g. plughw:CARD=CODEC,DEV=0")
    ap.add_argument("--rig", help='rigctl arguments, e.g. "-m 2 -r 127.0.0.1:4532"')
    ap.add_argument("--scale", type=float, default=1.0)
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

    root = tk.Tk(className="levelcheck")
    LevelCheckWindow(root, presets=presets, device=a.device, rig=a.rig, scale=a.scale,
                     busy_check=None if presets else busy)
    root.mainloop()


if __name__ == "__main__":
    main()
