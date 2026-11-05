# Decrypting BF firmware without a key

The SIGMA BF update package is obfuscated with a byte-wise additive stream, not
a block cipher and not a signature. The stream can be regenerated from the file
itself, so no secret is required.

Verified against BF 1.03 and BF 1.04: both decode to the same bytes as the
previously recovered trees, all 17 segments, byte-for-byte.

> This repository contains **no firmware files and no decrypted images**.
> `tools/decode_bf.py` operates on a package you obtained yourself. Decrypted
> output is Sigma code; keep it local and do not commit it.

## Container layout

| offset | type | meaning |
|---|---|---|
| `0x00` | 12 bytes | `SIGMA bf SOF` (the parser also accepts `SIGMA mi SOF`) |
| `0x0c` | u32 | xorshift32 seed, stored in clear (0 means 1) |
| `0x18` | u16 | segment count (17 on 1.03/1.04) |
| `0x100` | 16 bytes × count | segment table |
| `0x300` | | first segment data |

Segment table entry (`<HHIII`):

```
index u16   kind u16   offset u32   size u32   trailer u32
```

- `trailer & 0xFFFFFF == 0`
- the top byte of `trailer` is chosen so that
  `sum(encrypted_segment) + (trailer >> 24) ≡ 0 (mod 256)`

That checksum is not a MAC or a signature.

## The cipher

For every segment, independently:

```
P[i] = (C[i] - K[i]) mod 256
```

`K` restarts at index 0 for **each** segment (it is not a global file stream).

## Recovering K[0:32] from known plaintext

The first 32 bytes of segment 2 are fixed across 1.03/1.04:

```
4d 49 4c 42 45 41 55 54   "MILBEAUT"
00 00 00 00 00 50 01 00   u32 0, u32 0x00015000
e0 03 1f aa               mov x0, xzr
e1 03 1f aa               mov x1, xzr
e2 03 1f aa               mov x2, xzr
e3 03 1f aa               mov x3, xzr
```

so:

```
K[0:32] = (segment2_cipher[0:32] - known_plain[0:32]) mod 256
```

## Extending the stream (the recurrence)

The byte stream obeys the fixed linear recurrence over GF(2)^8:

```
K[i] = K[i-8]  ^ K[i-9]  ^ K[i-12] ^ K[i-13] ^ K[i-15]
       ^ K[i-21] ^ K[i-22] ^ K[i-24] ^ K[i-25] ^ K[i-32]
```

for `i >= 32`.

Because the recurrence is linear over GF(2), it has the doubling property

```
K[i] = XOR over t in {8,9,12,13,15,21,22,24,25,32} of K[i - t * 2^m]
```

for any `m`, which lets a block be computed from an earlier block at a larger
stride. `tools/decode_bf.py` uses this to extend the stream in large steps
instead of one byte at a time.

## The vendor implementation (header seed)

The firmware parser (`0x4d3157c00` on 1.04) implements the same stream as a
32-bit xorshift seeded from offset `0x0c`, reset per segment:

```c
uint32_t x = seed ? seed : 1;
for (i = 0; i < len; i++) {
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 15;
    P[i] = (C[i] - (x & 0xFF)) & 0xFF;
}
```

Known seeds: BF 1.03 `0x690ab9fb`, BF 1.04 `0x6985374a`. The low bytes of this
xorshift reproduce the 32-byte prefix recovered from known plaintext exactly,
which is why either path works. The tool prints both and compares them.

## Verification built into the tool

After decryption:

- segment 2 starts with `MILBEAUT`
- segment 8 starts with `Logo\x01\x00\x00\x00`
- segment 0 starts with `0x000100`
- all segment checksums pass before decryption

## Usage

```sh
python tools/decode_bf.py /path/to/BF__V104.bin -o decoded-V104
python tools/decode_bf.py /path/to/BF__V104.bin --verify-only
```

Output is one file per segment, named `<index>_<kind>.bin`.

## Caveats

- This is the update-package obfuscation, not a signature check bypass. Earlier
  research found no cryptographic authentication in the traced parser/writer
  path, but a complete secure-boot/installation audit was not performed, and
  this repository provides **no flashing path**.
- Do not redistribute decrypted firmware or modified images.
