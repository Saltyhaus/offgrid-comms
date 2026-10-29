"""
OffGrid Comms - client for modem73's JSON control port (default 127.0.0.1:8073)

Wire format per CONTROL_PORT.md: 4-byte big-endian length prefix + JSON payload,
in both directions.

The control port broadcasts unsolicited events ("rx_frame", "config_changed")
to every connected client, interleaved with command responses on the same
stream. modem73's docs don't show the literal JSON envelope for events, so
this client classifies incoming frames heuristically:

  - a frame with a top-level "seq" key and no "cmd"/"ok" key is treated as an
    rx_frame event (its fields match the rx_frame_history entry shape)
  - anything else that isn't a recognizable command response is passed to the
    generic on_event callback as-is, so you can inspect real traffic and
    tighten this up once you're running against actual hardware

Command/response correlation: this app only ever has one outstanding
synchronous request at a time (see send_command), so a single pending-response
queue is enough -- we don't need per-request IDs.
"""

import json
import socket
import struct
import threading
import queue
import time


class ModemControlClient:
    def __init__(self, host="127.0.0.1", port=8073, on_event=None, on_disconnect=None):
        self.host = host
        self.port = port
        self.on_event = on_event or (lambda frame: None)
        self.on_disconnect = on_disconnect or (lambda: None)

        self._sock = None
        self._reader_thread = None
        self._send_lock = threading.Lock()
        self._response_q = queue.Queue()
        self._connected = threading.Event()
        self._stop = threading.Event()

    @property
    def connected(self):
        return self._connected.is_set()

    def connect(self, timeout=3.0):
        self._stop.clear()
        try:
            sock = socket.create_connection((self.host, self.port), timeout=timeout)
        except OSError:
            return False
        sock.settimeout(None)
        self._sock = sock
        self._connected.set()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        return True

    def close(self):
        self._stop.set()
        self._connected.clear()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _read_loop(self):
        buf = b""
        sock = self._sock
        try:
            while not self._stop.is_set():
                header = self._recv_exact(sock, 4)
                if header is None:
                    break
                (length,) = struct.unpack(">I", header)
                payload = self._recv_exact(sock, length)
                if payload is None:
                    break
                try:
                    frame = json.loads(payload.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._dispatch(frame)
        finally:
            self._connected.clear()
            self.on_disconnect()

    @staticmethod
    def _recv_exact(sock, n):
        chunks = bytearray()
        while len(chunks) < n:
            try:
                chunk = sock.recv(n - len(chunks))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    def _dispatch(self, frame):
        looks_like_event = (
            isinstance(frame, dict)
            and "seq" in frame
            and "cmd" not in frame
            and "ok" not in frame
        )
        if looks_like_event:
            self.on_event(frame)
        else:
            self._response_q.put(frame)

    def send_command(self, payload, timeout=3.0):
        """Send a request and wait for the next non-event response.

        Returns the parsed response dict, or None on timeout/disconnect.
        """
        if not self.connected:
            return None
        body = json.dumps(payload).encode("utf-8")
        header = struct.pack(">I", len(body))
        with self._send_lock:
            # drain any stale queued responses from a previous timed-out call
            while not self._response_q.empty():
                try:
                    self._response_q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._sock.sendall(header + body)
            except OSError:
                return None
            try:
                return self._response_q.get(timeout=timeout)
            except queue.Empty:
                return None

    # -- convenience wrappers over the documented commands ------------------

    def get_status(self):
        return self.send_command({"cmd": "get_status"})

    def get_config(self):
        return self.send_command({"cmd": "get_config"})

    def set_config(self, **fields):
        return self.send_command({"cmd": "set_config", **fields})

    def rigctl(self, command):
        """Passthrough a raw rigctld command string, e.g. 'F 14074000' or 'f'."""
        return self.send_command({"cmd": "rigctl", "command": command})

    def get_freq_hz(self):
        resp = self.rigctl("f")
        if not resp or not resp.get("ok"):
            return None
        try:
            return int(resp["response"].strip())
        except (KeyError, ValueError, AttributeError):
            return None

    def set_freq_hz(self, hz):
        resp = self.rigctl(f"F {int(hz)}")
        return bool(resp and resp.get("ok"))

    def send_beacon(self):
        return self.send_command({"cmd": "send_beacon"})

    def rx_frame_history(self, limit=64, since_seq=0):
        return self.send_command(
            {"cmd": "rx_frame_history", "limit": limit, "since_seq": since_seq}
        )


def connect_with_retry(host, port, on_event, on_disconnect, stop_event, retry_s=2.0):
    """Blocking helper for a background thread: keeps trying to (re)connect
    until stop_event is set. Returns a connected ModemControlClient, or None
    if stop_event fired first.
    """
    client = ModemControlClient(host, port, on_event=on_event, on_disconnect=on_disconnect)
    while not stop_event.is_set():
        if client.connect():
            return client
        time.sleep(retry_s)
    return None
