# 02 — Analysis findings

Every claim below is labelled with its **evidence class**:

- **[PROVEN]** — directly demonstrated, reproducible with a command in this repo.
- **[STRONG]** — a conclusion drawn from multiple independent artefacts.
- **[INFERRED]** — the most consistent explanation; not independently confirmed.
- **[OPEN]** — explicitly unresolved.

---

## 1. Container format

The file is **not** an ELF, PE, raw-UART boot stream or archive. It is a
64-byte header followed by a flat image.

### 1.1 Header layout **[PROVEN]**

| Offset | Size | Field | Value in v1.13 |
|---|---|---|---|
| `0x00` | 9 | magic | `"ZOOM/D211"` — `ZOOM/D21` tag + ASCII `1` |
| `0x09` | 3 | padding | zero |
| `0x0c` | 4 | unknown | `0x00000000` |
| `0x10` | 1 | **image kind**: 0 SYSTEM, 1 BOOT, 2 RHYTHM (boot loader, `18_dump_analysis.md` §3.2) | `0` |
| `0x11` | 15 | reserved | zero |
| `0x20` | 16 | version, ASCII, NUL-padded | `"1.13"` |
| `0x30` | 4 | **checksum present** (byte at `0x30`; 0 = updater skips the sum) | `0x00000001` |
| `0x34` | 4 | **checksum** (u32 LE) | `0x3197CC47` |
| `0x38` | 8 | reserved | zero |
| `0x40` | … | payload | flat image, linked at `0x02080000` |

The magic is best read as two fields: the 8-byte tag `ZOOM/D21` plus a u32/pchar
`1`. It is stable across the observed family — the payload also contains the
sibling tags `ZOOM/D120SEQ` and `ZOOM/R16`.

### 1.2 The checksum **[PROVEN]**

```
header[0x34] (u32 LE) == (sum of every payload byte) mod 2^32
```

Verified exactly:

```
stored   = 0x3197CC47
computed = 0x3197CC47
```

This is a plain additive byte checksum, **not** a CRC. Consequences:

- Any single-byte payload edit requires recomputing the field.
- It detects accidental corruption but offers no cryptographic integrity, so
  it is trivially forgeable by the packer (`r24tool pack` does this).

It was rejected as a CRC/Adler test: `crc32(payload)=0x1C768459`,
`adler32(payload)=0x66B2B44E`, while `sum8` matched on the first attempt.

### 1.3 There is no compression and no encryption **[PROVEN]**

- Every container magic (gzip, bzip2, xz, zstd, lz4, lzo, lzma, zip) was
  searched **and** genuinely decompressed at every candidate offset that could
  plausibly start a stream. **Zero** candidates decompressed to more than 8 KiB.
- Byte-frequency is dominated by `0x00` (17.0 %), `0xFF` (12.0 %), `0x01`
  (2.7 %) — the signature of code and integer tables, not of entropy-coded data.
- Per-64 KiB entropy stays in the 6–7 bit/byte band across the whole code body.
- Thumb instructions are directly readable at their final addresses; `objdump`
  decodes the entry point cleanly with no prior transformation.

### 1.4 There are no relocations **[PROVEN]**

The image is position-dependent and pre-linked. Absolute addresses are baked
into ARM literal pools (for example the literal at `0x94` = `0x00400000`). No
relocation table exists, and none is needed: the loader places the payload at a
fixed address.

---

## 2. Architecture

### 2.1 CPU and execution states **[PROVEN]**

| Property | Finding | Evidence |
|---|---|---|
| ISA family | ARM | All markers below |
| Endianness | **little** | `e59ff058` decodes as `ldr pc,[pc,#88]` only as LE |
| States | ARM **and** Thumb-1 | Both instruction streams in the same image |
| Thumb-2 | **absent** | No 32-bit Thumb-2 encodings; ARMv4T/ARMv5TE class |
| FPU | none evident | No VFP/NEON instruction patterns |
| Core class | ARM7TDMI-class integer core | No CP15 MMU programming of ARM9/11 shape |

Marker census over the payload:

```
ARM   stmdb sp!,{...}  255
ARM   ldmia sp!,{...}  262
ARM   bx rX            356
Thumb push {...,lr}   6709
Thumb bl pairs       36418
```

The pattern is unambiguous: **ARM state for startup/interworking veneers,
Thumb state for the bulk of the code**. A strict sequential Thumb decode
accounts for **96.4 %** of payload bytes, which is what you would expect from a
firmware that is almost entirely Thumb.

### 2.2 Entry point and the missing vector table **[PROVEN]**

The payload begins at VA `0x02080000`:

```
02080000  e59ff058   ldr  pc, [pc, #88]    ; -> 02080060
02080004  0208002c   ; literal: crt0 entry
02080008  e59f0054   ldr  r0, [pc, #84]
0208000c  e1a0e00f   mov  lr, pc
02080010  e12fff10   bx   r0
...
02080060  (crt0 body: CP15 + GPIO setup, then a call through r1)
```

Notably, **there is no ARM exception vector table** (`b reset` / `b undef` /
`b swi` …) at the image start. Offset `0x00` is a jump veneer, not a vector
table. This means the payload is **not** what the ARM core fetches at reset —
it is a stage-2 image that something else has already loaded and then jumped
into. See §5 for the candidate loaders.

### 2.3 Startup code **[PROVEN]**

The ARM prologue at `0x02080060` does recognisably embedded init work:

```
ldr r0,=0x01C67000 ; CPSR/system control peripheral
ldr r1,=0x00400000
str r1,[r0,#0x44]  ; write a mode/control register
```

followed by a sequence of `ldr r1,=mask` / `and` / `str` pairs operating on the
same peripheral with masks such as `0xFFFFFFDF`, `0xFFFFFFFE`, `0xFFFFFFF7` —
classic individual-bit clear/set on a control register — then a `mov r1,#32`
delay loop and a tail-call through `r1`.

Interworking veneers at `0x02080408`, `0x0208042C`, `0x0208043C`, `0x02080490`
contain genuine **CP15** instructions (`mrc`/`mcr p15`), so this is a real ARM
core with a system-control coprocessor, not an emulated ARM subset.

---

## 3. Code / data separation

The payload is a **flat image with interleaved code, rodata and data** — there
are no section headers to consult, so the split has to be *derived*.

### 3.1 Segmentation result **[PROVEN]**

```
kind                  file range             virtual range              size
code       0x00000040 - 0x001f0040   0x02080000 - 0x02270000       0x1f0000
fill-ff    0x001f0040 - 0x00220040   0x02270000 - 0x022a0000       0x030000
code       0x00220040 - 0x006d6674   0x022a0000 - 0x02756634       0x4b6634

code        6,972,980 bytes  (97.3 % of payload)
fill-ff       196,608 bytes  ( 2.7 % of payload)
```

The method is a strict sequential Thumb decode per 64 KiB block; a block is
`code` when ≥80 % of its bytes parse as whole Thumb instructions, `data`
otherwise, and any single-valued run is `fill`. It is deterministic and
reproducible (`r24tool analyze`).

### 3.2 The `0xFF` region and what the reset code loads **[PROVEN]**

File `0x001EE950 → 0x00220040` is `0xFF` (202,480 bytes), VA
`0x0226E910 → 0x022A0000`. It separates the two parts of the image:

```
file 0x000040 .. 0x0005B4   reset / copy code (runs at its load address)
file 0x0005B4 .. 0x1EE910   code + data   -> copied to RAM 0x80000000 at reset
file 0x1EE910 .. 0x1EE950   0x40 bytes    -> copied to RAM 0x00008000
file 0x1EE950 .. 0x220040   0xFF          -> never copied anywhere
file 0x220040 .. EOF        PCM sample bank -> copied to RAM 0x87540000 by the
                            main init (07_pcm_samples.md §2.1); not code
```

The copies are done by the ARM reset code at VA `0x02080490` with parameters at
file `0x594`–`0x5B0` (`07_pcm_samples.md` §2.3), which then zeroes the bss at RAM
`0x801EE35C` (0x10EBF30 bytes). **All code executes in RAM at
`file − 0x5B4 + 0x80000000`**: absolute addresses in the code (function and
data pointers) are in that space, not in the `0x0208xxxx` load space.

Consequence: the `0xFF` region is not usable for patches — nothing loads it, and
a branch "into" it from running code lands in zeroed bss
(`10_loop_import.md` §0). Earlier revisions called it free patch space, then an
"allocated overlay" because 617 words in the image have values in its *load*
range; both were wrong (those 617 values are coincidences of the kind explained
in `07_pcm_samples.md` §2.4).

### 3.3 A low-entropy descriptor table sits in a zero region **[STRONG]**

Just before the `0xFF` region, VA `0x0226BB40` (= file `0x001EBB80`) is a large,
mostly-zero structure (81.7 % zeros over 1216 bytes) holding:

- a display-charset/placeholder string `-.--` at VA `0x0226BBB0`,
- the **UI version string** `1.13` at VA `0x0226BBBC`,
- arrays of **Thumb function pointers** into RAM (`0x80023B19`, `0x80023B4D`,
  `0x80023B5D`, … — all with bit 0 set, the Thumb interworking bit),
- fixed-size character tables (`0..9` as 4-byte entries, `Fast`/`Slow`,
  a `SWAP/BOUNCE 1/2` record),
- a task-descriptor-shaped record `{1, 0, 0x8000612D, 0x8000615B, 1000, …}`
  where `1000` is plausibly a period/priority and the two pointers are code
  entry points.

This is an excellent place to add data: it is sparse, unused space.

---

## 4. What the firmware *is* — runtime and hardware

### 4.1 The application-level identity **[PROVEN]**

61 FAT-style path strings survive in rodata (many repeated across
tables):

```
A:\ZOOM_R24\PROJECT\%s\HST_MSTR.ZDT      A:\ZOOM_R24\PROJECT\%s\AUDIO
A:\ZOOM_R24\PROJECT\%s\HST_S%02d.ZDT     STE-%03d.WAV / MONO-%03d.WAV
A:\ZOOM_R24\PROJECT\%s\HST_%02d.ZDT      MASTR%03d.WAV
A:\ZOOM_R24\PROJECT\%s\AUDIO\TRK%02d_%02d
A:\ZOOM_R24\SYS\RECDAT%02d.ZTP           A:\R24PANEL.ZEX
```

plus project container magic `ZOOM R24   PROJECT DATA VER0001`, sibling tags
`ZOOM/R16`, `ZOOM/R-24  EFFECT DATA`, `ZOOM D120SEQ`, `ZOOM R24   SEQPLAY DATA`.

**[STRONG]** The R24 shares file-format and container code with the Zoom R16 and
D120 families; the `A:\` prefix confirms a FAT filesystem on the SD card
(exactly what the update guide describes: "copy the file to the root directory
on an SD card").

### 4.2 The real-time OS: TI DSP/BIOS — on the DSP **[PROVEN]**

> Corrected 2026-11-19 (`17_dsp_and_dump.md`). Earlier revisions placed
> DSP/BIOS on the ARM and the DSP program outside this file; both were wrong.

The image holds verbatim TI DSP/BIOS strings:

```
Internal exception: IERR=0x%x
  Instruction fetch exception / Fetch packet exception / Execute patcket
  exception / Opcode / Resource conflict / Resource access / Privilege /
  Loop buffer exception / Missed stall          <- C64x+ IERR bits
EXC_exceptionHandler: EFR=0x%x  NRP=0x%x  mode=%s
SYS abort called with message '%s'
***LOCK NOT CALLED IN TSK CONTEXT
```

**[PROVEN]** They all sit in file `0x1D6D95..0x1D6E80`, inside DSP section 11
(the DSP's `.const`, copied to `0x87B24D60`): this is the **DSP's** OS. The
ARM side has its own, unidentified RTOS (`initial task`, `stack pool`,
`general pool`, `/dev/null`, `PUH_OTG_Int_task` lie outside the DSP sections).

### 4.3 The DSP program is inside this file **[PROVEN]**

The chip is DaVinci-class: ARM926 + C64x+ DSP (PSC at `0x01C41000`, system
module at `0x01C40000`, DSP L2 at `0x11800000`, L1D at `0x11F04000`). At boot
`0x8006C868` holds the DSP in local reset, sets DSPBOOTADDR = `0x11800000`,
`memcpy`s **14 sections** (115,655 bytes, file `0x1C83E0..0x1E47A7`, table
`0x801EDB24`) into L2, L1D and DDR `0x87B10000..`, and releases the reset.
Section 9 (→ `0x11800000`) disassembles as a C64x DSP/BIOS reset vector
(`mvk/mvklh 0x87B24B80, b0 ; b b0`). The old "no C6x code" test missed it
because the code is stored unaligned inside `.data` and uses C64x+ compact
(header-based) fetch packets.

Consequence: **a DSP change is a patch to R24SYS.BIN**, flashed by the normal
updater. Full table, boot sequence, MMU map: `17_dsp_and_dump.md`.

### 4.4 Peripherals and buses **[STRONG]**

| Subsystem | Evidence |
|---|---|
| USB **host** mass storage | `usbh_msc_app_start: failed to create task` |
| USB OTG host controller driver | `PUH_OTG_Int_task`, `PUH_HCD`, `PUH_HUB`, `PUH_USBD`, `PUH_CSTMC_TASK` |
| USB device/custom class | `USB CUSTOM H/D`, `USB Setup Error`, `LOOP[USB]` |
| SD/MMC card | `R24 SD R&W`, `Insert SD CARD`, `SD CARD FORMAT`, `Card Protected` |
| Control/mode peripheral | GPIO/system register base `0x01C67000` (crt0), `0x01C41000` (bit masks) |
| Audio DSP | C64x+ (DaVinci), program in this image (see §4.3) |

The `PUH_*` prefix (`PUH_HCD`, `PUH_HUB`, `PUH_OTG_Int_task`) indicates a
third-party USB-OTG host stack rather than a from-scratch one.

### 4.5 Audio feature set (data, not code) **[PROVEN]**

Rodata contains the complete user-facing vocabulary of the product:
`Stereo`, `32.0kHz`/`44.1kHz`/`48.0kHz`/`88.2kHz`/`96.0kHz`, `Limiter`,
`Comp`, `Chorus`, `Delay`, `Reverb`, `LO CUT FILTER`, `HI CUT FILTER`,
`Threshold`, `Balance`, `Mastering`, `8xComp EQ`, `Dual Mic`, `Aco/Bass`,
`SMF`/`TRACK SEQ`, plus 30+ patch parameter names.

---

## 5. The updater: how an image gets accepted **[PROVEN] / [OPEN]**

From the supplied guide (`R24_Firmware_Update_Guide_E.pdf`):

1. copy the update file to the **root** of an SD card,
2. insert the card,
3. hold a button and power on,
4. confirm `OK`,
5. wait; power off.

**[PROVEN by code, 2026-11-19]** (boot loader from the NOR dump,
`18_dump_analysis.md` §3): the updater checks the 9-byte magic `ZOOM/D211`,
the kind byte `0x10`, and — when byte `0x30` is non-zero — the byte sum at
`0x34` over the whole file after the header. It does **not** check the length,
the version order (no downgrade protection) or any signature. It writes the
payload unchanged at `0x02080000`. **A payload over `0x720000` bytes reaches
the boot flag; over `0x780000` it wraps onto the boot loader (brick).**

The firmware contains a second, independent update hook — `A:\R24PANEL.ZEX`
with the message `PANEL VERSIONUP` / `Failed` / `Finish!` — which applies a
**panel** (front-panel controller) update from the same card. `PANEL
VERSIONUP` occurs twice, at file `0x6930` and `0x238BC`.

**The loader is not in this file** (§2.2). There is no ARM vector table,
and no decompressor. **[PROVEN]** it is the boot loader at NOR
`0x02000000`: the payload lives in the same NOR flash at `0x02080000`, and the
main firmware's own Test Mode updater writes `D211BOOT.BIN` to `0x02000000`
and `D211MAIN.BIN` to `0x02080000` (`17_dsp_and_dump.md` §4). PROJECT > DUMP
MEMORY copied it to `A:\DUMP\NOR.BIN`; its rules, the boot flag at
`0x027A0000` and the flash layout are in `18_dump_analysis.md`.

---

## 6. Two independent version fields **[PROVEN]**

This is a trap worth documenting prominently. `1.13` appears exactly twice:

| # | File offset | VA | Location | Consumed by |
|---|---|---|---|---|
| 0 | `0x000020` | `0x0207FFE0` | header field (§1.1) | the updater / tooling |
| 1 | `0x1EBBFC` | `0x0226BBBC` | UI string table, next to charset `-.--` | the **LCD display** |

Editing only the header field changes what a host reads but **not what the
device shows on its screen**. `r24tool` therefore tracks both (`version` and
`ui_version`) and `--set-version` updates them together.

The UI copy was not located by pattern-guessing: the surrounding structure is
the giveaway. It sits immediately after the display placeholder charset, and is
followed by a word-aligned array of Thumb function pointers. The descriptor array starts at
`0x1EBC08`, so the version slot has `0x1EBC08 − 0x1EBBFC = 12` bytes. The live
string is terminated at 4 characters by its NUL, and the toolchain caps
replacements at 8 to keep a safety margin before the pointer array.

---

## 7. Summary of evidence classes

| Finding | Class |
|---|---|
| ARMv4T/v5TE, little-endian, ARM+Thumb, no Thumb-2 | PROVEN |
| Flat 64-byte-header container at VA `0x02080000` | PROVEN |
| Checksum = payload byte sum mod 2³² | PROVEN |
| No compression, no encryption, no relocations | PROVEN |
| Byte-identical reproduction | PROVEN |
| `0xFF` region `0x0226E910`–`0x022A0000` is never loaded into RAM | PROVEN (reset copy, §3.2) |
| code runs in RAM at `file − 0x5B4 + 0x80000000` | PROVEN (reset copy, §3.2) |
| Two independent version fields (header + UI) | PROVEN |
| DSP/BIOS strings belong to the DSP program (section 11) | PROVEN |
| DaVinci-class ARM926 + C64x+; DSP program in this file, loaded by `0x8006C868` | PROVEN |
| Boot loader = NOR `0x02000000`, 512 KiB | PROVEN by code |
| USB-OTG host + MSC + FAT + SD | STRONG |
| Shared lineage with Zoom R16 / D120 | STRONG |
| Loader rules: magic, kind byte, optional sum; no length/version/signature check | PROVEN (`18_dump_analysis.md`) |
| Header `0x10` = kind, `0x30` = checksum present; `0x0c` unused by the loader | PROVEN (`18_dump_analysis.md`) |
