# 12 — CUSTOM kit: drum samples loaded from the project folder

> Added 2026-11-19. Since the same day it is an SDK patch,
> `sdk/patches/custom_kit.c` (see `sdk/README.md`). Proven in an ARM926 emulator
> running the patched image (`tools/test_custom_kit.py`, 535 checks);
> **verified on a device** (2026-11-19, `build/R24SYS.BIN` sha256 `a4f1c0a2…`) — loader, KIT.TXT, FILE > KIT ASSIGN and the KIT SELECT editor.

## 1. What it does

`tools/custom_kit.py install` adds an eleventh kit, **CUSTOM**, to KIT SELECT and
reserves RAM for its audio. Every time a project becomes current (power-on,
PROJECT SELECT, NEW PROJECT, …) the R24 looks for

```
A:\ZOOM_R24\PROJECT\<project>\KIT\SAMPLE01.WAV   -> slot 1  (pad 1, kick)
...
A:\ZOOM_R24\PROJECT\<project>\KIT\SAMPLE16.WAV   -> slot 16
```

and loads what it finds into CUSTOM's 16 slots. A missing (or unreadable) file
leaves that slot at its default sound — a copy of BASIC — so a project without a
`KIT` folder simply has CUSTOM = BASIC. Switching to another project reloads, so
every project carries its own drum kit. The slot order is the one of
`08_drum_kits.md` §3.3 (1 kick, 2 snare, 3 closed hat, 4 open hat, 5 crash,
6 ride, 7–9 toms, 10–16 percussion).

Any WAV is accepted: 16/24/32-bit integer or 32-bit float, mono or up to
8 channels (folded to mono), 4–192 kHz (linearly resampled to 44.1 kHz on the
R24), `WAVE_FORMAT_EXTENSIBLE`, extra chunks. 8-bit WAVs are rejected. Converting
on the PC first (`prepare`, §4) gives better resampling and makes loading faster.

All 16 samples share the kit's memory. In `build_firmware.py` (since
2026-11-20, `-D CUSTOM_KIT_DDR=1`) that is free DDR: RTOS pool 3 (960 KiB) +
3 MiB at `0x819126C0` + 19.9 MiB at `0x81C128C0` + 5.25 MiB at `0x87000000`
≈ **5.8 min** of mono 44.1 kHz, longest single sample ~3.7 min (the big zones
verified unused by a device DDR dump diff; `20_kit_memory_plan.md`); built alone (`custom_kit.py install`) it is the
reserve in the bank tail (default **6 s**). Files are taken in slot order, each
put in the smallest free region that holds it; a sample that does not fit
entirely is cut with a 5 ms fade, and once memory is full the remaining slots
keep their default.

## 1.1 Project files: `KIT\` and `KIT.TXT` (2026-11-19)

```
PROJECT\PRJ001\KIT.TXT          # ZOOM R24 CUSTOM kit: pad=file in KIT\ ...
                                01=Kick Deep.WAV
                                02=SNARE.WAV
PROJECT\PRJ001\KIT\Kick Deep.WAV
PROJECT\PRJ001\KIT\SNARE.WAV
```

`KIT.TXT` maps pads (01..16) to files of **any name** in `KIT\`; it is plain
text, editable on a PC (`#` comments, spaces around `=` ignored, LF or CRLF).
Only plain file names are accepted (no `\`, `/`, `:`), so nothing outside
`KIT\` is ever read. A pad without a line plays `KIT\SAMPLEnn.WAV` if present
(kits made before KIT.TXT keep working and are written into KIT.TXT at the next
assignment). FILE > KIT ASSIGN and the KIT SELECT editor copy files with their
**original name** and rewrite `KIT.TXT`; replacing a pad's file deletes the old
one from `KIT\` when no other pad uses it.

### Pad settings: `Snn=level,pan,group` (2026-11-19)

> 2026-11-19: optional 4th value **pitch** −24..+19 (pad byte 5) and 5th
> **poly** 0/1 (0 = default = MONO bit 4 of byte 4 set), written only up to
> the last one that is not 0: `S01=11,69,2,-5`, `S01=11,69,2,0,1` —
> `19_drum_pitch.md`.

```
S01=11,69,2         pad 1: level 11, pan R5, mute group 2
```

Written by KIT SELECT > CUSTOM > SETTINGS (`14_kit_editor.md`) for the pads
whose values differ from the kit's own (the copy of BASIC in the image);
applied to the CUSTOM kit's 8-byte pad descriptors (`08_drum_kits.md` §3.2)
every time the project loads. `level` 0..15 (amplitude ∝ velocity × (level+1),
`0x8002DC16`; 16 would overflow), `pan` 0..126 (64 = C; the engine clamps to
0x7E), `group` 0..7 (low 3 bits of descriptor byte 4, `0x8002DD48`; the upper
bits are kept). A line with a value out of range or malformed is ignored.
Builds before this ignore `S` lines.

## 2. How it works

### 2.1 Firmware pieces used **[PROVEN, static]**

| RAM | what | evidence |
|---|---|---|
| `0x80054E54` | `fopen(path, mode)` | used by the file copy `0x800016C0` and the `PRJDATA.ZDT` loader `0x8001DC18` |
| `0x80054EA2` | `fread(buf, 1, n, f)` → bytes read | copy loop `0x8000170E` (chunks of `0x50000`) |
| `0x80054D56` | `fclose(f)` | same |
| `0x8001D212` | `project_name(n)` → `"PRJ001"`… (0 if n < 0) | every `PROJECT\%s` sprintf |
| `0x800BC598` | `strcpy` (Thumb) | |
| `0x800C40B0` | ARM: clean D-cache lines of `[a, a+len)` + drain write buffer (`mcr p15,0,rX,c7,c10,1` / `c7,c10,4`) | 3 firmware callers |
| `0x8002D888` | boot copy of the bank to `0x87540000` | called at `0x8000827E` in the main init |

### 2.2 The hook: `set_current_project(n)`, RAM `0x8001D6A6` (file `0x1DC5A`)

```
push {r4-r6,lr} ; [0x804C6E14] = n ; strcpy(0x804C6E24, project_name(n))
bl 0x8001D63A   ; write A:\ZOOM_R24\SYS\ZOOM.ZDT (last project) ; pop
```

It is called by `open_project` (`0x8001DF5C`, after `PRJDATA`/`EFXDATA`/`SMPLSEQ`
are read) and by eight other places that change the current project. Its first
10 bytes become

```
0x8001D6A6  4B01      ldr r3, [pc, #4]
0x8001D6A8  4718      bx  r3
0x8001D6AA  46C0      nop
0x8001D6AC  .word cust_hook | 1      (in the bank tail)
```

`cust_hook` (`sdk/patches/custom_kit.c`) repeats the three original steps
(same return value), then loads the kit. The other 22 bytes of the old body are
left as they were and are never reached.

### 2.3 Loading

For each slot the loader `fopen`s the file, `fread`s it (64 KiB chunks) straight
into the next free part of the reserve, parses the RIFF chunks and converts in
place (mono fold → resample → fade if cut), then writes `{rel_start, length}`
into that slot's sample-table entry — the same table the voice code reads on
every hit (`07_pcm_samples.md` §2). Samples start on 32-byte boundaries.

Afterwards it cleans the D-cache over the bytes written: the voice code hands
the **DSP** raw `start`/`end` pointers (`0x8002DAEC` → `0x8006C9F6`, which writes
them to the DSP mailbox at `0x11F07970`), so the audio must be in DDR, not only
in the ARM's write-back cache.

### 2.4 Memory layout

> 2026-11-20: with `-D CUSTOM_KIT_DDR=1` (the default firmware) the audio goes
> to free DDR (pool 3 + 3 MiB) through a region allocator and the reserve is 0
> — `20_kit_memory_plan.md`. The layout below is the reserve-only build.

Everything lives in the extension area at the end of the PCM bank
(`r24bank.py`), i.e. in the 5 MiB that the boot code copies to RAM:

```
R24KITS1   the kit table, now 11 kits (R24_KIT("CUSTOM") -> r24bank.expand_kits)
R24SDK01   the SDK build: header, code (~3.5 KB incl. libgcc division),
           the hook records, `cust_status`, and `reserve` (32-byte aligned)
```

The sample table becomes 176 entries (kit *k* slot *s* → `16k+s`); CUSTOM uses
160–175 and no other kit does, so loading never changes another kit. Nothing
is configured at build time: on its first run the loader finds the kit by name
(`r24_find_kit("CUSTOM")`), reads its 16 entry ids from the kit table and
copies their current values as the defaults — the table is still as the image
built it, so later repacks or kit edits need no extra step.

`cust_status` (address in `OUT.syms.json`) holds the number of runs, the bytes
in use and a per-slot result: > 0 loaded, 0 default, −1 not a RIFF/WAVE,
−2 unsupported format, −3 no data, −4 no room, −5 empty, −6 no CUSTOM kit.

### 2.5 Making room

The factory bank is full (4,941,364 bytes). `install` repacks it (identical
audio stored once) and, if that is not enough, caps every sample to the longest
length that frees the reserve, with a 20 ms fade. With the default 6 s that is
**≈1.5 s**: 23 table entries are shortened — crash/ride cymbals and a few long
toms; everything shorter is untouched. The command prints the cap and the
entries. `--reserve-sec` sets the size, `--no-trim` refuses instead.

| reserve | cap on factory samples |
|---|---|
| 2.6 s | 2.0 s |
| 6 s   | 1.5 s |
| 9 s   | 1.2 s |
| 12 s  | 1.0 s |

### 2.6 Boot order **[PROVEN, static]**

The loader runs from the bank tail, so it must not be called before the bank is
copied. The main init (`0x800081E4`) copies the bank at `0x8000827E`; a call-graph
sweep from every direct call before that point (240 functions) never reaches
`0x8001D6A6`, while the first project open after it does
(`0x80006030 → 0x80005FA6 → 0x8001DF5C → 0x8001D6A6`). Indirect calls (function
pointers, other tasks) are not covered by the sweep.

## 3. Install

The image to flash is built in one step from the stock file:

```bash
python3 tools/build_firmware.py            # -> build/R24SYS.BIN
python3 tools/build_firmware.py --reserve-sec 2 sdk/patches/mine.c   # options / extra patches
```

It applies the per-kit table relocation, `LOOP[SD]` (checked to be the exact
image verified on the device, sha256 `2af98550…`), then this patch and
`kit_assign.c`. Same inputs, same bytes (`eb15b37e…` with the defaults).

Building pieces by hand:

```bash
# stock image -> kit 11 CUSTOM, 6 s reserve
python3 tools/custom_kit.py install R24SYS.BIN -o out.BIN

# together with LOOP[SD] (order does not matter)
python3 tools/custom_kit.py install build/loop/R24SYS_160_LOOPSD_ROOT.BIN \
        -o out.BIN

# together with your own patches: one SDK build
python3 tools/r24sdk.py build R24SYS.BIN sdk/patches/custom_kit.c mine.c \
        -D CUSTOM_KIT_RESERVE_SEC=6 -o out.BIN

python3 tools/custom_kit.py check build/R24SYS.BIN
python3 tools/test_custom_kit.py
```

On an image whose kit table was already moved (`r24bank.py kits`), rename one
of its kits to `CUSTOM` with `r24kits.py` first; the build then uses it.

## 4. Putting samples on the card

**On the R24:** FILE menu → select a WAV → **KIT ASSIGN** → choose the pad
(`13_kit_assign.md`; built in by `custom_kit.py install` unless `--no-menu`).

**On a PC:**

```bash
python3 tools/custom_kit.py prepare kick.wav snare.wav hat.wav - crash.wav \
        -o /media/SD/ZOOM_R24/PROJECT/PRJ001/KIT
```

converts the files to mono 16-bit 44.1 kHz into `KIT\` keeping their names
(pads 1, 2, 3, 5 — a `-` skips a pad), updates `PRJ001\KIT.TXT` (other pads
kept) and prints the total against the reserve. Then open (or re-open) the project and choose KIT → CUSTOM.

## 5. What is not proven yet

* **On a device.** Everything above is static analysis plus emulation of the
  patched image (real firmware bytes, ARM926 core); only the file/project
  services are stubbed. To test: flash, put one `SAMPLE01.WAV` in a project's
  `KIT` folder, open the project, select CUSTOM, hit pad 1.
* **The DSP's own cache.** The ARM side is cleaned; if the DSP caches DDR, a
  pad that was played before a project switch could briefly play stale data.
  The firmware streams recorded audio from DDR to the DSP all the time, so this
  is unlikely.
* **File-system calls from this context.** `fopen` is called from inside
  `set_current_project`, i.e. from the same task that just read the project
  files, right after them.
* **Projects with kit 11 on stock firmware** read past the 10-kit table, as
  for any added kit (`08_drum_kits.md` §6.3).

## Pending

* **Long file names** (> 8.3) copied into `KIT\`: whether the R24 creates
  them correctly is not verified (FILE COPY/RENAME limit names to 8
  characters). If not, shorten the destination name and record it in
  `KIT.TXT`. Left open on purpose (2026-11-19).

