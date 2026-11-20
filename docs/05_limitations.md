# 05 — Limitations, open questions, and risk

This file exists so that no reader has to guess which parts of the analysis are
solid and which are not. It is deliberately blunt.

---

## 1. What this package does **not** do

### 1.1 It does not reconstruct compilable source

`R24SYS.BIN` is a stripped, statically linked, position-dependent image.

A decompiler **is** used (`docs/06_assembly_workflow.md`): Ghidra with
`ARM:LE:32:v4t` + `ARM_v45.cspec` produces readable C for 2,823 functions, and
8,941 map entries are disassembled with real mnemonics (only 2,837 are real code — 2,577 Thumb / 260 ARM; the other 6,104 entries are inside the PCM sample bank, audio decoded as code). That is enough to *read*
and to *patch at assembly level*.

What it still does not give you is a **source tree that recompiles to the same
bytes**. The decompiled C has generated names (`FUN_0208002c`, `uVar3`),
inferred types (`undefined4`, `unaff_r5`), and recompiling it would need the
original compiler version, flags, libraries and link order — none of which are
recoverable. So "fix a bug and rebuild from C" is not available; "reimplement
this one function in assembly and splice it back" is (see §7 and
`06_assembly_workflow.md`).

You can also change data the existing code reads — version strings, filenames,
UI text.

### 1.2 It does not byte-reproduce the firmware from source

"Reproduction" here means: *the container format is modelled exactly, so an
image can be unpacked, edited and re-sealed into a file the device accepts*, and
this is proved by a byte-identical round-trip. It does **not** mean the original
build is reproducible from source. Being clear about this distinction matters:
the round-trip proves the *format*, not the *build*.

### 1.3 It does not (yet) change the DSP

The DSP program is in the image and can be patched (§3), but no tool here
edits it yet.

### 1.4 It does not automate flashing

By design. Flashing is the one irreversible step and stays in your hands.

---

## 2. Container-level unknowns

| Item | Status | Risk if you guess |
|---|---|---|
| Header `0x0c` (4 bytes) | not read by the boot loader, always `0` | leave as-is |
| Header `0x10` | **PROVEN**: image kind, 0 = SYSTEM | must stay 0 for `R24SYS.BIN` |
| Header `0x30` | **PROVEN**: checksum present (0 = no sum check) | keep 1 |
| Header `0x11`–`0x1F`, `0x38`–`0x3F` | not read, always `0` | reserved |
| Does the updater compare file **length**? | **PROVEN: no** | any length is written — **payload > `0x720000` bytes (file > 7,471,168) overwrites the boot flag, > `0x780000` wraps onto the boot loader = brick** (`18_dump_analysis.md` §3.3) |
| Is there downgrade protection on the version string? | **PROVEN: no** | the version is only displayed |
| Any signature beyond the additive checksum? | **STRONG: none found** | a search found no signature block, no RSA/ECC constants, no hash other than the byte sum |
| Meaning of magic tail `/D211` | **INFERRED** | `D21` = platform/family (cf. `D120SEQ`), trailing `1` = variant or format revision |

### 2.1 The checksum is weak

`header[0x34]` is an additive byte sum, **not** a CRC. It catches accidental
corruption and nothing else. Anyone can forge it — `r24tool` does so on every
build. Do not treat it as an authenticity mechanism.

### 2.2 The loader is not in this file **[RESOLVED 2026-11-19]**

The payload starts with a jump veneer, **not** an ARM exception vector table
(see `02_analysis.md` §2.2). The image is therefore a stage-2 payload: something
else validates the header, places the payload at `0x02080000` and jumps to it.

That loader — the 512 KiB boot loader at NOR `0x02000000` (`17_dsp_and_dump.md`
§4) — is what actually defines the acceptance rules. It was dumped and read:
`18_dump_analysis.md` §3 lists every check. An interrupted SYSTEM update is
recoverable (the boot flag is erased first, written last); only damage to the
boot loader itself is fatal.

### 2.3 The `0xFF` region **[PROVEN: never loaded — not usable]**

> **Correction (2026-11-18).** The reset code copies only file `0x5B4..0x1EE950`
> to RAM (`02_analysis.md` §3.2); the `0xFF` region is never loaded, so it is
> neither free patch space nor an overlay. The "617 pointers" below are values
> that merely fall in its *load* range — the same coincidence effect as the
> "168 literal pools" of the sample bank (`07_pcm_samples.md` §2.4). The text
> below is the earlier, superseded reasoning, kept for the record.

VA `0x0226E910`–`0x022A0000` (202,480 bytes) is erased — every byte is `0xFF`.
**It is nevertheless not free space.** A full sweep of the image finds **617
four-byte-aligned pointers into that range**, 262 of them odd (thus Thumb
function pointers):

```
import struct
d = open('R24SYS.BIN','rb').read()
al = [(o, struct.unpack_from('<I', d, o)[0]) for o in range(0, len(d)-4, 4)]
al = [(o, v) for o, v in al if 0x0226E910 <= v < 0x022A0000]
print(len(al), 'pointers,', sum(1 for _, v in al if v & 1), 'odd')
# -> 617 pointers, 262 odd
```

Erased storage with live pointers aimed at it is the signature of an overlay or
work area: the image leaves it blank because something fills it at runtime.
Writing code or data there overwrites those targets. Earlier revisions of this
file and of `02_analysis.md` §3.2 called the region free on the strength of the
`0xFF` bytes alone; the pointer count is the stronger evidence and supersedes it.
See `docs/10_loop_import.md` §6.

### 2.4 Where new data or code *can* go

The PCM bank tail (copied to RAM `0x87540000+`) already holds the relocated
sample table, verified on a device (`07_pcm_samples.md` §3); it is the proven
place for new data, and the planned place for new code
(`10_loop_import.md` §5). The block below was an earlier, weaker candidate.

#### The 1,216-byte data block

VA `0x0226BB40`–`0x0226C000` (file `0x1EBB80`) is 1,216 bytes with 222 non-zero
and **no** pointers into the `0xFF` range stored inside it — the sparse descriptor
block `04_data_vs_code.md` points at. It is data space and far too small for a
routine (~350 bytes of dense Thumb plus pools). **Untested on a device.**

---

## 3. The DSP — resolved 2026-11-19

Earlier revisions called this "the DSP gap": DSP/BIOS strings but no DSP code
found, so the DSP program was assumed to live elsewhere. **That was wrong**
(`17_dsp_and_dump.md`):

- **[PROVEN]** the ARM boots the DSP at `0x8006C868` by copying 14 sections
  (115,655 bytes, file `0x1C83E0..0x1E47A7`) out of this image into DSP L2/L1D
  and DDR `0x87B10000..`; section 9 is a C64x DSP/BIOS reset vector;
- **[PROVEN]** the DSP/BIOS strings are in the DSP's `.const` (section 11).

What remains open:

- **[OPEN]** reading the DSP code needs TI's `dis6x` (capstone does not decode
  C64x+ compact packets); the ARM↔DSP L1D interface is only partly mapped;
- **[OPEN]** no DSP change has been made or tested. Anything that moves or
  resizes the DSP sections must also update `fw_dsp_sections` (`0x801EDB24`).

**Bottom line:** audio-path changes are reachable in principle — the same
updater flashes the DSP code — but need the TI toolchain first.

---

## 4. Risk assessment for actually flashing a custom image

Ranked by likelihood of a bad outcome:

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Image rejected by updater (checksum/magic/length rules) | Low | None — update aborts | `verify` before flashing; stock file as rollback |
| Device boots but behaves oddly | Low | Moderate | test only data changes; keep stock file |
| Device becomes unbootable ("unstartable", per the vendor guide) | Low, non-zero | **High** | accept the risk explicitly; recovery is the documented re-update |
| Silent audio/DSP misbehaviour | Low for ARM-only patches | Moderate–High | never write inside file `0x1C83E0..0x1E47A7` (DSP program, §3) unless that is the point |
| Bricking beyond self-recovery | Low | **Total** | vendor warns the unit can become unstartable; there is no confirmed recovery path if the loader itself is affected |

The vendor's own guide says: *"Do not turn the power off or remove the SD card
during a firmware update. Doing so could cause the R24 to become unstartable."*
The same warning applies to a malformed custom image.

### Recommendation

Treat custom images as **cosmetic-only** until the loader's rules are known
(dump the boot loader with PROJECT > DUMP MEMORY, `17_dsp_and_dump.md`). Version strings and filenames are reversible and
observable; code changes are not.

---

## 5. Confidently ruled out

Several hypotheses were tested and **rejected**, which is worth recording so
nobody repeats the work:

| Hypothesis | Verdict | Why |
|---|---|---|
| Compressed payload (gzip/zlib/lzma/zstd/bzip2/lz4/lzo/xz/zip) | **Rejected** | Every candidate offset was *actually decompressed*; zero streams produced >8 KiB |
| Encrypted / high-entropy payload | **Rejected** | Entropy 6–7 bit/byte in the code body; Thumb decodes directly at final addresses |
| ELF / PE / uImage / U-Boot container | **Rejected** | No container magic, no program headers, no section table |
| ARM EABI vectored table or raw ARM vector table at `0x02080000` | **Rejected** | First words are `ldr pc,[pc,#…]` + literal, not `b reset` |
| Thumb-2 (Cortex) code | **Rejected** | No 32-bit Thumb-2 encodings; 16-bit length model fits at 96.4 % |
| Big-endian ARM | **Rejected** | Instructions only decode as sensible ARM in little-endian |
| µITRON / VxWorks / ThreadX / Linux / uC-OS | **Rejected** | Exception/abort semantics are uniquely TI DSP/BIOS |
| C6x DSP code present in this file | ~~Rejected~~ **Wrong** | it is there, unaligned in `.data`; see §3 and `17_dsp_and_dump.md` |
| The second `1.13` is padding noise | **Rejected** | It sits between the display charset and a Thumb pointer array; structurally a UI string |
| Package is a single-userland (Linux) binary | **Rejected** | CP15 system control, direct GPIO writes at `0x01C6xxxx` |

---

## 6. Reproducibility of this analysis

Everything claimed above can be re-derived:

```bash
./tools/r24tool analyze R24SYS.BIN     # architecture, map, fingerprints
./tools/test_roundtrip.py              # 18 checks incl. byte-identical rebuild
./tools/r24tool verify  R24SYS.BIN     # magic + checksum
sha256sum R24SYS.BIN                   # 71c7e3c5...f81dc93
```

If your `R24SYS.BIN` has a different hash, you have a different revision and
**every offset in `04_data_vs_code.md` must be re-derived** before use. The
guarded patcher will refuse rather than corrupt, but do not assume the offsets
carry over.

## 7. Assembly-level limits (what `r24tool asm` cannot do)

Measured by `tools/validate_reasm.py` over the function ranges of
`tools/data/functions.csv` **that are code** (VA below `0x0226E910`; re-measured
2026-11-18):

```
non-overlapping ranges                : 1,730
functions reproduced byte-identically : 1,694 / 1,730   (97.9%)
code bytes reproduced                 :   160,174 / 193,182 (82.9%)
instructions as mnemonics / verbatim  :    62,206 / 2,271
```

The earlier figures (5,564 / 5,733 functions, 90.5 % of 2.8 MB) counted the
6,104 map entries inside the PCM sample bank, i.e. audio decoded as code, and
were inflated by it. That leaves **36 code functions that cannot be
round-tripped**. The honest
description of each failure class:

| Cause | Effect |
|---|---|
| Data misdecoded as an instruction, whose operands cannot be expressed | forced verbatim; a human cannot edit those bytes as mnemonics |
| Link-time relocation out of range (`R_ARM_THM_CALL`) | branch target beyond ±16 MB; assembled but not linked |
| Overlapping function ranges in the Ghidra map | truncated to the innermost function; a few bytes at boundaries end up as `.short` |
| Thumb/ARM state mis-attributed at a function entry | the whole function decodes in the wrong state and fails |

**What this means in practice:** you can edit ~97 % of functions and expect a
bit-exact result for the parts you did not touch. For the other 3 %, the
disassembly is still readable but the rebuild is not guaranteed — check with
`cmp -l` before trusting an edit.

### 7.1 Growth is impossible in place

There are no relocations. A function cannot move and cannot expand past its
original size; `r24patch.assemble()` refuses. `trampoline_patch()`, which
branched into the `0xFF` range, is **disabled**: that range is never loaded, so
the code would not exist at runtime (§2.3). New code has to go into a region the
boot copies to RAM — the PCM bank tail — and be reached with a long-call veneer,
because it is ~120 MB from the firmware code, beyond Thumb `bl` range. Anything that needs *more
code* is blocked for now; everything shipped in this repository is data
patching (including the sample-table relocation, which only rewrites two
literal words and data inside the bank), which needs no free code space.

### 7.2 No decompiler guarantee

Of 2,831 functions attempted, 2,823 decompiled (99.7 %). The 8 failures plus
the poor-quality cases (computed jumps, heavy register aliasing) are recorded
in `decomp_index.csv`. Decompiled C is an aid for reading; it is never a
correctness guarantee about the original semantics.

### 7.3 The PCM bank: what is and is not proven

**The sample table is data, not frozen [PROVEN].** The firmware copies the
bank to RAM at boot and computes every voice's start and end from the Master
Sample Table at playback time; the table is reached through two literal words
only (`07_pcm_samples.md` §2). Positions, lengths and the entry count can
change; the tools do it (`patch_samples.py --repack`, `r24bank.py relocate`).
Earlier revisions of this file claimed the opposite on the strength of "168
literal pools pointing into the bank"; §2.4 of `07_pcm_samples.md` shows those
words are not sample pointers.

**Still limited:** the bank base and the file size (56.0 s of audio at
44.1 kHz in total). The relocated 160-entry table was verified on a device
(2026-11-18).

**The sample rate is 44.1 kHz.** No rate field is stored with the bank, but the
rate is known, and the ARM code that computes a rhythm voice's duration divides
the sample count by the literal 44100 (`07_pcm_samples.md` §2.2). The tools
resample input WAVs to 44.1 kHz.

**Which hit is which** is no longer open: each sample's role follows from the
kit slots that use it (`08_drum_kits.md` §4, `09_sample_map.md`).
