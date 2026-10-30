"""
OffGrid Comms -- Systems tab.

  Radio setup   DATA | VOICE for each radio, using radio_profile.py (the same
                tool as Shack Services on the hamshack). There's no
                shack-mode out here, so this page does the handover itself:
                if Mercury or modem73 is using that radio it offers to stop
                them first, so nothing queued goes out mid-change. If no
                rigctld is up for the radio it starts one just for the switch.
  Audio         Level check (also in the ☰ menu).
  Status        one light per part of the stack.

Setups are stored in ~/.local/share/radio-profile/<radio>/ -- copy data.json
and voice.json from the hamshack to use the same ones out here.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

import mercury_check
from process_manager import ManagedProcess

HERE = Path(__file__).resolve().parent
RADIO_PROFILE = HERE / "radio_profile.py"
PROFILE_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "radio-profile"
PROFILE_IDS = {1035: "ft991a", 2045: "kx3"}          # hamlib model -> radio_profile radio
SETUPS = ("data", "voice")


def parse_plan(text):
    """The change lines from `radio_profile ... --dry-run` output
    ([] = already matches, None = couldn't tell)."""
    if "nothing to change" in text:
        return []
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if "change(s) to" in l) + 1
    except StopIteration:
        return None
    out = []
    for l in lines[start:]:
        if l.startswith("(dry run"):
            break
        if l.strip():
            out.append(" ".join(l.split()))
    return out


def profile_state(pid):
    d = PROFILE_HOME / pid
    saved = [n for n in SETUPS if (d / f"{n}.json").exists()]
    try:
        cur = json.loads((d / "current.json").read_text())
        return saved, cur.get("name"), cur.get("time")
    except (OSError, ValueError):
        return saved, None, None


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


class SystemsPage:
    def __init__(self, win, light_cls):
        self.w = win
        self.Light = light_cls
        self.setup = {}           # radio -> {"row", "btns": {name: btn}}
        self.busy = set()
        self.status = {}          # key -> (row, light)
        self._temp_rigctld = None
        self._ver = None              # (light, subtitle, mismatch) once checked
        self._ver_key = None          # (binary, mtime): re-ask -V only when the binary changes
        self._ver_checked = 0         # last home check (monotonic)
        self._ver_running = False
        self._ver_warned = False

    # ------------------------------------------------------------ layout

    def build(self):
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(
            title="Radio setup",
            description="Switch a radio between your saved data and voice setups. Save each one first "
                        "from the ⋯ menu, with the radio set up the way you want it.")
        for radio, rc in self.w.cfg["radios"].items():
            g.add(self._setup_row(radio, rc))
        page.add(g)

        g = Adw.PreferencesGroup(title="Audio")
        r = Adw.ActionRow(title="Level check",
                          subtitle="Sound card level and tone for Mercury -- run it with the HF stack stopped")
        b = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
        b.connect("clicked", lambda _b: self.w.open_level_check())
        r.add_suffix(b)
        r.set_activatable_widget(b)
        g.add(r)
        page.add(g)

        g = Adw.PreferencesGroup(title="Status")
        for key, title in (("rigctld", "rigctld"), ("mercury", "Mercury"), ("connector", "mercury-connector"),
                           ("modem73", "modem73"), ("rnsd", "rnsd"), ("meshchatx", "MeshChatX"),
                           ("anytone", "Anytone TNC"), ("mercury_version", "Mercury version")):
            row = Adw.ActionRow(title=title, subtitle="…")
            light = self.Light()
            row.add_prefix(light)
            g.add(row)
            self.status[key] = (row, light)
        page.add(g)

        a = Gio.SimpleAction.new("save-setup", GLib.VariantType.new("s"))
        a.connect("activate", lambda _a, p: self.save_setup(*p.get_string().split("|", 1)))
        self.w.app.add_action(a)
        self.refresh()
        return page

    def _setup_row(self, radio, rc):
        pid = PROFILE_IDS.get(rc.get("hamlib_model"))
        row = Adw.ActionRow(title=radio, subtitle="")
        box = Gtk.Box(valign=Gtk.Align.CENTER)
        box.add_css_class("linked")
        btns = {}
        for name in SETUPS:
            b = Gtk.Button(label=name.upper())
            b.add_css_class("setup-btn")
            b.connect("clicked", lambda _b, r=radio, n=name: self.clicked(r, n))
            box.append(b)
            btns[name] = b
        row.add_suffix(box)
        if pid:
            menu = Gio.Menu()
            for name in SETUPS:
                menu.append(f"Save {radio} setup as {name.upper()}…",
                            f"app.save-setup({GLib.Variant('s', f'{radio}|{name}').print_(False)})")
            mb = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                tooltip_text="Save setups")
            mb.add_css_class("flat")
            row.add_suffix(mb)
        self.setup[radio] = {"row": row, "btns": btns, "pid": pid}
        return row

    # ------------------------------------------------------------ refresh

    def refresh(self):
        for radio in self.setup:
            self._refresh_setup(radio)
        st = self.w.stack

        def proc(p):
            return bool(p and p.is_running)
        rig = proc(st.rigctld) or proc(self._temp_rigctld)
        rig_for = st.rigctld_radio if proc(st.rigctld) else ("radio setup" if rig else "")
        items = {
            "rigctld": (rig, f"running for {rig_for}" if rig else "stopped"),
            "mercury": (st.mercury_running, "running" if st.mercury_running else "stopped"),
            "connector": (st.connector_running, "running" if st.connector_running else "stopped"),
            "modem73": (st.modem73_running, "running" if st.modem73_running else "stopped"),
            "meshchatx": (proc(st.meshchatx), "running" if proc(st.meshchatx) else "stopped"),
        }
        rn = getattr(self.w.lights.get("rnsd"), "_color", "gray")
        items["rnsd"] = (rn == "green", {"green": "up", "red": "not running"}.get(rn, "unknown"))
        self._check_mercury_version()
        bound = self.w.anytone.is_bound()
        items["anytone"] = (bound, f"bound to {self.w.anytone.device_path}" if bound else "not bound")
        for key, (on, text) in items.items():
            row, light = self.status[key]
            light.set_color("green" if on else ("red" if text == "not running" else "gray"))
            row.set_subtitle(text)
        row, light = self.status["mercury_version"]
        if self._ver:
            light.set_color(self._ver[0])
            row.set_subtitle(self._ver[1])
        else:
            row.set_subtitle("checking…")

    # ------------------------------------------------------------ Mercury version

    VERSION_RECHECK = 15 * 60        # home's version, at most this often

    def _check_mercury_version(self):
        """Compare this machine's Mercury with home's. In the background, on
        start, then every 15 minutes or as soon as the binary changes."""
        if self._ver_running:
            return
        mc = self.w.cfg["mercury"]
        binary = self.w.stack._find_bin(mc.get("binary", "mercury"), ["~/mercury/mercury", "/usr/bin/mercury"])
        try:
            key = (binary, os.path.getmtime(os.path.expanduser(binary)))
        except (OSError, TypeError):
            key = (binary, None)
        if key == self._ver_key and time.monotonic() - self._ver_checked < self.VERSION_RECHECK:
            return
        self._ver_running = True
        url = mc.get("home_status_url", "")
        home_name = (mc.get("peers") or ["home"])[0]

        def work():
            local = mercury_check.binary_version(binary)
            home = mercury_check.home_version(url)
            GLib.idle_add(self._version_checked, key, mercury_check.summarize(local, home, home_name))
        threading.Thread(target=work, daemon=True).start()

    def _version_checked(self, key, result):
        self._ver_running = False
        self._ver_key, self._ver_checked, self._ver = key, time.monotonic(), result
        if result[2] and not self._ver_warned:
            self._ver_warned = True
            self.w.toast("Mercury here doesn't match home's -- see Systems")
            self.w._log(f"[ui] Mercury version: {result[1]}")
        return False

    def _refresh_setup(self, radio):
        s = self.setup[radio]
        row, btns, pid = s["row"], s["btns"], s["pid"]
        if radio in self.busy:
            return
        if not pid:
            row.set_subtitle("not supported by radio-profile yet")
            for b in btns.values():
                b.set_sensitive(False)
            return
        saved, cur, since = profile_state(pid)
        if not saved:
            text = "no setups saved yet -- ⋯ › Save…"
        elif cur:
            when = since[11:16] if since and len(since) >= 16 else ""
            text = f"last set to {cur.upper()}" + (f" at {when}" if when else "")
        else:
            text = "setup unknown"
        dev = self.w.cfg["radios"][radio].get("device", "")
        if dev and not os.path.exists(dev):
            text += " · not plugged in"
        row.set_subtitle(text)
        for name, b in btns.items():
            b.set_sensitive(name in saved)
            (b.add_css_class if name == cur else b.remove_css_class)("current")

    def _start_busy(self, radio, text):
        self.busy.add(radio)
        s = self.setup[radio]
        s["row"].set_subtitle(text)
        for b in s["btns"].values():
            b.set_sensitive(False)

    def _done(self, radio):
        self._stop_temp_rigctld()
        self.busy.discard(radio)
        self._refresh_setup(radio)
        self.refresh()

    # ------------------------------------------------------------ rigctld

    def _ensure_rigctld(self, radio):
        """True when a rigctld answers for this radio; starts one just for
        this switch if needed (stopped again afterwards)."""
        rc = self.w.cfg["radios"][radio]
        port = rc["rigctld_port"]
        if port_open(port):
            return True, ""
        dev = rc.get("device", "")
        if not dev or not os.path.exists(dev):
            return False, f"the {radio} isn't plugged in ({dev or 'no device set'})"
        cmd = ["rigctld", "-m", str(rc["hamlib_model"]), "-r", dev, "-s", str(rc["baud"]), "-t", str(port)]
        cmd += rc.get("rigctld_extra_args", [])
        self._temp_rigctld = ManagedProcess(f"rigctld ({radio}, radio setup)", cmd)
        if not self._temp_rigctld.start():
            return False, "rigctld didn't start"
        for _ in range(20):
            if port_open(port):
                return True, ""
            if not self._temp_rigctld.is_running:
                break
            time.sleep(0.25)
        return False, "rigctld started but isn't answering -- check the radio's cable and that it's on"

    def _stop_temp_rigctld(self):
        if self._temp_rigctld:
            self._temp_rigctld.stop()
            self._temp_rigctld = None

    def _run(self, pid, radio, *args, timeout=120):
        port = self.w.cfg["radios"][radio]["rigctld_port"]
        cmd = [sys.executable, str(RADIO_PROFILE), pid, *args, "--no-handover", "--rigctld", f"127.0.0.1:{port}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)

    # ------------------------------------------------------------ switch

    def _hf_user(self, radio):
        st = self.w.stack
        if st.rigctld_radio != radio:
            return None
        if st.mercury_running:
            return "Mercury"
        if st.modem73_running:
            return "modem73"
        return None

    def clicked(self, radio, name):
        if radio in self.busy:
            return
        user = self._hf_user(radio)
        if user:
            self.w.confirm(
                f"{user} is using the {radio}",
                f"Stop {user} while the {radio} is set for {name.upper()}? Anything queued stays queued; "
                f"start {user} again when you're ready.",
                lambda: self._stop_hf_then(radio, name, user), yes=f"Stop {user}")
            return
        self._dry_run(radio, name)

    def _stop_hf_then(self, radio, name, user):
        if user == "Mercury":
            self.w.stop_mercury()
        else:
            self.w.stack.stop_modem73_and_rigctld()
            self.w._log("[ui] modem73 stopped for radio setup")
        self._stopped_hf = user
        self._dry_run(radio, name)

    def _dry_run(self, radio, name):
        pid = self.setup[radio]["pid"]
        self._start_busy(radio, f"reading the {radio}…")

        def work():
            ok, msg = self._ensure_rigctld(radio)
            if not ok:
                GLib.idle_add(self._fail, radio, f"Can't reach the {radio}", msg)
                return
            ok, out = self._run(pid, radio, name, "--dry-run")
            plan = parse_plan(out) if ok else None
            GLib.idle_add(self._confirm, radio, name, plan, out)
        threading.Thread(target=work, daemon=True).start()

    def _fail(self, radio, heading, body):
        self._done(radio)
        self.w.alert(heading, body.strip()[-1200:] or "radio-profile failed")
        return False

    def _confirm(self, radio, name, plan, out):
        label = name.upper()
        if plan is None:
            return self._fail(radio, f"Couldn't read the {radio}", out)
        notes = [l.lstrip("⚠ ") for l in out.splitlines() if l.startswith("⚠")]
        if not plan:
            self._done(radio)
            if notes:
                self.w.alert(f"{radio} already set for {label}", "\n".join(notes))
            else:
                self.w.toast(f"{radio} already set for {label}")
            return False
        shown = "\n".join(plan[:14]) + (f"\n…and {len(plan) - 14} more" if len(plan) > 14 else "")
        self.w.ask(f"Set the {radio} for {label}?", f"{len(plan)} change(s):\n\n{shown}",
                   lambda rid: self._apply(radio, name) if rid == "yes" else self._done(radio),
                   [("no", "Cancel", None), ("yes", f"Set {label}", "suggested")], default="yes")
        return False

    def _apply(self, radio, name):
        pid = self.setup[radio]["pid"]
        self.setup[radio]["row"].set_subtitle(f"setting the {radio} for {name.upper()}…")
        self.w._log(f"[ui] setting the {radio} for {name.upper()}")

        def work():
            ok, out = self._run(pid, radio, name, "-y")
            GLib.idle_add(self._applied, radio, name, ok, out)
        threading.Thread(target=work, daemon=True).start()

    def _applied(self, radio, name, ok, out):
        label = name.upper()
        for line in out.splitlines():
            if line.strip():
                self.w._log(f"[radio-profile] {line}")
        self._done(radio)
        notes = [l.lstrip("⚠ ") for l in out.splitlines() if l.startswith("⚠")]
        stopped = getattr(self, "_stopped_hf", None)
        self._stopped_hf = None
        again = f"\n\n{stopped} was stopped for this -- start it again when you're ready." if stopped else ""
        if not ok:
            self.w.alert(f"{radio} not fully set for {label}", out.strip()[-1500:])
        elif notes or again:
            self.w.alert(f"{radio} set for {label}", ("\n".join(notes) + again).strip())
        else:
            self.w.toast(f"{radio} set for {label}")
        return False

    # ------------------------------------------------------------ save

    def save_setup(self, radio, name):
        if radio in self.busy or radio not in self.setup or not self.setup[radio]["pid"]:
            return
        user = self._hf_user(radio)
        if user:
            self.w.alert(f"{user} is using the {radio}",
                         f"Stop {user} first, then save -- reading every setting takes a few seconds.")
            return
        self.w.confirm(f"Save the {radio}'s setup as {name.upper()}?",
                       f"Reads the {radio}'s settings as they are right now and stores them as "
                       f"{name.upper()}, replacing the saved one.",
                       lambda: self._save(radio, name), yes="Save")

    def _save(self, radio, name):
        pid = self.setup[radio]["pid"]
        self._start_busy(radio, f"reading the {radio} to save as {name.upper()}…")

        def work():
            ok, msg = self._ensure_rigctld(radio)
            if ok:
                ok, msg = self._run(pid, radio, "save", name)
            GLib.idle_add(self._saved, radio, name, ok, msg)
        threading.Thread(target=work, daemon=True).start()

    def _saved(self, radio, name, ok, out):
        self._done(radio)
        if ok:
            self.w._log(f"[radio-profile] {out.strip()}")
            self.w.toast(f"{radio} saved as {name.upper()}")
        else:
            self.w.alert("Not saved", out.strip()[-800:])
        return False

    def shutdown(self):
        self._stop_temp_rigctld()
