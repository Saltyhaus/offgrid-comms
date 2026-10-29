"""
OffGrid Comms - HF mail files for mercury-connector.

Messages use the same plain header + body format as the hamshack's
hf_sync_service.py (both ends read and write it):

    type: report
    callsign: AG7JC-3
    category: status
    to: AG7JC-2            (optional: home relays it to that station)
    lat: 32.7157           (optional)
    lon: -117.1611         (optional)

    All well, heading to camp 2.

A body of exactly "WX <lat> <lon>" or "WXM <lat> <lon>" gets a land or
marine forecast sent back by home.

Outgoing files go in <outgoing>/<PEER>/, written under a hidden temp
name and renamed into place so the connector never reads a half-written
file. Anything in there gets transmitted the next time the connector
reaches that station.
"""

import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path

CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,10}(-[0-9]{1,2})?$")


def normalize_callsign(call):
    c = (call or "").strip().upper()
    if not CALLSIGN_RE.match(c):
        raise ValueError(f"invalid callsign {call!r}")
    return c


def build_message(sender, body, category="status", to=None, lat=None, lon=None):
    lines = ["type: report", f"callsign: {normalize_callsign(sender)}"]
    if category:
        lines.append(f"category: {category}")
    if to:
        lines.append(f"to: {normalize_callsign(to)}")
    if lat is not None and lon is not None:
        lines.append(f"lat: {lat:.4f}")
        lines.append(f"lon: {lon:.4f}")
    body = " ".join(body.split()) if "\n" not in body else body.strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


def parse_message(text):
    if "\n\n" not in text:
        return {"body": text.strip()}
    head, body = text.split("\n\n", 1)
    out = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    out["body"] = body.strip()
    return out


def queue_message(outgoing_dir, peer, content):
    """Write one message into <outgoing>/<PEER>/. Returns the path."""
    d = Path(outgoing_dir).expanduser() / normalize_callsign(peer)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 0
    while True:
        name = f"msg_{stamp}{'_' + str(n) if n else ''}.txt"
        if not (d / name).exists():
            break
        n += 1
    tmp = d / f".{name}.tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, d / name)
    return d / name


def _visible_files(d):
    try:
        return [p for p in Path(d).iterdir() if p.is_file() and not p.name.startswith(".")]
    except OSError:
        return []


def queued(outgoing_dir, peers):
    """{peer: [paths]} of messages waiting to go out. Files in the root
    of the outgoing dir go to the first peer, like the connector does."""
    root = Path(outgoing_dir).expanduser()
    result = {}
    for i, peer in enumerate(peers):
        files = _visible_files(root / peer)
        if i == 0:
            files += _visible_files(root)
        result[peer] = sorted(files)
    return result


def inbox(incoming_dir, limit=30):
    """Newest-first list of received messages as dicts (adds 'file', 'time')."""
    files = _visible_files(Path(incoming_dir).expanduser())
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        msg = parse_message(text)
        msg["file"] = p.name
        msg["time"] = datetime.fromtimestamp(p.stat().st_mtime)
        out.append(msg)
    return out


# -- sent copies, so the panel can show both sides like a chat ------------
#
# The connector deletes outbox files once they're delivered, so a copy of
# each queued message is kept in <sent_dir> under the same name. While the
# outbox still has that name it's "queued"; once it's gone it's "sent", and
# the copy's mtime is bumped to the moment we noticed (≈ delivery time).

def save_sent_copy(sent_dir, outbox_path, content):
    d = Path(sent_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    name = Path(outbox_path).name
    tmp = d / f".{name}.tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, d / name)
    return d / name


def delete_sent_copies(sent_dir, names):
    d = Path(sent_dir).expanduser()
    for n in names:
        try:
            (d / n).unlink()
        except OSError:
            pass


def mark_delivered(sent_dir, name):
    p = Path(sent_dir).expanduser() / name
    try:
        os.utime(p, None)
        return True
    except OSError:
        return False


def sent_messages(sent_dir, queued_names, limit=40):
    files = _visible_files(Path(sent_dir).expanduser())
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        try:
            msg = parse_message(p.read_text(errors="replace"))
        except OSError:
            continue
        msg["file"] = p.name
        msg["time"] = datetime.fromtimestamp(p.stat().st_mtime)
        msg["direction"] = "out"
        msg["status"] = "queued" if p.name in queued_names else "sent"
        out.append(msg)
    return out


def conversation(incoming_dir, sent_dir, queued_names, limit=40):
    """Received and sent messages together, oldest first (chat order)."""
    rx = inbox(incoming_dir, limit)
    for m in rx:
        m["direction"] = "in"
    both = rx + sent_messages(sent_dir, queued_names, limit)
    both.sort(key=lambda m: m["time"])
    return both[-limit:]


def format_chat_line(msg, home="AG7JC"):
    t = msg["time"].strftime("%m-%d %H:%M")
    body = msg.get("body", "")
    if msg.get("direction") == "out":
        dest = msg.get("to") or home
        via = f" via {home}" if msg.get("to") else ""
        mark = "✓ sent" if msg.get("status") == "sent" else "⏳ queued"
        return f"{body}\n{t}  me → {dest}{via}  {mark}"
    sender = msg.get("callsign", "?")
    if msg.get("type") == "reply":
        sender = "HOME"
    via = f" via {msg['relayed_by']}" if msg.get("relayed_by") else ""
    return f"{sender}{via}: {body}\n{t}"


def format_inbox_line(msg):
    t = msg["time"].strftime("%m-%d %H:%M")
    sender = msg.get("callsign", "?")
    if msg.get("type") == "reply":
        sender = "HOME"          # replies carry the recipient as callsign
    via = f" via {msg['relayed_by']}" if msg.get("relayed_by") else ""
    return f"{t}  {sender}{via}: {msg.get('body', '')}"


def gps_fix(host="127.0.0.1", port=2947, timeout=6.0):
    """(lat, lon) from gpsd, or None. Plain socket, no gps module needed."""
    try:
        s = socket.create_connection((host, port), timeout=2)
    except OSError:
        return None
    try:
        s.sendall(b'?WATCH={"enable":true,"json":true};\n')
        s.settimeout(1.0)
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("class") == "TPV" and r.get("mode", 0) >= 2 and "lat" in r and "lon" in r:
                    return float(r["lat"]), float(r["lon"])
    finally:
        s.close()
    return None


# connector stderr lines -> panel link state
_LINK_PATTERNS = [
    (re.compile(r"TNC: CONNECTED (\S+) (\S+)"), lambda m: ("connected", f"CONNECTED {m.group(1)} ↔ {m.group(2)}")),
    (re.compile(r"CONNECTING\.\.\. CONNECT \S+ (\S+)"), lambda m: ("calling", f"CALLING {m.group(1)}")),
    (re.compile(r"CONNECTING\.\.\."), lambda m: ("calling", "CALLING")),
    (re.compile(r"TNC: DISCONNECTED"), lambda m: ("idle", "IDLE")),
    (re.compile(r"Call to (\S+) failed, retrying in (\d+) s"), lambda m: ("failed", f"NO ANSWER {m.group(1)} -- retry {m.group(2)} s")),
    (re.compile(r"Session with (\S+) ended"), lambda m: ("idle", "IDLE")),
    (re.compile(r"Connection to TNC failure"), lambda m: ("failed", "LOST MERCURY")),
    # from Mercury's own log: another station calling us
    (re.compile(r"Incoming connection from (\S+) on"), lambda m: ("calling", f"INCOMING CALL from {m.group(1)}")),
    (re.compile(r"Incoming connection cancelled"), lambda m: ("idle", "IDLE -- listening")),
]


def link_state_from_line(line):
    for rx, fn in _LINK_PATTERNS:
        m = rx.search(line)
        if m:
            return fn(m)
    return None


# connector / Mercury lines -> live activity for the panel lights
_ACTIVITY_PATTERNS = [
    (re.compile(r"^PTT ON\b"), lambda m: ("ptt", True)),
    (re.compile(r"^PTT OFF\b"), lambda m: ("ptt", False)),
    (re.compile(r"^SN (-?\d+(?:\.\d+)?)"), lambda m: ("snr", float(m.group(1)))),
    (re.compile(r"^BITRATE \(\d+\) (\d+) BPS"), lambda m: ("bps", int(m.group(1)))),
    (re.compile(r"^Incoming message (\S+)"), lambda m: ("rx_msg", m.group(1))),
    (re.compile(r"Messages successfully sent"), lambda m: ("tx_done", True)),
]


def activity_from_line(line):
    s = line.strip()
    for rx, fn in _ACTIVITY_PATTERNS:
        m = rx.search(s)
        if m:
            return fn(m)
    return None
