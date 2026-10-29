#!/usr/bin/env python3
"""
levelcheck -- measure an HF audio path with a clean test tone.

One station sends a 1000 Hz tone for a few seconds; the other records what
its sound card actually receives and reports:
  * peak level / clipping   -> is the receive audio too hot (ADC clipping)?
  * channel SNR             -> tone vs. the noise heard before the tone
  * 2nd / 3rd harmonics     -> distortion (overdriven TX audio, ALC, clipping)

Nothing but Python + alsa-utils (arecord/aplay) + rigctl. The sound card
must be free: stop Mercury first (OffGrid Comms: Stop Mercury; hamshack:
shack-mode apps).

  receive side (start this first, it records 12 s):
      python3 levelcheck.py rx --device plughw:CARD=Device,DEV=0

  transmit side (within ~3 s of starting rx):
      python3 levelcheck.py tx --device plughw:CARD=CODEC,DEV=0 \\
          --rig "-m 2 -r 127.0.0.1:4532"
      KX3 without rigctld running:
          --rig "-m 2045 -r /dev/serial/by-id/usb-Silicon_Labs_CP2102N_...-port0 -s 38400"

  --level sets the tone level in dBFS (default -12), --channels 2 sends it
  on both channels (needed for the FT-991A, which listens on the right).
"""
import argparse
import array
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
import wave

RATE = 48000
TONE = 1000.0
# "quality" (tone vs. everything else) below this = no tone at all: pure
# noise or hum measures around -33 dB, even a tone buried in noise ~0 dB
NO_TONE_DB = -15.0


def rigctl(rig, *cmd):
    return subprocess.run(["rigctl", *shlex.split(rig), *cmd],
                          capture_output=True, text=True, timeout=10)


def make_tone_wav(seconds, level=-12.0, channels=2, side="both"):
    """Write a 1000 Hz test tone to a temp .wav; returns its path."""
    n = int(RATE * seconds)
    amp = 32767 * 10 ** (level / 20)
    fade = int(RATE * 0.02)
    samples = array.array("h")
    for i in range(n):
        env = min(1.0, i / fade, (n - 1 - i) / fade)
        v = int(amp * env * math.sin(2 * math.pi * TONE * i / RATE))
        if channels == 1:
            samples.append(v)
        else:
            samples.extend([v if side in ("both", "left") else 0,
                            v if side in ("both", "right") else 0])
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    with wave.open(path, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return path


def send_tone(device, rig, seconds=5, level=-12.0, channels=2, side="both",
              stop_event=None, log=print):
    """Key the radio, play the tone, always unkey. Returns (ok, message).
    stop_event (threading.Event) ends the tone early."""
    path = make_tone_wav(seconds, level, channels, side)
    try:
        r = rigctl(rig, "T", "1")
        if r.returncode != 0:
            return False, f"couldn't key the radio: {r.stderr.strip() or r.stdout.strip()}"
        log(f"PTT on -- sending {TONE:.0f} Hz at {level:.0f} dBFS for {seconds:.0f} s "
            f"({side} channel{'s' if side == 'both' else ''})")
        try:
            time.sleep(0.3)
            p = subprocess.Popen(["aplay", "-q", "-D", device, path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            while p.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    p.terminate()
                    break
                time.sleep(0.1)
            err = p.stderr.read().strip() if p.stderr else ""
            if p.returncode not in (0, None, -15) and err:
                return False, f"aplay failed: {err}"
        finally:
            rigctl(rig, "T", "0")
            log("PTT off")
        return True, "tone sent"
    finally:
        os.unlink(path)


def cmd_tx(a):
    ok, msg = send_tone(a.device, a.rig, a.seconds, a.level, a.channels, a.side)
    if not ok:
        print(f"!! {msg}"); sys.exit(1)


HANN_N = RATE // 5          # 0.2 s blocks -> 5 Hz resolution
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (HANN_N - 1)) for i in range(HANN_N)]


def _block_power(b, f):
    """Mean-square of the component at f in one Hann-windowed block (Goertzel)."""
    k = 2 * math.cos(2 * math.pi * f / RATE)
    s1 = s2 = 0.0
    for v, w in zip(b, _HANN):
        s0 = v * w + k * s1 - s2
        s2, s1 = s1, s0
    mag2 = s1 * s1 + s2 * s2 - k * s1 * s2
    amp = 2 * math.sqrt(max(mag2, 0.0)) / (HANN_N * 0.5)   # Hann coherent gain 0.5
    return amp * amp / 2


def tone_power(x, f, blocks=10):
    bs = [x[i:i + HANN_N] for i in range(0, len(x) - HANN_N + 1, HANN_N)][:blocks]
    return sum(_block_power(b, f) for b in bs) / len(bs) if bs else 0.0


def db(x):
    return 10 * math.log10(x) if x > 0 else float("-inf")


def record(device, seconds):
    """Record mono 16-bit audio. Returns (samples, error)."""
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
        path = f.name
    try:
        r = subprocess.run(["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE),
                            "-c", "1", "-t", "raw", "-d", str(int(seconds)), path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None, f"arecord failed: {r.stderr.strip()} (is Mercury still holding the card?)"
        return array.array("h", open(path, "rb").read()), None
    finally:
        os.unlink(path)


def cmd_rx(a):
    print(f"recording {a.seconds:.0f} s from {a.device} -- start the tone on the other station now")
    x, err = record(a.device, a.seconds)
    if err:
        print(f"!! {err}"); sys.exit(1)
    print_report(analyze(x))


def analyze(x):
    """Measure a recording that contains a test tone. Returns a dict; see print_report."""
    if len(x) < RATE:
        return {"error": "too little audio recorded"}
    blk = RATE // 10
    blocks = [x[i:i + blk] for i in range(0, len(x) - blk + 1, blk)]
    ms = [sum(v * v for v in b) / len(b) for b in blocks]
    top = max(ms)
    if top <= 0 or db(top / 32768 ** 2) < -50:
        return {"error": "no tone heard (input below -50 dBFS) -- is the other station sending, "
                         "and is the AF gain / receive level up?"}
    # 1) Find the tone's frequency first. Loudness alone can't tell the tone
    #    from the receiver's hiss: with AGC the band noise before the tone
    #    comes out about as loud as the tone itself, and the old code then
    #    measured a slice of noise (random "348 Hz", "489 Hz"...). So sum the
    #    spectrum of several loud windows from the later part of the
    #    recording -- the tone runs to the end once it has started -- and
    #    take the strongest line: noise is spread out, the tone isn't.
    nwin = len(x) // blk - 1                                          # 0.2 s windows on the 0.1 s grid
    loud = [i for i in range(nwin) if ms[i] > top / 4]
    late = loud[len(loud) // 2:] or loud
    picks = [late[int(k * (len(late) - 1) / 3)] for k in range(4)] if len(late) >= 4 else late
    wins = [x[i * blk:i * blk + HANN_N] for i in dict.fromkeys(picks)]
    best_f, best_p = TONE, 0.0
    for f in range(200, 3600, 20):
        pw = sum(_block_power(w, f) for w in wins)
        if pw > best_p:
            best_f, best_p = f, pw
    for f in range(int(best_f) - 20, int(best_f) + 21, 1):
        pw = sum(_block_power(w, f) for w in wins)
        if pw >= best_p:
            best_f, best_p = f, pw

    # 2) The tone is where that frequency is strong (within 6 dB of its
    #    loudest), not just where the audio is loud.
    tp = [_block_power(x[i * blk:i * blk + HANN_N], best_f) for i in range(nwin)]
    tmax = max(tp)
    tone_idx = [i for i in range(nwin) if tp[i] > tmax / 4]
    steady = len(tone_idx) > 0.9 * len(blocks)                        # tone the whole time: no quiet part
    first = tone_idx[0]
    quiet_idx = [i for i in range(0, max(0, first - 2))]              # before the tone started
    tone = array.array("h")
    for i in tone_idx[2:-2] or tone_idx:                              # skip the edges
        tone.extend(blocks[i])
    peak = max(abs(v) for v in tone)
    clipped = sum(1 for v in tone if abs(v) >= 32000)
    total = sum(v * v for v in tone) / len(tone)
    best_p = tone_power(tone, best_f)
    h2 = tone_power(tone, 2 * best_f)
    h3 = tone_power(tone, 3 * best_f)
    rest = max(total - best_p - h2 - h3, total * 1e-7)              # floor: avoid "inf"

    r = {
        "error": None,
        "freq": best_f, "offset": best_f - TONE, "tone_secs": len(tone_idx) / 10,
        "peak_db": 20 * math.log10(max(peak, 1) / 32768), "clipped": clipped,
        "rms_db": db(total / 32768 ** 2),
        "noise_db": None, "snr_db": None,
        "h2_dbc": db(h2 / best_p), "h3_dbc": db(h3 / best_p),
        "clean_db": db(best_p / rest),
        "steady": steady,
    }
    if quiet_idx:
        noise = sum(ms[i] for i in quiet_idx) / len(quiet_idx)
        r["noise_db"] = db(noise / 32768 ** 2)
        r["snr_db"] = db(best_p / noise) if noise else None

    if r["clean_db"] < NO_TONE_DB:
        return {"error": "no test tone found -- only noise or hum. Is the other station sending, "
                         "on the same frequency, in DATA mode?"}

    # plain-language verdicts: (ok?, text)
    v = []
    if clipped:
        v.append((False, f"CLIPPING -- receive audio too hot ({clipped} samples). Turn the AF gain / receive level down."))
    elif r["rms_db"] > -8:
        v.append((False, "Level very hot -- turn the receive level down a little."))
    elif r["rms_db"] < -30:
        v.append((False, "Level low -- turn the receive level (AF gain / DATA OUT) up."))
    else:
        v.append((True, "Level OK."))
    if abs(r["offset"]) > 50:
        v.append((False, f"Tone {r['offset']:+.0f} Hz off -- radios not on the same dial frequency/mode "
                          "(check RIT/XIT, DATA vs. USB). Could also be hum, if you hear a buzz."))
    worst_h = max(r["h2_dbc"], r["h3_dbc"])
    if worst_h > -30 and clipped:
        v.append((False, "Distortion -- most likely from the clipping above; fix that first."))
    elif worst_h > -20:
        v.append((False, "Heavy distortion -- overdriven transmitter (ALC), RF overload or hum."))
    elif worst_h > -30:
        v.append((False, "Some distortion -- lower the TX drive (mic gain / DT GAIN) a little."))
    else:
        v.append((True, "Clean tone (low distortion)."))
    if r["clean_db"] >= 20:
        v.append((True, f"Signal quality {r['clean_db']:.0f} dB -- good."))
    else:
        v.append((False, f"Signal quality only {r['clean_db']:.0f} dB -- noise or distortion during the tone."))
    if steady:
        v.append((None, "No quiet part before the tone -- start the analysis first, then the tone."))
    r["verdicts"] = v
    return r


def print_report(r):
    if r.get("error"):
        print(f"!! {r['error']}"); return
    print()
    note = "" if abs(r["offset"]) <= 50 else f"   !! {r['offset']:+.0f} Hz from 1000"
    print(f"tone heard at        {r['freq']:.0f} Hz   ({r['tone_secs']:.1f} s of tone found){note}")
    print(f"peak level           {r['peak_db']:6.1f} dBFS"
          f"   {'!! CLIPPING (' + str(r['clipped']) + ' samples)' if r['clipped'] else '(no clipping)'}")
    print(f"tone level (RMS)     {r['rms_db']:6.1f} dBFS   (aim for about -20 to -10)")
    if r["noise_db"] is not None:
        print(f"noise before tone    {r['noise_db']:6.1f} dBFS")
    print(f"2nd / 3rd harmonic   {r['h2_dbc']:6.1f} / {r['h3_dbc']:.1f} dBc   (better than -30 is clean)")
    print(f"signal quality       {r['clean_db']:6.1f} dB   (tone vs. noise+distortion during it; 20+ is good)")
    print()
    for ok, text in r["verdicts"]:
        print(("  OK  " if ok else ("  ..  " if ok is None else "  !!  ")) + text)


def level_of(data):
    """(rms_dbfs, peak_dbfs, clipping?) of a chunk of 16-bit samples."""
    x = array.array("h", data)
    if not x:
        return -120.0, -120.0, False
    pk = max(abs(v) for v in x) or 1
    rms = math.sqrt(sum(v * v for v in x) / len(x)) or 1
    return 20 * math.log10(rms / 32768), 20 * math.log10(pk / 32768), pk >= 32000


def cmd_meter(a):
    """Live level meter: turn the AF gain while the other station sends a long tone."""
    cmd = ["arecord", "-q", "-D", a.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunk = RATE // 4 * 2                      # 0.25 s of 16-bit mono
    print("live input level -- aim for RMS about -15 dBFS while the tone is on, and never CLIP.  Ctrl-C to stop.")
    try:
        while True:
            data = p.stdout.read(chunk)
            if not data:
                err = p.stderr.read().decode(errors="replace").strip()
                print(f"!! arecord stopped: {err}  (is Mercury still holding the card?)")
                break
            rdb, pdb, clip = level_of(data)
            bar = "#" * max(0, int((rdb + 60) / 1.5))
            flag = "  CLIP!" if clip else ("  <- good" if -20 <= rdb <= -10 else "")
            print(f"\rRMS {rdb:6.1f}  peak {pdb:6.1f} dBFS  {bar:<40}{flag:<9}", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        p.terminate()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tx"); t.add_argument("--device", required=True); t.add_argument("--rig", required=True)
    t.add_argument("--seconds", type=float, default=5); t.add_argument("--level", type=float, default=-12)
    t.add_argument("--channels", type=int, default=2)
    t.add_argument("--side", choices=("both", "left", "right"), default="both",
                   help="which stereo channel carries the tone (find which one the interface sends to the radio)")
    r = sub.add_parser("rx"); r.add_argument("--device", required=True)
    r.add_argument("--seconds", type=float, default=12)
    m = sub.add_parser("meter"); m.add_argument("--device", required=True)
    a = ap.parse_args()
    {"tx": cmd_tx, "rx": cmd_rx, "meter": cmd_meter}[a.cmd](a)


if __name__ == "__main__":
    main()
