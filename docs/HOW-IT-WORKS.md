# How it works

BF firmware 1.04, USB `1003:c462` (normal MTP), Linux 4.19.124 on the camera.

## The chain

```
host (this tool)                BF RTOS                      BF Linux
      |                            |                             |
      | PTP 0x900a SetAdjustValue  |                             |
      | offset = addr - 0x400d10c00|                             |
      |--------------------------->| memcpy([0x4d7301a00]+off)   |
      |                            |   no bounds check           |
      |                            |                             |
      | PTP 0x900a, offset =       |                             |
      |   0x4d729bd00 - base       |                             |
      | data = payload address     |                             |
      |--------------------------->| [0x4d729bd00] := payload    |
      |                            |                             |
      |      every 0x900a is followed by a calibration save     |
      |                            | save calls [0x4d729bd00]    |
      |                            | -> payload:                 |
      |                            |    restore callback         |
      |                            |    send IPC selector 1      |
      |                            |---------------------------->| usbipcu
      |                            |                             | system(cmd)
```

1. **Unbounded write.** `0x900a SetAdjustValue` copies host data to
   `[0x4d7301a00] + offset`. The base global points at `0x400d10c00`; there is
   no length or range check. `0x4d7000000+` is RWX, so a payload can live there.
2. **Trigger.** The storage write callback slot `0x4d729bd00` normally holds
   `0x4d31254f0`. Writing the payload address there and issuing one more
   `0x900a` makes the calibration save call the payload.
3. **Payload.** It restores `0x4d31254f0`, sends one IPC message through
   `0x4d31dd0f0`, and returns 0 so the save completes normally.
4. **Linux.** The message is `{u16 tid, u16 selector=1, u32 0, char cmd[]}`.
   The Linux `usbipcu` service reads selector 1 and passes the string to
   `system()`.

## Live addresses (firmware 1.04)

| item | value |
|---|---|
| base global | `0x4d7301a00` → `0x400d10c00` |
| read-buffer cache slot | `0x4d72c2230` → heap buffer (`0x447579000` at last check; moves per boot) |
| storage write callback slot | `0x4d729bd00` |
| original callback | `0x4d31254f0` |
| RTOS→Linux IPC sender | `0x4d31dd0f0` |
| payload offset in buffer | `0x8000` |
| PTP ops | `0x900a` write, `0x900b` read |

The read buffer is discovered at runtime from `0x4d72c2230`; do not hardcode it.

## Why the output comes back as a file name

MTP `GetObject` fails for files the server did not index (the server opens the
path, then hands the fd to a Sigma kernel ioctl). Object **names** are served,
so the command wraps itself like this:

```sh
( sleep 10;
  o=$( { CMD ; } 2>&1 | hexdump -v -n 100 -e '1/1 "%02x"');
  : > /media/root/SIGMA/bo_<tag>${o}.txt ) &
```

The host polls `GetObjectHandles`/`GetObjectInfo` until a `bo_<tag>...` name
appears and decodes the hex from it.

The `sleep 10` keeps Linux-side SD-card I/O out of the calibration-save window.

## Why the cooldown

Every `0x900a` ends with a 64 KiB calibration block save. Rapid repeated saves
can stall the RTOS (screen and USB dead) until a power cycle. The tools do one
payload write and one trigger per command, verify the payload landed before
triggering, and refuse to rewrite the payload in a retry loop.
