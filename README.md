# sigma-bf-root-shell

RAM-only root shell for the **SIGMA BF** (firmware 1.04) over USB.

No flashing, no memory card, no soldering. The camera enumerates as its normal
MTP device, a small payload runs in the RTOS, and commands execute as `root` in
the camera's Linux side.

Verified on hardware: `uid=0(root) gid=0(root)`, Linux 4.19.124, aarch64.

```
$ python bf_shell.py -c "id; uname -a"
uid=0(root) gid=0(root)
Linux mlbel 4.19.124 #189 SMP PREEMPT ... aarch64 GNU/Linux
```

> **Firmware 1.04 only.** The addresses below are live values from 1.04 and
> differ in other builds. Do not run this against a different firmware.

---

## 1. Requirements

- SIGMA BF, firmware 1.04
- macOS or Linux, Python 3.9+
- `pyusb`
- USB-C data cable
- Camera awake, **not recording**

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Put the camera in MTP mode

This is the step people get wrong. The BF does **not** use MTP by default.

| USB mode | Host sees | Usable for this tool |
|---|---|---|
| Stream Video / UVC | `1003:c464` | no |
| Mass Storage | USB disk | no |
| **Normal MTP** | `1003:c462` (MTP interface, `mtp-server` running) | **yes** |

Normal MTP is reachable from the **stock USB connection chooser** with a hidden
gesture. It is a firmware quirk, not a menu setting.

### 2.1 Get the chooser to appear

Unplug the camera and plug it into the computer again. On the camera screen a
USB connection page appears:

- **Access Camera** — "Operate int. storage from an ext. device."
- **Stream Video** — "Stream video to an external device."

Choose **Access Camera**. The next page has two choices:

- **Mass Storage**
- **Cancel**

Mass Storage is highlighted by default. **Do not confirm it.**

> Do not start the gesture from an active UVC session. If the camera is already
> streaming video over USB, unplug/replug first. The MTP branch is explicitly
> blocked when the effective connection is UVC.

### 2.2 Hidden MTP gesture

On the **Access Camera** page, with **Mass Storage** highlighted:

1. Put one finger on an empty part of the LCD, drag a little, and **keep the
   finger down**. Do not lift it.
2. Without lifting, turn the rear dial so the highlight moves:

   ```
   Cancel  ->  Mass Storage  ->  Cancel
   ```

   That is **three separate movements**. Pause and let each highlight appear
   before reversing direction. Do not spin fast; multiple detents in one event
   count only once.
3. **Lift the finger.**
4. Confirm the highlighted **Cancel** with the normal confirm control, as a
   **separate action** after the last dial movement.

If the hidden counter reached 3, the firmware selects normal MTP instead of
cancelling. The chooser screen closes and the USB device re-enumerates.

### 2.3 Verify

macOS:

```sh
ioreg -p IOUSB -l -w0 | grep -i "SIGMA C46"
```

Linux:

```sh
lsusb | grep 1003:c462
```

Expected: `1003:c462`. Then confirm the tool sees it:

```sh
python bf_shell.py --probe
# {"ok": true, "output": "probe-ok\r\n", ...}
```

If `--probe` says "device not found", the camera is not in MTP mode. If it
enumerates as `1003:c464`, you are in UVC mode.

### 2.4 Notes and caveats

- The chooser labels and the dial polarity were traced from the stock firmware
  and the sequence was used successfully, but wording can vary by UI language.
  If the page or choices do not match the description, **stop** and do not
  improvise.
- Entering MTP changes the camera's USB mode setting; it is not a
  settings-neutral operation. The camera may remember the last USB mode across
  shutdowns. To return to UVC, reconnect and pick the ordinary
  **Stream Video** option without the gesture.
- Plugging the camera in again later may go straight to the last-used mode.
  Unplug/replug until the chooser appears.

---

## 3. Use the shell

One command:

```sh
python bf_shell.py -c "id; uname -a"
```

Interactive (one calibration save per command, ~10 s apart):

```sh
python bf_shell.py
bf> ls /media/root
bf> ps | head -n 5
bf> exit
```

Check / recover:

```sh
python bf_shell.py --probe     # channel self-test
python bf_shell.py --restore   # put the storage callback back
python bf_shell.py --clean     # remove leftover output files
```

### Limits

- Command text: **140 characters**
- Output: **100 bytes**
- Output comes back as a hex string inside an MTP object *name*
  (`bo_<tag><hex>.txt` in `/media/root/SIGMA`), because MTP `GetObject` does not
  serve files created this way.
- One command per ~10 s. The tool enforces the cooldown.

---

## 4. Safety

The method is RAM-only, but it is **not** a no-flash operation:

- Every `0x900a` write triggers an unconditional 64 KiB calibration block save.
  The block's contents are unchanged (we never target it), but flash is written.
- Several saves in quick succession can wedge the RTOS: screen and USB stop
  responding. Recovery is a power cycle.
- So: **one payload write, one trigger, no retries, ~10 s apart.** The tools do
  this for you.

The callback slot is verified before every write. If it is not the recorded
original `0x4d31254f0`, the tool refuses to run.

| Situation | Action |
|---|---|
| Normal end of command | nothing; callback is restored automatically |
| Callback left pointing at the payload | `python bf_shell.py --restore` |
| Shell stops responding | power-cycle the camera |
| Camera unresponsive after rapid triggers | power-cycle the camera |

Do not use this while recording, and do not write to the calibration base or
the base global.

---

## 5. Optional: persistent interactive shell (CDC-ACM)

Once you have the one-shot shell, you can add a USB serial function to the MTP
gadget and get a normal interactive root shell for the rest of the power cycle.
**This costs the MTP channel until the next power cycle.**

Write this to the card as `/media/root/SIGMA/a.sh` (over MTP, or with a few
`bf_shell.py -c "echo ... >> ..."` commands):

```sh
#!/bin/sh
cd /sys/kernel/config/usb_gadget/mtp
U=$(cat UDC)
modprobe usb_f_acm
mkdir -p functions/acm.gs0
echo > UDC
sleep 1
ln -sf functions/acm.gs0 configs/c.1/acm.gs0
echo "$U" > UDC
sleep 2
sh -i </dev/ttyGS0 >/dev/ttyGS0 2>&1 &
```

Run it once:

```sh
python bf_shell.py -c "sh /media/root/SIGMA/a.sh"
```

On macOS a serial device appears:

```sh
ls /dev/cu.usbmodem*
python acm_sh.py /dev/cu.usbmodem4202 "id; uname -a"
```

or `screen /dev/cu.usbmodem* 115200`.

Caveats from the verified run:

- MTP / `0x900a` does not survive the rebind; it returns after a power cycle.
- A host USB reset can kill the shell; power-cycle if that happens.
- `mtp-server` inherits `/dev/ttyGS0` if launched from that shell; redirect it.

Untested improved recipe (keeps MTP alive and respawns the shell):

```sh
killall mtp-server
cd /sys/kernel/config/usb_gadget/mtp
U=$(cat UDC)
echo > UDC ; sleep 1
ln -sf functions/acm.gs0 configs/c.1/acm.gs0
echo "$U" > UDC ; sleep 2
mtp-server </dev/null >/dev/null 2>&1 &
sh -c 'while :; do sh -i </dev/ttyGS0 >/dev/ttyGS0 2>&1; sleep 1; done' \
    </dev/null >/dev/null 2>&1 &
```

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `device not found` | camera is not in normal MTP; repeat §2 |
| Enumerates as `1003:c464` | UVC mode; unplug/replug, choose Access Camera + gesture |
| `read buffer not allocated` | run `--probe` once after MTP enumeration |
| `callback slot is ... not ...` | previous run left the slot dirty; `--restore`, then power-cycle if needed |
| `payload write rejected` | USB/RTOS busy; wait, run `--probe`, retry once |
| `no output object appeared` | command crashed or wrote nothing; try a simpler command |
| Camera freezes / USB dies | rapid saves; power-cycle and respect the 10 s cooldown |

---

## 7. Files

| file | purpose |
|---|---|
| `bf_shell.py` | one-shot and interactive root command channel |
| `live_stage.py` | low-level payload stages (`scratch`, `marker`, `ipc`), `--restore` |
| `build_live_payload.py` | payload builder |
| `acm_sh.py` | serial client for the optional CDC-ACM shell |
| `display_mmio.py` | extra: RTOS MMIO read/write and IMX410 sensor register writes |
| `docs/HOW-IT-WORKS.md` | the full chain and live addresses |
| `docs/SENSOR-NOTES.md` | what the sensor access was used for (and why mirroring is not possible) |

---

## 8. Scope

This is a research tool for your own camera. It runs code in the camera's RTOS
through a stock vendor command. It does not modify firmware, does not change
persistent camera settings, and does not survive a reboot except for the USB
mode change described in §2.4.
