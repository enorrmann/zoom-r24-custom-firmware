# 01 — Overview

## The task

Analyse a firmware image, determine its architecture, and produce a toolchain
capable of (a) reproducing it and (b) generating a personalised installable —
while keeping data and code clearly separated and documenting every finding.

## The target

| | |
|---|---|
| File | `R24SYS.BIN` |
| Device | Zoom R24 multitrack recorder |
| Version | 1.13 |
| Date | 2019-05-10 (build), released July 2019 |
| Size | 7,169,652 bytes |
| sha256 | `71c7e3c5715aef27a6e2eebf8eafa090ae607ce5ddccb5ea08ca1eaccf81dc93` |
| Integrity | header checksum `0x3197CC47` — verified |

Supplied alongside: `R24_Firmware_Update_Guide_E.pdf` (install procedure) and
`R24_System Software Version History_ver113_en.pdf` (1.00 → 1.13).

---

## Results

### Architecture — detected and proven

```
ISA             ARM, ARMv4T / ARMv5TE class
Endianness      little
States          ARM (startup, veneers) + Thumb-1 (96.4 % of payload)
Excluded        Thumb-2, VFP/NEON, big-endian, ELF/PE container
Load address    0x02080000  (payload begins at file offset 0x40)
Entry point     0x0208002C  (reached via the veneer at 0x02080000)
Toolchain       arm-none-eabi (GNU Arm Embedded) for analysis
```

### Container — fully modelled

```
[0x00 .. 0x40)   64-byte header, structured, checksummed
[0x40 .. EOF)    flat, position-dependent, pre-linked image at VA 0x02080000
```

- **No compression** — every candidate stream was actually decompressed; none
  expanded. Code is readable at its final addresses.
- **No encryption** — entropy 6–7 bit/byte through the code body.
- **No relocations** — absolute addresses baked into literal pools.
- **Checksum** — `header[0x34] = sum(payload bytes) mod 2³²`, matched exactly.
  It is an additive byte sum, not a CRC: it detects accidents, not tampering.

### Code vs. data — derived, not assumed

```
reset/copy code           0x02080000 – 0x02080574
code + interleaved data   0x02080574 – 0x0226E910   (copied to RAM 0x80000000, runs there)
erased (0xFF), not loaded 0x0226E910 – 0x022A0000   (202,480 bytes)
PCM sample bank           0x022A0000 – 0x02756634   (copied to RAM 0x87540000)
```

Code and data are **interleaved at word granularity** (literal pools follow
functions immediately). This is the defining constraint on any modification and
the reason the toolchain only ever performs equal-length patches.

### What the firmware is

- **Chip:** TI **DM644x** (JTAGID `0x1B70002F`), **ARM926EJ-S + C64x+ DSP** (memory map:
  PSC `0x01C41000`, DSP L2 `0x11800000`, L1D `0x11F04000`).
- **DSP program:** **in this file** — 14 sections (115 KiB, file
  `0x1C83E0..0x1E47A7`) the ARM copies to the DSP at boot; the DSP runs TI
  **DSP/BIOS** (its strings are in those sections). See `17_dsp_and_dump.md`.
- **Application:** 61 `A:\` FAT path templates, project containers shared with
  the Zoom R16/D120 families, USB-OTG host + mass-storage stack, SD-card
  recording, and a full audio-effect vocabulary in rodata.

### Two version fields — a documented trap

`1.13` exists **twice**: in the header (what tooling reads) and in a UI string
table (what the LCD shows). Changing one does not change the other.
`r24tool` tracks both and updates them together.

---

## Toolchain

```
tools/r24image.py        format library — the only module that knows the layout
tools/r24analyze.py      architecture detection + segmentation + fingerprints
tools/r24tool            CLI: info verify unpack pack analyze patch build
tools/test_roundtrip.py  executable specification; 18 checks
specs/examples/          declarative customisation specs (JSON, reviewable)
```

### Reproduction is proved, not claimed

```console
$ ./tools/test_roundtrip.py
  [PASS] rebuilt file is byte-identical  71c7e3c5...f81dc93
  ...
ALL CHECKS PASSED
```

Unpack → repack yields the same sha256 as the original. The format model is
exact, not approximate.

### Personalised installable

```console
$ ./tools/r24tool build specs/examples/version-bump.json -o build/
$ ./tools/r24tool verify build/R24SYS.BIN
```

Customisation is expressed as **data** (a JSON spec), guarded by preconditions:

- `expected` bytes must match or the build aborts — no silent corruption across
  revisions;
- replacement must be **equal length** — no shifted literal pools;
- ambiguous pattern matches are **refused**;
- the checksum is recomputed and the output re-verified before it is written;
- `build.json` records source hash, output hash and every change.

Two ready-made artefacts are included (see `docs/03_toolchain.md` §4).

---

## Reading order

| If you want… | Read |
|---|---|
| the findings and their evidence | `02_analysis.md` |
| to use the tools / build an image | `03_toolchain.md` |
| the strict data/code split with offsets | `04_data_vs_code.md` |
| to know what is *not* proven | `05_limitations.md` |

---

## Honest summary

**Solid:** architecture, container format, checksum, segmentation, the OS
identity, and byte-exact reproduction.

**Not delivered:** a source-code rebuild — the firmware is stripped and
position-dependent; no decompiler is included and none would reproduce the
binary. **Not yet edited:** the DSP program — it is in this file
(`17_dsp_and_dump.md`) but reading it needs TI's C6000 tools.

Practically, this package lets you safely personalise **data** — version
strings, filenames, UI text, and sparse table space — and re-seal it into an
installable image, with every change traceable and reversible. It does not let
you modify code, and `05_limitations.md` explains exactly why that boundary is
where it is.
