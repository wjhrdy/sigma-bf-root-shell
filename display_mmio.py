#!/usr/bin/env python3
"""Read/write RTOS MMIO from the RTOS via the storage-callback payload.

The Linux side cannot touch display registers (/dev/mem is EPERM, no fb/drm),
so the access is done by a small payload that runs in the RTOS context during
the calibration save:

  mmio_read : restore callback; ldr w1,[addr]; str w1,[scratch]; return 0
  mmio_write: restore callback; str w1,[addr]; return 0

The scratch word lands in the RAM read buffer at buf+0x8000+0x400 and is read
back with 0x900b.

One trigger per operation, 10 s cooldown, health check, no write retries.

Usage:
  display_mmio.py read 0x16482690 [more addrs...]
  display_mmio.py write 0x16482690 0x2
"""

import json
import struct
import sys
import time

import usb.core
import usb.util

import build_live_payload as blp
import live_stage as ls

COOLDOWN = 10.0


class Mmio:
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
        self.dev.write(self.out_ep.bEndpointAddress,
                       ls.CONTAINER.pack(16, ls.TYPE_COMMAND, ls.CODE_OPEN_SESSION, 0x11)
                       + struct.pack("<I", 1), timeout=8000)
        self._drain()
        st, data = ls.ptp_read(self.dev, self.out_ep, self.in_ep, ls.CACHE_SLOT, 8, 0x12)
        self.buf = ls.qword(data)
        if not self.buf:
            raise SystemExit("read buffer not allocated")
        self.last = 0.0

    def _drain(self):
        try:
            while True:
                ls.read_container(self.dev, self.in_ep, timeout=200)
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

    def health(self, tid=0x51):
        try:
            self.dev.write(self.out_ep.bEndpointAddress,
                           ls.CONTAINER.pack(12, ls.TYPE_COMMAND, 0x1001, tid), timeout=5000)
            for _ in range(4):
                raw = ls.read_container(self.dev, self.in_ep, timeout=5000)
                _, ctype, _, _ = ls.CONTAINER.unpack_from(raw, 0)
                if ctype == ls.TYPE_RESPONSE:
                    return True
            return False
        except Exception:
            return False

    def slot(self):
        st, d = ls.ptp_read(self.dev, self.out_ep, self.in_ep, ls.CALLBACK_SLOT, 8, 0x52)
        return ls.qword(d)

    def _trigger(self, mode, **kw):
        if self.last == 0.0:
            time.sleep(15.0)         # let any save from a previous run drain
        wait = COOLDOWN - (time.time() - self.last)
        if self.last and wait > 0:
            time.sleep(wait)
        if not self.health():
            raise SystemExit("device unhealthy")
        if self.slot() != ls.ORIGINAL_CALLBACK:
            raise SystemExit("callback slot is not the original value")
        addr = self.buf + ls.PAYLOAD_OFF
        blob = bytes.fromhex(blp.build(mode, addr, sender=ls.SENDER, **kw)["blob_hex"])
        st = ls.ptp_write(self.dev, self.out_ep, self.in_ep, addr, blob, 0x53)
        if st != 0x2001:
            raise SystemExit("payload write rejected: %s" % hex(st))
        back = None
        for attempt in range(2):
            for _ in range(20):
                time.sleep(0.5)
                st, back = ls.ptp_read(self.dev, self.out_ep, self.in_ep, addr, len(blob), 0x54)
                if back == blob:
                    break
            if back == blob:
                break
            time.sleep(5.0)
            ls.ptp_write(self.dev, self.out_ep, self.in_ep, addr, blob, 0x53)
        if back != blob:
            print("DEBUG back_len=%s blob_len=%s back=%s" % (
                len(back) if back else None, len(blob),
                back.hex()[:120] if back else None))
            raise SystemExit("payload did not land; not triggering")
        self.last = time.time()
        st = ls.ptp_write(self.dev, self.out_ep, self.in_ep, ls.CALLBACK_SLOT,
                          struct.pack("<Q", addr), 0x55)
        if st != 0x2001:
            raise SystemExit("trigger rejected: %s" % hex(st))
        time.sleep(2.5)
        if self.slot() != ls.ORIGINAL_CALLBACK:
            raise SystemExit("callback not restored after payload")

    def read32(self, addr):
        self._trigger("mmio_read", args_addr=addr)
        dest = self.buf + ls.PAYLOAD_OFF + 0x400
        st, d = ls.ptp_read(self.dev, self.out_ep, self.in_ep, dest, 4, 0x56)
        if not d or len(d) < 4:
            return None
        return struct.unpack_from("<I", d, 0)[0]

    def write32(self, addr, value):
        self._trigger("mmio_write", args_addr=addr, value=value)

    def applypatch(self):
        self._trigger("setter_patch")

    def revertpatch(self):
        self._trigger("setter_revert")

    def sensorwrite(self, reg, value):
        self._trigger("sensor_write", args_addr=reg, value=value)
        dest = self.buf + ls.PAYLOAD_OFF + 0x400
        st, d = ls.ptp_read(self.dev, self.out_ep, self.in_ep, dest, 4, 0x58)
        return struct.unpack_from("<I", d, 0)[0] if d and len(d) >= 4 else None

    def nopcall(self):
        self._trigger("caller_nop")

    def restorecall(self):
        self._trigger("caller_restore")

    def wr32(self, addr, value):
        self._trigger("mmio_wr", args_addr=addr, value=value)
        dest = self.buf + ls.PAYLOAD_OFF + 0x400
        st, d = ls.ptp_read(self.dev, self.out_ep, self.in_ep, dest, 4, 0x57)
        return struct.unpack_from("<I", d, 0)[0] if d and len(d) >= 4 else None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    op = sys.argv[1]
    m = Mmio()
    try:
        if op == "read":
            out = {}
            for a in sys.argv[2:]:
                addr = int(a, 16)
                val = m.read32(addr)
                out[hex(addr)] = hex(val) if val is not None else None
            print(json.dumps(out, indent=2))
        elif op == "sensor":
            reg = int(sys.argv[2], 16)
            val = int(sys.argv[3], 16)
            status = m.sensorwrite(reg, val)
            print(json.dumps({"sensor_reg": hex(reg), "wrote": hex(val),
                              "spi_result": hex(status) if status is not None else None}))
        elif op == "nopcall":
            m.nopcall()
            print(json.dumps({"nop": "0x4d321dc54", "call": "bl 0x4d30854f0"}))
        elif op == "restorecall":
            m.restorecall()
            print(json.dumps({"restored": "0x4d321dc54"}))
        elif op == "revert":
            m.revertpatch()
            print(json.dumps({"reverted": True}))
        elif op == "patch":
            m.applypatch()
            print(json.dumps({"patched": True,
                              "setter": "0x4d30854f0",
                              "layers": "4 and 8 now always write 0x101"}))
        elif op == "wr":
            addr = int(sys.argv[2], 16)
            value = int(sys.argv[3], 16)
            back = m.wr32(addr, value)
            print(json.dumps({"addr": hex(addr), "wrote": hex(value),
                              "readback": hex(back) if back is not None else None}))
        elif op == "write":
            addr = int(sys.argv[2], 16)
            value = int(sys.argv[3], 16)
            before = m.read32(addr) if len(sys.argv) > 4 and sys.argv[4] == "readfirst" else None
            if before is None:
                time.sleep(COOLDOWN)
            m.write32(addr, value)
            print(json.dumps({"addr": hex(addr), "wrote": hex(value),
                              "before": hex(before) if before is not None else None}))
        else:
            print("unknown op")
            return 2
    finally:
        m.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
