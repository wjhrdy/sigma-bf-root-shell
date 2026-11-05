#!/usr/bin/env python3
"""Talk to the camera's CDC-ACM shell (created by /media/root/SIGMA/a.sh).

One-shot:
  acm_sh.py /dev/cu.usbmodem4202 "id"

Interactive (stdin -> port, port -> stdout) when no command is given.
"""

import os
import select
import sys
import termios
import time
import tty


def open_port(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    # raw, 8N1, no flow control, 115200
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag = 0
    oflag = 0
    lflag = 0
    cflag = (cflag & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)) | termios.CS8 | termios.CREAD | termios.CLOCAL
    cflag &= ~(termios.CRTSCTS | termios.CCTS_OFLOW | termios.CRTS_IFLOW)
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
    return fd


def drain(fd, wait):
    end = time.time() + wait
    out = b""
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: acm_sh.py <port> [command]")
        return 2
    port = sys.argv[1]
    fd = open_port(port)
    try:
        drain(fd, 0.5)
        if len(sys.argv) > 2:
            cmd = sys.argv[2]
            os.write(fd, (cmd + "\n").encode())
            out = drain(fd, 4.0)
            sys.stdout.write(out.decode("utf-8", "replace"))
            return 0
        # interactive
        print("[connected to %s - Ctrl-] to quit]" % port)
        while True:
            r, _, _ = select.select([fd, sys.stdin], [], [], 2.0)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            if sys.stdin in r:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                os.write(fd, line)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
