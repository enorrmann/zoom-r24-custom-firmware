# Zoom R24 firmware — analysis & reconstruction toolchain

Target: `R24SYS.BIN`, Zoom R24 system software **v1.13** (2019-05-10), 7,169,652 bytes.

Everything in this repository was derived by static analysis of the one firmware
file that shipped with it. No vendor toolchain, symbol file or source was used,
and the firmware was never executed.

> **Not in this repository:** Zoom's stock firmware and anything built from
> it. Download the R24 system software **v1.13** from zoom.co.jp, put its
> `R24SYS.BIN` at the root of this folder (sha256 check: `./tools/r24tool
> verify R24SYS.BIN`), then `python3 tools/build_firmware.py` writes the image
> to flash, `build/R24SYS.BIN`. Device dumps, recordings and sample kits are
> not included either.

## Start here

| Document | What it answers |
|---|---|
| **`python3 tools/build_firmware.py`** | **The image to flash** → `build/R24SYS.BIN`: LOOP[SD] + CUSTOM kit + FILE > KIT ASSIGN + PROJECT menu (IMPORT, CLEAR UNUSED), built from `R24SYS.BIN`. `--dump` adds the diagnostic PROJECT > DUMP MEMORY |
| [`docs/00_quickstart.md`](docs/00_quickstart.md) | **Start here for custom samples or kits**: every command on one page |
| [`docs/01_overview.md`](docs/01_overview.md) | What this package is, how to run it, results at a glance |
| [`docs/02_analysis.md`](docs/02_analysis.md) | **Every finding**: architecture, container format, code/data split, OS, hardware |
| [`docs/03_toolchain.md`](docs/03_toolchain.md) | How to use the tools, and how to build a custom installable |
| [`docs/04_data_vs_code.md`](docs/04_data_vs_code.md) | The strict data/code separation, with offsets and evidence |
| [`docs/05_limitations.md`](docs/05_limitations.md) | What is *not* proven, what is inferred, what will break |
| [`docs/06_assembly_workflow.md`](docs/06_assembly_workflow.md) | **Writing firmware**: disassembly, decompiled C, editing assembly |
| [`docs/07_pcm_samples.md`](docs/07_pcm_samples.md) | **Replacing the embedded PCM drum samples with your own WAVs** |
| [`docs/08_drum_kits.md`](docs/08_drum_kits.md) | **Drum kit definitions, voice mapping, 73-sample bank & customization** |
| [`docs/09_sample_map.md`](docs/09_sample_map.md) | **Byte position of every sample** (generated from the binary) |
| [`docs/10_loop_import.md`](docs/10_loop_import.md) | **Loop import from USB / SD**: how `LOOP[USB]` works, and the switch to the USB root or the SD card |
| [`docs/11_patterns.md`](docs/11_patterns.md) | **Rhythm patterns**: the 472-record SEQPLAY layout, and exporting/importing the note data |
| [`sdk/README.md`](sdk/README.md) | **C SDK**: write firmware patches in C (hooks, firmware API, emulator) |
| [`docs/12_custom_kit.md`](docs/12_custom_kit.md) | **CUSTOM kit**: a kit whose 16 samples are loaded from `PROJECT\<prj>\KIT\SAMPLEnn.WAV` when the project opens |
| [`docs/13_kit_assign.md`](docs/13_kit_assign.md) | **FILE > KIT ASSIGN**: put a WAV on a CUSTOM-kit pad from the R24 itself |
| [`docs/14_kit_editor.md`](docs/14_kit_editor.md) | **KIT SELECT > CUSTOM**: 16-pad editor, a file per pad from the project's files |
| [`docs/15_project_menu.md`](docs/15_project_menu.md) | **PROJECT > IMPORT SD / IMPORT USB / CLEAR UNUSED**: fill or clean the project's pool (DUMP MEMORY: doc 17) |
| [`docs/16_pattern_track.md`](docs/16_pattern_track.md) | **Optional** (`build_firmware.py sdk/patches/pattern_track.c`): rhythm pattern on ONE track (as the R8), not a track pair |
| [`docs/22_usb_midi.md`](docs/22_usb_midi.md) | **SYSTEM > USB MIDI**: MIDI clock + Start/Continue/Stop to a USB-MIDI cable (R24 as USB host) |
| [`docs/18_dump_analysis.md`](docs/18_dump_analysis.md) | **What the dump shows**: chip (DM644x), 8 MiB NOR layout, the boot loader's update rules and boot flag, **the size limit that protects the boot loader**, the live DSP (`tools/r24dump.py`) |
| [`docs/17_dsp_and_dump.md`](docs/17_dsp_and_dump.md) | **The DSP program is in this image** (how the ARM boots it), the NOR flash/boot loader, and **PROJECT > DUMP MEMORY** (flash + DSP memory to `A:\DUMP`) |

## Quick start

```bash
./tools/r24tool info     R24SYS.BIN     # header + checksum
./tools/r24tool verify   R24SYS.BIN     # validate
./tools/r24tool analyze  R24SYS.BIN     # architecture + code/data map
./tools/test_roundtrip.py               # prove byte-identical reproduction

# write firmware: disassemble -> edit -> assemble back
./tools/r24tool disasm R24SYS.BIN            # list functions (2,837 real; see note)
./tools/r24tool disasm R24SYS.BIN -a 0x0208084e -o f.s
./tools/r24tool asm    R24SYS.BIN f.s -a 0x0208084e -o build-asm/R24SYS.BIN
./tools/test_asm_edit.py                     # prove the edit loop end-to-end

# build a data-customised image
./tools/r24tool build specs/examples/version-bump.json -o build/

# replace the embedded PCM drum samples with your own WAVs
python3 tools/patch_samples.py R24SYS.BIN --extract -o build/samples
python3 tools/patch_samples.py R24SYS.BIN build/samples/samples.txt -o build/repack.BIN
python3 tools/test_patch_samples.py                  # prove the per-sample bounds rule

# build the TR-606 / TR-808 / TR-909 image (reproduces R24SYS_X0X.BIN)
python3 tools/build_x0x_firmware.py

# repack: WAVs keep their own length, the sample table is rewritten
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/R24SYS.BIN --repack

# 16 kits, each with 16 samples of its own (256 sample-table entries)
python3 tools/r24bank.py kits R24SYS.BIN --count 16 -o build/R24SYS_16KITS.BIN

# 16 own samples per kit: move the sample table, 160 entries (kit k slot s -> 16k+s)
python3 tools/r24bank.py relocate R24SYS.BIN --per-kit -o build/R24SYS_160.BIN
python3 tools/r24bank.py info build/R24SYS_160.BIN
python3 tools/test_r24bank.py                        # firmware evidence + relocation

# patch individual samples from a text list (bounds-checked, per sample)
python3 tools/patch_samples.py R24SYS.BIN specs/examples/samples.txt -o build/R24SYS.BIN
python3 tools/test_patch_samples.py                  # prove the bounds rule holds

# extract every sample, round-trip it untouched, then edit the WAVs
python3 tools/patch_samples.py R24SYS.BIN --extract -o build/samples
python3 tools/patch_samples.py R24SYS.BIN build/samples/samples.txt -o build/repack.BIN

# export the drum kits to an editable file, edit, re-import
python3 tools/r24kits.py R24SYS.BIN --export -o build/kits
python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN
python3 tools/test_r24kits.py                        # prove kits round-trip exactly

# export the 472 rhythm patterns to an editable file, edit, re-import
python3 tools/r24patterns.py R24SYS.BIN --export -o build/patterns
python3 tools/r24patterns.py R24SYS.BIN build/patterns/patterns.json -o build/R24SYS.BIN
python3 tools/r24patterns.py R24SYS.BIN --show 08Beat01
python3 tools/test_r24patterns.py                    # 306 checks

# regenerate docs/09_sample_map.md from the image
python3 tools/gen_sample_map.py R24SYS.BIN -o docs/09_sample_map.md

# inspect the loop-import mechanism (data patches only -- NOT a working
# SD import; see the "Loops" section before flashing anything)
python3 tools/loop_import.py R24SYS.BIN --list-sites
python3 tools/test_loop_import.py                                 # 32 checks
```

## Extracting and re-packing the samples

`patch_samples.py` has two modes: **extract** and **patch** (the default).

### Extract

```bash
python3 tools/patch_samples.py R24SYS.BIN --extract -o build/samples
```

writes one WAV per sample plus a ready-to-use list:

```
build/samples/sample00.wav … sample72.wav     73 files, mono 16-bit @ 44.1 kHz
build/samples/samples.txt                     <wav path> <id> <bytes>
```

Each WAV holds the PCM **verbatim at its exact stored length** — no padding —
and the third column of the list records that length for reference. Because
every file is already exactly its slot's capacity, packing them straight back
reproduces the image byte for byte:

```bash
python3 tools/patch_samples.py R24SYS.BIN build/samples/samples.txt -o build/repack.BIN
cmp R24SYS.BIN build/repack.BIN        # identical — every slot reports "exact"
```

That round-trip is asserted by `tools/test_patch_samples.py` (44 checks) for both
`R24SYS.BIN` and `R24SYS_X0X.BIN`. So the practical workflow is: extract, edit
the WAVs you care about in any editor, re-pack the same list.

The bank is 44.1 kHz: extracted WAVs are written at 44.1 kHz and input WAVs at
any other rate are resampled to it.

### Patch

Each line is a WAV path followed by the sample position, and the optional third
column is ignored:

```
# <wav path>                       <sample id>
REVERB_X0X/606/Kick1.wav           0
build/samples/sample13.wav        13        59994
```

* the path may contain spaces (`606/Hat Closed1.wav 2` — the **last** token is
  always the id);
* blank lines and `#` comments are ignored;
* a duplicate, out-of-range or non-numeric id is an **error**, not a guess.

**In place (default).** Sample `id` owns exactly
`[0x00220040 + rel_start, + length)`. A longer WAV is truncated with a 64-sample
fade, a shorter one is zero-padded, and the bytes written always equal that
capacity — so a patch can never reach into the next sample. The table is not
touched.

**`--repack`.** Each WAV keeps its own length; the bank is laid out again,
identical audio is stored once, and the table is rewritten. The only hard
limit is the bank: 4,941,364 bytes (56.0 s at 44.1 kHz) shared by all samples.
The tool stops with the overflow size if it does not fit (`--max-ms`,
`--trim-all` cut samples down).

Positions for every sample: **[`docs/09_sample_map.md`](docs/09_sample_map.md)**
(generated from the binary by `tools/gen_sample_map.py`).

## Sample table: corrected findings

Earlier revisions of this repository said the sample table was **frozen**:
positions, lengths and the number of samples fixed, because "168 literal-pool
words hold hard RAM addresses into the bank". Disassembling the code that uses
the bank shows that is **wrong**:

| Address (VA) | What it does |
|---|---|
| `0x020ADDFC` | at boot: `memcpy(0x87540000, 0x022A0000, 0x500000)` — the bank goes to RAM |
| `0x020AE016` | every hit: `start = 0x87540000 + T[id].rel_start`, `end = start + T[id].length - 2` |
| `0x020AE508` | duration: `T[id].length / 2 * 1000 / 44100` |

`T` (file `0x0E20EC`, RAM `0x800E1B38`) is reached through two literal words,
file `0x2E244` and `0x2E624`, and nothing else. Of the 1,052 words in the code
region whose value falls in the bank's address range, the code loads 18. Only
one is a real pointer (`0x022A0000`, the `memcpy` source); the rest are constants
and data. No code addresses an individual sample.

So: **positions and lengths are free** (`patch_samples.py --repack`), entries
may share audio, and **the table can move** to hold more entries
(`r24bank.py relocate`: table at the end of the bank, inside the RAM copy, both
literals repointed). Still fixed: the bank base and the file size (56.0 s of
audio in total). Proven statically and by `tools/test_r24bank.py`, and the
relocated 160-entry image was **flashed and verified on an R24** (2026-11-18). Details:
[`docs/07_pcm_samples.md`](docs/07_pcm_samples.md) §2–3.

## Editing the drum kits

`r24kits.py` exports the 10 kit descriptors to an editable file and imports
them back. Any slot can be repointed at any sample-table entry (73 factory,
160 after relocation), and its level,
pan and mute group changed:

```bash
python3 tools/r24kits.py R24SYS.BIN --export -o build/kits     # writes kits.json
python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN
cmp R24SYS.BIN build/R24SYS.BIN     # untouched export repacks byte-identically
```

One file, `kits.json`, self-documented by a `notes` block. A slot is:

```json
{ "sample_id": 56, "level": 14, "pan": 64, "group": 0, "reserved": "000000" }
```

`sample_id` is which table entry the slot plays (slot 0 of BASIC above is
the kick); `level`, `pan` and `group` are its playback parameters. Renaming a
kit is a single-value edit: set `name` to 1..15 ASCII characters (unique across
kits) and it is written as 15 chars + NUL, as every factory kit stores it.

This changes *routing*, not audio — combine it with `patch_samples.py` when you
want new sound as well. `kits.json` and
`R24SYS.BIN` round-trip byte-identically; for `R24SYS_X0X.BIN` the three
`TR-xxx` names normalise to 15 chars + NUL — 3 name bytes plus the 2 checksum
bytes that follow, **5 in total**, and nothing else.
Details: **[`docs/08_drum_kits.md`](docs/08_drum_kits.md) §7**, asserted by
`tools/test_r24kits.py`.

## Loops: why the SD import does not work yet

The sampler's `LOOP[USB]` entry reads one hard-coded folder, `B:\ZOOM_R24\LOOP`.
`tools/loop_import.py` can repoint that path with **same-length data patches** —
no instruction is touched, the image size never changes and the checksum is
recomputed.

**A `LOOP[SD]` build made that way does not work. Confirmed on hardware:** the
entry opens, the **USB indicator lights**, and no files are found.

### Why

Patching the path strings is not enough, because the entry is USB all the way
through its code, not just in its literal:

| site | what it does | in the `LOOP` lister |
|---|---|---|
| `0x42b7a` → `0x80006256` | `r0 = 0xFFFF`, i.e. bits 0-15 | **drives the USB indicator** |
| `0x42b7ce` | `0x800274B0` | waits for a **USB device**, refuses if absent |
| `0x42b8a` | `0x80027498` | aborts with *Disable Sync Rec* if the SD is busy |
| `0x42c56` | opens the path through the USB file layer | USB-only |

`0x800274B0` is a USB wait loop: it polls, and on timeout shows
*No USB Device*. On the patched image the path said `A:\` but everything else
still went to USB — which is exactly what you saw.

Replacing the path text is therefore cosmetic. A real SD import needs the
listing **and** the file I/O routed to the SD layer, i.e. **different code**,
not different data.

### SD loop import (2026-11-18)

```bash
python3 tools/loop_sd.py R24SYS.BIN --replace -o build/loop/R24SYS_LOOPSD_REPLACE.BIN
python3 tools/loop_sd.py build/R24SYS_160.BIN --add -o build/loop/R24SYS_160_LOOPSD.BIN
python3 tools/test_loop_sd.py
```

`--replace` turns `LOOP[USB]` into `LOOP[SD]` (reads `A:\ZOOM_R24\LOOP`);
`--add` keeps it and adds `LOOP[SD]` as a fifth TAKE SELECT item, with its code
in the PCM bank tail. Both untested on a device yet; details in
`docs/10_loop_import.md` §4–§6.

### Corrected analysis (2026-11-18)

`docs/10_loop_import.md` was rewritten. In short: the callback *copies* the
chosen WAV from the stick into the project's `AUDIO` folder on the SD card; only
three calls in it are USB-specific (host mode, wait for device, USB off), so an
SD import is four same-size patches (in place, replacing `LOOP[USB]`), and a
fifth TAKE SELECT item can live in the PCM bank tail. The `0xFF` region is never
loaded into RAM, so it is not usable at all.

### What is in the repo today

Only the analysis and the tooling:

```bash
python3 tools/loop_import.py R24SYS.BIN --list-sites    # every patchable site
python3 tools/test_loop_import.py                       # 32 checks, data patches only
```

`--sd-import` and `--usb-root` still exist and still produce byte-verified
images, but **neither makes the sampler read a different volume** — do not
flash them expecting an SD loop menu. The USB-root variant has the same
weakness for the same reason: the code still wants a USB device.

Two entries side by side (`LOOP[USB]` + `LOOP SDCARD`) additionally need the
menu-record count and label resolution traced; `tools/R24MenuTrace.java` is the
Ghidra script for that, and `docs/10_loop_import.md` §6 records what is already
known.


## C SDK: firmware patches in C (2026-11-19)

```bash
python3 tools/r24sdk.py build R24SYS.BIN sdk/patches/custom_kit.c mine.c -o build/R24SYS_MINE.BIN
python3 tools/r24sdk.py check build/R24SYS_MINE.BIN
python3 tools/test_r24sdk.py                          # 181 checks, patches run in the emulator
```

Rewriting the firmware in C is not practical (2,800 unnamed functions, a TI
compiler, an undocumented DSP side). Instead the original code stays and C
patches redirect the parts you want: `R24_REPLACE` (a function),
`R24_POINTER` (a callback/pointer in data), `R24_BYTES`, `R24_KIT`. The firmware
API is `sdk/include/r24fw.h`, bound to addresses checked against the image by
`sdk/firmware.sym`. Guide: [`sdk/README.md`](sdk/README.md).

## CUSTOM kit: per-project drum samples (2026-11-19)

```bash
python3 tools/build_firmware.py        # THE image to flash: build/R24SYS.BIN
                                      # = stock + LOOP[SD] + CUSTOM kit + FILE > KIT ASSIGN
                                      #   + KIT SELECT > CUSTOM editor + PROJECT > IMPORT SD/USB, CLEAR UNUSED
python3 tools/custom_kit.py prepare kick.wav snare.wav -o /media/SD/ZOOM_R24/PROJECT/PRJ001/KIT
python3 tools/test_custom_kit.py                      # runs the patched image in an ARM926 emulator
```

An SDK patch (`sdk/patches/custom_kit.c`) hooks `set_current_project`
and loads `A:\ZOOM_R24\PROJECT\<project>\KIT\SAMPLE01..16.WAV` into the CUSTOM
kit whenever a project becomes current; missing files fall back to BASIC's
sounds. The reserve is freed by capping the longest factory samples (≈1.5 s for
6 s). On the R24 itself: FILE menu → select a WAV → **KIT ASSIGN** → pad
([`docs/13_kit_assign.md`](docs/13_kit_assign.md)). Emulator-verified, untested on a device. Details:
[`docs/12_custom_kit.md`](docs/12_custom_kit.md).

## Custom image: `R24SYS_X0X.BIN`

A ready-to-install image is committed at the repository root. It adds **TR-606,
TR-808 and TR-909** kits, and every one of the 73 PCM slots is overwritten with
WAVs from `REVERB_X0X` — no factory drum audio remains reachable.

```
file      R24SYS_X0X.BIN      7,169,652 bytes (same size as the stock image)
checksum  0x1de6c91b          recomputed, verifies
sha256    3fe537c4450396e3556b4b549268eefe1dad6aaa8b48fca67257cb6b66a9b835
kits      slots 7/8/9 = TR-606 / TR-808 / TR-909 (was ACOUSTIC / TECHNO / URBAN)
```

Install: copy to the **root** of a FAT32 SD card as `R24SYS.BIN`, then follow
`R24_Firmware_Update_Guide_E.pdf`. Keep the stock `R24SYS.BIN` to flash back.

The build is deterministic — rerunning `tools/build_x0x_firmware.py` reproduces
the committed file byte for byte.

**Why this build keeps the factory lengths** (a choice, not a firmware limit).
The X0X build rewrites audio in place over the factory table so that it stays
reproducible byte for byte. Earlier revisions said the table was *frozen*
because "168 literal-pool words hold hard addresses into the bank"; that was
wrong — see **Sample table: corrected findings** below.

## Results at a glance

```
Architecture    ARMv4T / ARMv5TE-class, little-endian, ARM + Thumb-1
Load address    0x02080000 (file offset 0x40)
Container       flat binary, 64-byte header, NO compression, NO relocations
Integrity       header[0x34] = u32le sum of payload bytes  (verified exactly)
Chip            TI DM644x (JTAGID 0x1B70002F): ARM926EJ-S r0p5 + C64x+ DSP
RAM             128 MiB DDR: 28 MiB static, 63 MiB track buffers, 5 MiB PCM bank,
                5 MiB DSP; fw_malloc = 1 MiB pool; ~26 MiB with no fixed user (docs/18 §5)
Flash           8 MiB NOR: boot loader 0x02000000, image 0x02080000, boot flag
                0x027A0000; payload limit 0x720000 bytes (docs/18)
DSP program     IN this file: 14 sections, file 0x1C83E0..0x1E47A7, copied to
                the DSP at boot by 0x8006C868; the DSP runs TI DSP/BIOS
Reproduction    unpack → repack is byte-identical (sha256 match)

Disassembly     2,837 real functions (2,577 Thumb / 260 ARM); the map lists
                8,941, of which 6,104 are PCM audio misread as code
Reassembly      97.9% of code functions (1,694/1,730) and 82.9% of mapped
                code bytes rebuild bit-exact
Decompiled C    Ghidra ARM_v45.cspec — readable, but NOT recompilable

PCM samples     4.7 MiB of 16-bit LE mono drum one-shots, file 0x220040..EOF,
                copied to RAM 0x87540000 at boot; 73-entry table read at
                playback time — repackable at any length, relocatable (160)
```

## Repository layout

```
R24SYS.BIN                  original firmware (read-only reference)
R24SYS_X0X.BIN              installable image: TR-606/808/909 kits, all 73
                            samples overwritten from REVERB_X0X (see above)
tools/
  r24image.py               format library — single source of truth
  r24analyze.py             architecture detection + segmentation
  r24asm.py                 disassemble/assemble one function (byte-exact)
  r24patch.py               extract, splice, trampolines
  r24wav.py                 WAV decode/resample helpers (used by r24tool paste)
  build_x0x_firmware.py     builds R24SYS_X0X.BIN (deterministic)
  patch_samples.py          extract / patch in place / --repack samples from a list
  r24bank.py                sample table: locate, info, relocate (--per-kit = 160)
  test_r24bank.py           proves the firmware evidence and the relocation
  r24kits.py                export / re-import the drum kit table (editable)
  r24patterns.py            export / re-import the 472 rhythm patterns
  gen_sample_map.py         regenerate docs/09_sample_map.md from an image
  loop_import.py            LOOP path-string patches (inspection only; not a working SD import)
  loop_sd.py                SD loop import: --replace (in place) or --add (fifth item)
  test_loop_sd.py           proves loop_sd.py instruction by instruction
  r24sdk.py                 C SDK: build / check / syms (see sdk/README.md)
  r24emu.py                 ARM926 emulator of the image (unicorn) for tests
  test_r24sdk.py            proves the SDK, runs sdk/tests/selftest.c
  build_firmware.py         builds THE image to flash from R24SYS.BIN (LOOP[SD] + SDK patches)
  custom_kit.py             CUSTOM kit: install / check / prepare (WAVs)
  test_custom_kit.py        runs sdk/patches/custom_kit.c in the emulator
  test_kit_assign.py        runs sdk/patches/kit_assign.c (FILE > KIT ASSIGN) in the emulator
  test_kit_editor.py        runs sdk/patches/kit_editor.c (KIT SELECT > CUSTOM editor)
  test_flash_safety.py      no tool writes an image over 7,471,168 bytes, kind != 0, sum flag 0
  r24dump.py                checks a DUMP MEMORY dump: NOR layout, boot flag, flashed image, live DSP
  test_project_menu.py      runs sdk/patches/project_menu.c + mem_dump.c (PROJECT > IMPORT SD/USB, CLEAR UNUSED, DUMP MEMORY)
  test_loop_import.py       proves the loop-import patch set is exact/minimal
  test_patch_samples.py     proves the per-sample bounds rule
  test_r24kits.py           proves kit export -> re-import is byte-identical
  r24tool                   CLI entry point
  ghidra_run.sh             one-command Ghidra analysis/decompilation
  R24Analyze.java           Ghidra: function map (ARM/Thumb seeding)
  R24Decompile.java         Ghidra: decompiled C export
  validate_reasm.py         measures the reassembly round-trip
  test_roundtrip.py         container round-trip test
  test_asm_edit.py          human-edit end-to-end test
  test_r24patterns.py       proves pattern export -> import is byte-identical
  data/functions.csv        function map (8,941 entries; 2,837 below VA 0x0226E910 are code)
sdk/                        C SDK: firmware.sym, include/, runtime/, patches/, tests/
specs/examples/             declarative custom-build specs
decomp/                     decompiled C samples + index
docs/                       the documentation set
```

## Legal / safety

This is firmware for hardware you own. The vendor's update guide prohibits
redistribution and reverse engineering of the software. Nothing here
redistributes the firmware; `R24SYS.BIN` stays where you put it. The toolchain
does not automate flashing — you install images yourself via the stock
SD-card procedure.
