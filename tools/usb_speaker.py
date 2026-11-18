#!/usr/bin/env python3
"""usb_speaker.py: play a board's speech from its USB cable on this computer (a board with no
speaker of its own, such as the XIAO ESP32S3 Sense before an amplifier is fitted).

    python tools/usb_speaker.py [/dev/ttyACM0] [--all]

The board sends "SPK!" + a 4-byte length + 16-bit mono 24 kHz samples; everything else on the
cable is its console log. Only status lines are printed (Wi-Fi, state changes, camera, errors):
the console also carries what she says and what her photos show, which stay out of the system
journal. --all prints every line. Exits when the cable goes away; the service restarts it.
Needs pyserial and PipeWire's pw-cat.
"""
import re
import struct
import subprocess
import sys

import serial

# Only the board's own log lines ("I (123) Tag: ..."), and of those only short status ones: the
# console also carries her replies and photo descriptions (JSON, free text), which never pass.
LOG_LINE = re.compile(rb"^[IW] \(\d+\) [A-Za-z0-9_]+: ")
STATUS = re.compile(rb"Got IP|State: |photo \d+x\d+: kept|exposure: target|camera: ")
ERROR = re.compile(rb"^E \(\d+\) ")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
everything = "--all" in sys.argv
port = args[0] if args else "/dev/ttyACM0"


def show(lines: bytes) -> None:
    for line in lines.splitlines(keepends=True):
        text = line.rstrip(b"\r\n")
        if everything or ERROR.match(text) or (LOG_LINE.match(text) and STATUS.search(text) and len(text) < 220):
            sys.stdout.write(line.decode(errors="replace"))
    sys.stdout.flush()


def main() -> int:
    s = serial.Serial(port, 115200, timeout=0.05)
    player = subprocess.Popen(["pw-cat", "--playback", "--rate", "24000", "--channels", "1", "--format", "s16",
                               "--latency", "60ms", "--raw", "-"], stdin=subprocess.PIPE)
    print(f"[speaker] playing the board's speech from {port}", flush=True)
    buf = b""
    while True:
        buf += s.read(8192)
        while True:
            i = buf.find(b"SPK!")
            if i < 0:
                cut = buf.rfind(b"\n")                   # plain console text: whole lines
                if cut >= 0:
                    show(buf[:cut + 1])
                    buf = buf[cut + 1:]
                buf = buf[-4096:]
                break
            if i:
                show(buf[:i])
            if len(buf) < i + 8:
                buf = buf[i:]
                break
            n = struct.unpack("<I", buf[i + 4:i + 8])[0]
            if n > 32768 or n % 2:
                buf = buf[i + 4:]
                continue
            if len(buf) < i + 8 + n:
                buf = buf[i:]
                break
            player.stdin.write(buf[i + 8:i + 8 + n])
            player.stdin.flush()
            buf = buf[i + 8 + n:]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except serial.SerialException as e:
        print(f"[speaker] no board on {port} ({e}); waiting", flush=True)
        sys.exit(1)
