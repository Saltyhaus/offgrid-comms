#!/usr/bin/env python3
"""
radio-profile -- save and switch whole-radio setups (data <-> voice) on the
FT-991A or the KX3 over CAT, through the rigctld that's already running.
(Examples use ft991a; kx3 works the same.)

    radio-profile ft991a ping                 check CAT works (reads the dial + one menu)
    radio-profile ft991a save data            read every menu setting + power/mic/processor/
                                              VOX/monitor and save them as "data"
    radio-profile ft991a list                 saved profiles
    radio-profile ft991a diff data voice      what differs, menu by menu (the cheat sheet)
    radio-profile ft991a voice                switch to the "voice" profile
    radio-profile ft991a data                 switch back to "data"
    radio-profile ft991a apply <name>         same, for any profile name
    radio-profile ft991a undo                 put back what the radio had before the last switch

How a switch works:
  1. reads the radio now and compares it with the profile; only what
     differs gets written, and you see the list first (-y skips the question)
  2. saves a snapshot of the radio as it was (that's what `undo` restores)
  3. FT-991A "voice": hands the radio away from Mercury first (`shack-mode
     apps`), so queued HF mail can't go out with voice settings
  4. writes each setting and reads it back to check it took
  5. FT-991A "data": hands the radio back to Mercury afterwards (`shack-mode
     mercury`). --no-handover skips steps 3 and 5. The KX3 has no handover.

Never touched: CAT/serial menu items (changing them would cut this very
connection), calibration items, and anything in skip= in the config file.
Writes are refused while the radio is transmitting.

KX3 notes: only menu entries the KX3 opens to CAT (MN/MP) can be saved -- a
useful set is read by default, add more with menus= in the config file. TX EQ
and VOX can't be set over CAT on the KX3: if VOX differs from the saved setup
after a switch you're told to press VOX on the radio. DATA mode keeps its
sub-mode (DATA A, PSK D...); going back to voice picks LSB below 10 MHz
(except 60 m) and USB above.

Files:
  profiles   ~/.local/share/radio-profile/<radio>/<name>.json
  snapshots  ~/.local/share/radio-profile/<radio>/snapshots/
  config     ~/.config/radio-profile/<radio>.ini  (rigctld address, skip list)
  labels     ~/.config/radio-profile/<radio>-labels.txt  ("072 DATA PORT SELECT",
             one per line -- optional, makes diff output readable)
"""

from __future__ import annotations

import argparse
import configparser
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "radio-profile"
CONF_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "radio-profile"

KX3_LABELS = {
    "010": "VOX GN", "016": "TX DLY", "047": "AF LIM", "058": "TUN PWR", "082": "MIC BTN",
    "096": "TX ESSB", "101": "TX GATE", "105": "AFX MD", "122": "VOX INH", "128": "AGC MD",
    "129": "AGC SPD", "134": "RX XFIL", "135": "MICBIAS", "140": "DUAL RX", "142": "RX SHFT",
    "143": "RX NR", "144": "PBT SSB",
    # readable but never written (see skip below)
    "045": "AUTOINF", "057": "RS232", "072": "TECH MD", "087": "VFO OFS", "112": "ATU DATA",
    "123": "RX I/Q", "125": "RXSBNUL", "127": "TXSBNUL", "132": "TXCRNUL", "141": "ACC2 IO",
    "146": "PA MODE", "024": "BAT MIN", "137": "BAT CHG",
}

RADIOS = {
    "ft991a": {
        "name": "FT-991A",
        "family": "yaesu",
        "rigctld": "127.0.0.1:4532",
        "menus": list(range(1, 161)),   # reads past the real end just answer "?;" and are skipped
        # CAT RATE / CAT TOT / CAT RTS on the FT-991A -- never write these
        "skip": "031 032 033",
        "handover": True,
        # direct commands saved alongside the menus: (query, reply prefix, label)
        "state": [
            ("PC;", "PC", "RF power"),
            ("MG;", "MG", "mic gain"),
            ("PR0;", "PR0", "speech processor on/off"),
            ("PL;", "PL", "processor level"),
            ("PR1;", "PR1", "parametric mic EQ on/off"),
            ("VX;", "VX", "VOX on/off"),
            ("ML0;", "ML0", "monitor on/off"),
            ("ML1;", "ML1", "monitor level"),
        ],
        "readonly": [],
        "mode": ("MD0;", "MD0"),
        # data mode <-> its voice twin: DATA-LSB/LSB, DATA-USB/USB, DATA-FM/FM
        "mode_pairs": {"8": "1", "C": "2", "A": "4"},
        "mode_first": False,
        "labels": {},
    },
    "kx3": {
        "name": "KX3",
        "family": "elecraft",
        "rigctld": "127.0.0.1:4534",
        # only entries the KX3 opens to MP; add more with menus= in the config
        "menus": [10, 16, 47, 58, 82, 96, 101, 105, 122, 128, 129, 134, 135, 140, 142, 143, 144],
        # serial/CAT, tech mode, calibration, ATU memory, amp/ACC2 I/O, battery -- never write
        "skip": "024 045 057 072 087 112 123 125 127 132 137 141 146",
        "handover": False,
        "state": [
            ("PC;", "PC", "RF power"),
            ("MG;", "MG", "mic gain"),
            ("CP;", "CP", "compression"),
            ("ML;", "ML", "monitor level"),
            ("ES;", "ES", "ESSB on/off"),
        ],
        "readonly": [("VX;", "VX", "VOX")],        # GET only on the KX3
        "mode": ("MD;", "MD"),
        "mode_first": True,                        # mic/monitor levels follow the mode
        "labels": KX3_LABELS,
    },
}
MODE_NAMES = {"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM", "6": "RTTY-LSB", "7": "CW-R",
              "8": "DATA-LSB", "9": "RTTY-USB", "A": "DATA-FM", "B": "FM-N", "C": "DATA-USB", "D": "AM-N",
              "E": "C4FM"}
KX3_MODES = {"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM", "6": "DATA", "7": "CW-REV",
             "9": "DATA-REV"}
KX3_SUB = {"0": "DATA A", "1": "AFSK A", "2": "FSK D", "3": "PSK D"}


def mode_name(spec, m):
    if m is None:
        return "?"
    if spec["family"] == "elecraft":
        md, _, sub = m.partition("/")
        if md == "6" and sub in KX3_SUB:
            return KX3_SUB[sub]
        return KX3_MODES.get(md, md) + (f" {KX3_SUB.get(sub, sub)}" if sub else "")
    return MODE_NAMES.get(m, m)


class CatError(Exception):
    pass


# ------------------------------------------------------------- transport

class Rigctld:
    """Raw CAT through rigctld's `w` (send_cmd), so we share the radio with
    everything else using that rigctld instead of fighting for the port."""

    def __init__(self, addr: str, timeout: float = 2.0):
        host, _, port = addr.rpartition(":")
        self.addr = (host or "127.0.0.1", int(port))
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def __enter__(self):
        try:
            self.sock = socket.create_connection(self.addr, timeout=self.timeout)
        except OSError as e:
            raise CatError(f"can't reach rigctld at {self.addr[0]}:{self.addr[1]} ({e}) -- is it running?")
        return self

    def __exit__(self, *a):
        if self.sock:
            self.sock.close()

    def _line(self) -> str:
        """One reply. Hamlib 4.6's rigctld ends a raw `w` reply with a NUL
        byte (no newline); other replies (PTT, RPRT) end with a newline."""
        end = time.time() + self.timeout
        while b"\n" not in self.buf and b"\0" not in self.buf:
            if time.time() > end:
                raise CatError("rigctld didn't answer in time")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise CatError("rigctld closed the connection")
            self.buf += chunk
        cut = min(i for i in (self.buf.find(b"\n"), self.buf.find(b"\0")) if i >= 0)
        line, self.buf = self.buf[:cut], self.buf[cut + 1:]
        return line.decode("ascii", "replace").strip()

    def ask(self, cmd: str, expect: str) -> str:
        """Send raw CAT (e.g. 'EX031;' or 'EX0311;EX031;') and return the
        rig's reply that starts with `expect`, e.g. 'EX0311'."""
        self.sock.sendall(f"w {cmd}\n".encode())
        for _ in range(4):
            line = self._line()
            if line.startswith("RPRT"):
                code = line.split()[-1]
                if code != "0":
                    raise CatError(f"rigctld error {code} for {cmd}")
                continue
            for token in line.split(";"):
                token = token.strip()
                if token == "?":
                    raise CatError(f"radio rejected {cmd}")
                if token.startswith(expect):
                    return token
        raise CatError(f"no {expect}… reply to {cmd}")

    def ptt(self) -> bool:
        self.sock.sendall(b"t\n")
        line = self._line()
        return line.strip() not in ("0", "")


# --------------------------------------------------------------- radio

def menu_key(n: int) -> str:
    return f"{n:03d}"


def menu_get(cat, spec, k):
    if spec["family"] == "elecraft":
        return cat.ask(f"MN{k};MP;", "MP")[2:]
    return cat.ask(f"EX{k};", f"EX{k}")[len(f"EX{k}"):]


def menu_set(cat, spec, k, v):
    if spec["family"] == "elecraft":
        return cat.ask(f"MN{k};MP{v};MP;", "MP")[2:]
    return cat.ask(f"EX{k}{v};EX{k};", f"EX{k}")[len(f"EX{k}"):]


def menu_close(cat, spec):
    """The KX3 shows the menu on its display while MN is in use -- always leave it."""
    if spec["family"] == "elecraft":
        try:
            cat.ask("MN255;MN;", "MN")
        except CatError:
            pass


def read_mode(cat, spec):
    q, prefix = spec["mode"]
    try:
        m = cat.ask(q, prefix)[len(prefix):]
    except CatError:
        return None
    if spec["family"] == "elecraft" and m in ("6", "9"):
        try:
            m += "/" + cat.ask("DT;", "DT")[2:]
        except CatError:
            pass
    return m


def read_state(cat, spec, items=None):
    state = {}
    for q, prefix, _label in (items if items is not None else spec["state"]):
        try:
            state[prefix] = cat.ask(q, prefix)[len(prefix):]
        except CatError:
            pass                                 # e.g. ESSB isn't readable in DATA on the KX3
    return state


def read_radio(cat, spec, progress=True) -> dict:
    menus = {}
    rng = spec["menus"]
    try:
        for i, n in enumerate(rng):
            k = menu_key(n)
            try:
                menus[k] = menu_get(cat, spec, k)
            except CatError:
                pass                             # not a (CAT-readable) menu on this radio
            if progress and sys.stderr.isatty():
                print(f"\r  reading menus… {i + 1}/{len(rng)}", end="", file=sys.stderr, flush=True)
    finally:
        menu_close(cat, spec)
    if progress and sys.stderr.isatty():
        print(file=sys.stderr)
    state = read_state(cat, spec)
    ro = read_state(cat, spec, spec["readonly"])
    mode = read_mode(cat, spec)
    try:
        freq = int(cat.ask("FA;", "FA")[2:])
    except (CatError, ValueError):
        freq = None
    if not menus:
        raise CatError("the radio answered no menu reads -- is this the right radio/rigctld?")
    out = {"menus": menus, "state": state, "mode": mode}
    if ro:
        out["readonly"] = ro
    if freq:
        out["freq"] = freq
    return out


def kx3_voice_sideband(freq):
    """LSB below 10 MHz except 60 m (USB by rule), USB above."""
    if freq is None:
        return "2"
    if 5_250_000 <= freq <= 5_450_000:
        return "2"
    return "1" if freq < 10_000_000 else "2"


def target_mode(cur_mode, profile_mode, spec, freq=None):
    """Only flip between voice (LSB/USB) and data; never change CW/AM/FM or
    whatever else the operator picked."""
    if not cur_mode or not profile_mode:
        return None
    if spec["family"] == "elecraft":
        cur_md, prof_md = cur_mode.split("/")[0], profile_mode.split("/")[0]
        data, voice = ("6", "9"), ("1", "2")
        if prof_md in data and cur_md in voice + data:
            return profile_mode                  # into data, with the saved sub-mode
        if prof_md in voice and cur_md in data:
            return kx3_voice_sideband(freq)      # out of data: the band's normal sideband
        return None
    pairs = spec["mode_pairs"]                       # data -> voice
    rev = {v: k for k, v in pairs.items()}           # voice -> data
    want_data = profile_mode in pairs
    want_voice = profile_mode in rev
    if want_voice and cur_mode in pairs:
        return pairs[cur_mode]
    if want_data and cur_mode in rev:
        return rev[cur_mode]
    return None


def plan(current, profile, spec, skip):
    """[(kind, key, old, new)] for everything that would change."""
    changes = []
    m = target_mode(current.get("mode"), profile.get("mode"), spec, current.get("freq"))
    mode_change = ("mode", spec["mode"][1], current.get("mode"), m) if m and m != current.get("mode") else None
    if mode_change and spec["mode_first"]:
        changes.append(mode_change)
    for k, v in sorted(profile["menus"].items()):
        if k in skip:
            continue
        if k in current["menus"] and current["menus"][k] != v:
            changes.append(("menu", k, current["menus"][k], v))
    for _q, prefix, _label in spec["state"]:
        if prefix in profile["state"] and prefix in current["state"] and current["state"][prefix] != profile["state"][prefix]:
            changes.append(("state", prefix, current["state"][prefix], profile["state"][prefix]))
    if mode_change and not spec["mode_first"]:
        changes.append(mode_change)
    return changes


def write(cat, spec, change):
    kind, key, _old, new = change
    if kind == "menu":
        got = menu_set(cat, spec, key, new)
    elif kind == "mode" and spec["family"] == "elecraft":
        md, _, sub = new.partition("/")
        got = cat.ask(f"MD{md};MD;", "MD")[2:]
        if sub and got == md:
            got += "/" + cat.ask(f"DT{sub};DT;", "DT")[2:]
    else:
        q = spec["mode"][0] if kind == "mode" else next(q for q, p, _l in spec["state"] if p == key)
        got = cat.ask(f"{key}{new};{q}", key)[len(key):]
    if got != new:
        raise CatError(f"{key}: wrote {new}, radio reads back {got}")


# ------------------------------------------------------------ files

def load_conf(radio):
    spec = dict(RADIOS[radio])
    p = CONF_HOME / f"{radio}.ini"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[{radio}]\n# rigctld that owns the radio\nrigctld = {spec['rigctld']}\n"
                     f"# menu numbers never to write (CAT/serial and calibration items are always skipped)\nskip =\n"
                     + ("# extra KX3 menu numbers to save too (only entries the KX3 opens to CAT)\nmenus =\n"
                        if spec["family"] == "elecraft" else ""))
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(p)
    sec = cp[radio] if cp.has_section(radio) else {}
    spec["rigctld"] = sec.get("rigctld", spec["rigctld"]) or spec["rigctld"]
    skip = set(spec["skip"].split()) | {menu_key(int(x)) for x in re.findall(r"\d+", sec.get("skip", "") or "")}
    extra = [int(x) for x in re.findall(r"\d+", sec.get("menus", "") or "")]
    if extra:
        spec["menus"] = sorted(set(spec["menus"]) | set(extra))
    return spec, skip


def labels(radio):
    out = dict(RADIOS[radio].get("labels", {}))
    p = CONF_HOME / f"{radio}-labels.txt"
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(r"\s*(\d{1,3})\s+(.+)", line)
            if m:
                out[menu_key(int(m.group(1)))] = m.group(2).strip()
    return out


def pdir(radio):
    d = DATA_HOME / radio
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_profile(radio, name, data, folder=None):
    d = folder or pdir(radio)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    data = dict(data, saved=dt.datetime.now().isoformat(timespec="seconds"), radio=radio, name=name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return p


def load_profile(radio, name):
    p = pdir(radio) / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"no profile '{name}' yet -- set the radio up, then: radio-profile {radio} save {name}")
    return json.loads(p.read_text())


def mark_current(radio, name):
    """Which profile the radio was last set to (shown by Shack Services)."""
    p = pdir(radio) / "current.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": name, "time": dt.datetime.now().isoformat(timespec="seconds")}))
    os.replace(tmp, p)


def describe(change, spec, lab):
    kind, key, old, new = change
    if kind == "menu":
        return f"  menu {key}  {lab.get(key, ''):<24} {old:>6}  →  {new}"
    if kind == "mode":
        return f"  mode        {'':<24} {mode_name(spec, old):>6}  →  {mode_name(spec, new)}"
    label = next(l for _q, p, l in spec["state"] if p == key)
    return f"  {key:<4}        {label:<24} {old:>6}  →  {new}"


# ------------------------------------------------------------ commands

def handover(mode, dry):
    sm = shutil.which("shack-mode") or "/usr/local/bin/shack-mode"
    if not os.path.exists(sm):
        print("(shack-mode isn't installed -- no radio handover, settings only)")
        return
    print(f"→ shack-mode {mode}")
    if dry:
        return
    r = subprocess.run([sm, "-y", mode])
    if r.returncode != 0:
        raise SystemExit(f"shack-mode {mode} didn't finish -- radio settings NOT changed")


def vox_check(cur, prof, spec):
    """Things the radio won't let CAT set (KX3 VOX): say so if they differ."""
    for _q, p, label in spec["readonly"]:
        now, want = cur.get("readonly", {}).get(p), prof.get("readonly", {}).get(p)
        if now is not None and want is not None and now != want:
            print(f"⚠ {label} isn't the way it was saved -- press {label} on the radio "
                  f"(the {spec['name']} can't set it over CAT)")


def cmd_apply(radio, name, args, spec, skip, lab, snapshot=True):
    prof = load_profile(radio, name) if not isinstance(name, dict) else name
    pname = prof.get("name", "?")
    hand = spec["handover"] and not args.no_handover
    with Rigctld(spec["rigctld"]) as cat:
        if cat.ptt():
            raise SystemExit("the radio is transmitting -- try again when it's receiving")
        print(f"reading the {spec['name']}…")
        cur = read_radio(cat, spec)
        changes = plan(cur, prof, spec, skip)
        if not changes:
            print(f"already matches '{pname}' -- nothing to change")
            vox_check(cur, prof, spec)
            if not args.dry_run:
                mark_current(radio, pname)
            if pname == "data" and hand and not args.dry_run:
                handover("mercury", False)
            return
        print(f"{len(changes)} change(s) to '{pname}':")
        for c in changes:
            print(describe(c, spec, lab))
        if args.dry_run:
            print("(dry run -- nothing written)")
            return
        if not args.yes and input("write them? [y/N] ").strip().lower() not in ("y", "yes"):
            raise SystemExit("nothing changed")
        if snapshot:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            save_profile(radio, f"before-{pname}-{stamp}", cur, pdir(radio) / "snapshots")
    if pname == "voice" and hand:
        handover("apps", False)
    failed = []
    with Rigctld(spec["rigctld"]) as cat:
        if cat.ptt():
            raise SystemExit("the radio started transmitting -- stopped before writing anything")
        try:
            if spec["mode_first"] and changes and changes[0][0] == "mode":
                # levels like mic gain / monitor belong to the mode, so set the
                # mode first, then compare the levels again in the new mode
                try:
                    write(cat, spec, changes[0])
                except CatError as e:
                    failed.append((changes[0], str(e)))
                now = read_state(cat, spec)
                rest = [c for c in changes[1:] if c[0] != "state"]
                rest = [("state", p, now[p], prof["state"][p]) for _q, p, _l in spec["state"]
                        if p in now and p in prof["state"] and now[p] != prof["state"][p]] + rest
                changes = changes[:1] + rest
                todo = rest
            else:
                todo = changes
            for c in todo:
                try:
                    write(cat, spec, c)
                except CatError as e:
                    failed.append((c, str(e)))
        finally:
            menu_close(cat, spec)
        after = {"readonly": read_state(cat, spec, spec["readonly"])}
    done = len(changes) - len(failed)
    mark_current(radio, pname if not failed else f"{pname} (partly)")
    print(f"✓ {done} of {len(changes)} written and read back")
    for c, err in failed:
        print(f"✗ {describe(c, spec, lab).strip()}   ({err})")
    vox_check(after, prof, spec)
    if pname == "data" and hand:
        if failed:
            print("not handing the radio back to Mercury -- fix the items above first")
        else:
            handover("mercury", False)
    if failed:
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="radio-profile", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("radio", choices=sorted(RADIOS))
    ap.add_argument("command", help="ping | save NAME | list | show NAME | diff A B | apply NAME | voice | data | undo")
    ap.add_argument("names", nargs="*")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask before writing")
    ap.add_argument("-n", "--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--no-handover", action="store_true", help="don't run shack-mode before/after")
    ap.add_argument("--rigctld", metavar="HOST:PORT", help="use this rigctld instead of the config file's")
    args = ap.parse_args(argv)
    radio, cmd = args.radio, args.command
    spec, skip = load_conf(radio)
    if args.rigctld:
        spec["rigctld"] = args.rigctld
    lab = labels(radio)

    try:
        if cmd == "ping":
            with Rigctld(spec["rigctld"]) as cat:
                fa = cat.ask("FA;", "FA")[2:]
                if spec["family"] == "elecraft":
                    extra = f"mode {mode_name(spec, read_mode(cat, spec))}"
                else:
                    extra = f"menu 001 = {cat.ask('EX001;', 'EX001')[5:]}"
                print(f"OK  {spec['name']} dial {int(fa) / 1e6:.6f} MHz, {extra}, "
                      f"{'TRANSMITTING' if cat.ptt() else 'receiving'}  (via rigctld {spec['rigctld']})")
        elif cmd == "save":
            if len(args.names) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", args.names[0]):
                raise SystemExit("usage: radio-profile RADIO save NAME   (letters, digits, - and _)")
            with Rigctld(spec["rigctld"]) as cat:
                data = read_radio(cat, spec)
            p = save_profile(radio, args.names[0], data)
            mark_current(radio, args.names[0])
            print(f"saved {len(data['menus'])} menu settings + {len(data['state'])} others "
                  f"(mode {mode_name(spec, data['mode'])}) as '{args.names[0]}'  → {p}")
        elif cmd == "list":
            for p in sorted(pdir(radio).glob("*.json")):
                d = json.loads(p.read_text())
                print(f"  {p.stem:<12} saved {d.get('saved', '?')}  ({len(d.get('menus', {}))} menus)")
            snaps = sorted((pdir(radio) / "snapshots").glob("*.json"))
            if snaps:
                print(f"  + {len(snaps)} snapshot(s); newest: {snaps[-1].stem}")
        elif cmd == "show":
            d = load_profile(radio, args.names[0])
            for k, v in sorted(d["menus"].items()):
                print(f"  menu {k}  {lab.get(k, ''):<24} {v}")
            for _q, p, l in spec["state"]:
                if p in d["state"]:
                    print(f"  {p:<4}      {l:<24} {d['state'][p]}")
            for _q, p, l in spec["readonly"]:
                if p in d.get("readonly", {}):
                    print(f"  {p:<4}      {l:<24} {d['readonly'][p]}   (set by hand)")
            print(f"  mode                               {mode_name(spec, d.get('mode'))}")
        elif cmd == "diff":
            if len(args.names) != 2:
                raise SystemExit("usage: radio-profile RADIO diff A B")
            a, b = (load_profile(radio, n) for n in args.names)
            fake_cur = {"menus": a["menus"], "state": a["state"], "mode": a.get("mode"), "freq": a.get("freq")}
            changes = plan(fake_cur, b, spec, set())
            print(f"{args.names[0]} → {args.names[1]}: {len(changes)} difference(s)")
            for c in changes:
                print(describe(c, spec, lab) + ("   (never written)" if c[1] in skip and c[0] == "menu" else ""))
            for _q, p, l in spec["readonly"]:
                va, vb = a.get("readonly", {}).get(p), b.get("readonly", {}).get(p)
                if va is not None and vb is not None and va != vb:
                    print(f"  {p:<4}        {l:<24} {va:>6}  →  {vb}   (set by hand on the radio)")
        elif cmd in ("apply", "voice", "data"):
            name = args.names[0] if cmd == "apply" else cmd
            if cmd == "apply" and not args.names:
                raise SystemExit("usage: radio-profile RADIO apply NAME")
            cmd_apply(radio, name, args, spec, skip, lab)
        elif cmd == "undo":
            snaps = sorted((pdir(radio) / "snapshots").glob("*.json"))
            if not snaps:
                raise SystemExit("no snapshot to go back to")
            d = json.loads(snaps[-1].read_text())
            print(f"going back to {snaps[-1].stem}")
            d["name"] = "undo"
            cmd_apply(radio, d, args, spec, skip, lab, snapshot=False)
            snaps[-1].rename(snaps[-1].with_suffix(".used"))
        else:
            raise SystemExit(f"unknown command '{cmd}'")
    except CatError as e:
        raise SystemExit(f"CAT: {e}")


if __name__ == "__main__":
    main()
