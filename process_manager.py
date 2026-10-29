"""
OffGrid Comms - subprocess lifecycle for rigctld, modem73, and MeshChatX.

Ordering matters (learned the hard way getting the anchor/hamshack stable):
rigctld and modem73 need to be up and listening BEFORE MeshChatX starts, so
that when MeshChatX's embedded Reticulum instance reads the Modem73Interface
block from ~/.reticulum/config, modem73's KISS port is already there to
connect to. Use start_all() rather than starting things in your own order.

Each managed process's stdout/stderr is captured line-by-line into a bounded
deque that the UI can poll -- see LogBuffer.
"""

import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class LogBuffer:
    def __init__(self, maxlen=500):
        self._lines = deque(maxlen=maxlen)
        self._lock = threading.Lock()

        self._total = 0  # lines ever appended (the deque drops old ones)

    def append(self, line):
        with self._lock:
            self._lines.append(line.rstrip("\r\n"))
            self._total += 1

    def snapshot(self):
        with self._lock:
            return list(self._lines)

    def since(self, seen):
        """(total, lines appended after the first `seen`) -- keeps working
        after the buffer has wrapped, unlike slicing snapshot()."""
        with self._lock:
            new = self._total - seen
            lines = list(self._lines)
            return self._total, (lines[-new:] if 0 < new <= len(lines) else
                                 lines if new > 0 else [])


class SleepInhibitor:
    """Holds a systemd-logind "block" inhibitor for sleep + idle while an
    HF modem is running, so automatic suspend can't cut a transmission or
    an ARQ session off halfway. It's just a `systemd-inhibit ... sleep
    infinity` child process: releasing it (or the app dying) drops the
    inhibitor. Closing a laptop lid still suspends -- logind ignores
    inhibitors for the lid switch by default (LidSwitchIgnoreInhibited)."""

    def __init__(self):
        self._proc = None

    @property
    def active(self):
        return self._proc is not None and self._proc.poll() is None

    def acquire(self, why):
        if self.active:
            return True, "already held"
        exe = shutil.which("systemd-inhibit")
        if not exe:
            return False, "systemd-inhibit not found"
        try:
            self._proc = subprocess.Popen(
                [exe, "--what=sleep:idle", "--who=OffGrid Comms", f"--why={why}",
                 "--mode=block", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            return False, str(e)
        time.sleep(0.3)
        if self._proc.poll() is not None:
            err = (self._proc.stderr.read() or "").strip()
            self._proc = None
            return False, err or "systemd-inhibit exited"
        return True, "held"

    def release(self):
        if self.active:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class ManagedProcess:
    """Wraps a subprocess.Popen with a background line-reader thread."""

    def __init__(self, name, cmd, env=None, cwd=None):
        self.name = name
        self.cmd = cmd
        self.env = env
        self.cwd = cwd
        self.log = LogBuffer()
        self._proc = None
        self._reader = None

    @property
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        if self.is_running:
            return True
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as e:
            self.log.append(f"[offgrid-comms] failed to start {self.name}: {e}")
            return False
        self.log.append(f"[offgrid-comms] started {self.name}: {' '.join(self.cmd)}")
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._reader.start()
        return True

    def _read_output(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            self.log.append(line)
        self.log.append(f"[offgrid-comms] {self.name} exited (code {proc.poll()})")

    def stop(self, timeout=5.0):
        if not self.is_running:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2.0)
        self.log.append(f"[offgrid-comms] stopped {self.name}")


class OffGridStack:
    """Owns rnsd, rigctld, modem73, and MeshChatX for one radio/mode combination."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.rnsd = None
        self.rigctld = None
        self.rigctld_radio = None
        self.modem73 = None
        self.meshchatx = None
        self.mercury = None
        self.connector = None

    def _venv_bin(self, name):
        vb = Path(self.cfg["venv_bin"])
        candidate = vb / name
        return str(candidate) if candidate.exists() else name

    def start_rnsd(self):
        """rnsd is the base Reticulum transport layer everything else here
        shares via Reticulum's shared-instance mechanism (MeshChatX, the
        Anytone KISS interface, modem73's own interface all connect to
        whichever process is hosting it). Tied to the app's own lifetime --
        started once at app startup, stopped once at quit -- independent of
        the HF Start All/Stop All toggle, since stopping it would drop every
        interface at once, not just the HF stack. Deliberately NOT a
        systemd service: previously ran as an untracked `rnsd &` background
        job that could silently die with nothing to restart it (caught via
        the Network Status panel's health light); this keeps it visible and
        tied to the app instead of adding another independent service to
        keep track of."""
        if self.rnsd and self.rnsd.is_running:
            return True
        cmd = [self._venv_bin("rnsd")]
        self.rnsd = ManagedProcess("rnsd", cmd)
        ok = self.rnsd.start()
        time.sleep(1.0)  # give it a moment to bind the shared instance before anything else starts
        return ok

    def stop_rnsd(self):
        if self.rnsd:
            self.rnsd.stop()

    def start_rigctld(self, radio_name):
        radio = self.cfg["radios"][radio_name]
        cmd = [
            "rigctld",
            "-m", str(radio["hamlib_model"]),
            "-r", radio["device"],
            "-s", str(radio["baud"]),
            "-t", str(radio["rigctld_port"]),
        ]
        cmd += radio.get("rigctld_extra_args", [])
        self.rigctld = ManagedProcess(f"rigctld ({radio_name})", cmd)
        self.rigctld_radio = radio_name
        ok = self.rigctld.start()
        # give rigctld a moment to bind before modem73 tries to connect
        time.sleep(1.0)
        return ok

    def start_modem73(self, radio_name, mode_name):
        m73 = self.cfg["modem73"]
        radio = self.cfg["radios"][radio_name]
        mode = self.cfg["modes"][mode_name]

        cmd = [
            self._venv_bin(m73["binary"]),
            "--headless",
            "-c", self.cfg["callsign"],
            "--rigctl", f"127.0.0.1:{radio['rigctld_port']}",
            "-p", str(m73["kiss_port"]),
            "--control-port", str(m73["control_port"]),
        ]
        if radio.get("audio_device"):
            cmd += ["-d", radio["audio_device"]]

        if mode.get("modem_type") == 2:
            cmd += ["--modem", "robust", "--robust-mode", _robust_mode_name(mode["robust_mode"])]
        else:
            cmd += ["-m", mode.get("modulation", "QPSK"), "-r", mode.get("code_rate", "1/2")]

        self.modem73 = ManagedProcess("modem73", cmd)
        ok = self.modem73.start()
        time.sleep(1.0)
        return ok

    def start_meshchatx(self):
        mc = self.cfg["meshchatx"]
        cmd = [
            self._venv_bin("meshchatx"),
            "--headless",
            "--host", mc["host"],
            "--port", str(mc["port"]),
            "--storage-dir", mc["storage_dir"],
        ]
        self.meshchatx = ManagedProcess("meshchatx", cmd)
        return self.meshchatx.start()

    def start_all(self, radio_name, mode_name):
        if self.mercury_running:
            return False  # the app asks to stop Mercury before calling this
        if self.rigctld and self.rigctld.is_running:
            self.rigctld.stop()
        if not self.start_rigctld(radio_name):
            return False
        if not self.start_modem73(radio_name, mode_name):
            return False
        return self.start_meshchatx()

    def stop_meshchatx(self):
        """Stop MeshChatX alone -- leaves rigctld/modem73 untouched. Safe to
        call whether or not the HF stack is running: MeshChatX is just a
        client of whatever interfaces rnsd already has open (LAN, the
        anchor TCP link, the Anytone KISS TNC), so it doesn't depend on
        modem73/rigctld being up at all."""
        if self.meshchatx:
            self.meshchatx.stop()

    def stop_modem73_and_rigctld(self):
        """Used when switching radios: MeshChatX stays up, only the
        modem73/rigctld pair needs to bounce."""
        if self.modem73:
            self.modem73.stop()
        if self.rigctld and not self.mercury_running:
            self.rigctld.stop()

    def stop_all(self):
        """Stops the Reticulum HF stack. rigctld is left alone while
        Mercury is using it."""
        if self.meshchatx:
            self.meshchatx.stop()
        if self.modem73:
            self.modem73.stop()
        if self.rigctld and not self.mercury_running:
            self.rigctld.stop()

    # -- Mercury HF modem + mercury-connector (HF mail to/from home) ------
    #
    # Mirrors the hamshack's mercury.service:
    #   mercury -G -i plughw:2,0 -o plughw:2,0 -p 8300 -R 2 -A 127.0.0.1:4532
    # -R 2 is Hamlib's NET rigctl model, so Mercury keys the radio through
    # the same rigctld the rest of the app uses. Mercury and modem73 both
    # need the radio's audio and PTT, so the app never runs them together.

    @property
    def modem73_running(self):
        return bool(self.modem73 and self.modem73.is_running)

    @property
    def mercury_running(self):
        return bool(self.mercury and self.mercury.is_running)

    @property
    def connector_running(self):
        return bool(self.connector and self.connector.is_running)

    def _find_bin(self, configured, fallbacks):
        for cand in [configured] + list(fallbacks):
            if not cand:
                continue
            p = Path(cand).expanduser()
            if p.is_absolute() and p.exists():
                return str(p)
            found = shutil.which(cand)
            if found:
                return found
        return configured

    def mercury_audio_device(self, radio_name):
        radio = self.cfg["radios"].get(radio_name, {})
        return radio.get("mercury_audio") or self.cfg["mercury"]["audio_device"]

    def mercury_cmd(self, radio_name):
        mc = self.cfg["mercury"]
        radio = self.cfg["radios"][radio_name]
        audio = self.mercury_audio_device(radio_name)
        cmd = [self._find_bin(mc["binary"], ["~/mercury/mercury", "/usr/bin/mercury"]),
               "-i", audio, "-o", audio,
               "-p", str(mc["base_port"]),
               "-R", "2", "-A", f"127.0.0.1:{radio['rigctld_port']}"]
        return cmd + list(mc.get("extra_args", []))

    def connector_cmd(self):
        mc = self.cfg["mercury"]
        out_dir = str(Path(mc["outgoing_dir"]).expanduser()).rstrip("/") + "/"
        in_dir = str(Path(mc["incoming_dir"]).expanduser()).rstrip("/") + "/"
        cmd = [self._find_bin(mc["connector_binary"], ["~/mercury-connector/mercury-connector"]),
               "-x", "mercury", "-i", out_dir, "-o", in_dir,
               "-c", mc["mycall"], "-d", ",".join(mc["peers"]),
               "-a", "127.0.0.1", "-p", str(mc["base_port"])]
        if mc.get("retry_wait"):
            cmd += ["-w", str(mc["retry_wait"])]
        return cmd

    @staticmethod
    def port_open(port, host="127.0.0.1", timeout=0.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def start_mercury(self, radio_name, log=print):
        """rigctld (reused if it's already up for this radio) -> Mercury ->
        wait for Mercury's control port -> mercury-connector. Returns
        (ok, message)."""
        mc = self.cfg["mercury"]
        if not mc.get("mycall") or not mc.get("peers"):
            return False, "set mercury.mycall and mercury.peers in config.json first"
        if self.modem73_running:
            return False, "modem73 is running -- stop the HF stack first"
        if self.mercury_running:
            return True, "Mercury already running"
        port = int(mc["base_port"])
        if self.port_open(port):
            return False, (f"something is already listening on port {port} "
                           "(Pat's Mercury transport, or a mercury systemd service?)")

        for d in (mc["outgoing_dir"], mc["incoming_dir"]):
            Path(d).expanduser().mkdir(parents=True, exist_ok=True)

        if not (self.rigctld and self.rigctld.is_running and self.rigctld_radio == radio_name):
            if self.rigctld:
                self.rigctld.stop()
            if not self.start_rigctld(radio_name):
                return False, "rigctld failed to start"

        workdir = Path(mc.get("workdir") or "~/.config/offgrid-comms/mercury").expanduser()
        workdir.mkdir(parents=True, exist_ok=True)  # mercury.ini lives here if you want one
        self.mercury = ManagedProcess("mercury", self.mercury_cmd(radio_name), cwd=str(workdir))
        if not self.mercury.start():
            return False, "Mercury failed to start"

        deadline = time.time() + float(mc.get("startup_timeout", 20))
        while time.time() < deadline:
            if not self.mercury.is_running:
                return False, "Mercury exited during startup -- check the audio device"
            if self.port_open(port):
                break
            time.sleep(0.5)
        else:
            return False, f"Mercury never opened port {port}"

        self.connector = ManagedProcess("mercury-connector", self.connector_cmd())
        if not self.connector.start():
            return False, "mercury-connector failed to start"
        return True, f"Mercury up as {mc['mycall']}, mail for {', '.join(mc['peers'])}"

    def stop_mercury(self):
        if self.connector:
            self.connector.stop()
        if self.mercury:
            self.mercury.stop()
        if self.rigctld and not self.modem73_running:
            self.rigctld.stop()

    def switch_radio(self, radio_name, mode_name):
        """Stop modem73+rigctld, start them again against the new radio.
        MeshChatX is left running -- its Modem73Interface will show a brief
        reconnect in rnstatus, same self-healing behavior we've already seen
        elsewhere, not a real outage."""
        self.stop_modem73_and_rigctld()
        if not self.start_rigctld(radio_name):
            return False
        return self.start_modem73(radio_name, mode_name)


_ROBUST_NAMES = {
    0: "RDM-1200", 1: "RDM-600", 2: "RDM-300", 3: "RDMN-300", 4: "RDMN-150",
    5: "RDM-1200S", 6: "RDM-600S", 7: "RDM-300S", 8: "RDMN-300S", 9: "RDMN-150S",
    10: "RDM-800", 11: "RDM-800S",
}


def _robust_mode_name(index):
    return _ROBUST_NAMES.get(index, "RDM-600")
