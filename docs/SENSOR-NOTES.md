# Sensor notes (why the image cannot be mirrored in hardware)

The root shell was originally built to test a sensor-side image flip on the
IMX410. The result is negative; this note records the evidence so it does not
have to be re-derived.

## Sensor write primitive

```
sub_4D3223D90(u16 reg, u8 value):
    frame = { 0x81, reg >> 8, reg & 0xFF, value }   // Sony SPI write
    DD_SPI_Set_Slave_Select(0)
    DD_SPI_Set_Send_Data(0, frame, 4)
    DD_SPI_Start_Send(0); DD_SPI_Stop(0)
    // returns 0 on success
```

`display_mmio.py sensor <reg> <val>` calls it from a payload and returns the
SPI status.

Proof the path works: `0x0600 = 0x01` turns the live image strongly green;
`0x0600 = 0x00` restores normal colour.

The IMX410 SPI path is **write-only**: `ImagerSPI` has open/ctrl/send/start/
stop/select but no receive call. The firmware cannot read a register back, so it
cannot persist sensor state; all sensor writes are volatile by construction.

## `0x0101` is not the IMX410 orientation register

`0x0101` is the image-orientation register on IMX477/IMX219. On this camera:

- writing `0x01`, `0x02` or `0x03` returns SPI success and produces no visible
  change, in live view or after playback→shoot;
- there is no `movz #0x101` and no `0x0101` literal anywhere in the firmware;
- the camera's own mode table (below) has **no register in `0x0100-0x01FF`**;
- the fp firmware's independent 161-register IMX410 table also has none;
- external IMX410 research (SLVS-EC protocol decode, fp/BF reverse engineering)
  documents mode, gain, shutter and ROI registers but no orientation register.

## The complete BF sensor mode table

```
sub_4D3225580(mode, arg):
    x22 = *(0x4D3225628);            // live @ 0x4D3C9F710
    x21 = x22 + 0x166 + mode*0xB3;   // one byte of value per register
    for i in 0..2:   write(x22[i], x21[i]);
    write(0x0016, x21[3] + (arg>>1));
    for i in 4..0xB2: write(x22[i], x21[i]);
```

- register list: `0x4D3C9F710`, 179 × u16 little-endian
- values: `0x4D3C9F876`, stride `0xB3` (179 bytes) per mode

```
0001 0003 0005 0016 001c 0042 0043 0058 0062 0067 0068 006a 006f 0070
0073 007e 007f 0081 0083 0084 0085 0086 0089 008a 008c 0094 00c5 00c6
00ca 00e9 0240 0241 0242 0244 0246 0247 0248 0249 024e 024f 0250 0251
0277 0278 0279 027b 0403 041b 0429 042a 042d 0430 0433 0434 0435 0436
0437 0438 0439 043a 043b 043c 043d 043e 0441 0442 0444 044d 0453 0482
0484 0487 0489 048a 048b 04d0 04d4 04db 04dc 04de 04df 04e0 04e1 04e2
04e4 04e6 04f1 0505 050a 050d 050f 0511 0516 0517 0518 0519 0520 0521
0522 0523 0524 0525 052e 052f 0530 0531 0532 0569 056c 0571 0575 0576
0577 0578 057a 0583 0584 0585 0590 0591 0592 0593 0594 0595 059a 059b
059c 059d 05d8 0607 06c6 06d0 06d3 06d6 06d9 0704 0705 0706 0708 070f
0710 0716 0717 071e 0720 0721 0722 0723 0724 0725 0726 0727 0728 0729
072a 072b 072c 0734 0735 0736 0737 0738 0739 073a 078d 0790 0792 0793
0794 0795 0796 0797 0798 079a 079c 079e 079f 08b6 08b7
```

Direct writes outside the table add only PLL and gain registers:
`0x0006-0x000A` (4-bit PLL fields), `0x003D-0x0040` (analog gain),
`0x05E1/0x05E2` (conversion gain), plus a few init values.

## Why a reversed readout window is not possible

- Horizontal reduction uses **step registers**: `0x0716`, `0x0717` (2 for 1:1,
  4 for ÷2), `0x071E`, `0x0720`, `0x0722-0x0727`.
- Vertical size is a count: `2 × (0x072A | 0x072B<<8)` — 2021 → 4042 lines at
  1:1, 1008 → 2016 at ÷2.
- No register tracks width, height, half-width or half-height across the 70
  known modes, and there is no direction/inversion bit.

The sensor runs with a fixed origin. A reversed window is not expressible, so
there is no start/end pair to swap.

## What this means

The display controller's only reverse control is
`IM_DISP_Set_OSD_Reverse_Display`, which rotates/reverses the OSD (layer 4) and
button mapping (layer 8), not the image plane. The image plane has no reverse
field, and the ISP has no flip/rotation block. With no sensor mirror, an
in-hardware mirrored live view is not available on this camera.
