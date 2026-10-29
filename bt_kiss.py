r"""
OffGrid Comms - Anytone Bluetooth KISS TNC control.

Unlike the HF radios, the Anytone's KISS TNC is a hardware/firmware TNC --
it does its own AFSK framing on the radio itself, and rigctld/modem73 are
never involved. Reticulum's KISSInterface for it is already defined in
~/.reticulum/config and just needs /dev/rfcommN to exist and be bound to
the radio's Bluetooth SPP service; rnsd's own interface-reconnect logic
picks it up automatically once the device node appears, so there's no
need to restart rnsd after binding.

This radio isn't always with the laptop, so binding is on-demand (a
button in the app) rather than a boot-time systemd service -- a service
that runs once at boot has no way to retry later once you actually walk
in with the HT.

`rfcomm bind`/`release` require root. To run this without a password
prompt every time, add a sudoers rule scoped to the exact command (find
your username with `whoami`, and confirm the rfcomm path with
`which rfcomm` if it's not at /usr/bin/rfcomm):

    sudo visudo -f /etc/sudoers.d/offgrid-anytone

    <youruser> ALL=(root) NOPASSWD: /usr/bin/rfcomm bind rfcomm0 AA\:BB\:CC\:DD\:EE\:FF 2, /usr/bin/rfcomm release rfcomm0

Note the backslash-escaped colons in the MAC address -- sudoers treats
':' as a reserved character in command specs (like a comma or '='), so
a bare MAC address there is a syntax error. The escaping is a sudoers
quoting rule only; it still matches the literal unescaped MAC address
the app actually passes on the command line. Validate with
`sudo visudo -c -f /etc/sudoers.d/offgrid-anytone` (should print
"parsed OK") before trusting it.

Without that rule, connect()/disconnect() will just fail (sudo -n refuses
to prompt) -- the app will log the failure rather than hang.
"""

import shutil
import subprocess
import time
from pathlib import Path


class AnytoneKissTnc:
    def __init__(self, cfg):
        a = cfg["anytone"]
        self.mac = a["mac"]
        self.channel = a["channel"]
        self.device = a.get("device", "rfcomm0")
        self.rfcomm_bin = a.get("rfcomm_bin", "/usr/bin/rfcomm")

    @property
    def device_path(self):
        return f"/dev/{self.device}"

    def is_bound(self):
        return Path(self.device_path).exists()

    # -- adapter power --------------------------------------------------
    # The MSI has AutoEnable=false in /etc/bluetooth/main.conf, so the
    # adapter is off after boot. rfcomm bind still "succeeds" with the
    # adapter off, and rnsd then can never open the link -- so Connect
    # switches it on first. bluetoothctl and rfkill both work without root
    # for the logged-in desktop user (polkit / logind seat ACLs).

    @staticmethod
    def _run(cmd, timeout=8):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)

    def adapter_powered(self):
        """True/False, or None if bluetoothctl isn't available / no adapter."""
        if not shutil.which("bluetoothctl"):
            return None
        rc, out = self._run(["bluetoothctl", "show"])
        if rc != 0 or "Controller" not in out:
            return None
        return "Powered: yes" in out

    def ensure_powered(self):
        """Switch the Bluetooth adapter on if it's off. Returns (ok, message)."""
        state = self.adapter_powered()
        if state is True:
            return True, "Bluetooth already on"
        if state is None:
            return False, "no Bluetooth adapter found (bluetoothctl show)"
        rc, out = self._run(["bluetoothctl", "power", "on"])
        if rc != 0 or "Blocked" in out or "Failed" in out:
            # soft-blocked (airplane mode / Settings toggle): unblock, retry
            self._run(["rfkill", "unblock", "bluetooth"])
            time.sleep(1.0)
            self._run(["bluetoothctl", "power", "on"])
        for _ in range(10):
            if self.adapter_powered():
                return True, "Bluetooth switched on"
            time.sleep(0.5)
        return False, ("couldn't switch Bluetooth on -- check for a hardware switch or "
                       "airplane mode (rfkill list bluetooth)")

    def connect(self, timeout=15.0):
        """Switch Bluetooth on if needed, then bind /dev/rfcommN to the
        radio. Returns (ok, message)."""
        ok, power_msg = self.ensure_powered()
        if not ok:
            return False, power_msg
        prefix = "" if power_msg == "Bluetooth already on" else power_msg + "; "
        if self.is_bound():
            return True, f"{prefix}{self.device_path} already bound"
        try:
            result = subprocess.run(
                ["sudo", "-n", self.rfcomm_bin, "bind", self.device, self.mac, str(self.channel)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "timed out -- radio may be off or out of Bluetooth range"
        if result.returncode == 0:
            return True, f"{prefix}bound {self.device_path}"
        msg = (result.stderr or result.stdout or "rfcomm bind failed").strip()
        if "a password is required" in msg.lower() or result.returncode == 1 and not msg:
            msg += " (check the sudoers rule in bt_kiss.py's docstring)"
        return False, msg

    def reconnect(self, timeout=15.0):
        """Force a clean release + rebind. is_bound() only checks whether
        /dev/rfcommN exists, not whether the underlying Bluetooth link is
        actually alive -- if the radio dropped out of range and the OS
        never cleanly released the device node, connect() will wrongly
        report "already bound" and do nothing, while `rfcomm bind` itself
        fails with "Address already in use". This clears that stale state
        first, then binds fresh."""
        if self.is_bound():
            ok, msg = self.disconnect(timeout=min(timeout, 10.0))
            if not ok:
                return False, f"release failed: {msg}"
        return self.connect(timeout=timeout)

    def disconnect(self, timeout=10.0):
        if not self.is_bound():
            return True, f"{self.device_path} not bound"
        try:
            result = subprocess.run(
                ["sudo", "-n", self.rfcomm_bin, "release", self.device],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "timed out releasing device"
        if result.returncode == 0:
            return True, f"released {self.device_path}"
        return False, (result.stderr or result.stdout or "rfcomm release failed").strip()
