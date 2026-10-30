"""
OffGrid Comms - is this machine's Mercury the same version as home's?

Different Mercury versions hear each other's calls but don't connect
(1.9.13 and 1.9.21 don't), so a portable that's out of step with home
can't send mail. Better to find out at home, before a trip.

  this machine   `mercury -V` on the binary OffGrid runs (1.9.21 and newer
                 answer it; an older build prints its usage instead)
  home           "mercury_version" in home's published status file
                 (mercury.home_status_url in config.json, e.g. the
                 hf-contact.json your home station's website serves)

No internet (the normal state in the field), no URL set, or anything
unexpected: no comparison, never an error.
"""
import json
import os
import re
import subprocess
import urllib.request

VERSION_RE = re.compile(r"Mercury Version (\d{1,3}\.\d{1,3}\.\d{1,3})")
PLAIN_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")


def binary_version(path):
    """("ok", "1.9.21") / ("old", None) / ("missing", None)."""
    if not path or not os.path.isfile(os.path.expanduser(path)):
        return "missing", None
    try:
        r = subprocess.run([os.path.expanduser(path), "-V"], capture_output=True, text=True,
                           timeout=6, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "missing", None
    m = VERSION_RE.search((r.stdout or "") + (r.stderr or ""))
    return ("ok", m.group(1)) if m else ("old", None)


def home_version(url, timeout=6):
    """Home's published Mercury version, or None (offline, not set, not published)."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "offgrid-comms"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read(65536).decode("utf-8"))
    except Exception:
        return None
    v = doc.get("mercury_version") if isinstance(doc, dict) else None
    return v if isinstance(v, str) and PLAIN_RE.fullmatch(v) else None


def summarize(local, home, home_name="home"):
    """(light, subtitle, mismatch) for the Systems tab row."""
    state, mine = local
    if state == "missing":
        return "gray", "Mercury not found", False
    here = mine if state == "ok" else "older than 1.9.21 (doesn't report a version)"
    if not home:
        return ("green" if state == "ok" else "gray"), f"{here} · {home_name} unknown (offline?)", False
    if state == "ok" and mine == home:
        return "green", f"{mine} · {home_name} {home} ✓", False
    return "orange", (f"{here} · {home_name} runs {home} -- update before you go: "
                      f"different versions don't connect"), True
