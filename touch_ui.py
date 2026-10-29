"""
OffGrid Comms - screen profile support (desktop vs. touch).

The same app runs on the MSI laptop (mouse, ~15" screen) and the FZ-M1
Toughpad (7" 1280x800 touchscreen, ~216 ppi, rotates). On the tablet the
desktop layout is physically tiny, rows are wider than the screen in
portrait, and Tk has no touch scrolling and never triggers GNOME's
on-screen keyboard. This module provides:

  * UIMetrics      -- picks the profile and scales fonts/pixel sizes
  * DragScroller   -- finger-drag scrolling for the main canvas
  * FreqKeypad     -- big on-screen number pad for the frequency field

Profile selection ("ui" section of config.json):
  profile: "auto" (default) -> "touch" if a touchscreen is present,
           else "desktop"; or force "touch" / "desktop".
  scale:   touch size multiplier, null -> 1.75.

Touchscreen detection reads udev's database (/run/udev/data), which is
world-readable and already has ID_INPUT_TOUCHSCREEN=1 on the tablet's
Sharp touch panel -- no root, no extra packages.
"""

import glob
import tkinter as tk
import tkinter.font as tkfont

DEFAULT_TOUCH_SCALE = 1.75
NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
               "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
               "TkIconFont", "TkTooltipFont")


def touchscreen_present():
    for path in glob.glob("/run/udev/data/c13:*"):
        try:
            with open(path, errors="replace") as f:
                if "E:ID_INPUT_TOUCHSCREEN=1" in f.read():
                    return True
        except OSError:
            continue
    return False


class UIMetrics:
    def __init__(self, ui_cfg):
        ui_cfg = ui_cfg or {}
        profile = (ui_cfg.get("profile") or "auto").lower()
        if profile not in ("auto", "touch", "desktop"):
            profile = "auto"
        if profile == "auto":
            profile = "touch" if touchscreen_present() else "desktop"
        self.profile = profile
        self.touch = profile == "touch"

        scale = ui_cfg.get("scale")
        try:
            scale = float(scale) if scale is not None else None
        except (TypeError, ValueError):
            scale = None
        if self.touch:
            self.s = scale if scale and 0.5 <= scale <= 4.0 else DEFAULT_TOUCH_SCALE
        else:
            self.s = 1.0

    def px(self, n):
        """Scale a pixel measurement."""
        return max(1, int(round(n * self.s)))

    def pt(self, size):
        """Scale a font size in points."""
        return max(6, int(round(size * self.s)))

    def font(self, size, bold=False, mono=False):
        family = "DejaVu Sans Mono" if mono else "DejaVu Sans"
        return (family, self.pt(size), "bold") if bold else (family, self.pt(size))

    def scale_named_fonts(self, root):
        """Scale Tk's named fonts so ttk widgets, combobox drop-downs and
        message boxes grow along with everything else."""
        if self.s == 1.0:
            return
        for name in NAMED_FONTS:
            try:
                f = tkfont.nametofont(name, root=root)
            except (tk.TclError, TypeError):
                try:
                    f = tkfont.nametofont(name)
                except tk.TclError:
                    continue
            size = f.cget("size")
            # negative sizes are pixels, positive are points
            if size < 0:
                f.configure(size=-self.px(-size))
            elif size > 0:
                f.configure(size=self.pt(size))


class DragScroller:
    """Finger-drag scrolling for a Canvas used as a scrolling container.

    Only drags that start on passive widgets (frames, labels, the canvas
    itself, the status-light canvases) scroll. Buttons, entries, text
    boxes, combo boxes and the tree keep their normal tap behaviour, so a
    scroll can never accidentally press a button. There's plenty of panel
    background between controls to drag on.
    """

    PASSIVE = (tk.Frame, tk.Label, tk.Canvas)

    def __init__(self, root, canvas):
        self.root = root
        self.canvas = canvas
        self._active = False
        root.bind_all("<ButtonPress-1>", self._press, add="+")
        root.bind_all("<B1-Motion>", self._motion, add="+")
        root.bind_all("<ButtonRelease-1>", self._release, add="+")

    def _inside_canvas(self, widget):
        w = widget
        while w is not None:
            if w is self.canvas:
                return True
            w = getattr(w, "master", None)
        return False

    def _press(self, event):
        w = event.widget
        self._active = (isinstance(w, self.PASSIVE) and not isinstance(w, tk.Button)
                        and self._inside_canvas(w))
        if self._active:
            self.canvas.scan_mark(0, event.y_root)

    def _motion(self, event):
        if self._active:
            self.canvas.scan_dragto(0, event.y_root, gain=1)

    def _release(self, event):
        self._active = False


class FreqKeypad(tk.Toplevel):
    """Modal number pad for entering a frequency in MHz on a touchscreen.

    on_set(value_str) is called with the entered text when Set is pressed.
    """

    def __init__(self, root, metrics, colors, initial, on_set):
        super().__init__(root)
        self.m = metrics
        self.c = colors
        self.on_set = on_set
        self.value = tk.StringVar(value=initial or "")

        self.title("Frequency (MHz)")
        self.configure(bg=colors["BG"])
        self.transient(root)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        pad = metrics.px(6)
        display = tk.Label(self, textvariable=self.value, anchor="e",
                           bg="#0c0f12", fg=colors["TEAL"],
                           font=metrics.font(20, bold=True, mono=True),
                           padx=pad * 2, pady=pad * 2, width=11)
        display.grid(row=0, column=0, columnspan=3, sticky="ew", padx=pad, pady=(pad, pad * 2))
        tk.Label(self, text="MHz, e.g. 14.105 or 7.100", bg=colors["BG"], fg=colors["GRAY"],
                 font=metrics.font(9)).grid(row=1, column=0, columnspan=3, pady=(0, pad))

        keys = [("7", "8", "9"), ("4", "5", "6"), ("1", "2", "3"), (".", "0", "⌫")]
        for r, row in enumerate(keys, start=2):
            for col, key in enumerate(row):
                self._key(key, r, col)

        self._action("Clear", lambda: self.value.set(""), colors["GRAY"], 6, 0)
        self._action("Cancel", self.destroy, colors["RED"], 6, 1)
        self._action("Set", self._set, colors["GREEN"], 6, 2)

        for col in range(3):
            self.grid_columnconfigure(col, weight=1, uniform="keys")

        self.bind("<Return>", lambda e: self._set())
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<KeyPress>", self._keypress)

        self.update_idletasks()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - self.winfo_reqwidth()) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        self.lift()
        self.focus_force()
        # No grab_set(): on the tablet a pointer grab can swallow touch
        # input, leaving the pad visible but dead. transient + lift keep it
        # on top of the main window anyway.

    def _button(self, text, command, fg, bg=None):
        c = self.c
        return tk.Button(self, text=text, command=command,
                         bg=bg or c["PANEL_BG"], fg=fg,
                         activebackground=fg, activeforeground=c["BG"],
                         relief="flat", font=self.m.font(18, bold=True),
                         width=4, padx=self.m.px(6), pady=self.m.px(8))

    def _key(self, key, row, col):
        if key == "⌫":
            cmd = self._backspace
        else:
            cmd = lambda k=key: self._press(k)
        b = self._button(key, cmd, self.c["TEAL"] if key != "⌫" else self.c["ORANGE"])
        p = self.m.px(3)
        b.grid(row=row, column=col, sticky="nsew", padx=p, pady=p)

    def _action(self, text, command, fg, row, col):
        b = self._button(text, command, fg)
        b.configure(font=self.m.font(13, bold=True))
        p = self.m.px(3)
        b.grid(row=row, column=col, sticky="nsew", padx=p, pady=(self.m.px(8), p))

    def _press(self, key):
        v = self.value.get()
        if key == "." and "." in v:
            return
        if len(v) >= 11:
            return
        self.value.set(v + key)

    def _backspace(self):
        self.value.set(self.value.get()[:-1])

    def _keypress(self, event):
        if event.char and (event.char.isdigit() or event.char == "."):
            self._press(event.char)
        elif event.keysym == "BackSpace":
            self._backspace()

    def _set(self):
        v = self.value.get().strip()
        self.destroy()
        if v:
            self.on_set(v)


class TextKeyboard(tk.Toplevel):
    """Modal message editor. On the touch profile it carries its own
    QWERTY keyboard (Tk under XWayland never raises GNOME's on-screen
    one); on the desktop it's just a text box. A physical keyboard works
    in both. Calls on_done(text) on the action button."""

    ROWS = ["1234567890", "QWERTYUIOP", "ASDFGHJKL'", "ZXCVBNM,.?"]

    def __init__(self, root, metrics, colors, title, on_done, initial="",
                 action="Queue", max_len=200):
        super().__init__(root, bg=colors["BG"])
        self.m, self.c, self.on_done, self.max_len = metrics, colors, on_done, max_len
        self.title(title)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        m, c = metrics, colors
        pad = m.px(4)
        cols = 10

        self.text = tk.Text(self, height=3, width=20, wrap="word", bg="#0c0f12", fg=c["TEAL"],
                            insertbackground=c["TEAL"], font=m.font(13, mono=True),
                            padx=pad * 2, pady=pad * 2)
        self.text.insert("1.0", initial)
        self.text.grid(row=0, column=0, columnspan=cols, sticky="ew", padx=pad, pady=(pad, 0))
        self.count = tk.Label(self, bg=c["BG"], fg=c["GRAY"], font=m.font(9), anchor="e")
        self.count.grid(row=1, column=0, columnspan=cols, sticky="ew", padx=pad)
        self.text.bind("<<Modified>>", self._changed)
        self._changed()

        row = 2
        if m.touch:
            self.shift = False
            self.key_buttons = []
            for keys in self.ROWS:
                for col, k in enumerate(keys):
                    b = self._btn(k, lambda k=k: self._type(k), c["TEAL"], 13)
                    b.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)
                    self.key_buttons.append(b)
                row += 1
            specials = [("-", 0, 1, lambda: self._type("-")),
                        ("/", 1, 1, lambda: self._type("/")),
                        ("space", 2, 5, lambda: self._type(" ")),
                        ("⌫", 7, 3, self._backspace)]
            for label, col, span, cmd in specials:
                b = self._btn(label, cmd, c["ORANGE"] if label == "⌫" else c["TEAL"], 12)
                b.grid(row=row, column=col, columnspan=span, sticky="nsew", padx=1, pady=1)
            row += 1

        acts = tk.Frame(self, bg=c["BG"])
        acts.grid(row=row, column=0, columnspan=cols, sticky="ew", pady=(pad * 2, pad))
        for i, (label, cmd, fg) in enumerate([("Clear", self._clear, c["GRAY"]),
                                              ("Cancel", self.destroy, c["RED"]),
                                              (action, self._done, c["GREEN"])]):
            self._btn(label, cmd, fg, 12, parent=acts).grid(row=0, column=i, sticky="ew", padx=pad)
            acts.grid_columnconfigure(i, weight=1, uniform="acts")

        for col in range(cols):
            self.grid_columnconfigure(col, weight=1, uniform="kb")

        self.bind("<Escape>", lambda e: self.destroy())
        self.text.bind("<Return>", lambda e: (self._done(), "break")[1])

        self.update_idletasks()
        if m.touch:
            w = min(root.winfo_width() - m.px(8), m.px(620))
            w = max(w, self.winfo_reqwidth())
        else:
            w = max(self.winfo_reqwidth(), 480)
        h = self.winfo_reqheight()
        x = root.winfo_rootx() + max(0, (root.winfo_width() - w) // 2)
        y = root.winfo_rooty() + max(0, (root.winfo_height() - h) // 4)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.lift()
        self.text.focus_force()
        # no grab_set() -- see FreqKeypad: it can swallow touch input

    def _btn(self, text, command, fg, size, parent=None):
        c, m = self.c, self.m
        kw = dict(text=text, command=command, bg=c["PANEL_BG"], fg=fg,
                  activebackground=fg, activeforeground=c["BG"], relief="flat",
                  font=m.font(size, bold=True), padx=0)
        kw["pady"] = m.px(9) if m.touch else 4
        return tk.Button(parent or self, **kw)

    def value(self):
        return " ".join(self.text.get("1.0", "end").split())

    def _changed(self, event=None):
        self.text.edit_modified(False)
        raw = self.text.get("1.0", "end-1c")
        if len(raw) > self.max_len:
            self.text.delete(f"1.0+{self.max_len}c", "end")
        self.count.configure(text=f"{len(self.text.get('1.0', 'end-1c'))}/{self.max_len}")

    def _type(self, ch):
        self.text.insert("insert", ch)
        self.text.see("insert")

    def _backspace(self):
        self.text.delete("insert-1c", "insert")

    def _clear(self):
        self.text.delete("1.0", "end")

    def _done(self):
        v = self.value()
        if not v:
            return
        self.destroy()
        self.on_done(v)
