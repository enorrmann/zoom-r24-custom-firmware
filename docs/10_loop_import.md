# 10 — TAKE SELECT and `LOOP[USB]`: what the firmware really does

> **Rewritten 2026-11-18** after a full re-check against the binary. The
> previous version (kept in `build-backup/docs10-before-rewrite-2026-11-18.tar.gz`)
> had several wrong conclusions; §8 lists them. Everything below was re-derived
> from disassembly and is reproducible with the addresses given.

## 0. Address convention — read this first

The reset code (VA `0x02080490`, ARM) copies the image to RAM before anything
runs (`07_pcm_samples.md` §2.3):

```
file 0x0005B4 .. 0x1EE910   ->  RAM 0x80000000 .. 0x801EE35C    (code + data)
file 0x1EE910 .. 0x1EE950   ->  RAM 0x00008000                   (0x40 bytes)
RAM 0x801EE35C, 0x010EBF30 bytes                -> zeroed (bss)
file 0x220040 .. EOF        ->  RAM 0x87540000  (later, by the main init: PCM bank)
```

**The code runs at `RAM = file − 0x5B4 + 0x80000000`.** Every absolute
address inside the code (function pointers such as `0x800425BB`, data such as
`0x801EBE6C`, strings such as `0x80042E7C`) is in that RAM space. The "VA"
`0x0208xxxx` used by Ghidra and by older docs is only the *load* address; `bl`
offsets are the same in both spaces, absolute pointers are not. In this file:

* `RAM 0x800425BA` = file `0x042B6E`; addresses are written as RAM unless a
  `file` offset is stated;
* convert with `file = RAM − 0x80000000 + 0x5B4`.

File `0x1EE950..0x220040` (the `0xFF` "gap") is **not copied anywhere**. It is
neither free code space nor an overlay at runtime; the "617 pointers into it"
of older docs are 32-bit values that happen to fall in its *load* range, the
same false-pointer effect as the "168 literal pools" of the sample bank
(`07_pcm_samples.md` §2.4).

---

## 1. TAKE SELECT is built in RAM **[PROVEN]**

`RAM 0x80042C1A` (file `0x431CE`) builds one TAKE SELECT menu **per track**
(`r4 = 0..23`, `cmp r4,#0x18` at `0x80043106`). There is no menu table in the
image: the labels are strings in the function's literal pool, loaded with `adr`.

Per track it writes four **80-byte item records** at
`0x811449DC + track*320 + n*80`:

| n | label (literal, RAM) | `+0x10` callback | what it does |
|---|---|---|---|
| 0 | `"FILE     "` `0x80042E50` | `0x80041E8F` | pick a file of the current project |
| 1 | `"PATTERN  "` `0x80042E6C` | `0x80042BB3` | rhythm pattern → audio |
| 2 | `"LOOP[USB]"` `0x80042E7C` | `0x800425BB` | import a WAV from the USB stick (§2) |
| 3 | `"OTHER PRJ"` `0x80043280` | `0x80040927` | take from another project |

Item record layout (all four are filled the same way):

```
+0x00  label pointer
+0x04  1                       (enabled)
+0x08  0
+0x0C  0x800D38DC              (common handler)
+0x10  callback (Thumb, odd)
+0x14..+0x2C  0  (+0x18 is written as a byte)
+0x30..+0x3C  4 x pointer to an empty string
+0x40..+0x4C  0
```

then a pointer array and a 48-byte menu header:

```
0x80043080  for n in 0..3:  ptr[0x8114691C + track*16 + n*4] = &item[track][n]
0x800430A0  movs r0,#4  -> header[0x8114452C + track*0x30 + 0x00] = 4   (item count)
            header +0x04 = "TAKE SELECT"   +0x14 = 0x801EBE90 (selection var)
            header +0x1C = 0x00010019       +0x20 = &ptr[track][0]
            header +0x24 = 0x80040BF5       (menu callback)
```

So the item count is the immediate in `movs r0,#4` (file `0x43654`), the
pointer array has a 16-byte stride (room for exactly four), and the item records
a 320-byte stride (four × 80). **Adding a fifth item** therefore means changing
those strides or moving the arrays — see §5.

**The three arrays are a shared work area, rebuilt per menu.** The same
`0x811449DC` (items), `0x8114691C` (pointers) and `0x8114452C` (headers) are
filled by other per-track menus with the same code shape:

| builder | title | items | count site |
|---|---|---|---|
| `0x80042C1A` | `TAKE SELECT` | 4 | `0x800430A0` |
| `0x80046C56`… | `TRACK EDIT` | 2 | `0x80046E44` |
| `0x80047186`… | `LOOP SETTING` | 3 | `0x800474E8` |

so each builder runs when its menu is opened, and four items per track is the
work area's capacity. A fifth TAKE SELECT item must not use these arrays with a
larger stride (it would collide with the next track); it needs its own pointer
array (§5).

Patch sites in the TAKE SELECT builder (verified bytes):

| file | RAM | bytes | instruction |
|---|---|---|---|
| `0x43644` | `0x80043090` | `21 01` | `lsls r1,r4,#4` (pointer array stride, write loop) |
| `0x4368E` | `0x800430DA` | `20 01` | `lsls r0,r4,#4` (pointer array stride, header +0x20) |
| `0x43654` | `0x800430A0` | `04 20` | `movs r0,#4` (item count) |
| `0x43848` | `0x80043294` | `1C 69 14 81` | literal `0x8114691C`, used only by the two `lsls` sites above |

## 2. `LOOP[USB]` — `RAM 0x800425BA .. 0x80042AD6` **[PROVEN]**

The callback does not *play* files from the stick. It **copies** the chosen
file from the USB stick into the current project's `AUDIO` folder on the SD
card, then assigns it to the track:

```
0x800425C6  0x80005CA2(0xFFFF)    busy guard (recording, playing, …) -> abort
0x800425D6  0x80026EE4()          "Disable Sync Rec" guard -> abort
0x80042604  0x8005B066(1, 2)      USB PORT -> HOST MODE   ("USB Setup Error")   [USB]
0x8004261A  0x80026EFC()          wait up to 300 polls for a USB device,
                                  "No USB Device" -> skip to cleanup           [USB]
0x8004262A  strcpy(buf, "B:\ZOOM_R24\LOOP")
0x80042636  0x80054F8A(path)      open/stat the folder
loop:
0x8004264A  0x800423C4(buf, sel, path)   "FILE SELECT" browser (sub-folders work)
            -2 -> "No File", -1 -> leave
0x8004267C  sprintf(dst, "A:\ZOOM_R24\PROJECT\%s", project); strcat "\AUDIO\", name
0x800426A2  stat(dst) == exists -> "Already Exist", browse again
0x800426EC  0x800016C0(src, dst)  COPY the file  -> "Error" on failure
cleanup:
0x8004271A  0x8001D2E8('A')       rescan the project folder on the SD card
0x8004271E  0x8005AFC6()          USB port off / back to previous mode          [USB]
0x80042746..                      assign the file to the track (mono/stereo,
                                  44100 / 48000 by the entry's type byte)
```

Only the three calls marked **[USB]** are specific to the stick. The browser
(`0x800423C4`), the open (`0x80054F8A`), the copy (`0x800016C0`) and the rescan
are generic and take a drive letter in the path. The source path is the
literal at **file `0x42F68`** (RAM `0x800429B4`), used by the three `adr` at
`0x8004262A / 0x80042634 / 0x80042644`.

The browser entries are 28-byte records at `[0x811496E8 + 4] + i*0x1C`:
`+0x00` name pointer, `+0x07` channels (1 = mono, 2 = stereo), `+0x08` rate
type (2 → 44100, 3 → 48000), `+0x14` length passed to the track loader.

### 2.1 The second `B:\ZOOM_R24\LOOP` (file `0x281B4`)

Belongs to `USB > STORAGE > INIT` (`0x80027A80`…): it creates
`B:\ZOOM_R24\PROJECT`, `\AUDIO` and `\LOOP` on the stick (`0x8001DBA8`,
"USB Access Error"), then turns USB off (`0x8005AFC6`). It has nothing to do with
the import.

## 3. Why the earlier `LOOP[SD]` build failed on the device **[explained]**

That build only changed the path string to `A:\`. The callback still ran
`0x8005B066` (USB host mode: the USB LED lights) and `0x80026EFC` (waits for a
USB device; without one it gives up and goes to cleanup, so no list is shown).
Exactly what was observed.

## 4. What an SD import needs **[PROVEN by the code, untested on device]**

The same callback with:

| file | RAM | was | becomes |
|---|---|---|---|
| `0x42BB8` | `0x80042604` | `bl 0x8005B066` (USB host mode) | `movs r0,#0 ; nop` |
| `0x42BCE` | `0x8004261A` | `bl 0x80026EFC` (wait for USB) | `movs r0,#0 ; nop` |
| `0x42CD2` | `0x8004271E` | `bl 0x8005AFC6` (USB off) | `nop ; nop` |
| `0x42F68` | `0x800429B4` | `"B:\ZOOM_R24\LOOP"` | `"A:\ZOOM_R24\LOOP"` |

Source and destination are then both on the SD card
(`A:\ZOOM_R24\LOOP\…` → `A:\ZOOM_R24\PROJECT\<project>\AUDIO\…`). Done in place,
this *replaces* `LOOP[USB]`; keeping both needs §5.

**Verified on a device (2026-11-18):** `loop_sd.py --replace` lists and imports
from `A:\ZOOM_R24\LOOP`.

### 4.1 Browsing from the root of the card (`--root`)

The browser (`0x800423C4`) computes the depth as "backslashes in the current
path − backslashes in the start path", enters a folder by appending `"\"` +
name, goes up by cutting at the last `\`; the lister (`0x8000205C`) appends
`"\*.*"` and calls find-first/next (file service `0x800772B4`, op `0x1C`/`0x1D`).
So the root must be written **`A:`** (no trailing backslash), giving `A:\*.*`,
`A:\FOLDER`, … One more change:

| file | RAM | was | becomes |
|---|---|---|---|
| `0x42BEA` | `0x80042636` | `bl 0x80054F8A` (stat of the start folder, op `0x0C`) | `movs r0,#0 ; nop` |
| `0x42F68` | `0x800429B4` | path | `"A:"` + 15 × NUL |

The stat only gates "No File" when the start folder is missing; the FAT root has
no directory entry of its own, so it is skipped rather than relied on.

### 4.2 `LOOP[USB]` from the root of the stick (`--usb-root`)

The same two changes applied to the **original** callback: path `"B:"` + 15 ×
NUL at file `0x42F68`, and the stat at file `0x42BEA` → `movs r0,#0 ; nop`.
The USB host mode and the wait for the stick stay, so it is still a USB import,
just starting at `B:\`. Combine with `--add`: `LOOP[SD]` is a separate copy,
so each item keeps its own start folder.

## 5. Adding a fifth item (`LOOP[SD]` next to `LOOP[USB]`) — design

1. **The code.** A copy of `0x800425B8..0x80042E28` (the callback and both of
   its literal pools; its `ldr`/`adr` targets span `0x8004299C..0x80042E24`) placed in the PCM bank tail, which the boot code copies to
   `0x87540000+` (verified on a device for data, `07_pcm_samples.md` §3). In the
   copy: the patches of §4, and every `bl`/`blx` to firmware code redirected to a
   12-byte veneer (`bx pc; nop; ldr pc,[pc,#-4]; .word target`), because the
   firmware is ~120 MB away, beyond Thumb `bl` range (±4 MB).
2. **The item.** An 80-byte record like `LOOP[USB]`'s, label `"LOOP[SD] "`,
   callback = the copy, also in the bank tail.
3. **The menu.** Same-size patches in the TAKE SELECT builder only: item count
   `#4 → #5`, pointer-array stride `lsls #4 → #5` (both sites) and the literal
   `0x8114691C → ` a new 24 × 32-byte pointer array in the bank tail. The
   builder still writes items 0–3 into the shared work area and their pointers
   into the new array; slot 4 of every row is pre-filled in the image with the
   new item, and the builder never touches it. TRACK EDIT and LOOP SETTING keep
   using the original array (they have their own literal words).

Built by `tools/loop_sd.py --add` (§6). **Verified on a device (2026-11-18):**
`build/loop/R24SYS_160_LOOPSD_ROOT.BIN` shows five TAKE SELECT items and
`LOOP[SD]` browses and imports from the card root — which also confirms that
code placed in the PCM bank tail (RAM `0x8754xxxx`) executes. The two points
that were open before that test:

* **executing from `0x8754xxxx`** — the bank tail is ordinary RAM written by the
  boot `memcpy` (data there is verified on a device); the ARM926 has no
  execute-never bit, so a readable mapping is executable. The one theoretical
  risk is a stale I-fetch if the boot copy's last lines were still dirty in the
  D-cache when the menu is first used — very unlikely after the rest of boot;
* **the menu drawing five items** — the count is data the menu code reads, but
  whether the TAKE SELECT screen scrolls correctly with five, and whether any
  code compares the selection against 3, can only be seen on the device.

If either fails the symptom is at worst a crash when opening TAKE SELECT or
choosing `LOOP[SD]`; reflash the previous image.

## 6. Tools

```bash
# in place: LOOP[USB] becomes LOOP[SD]  (4 patches + label, no new code)
python3 tools/loop_sd.py R24SYS.BIN --replace -o build/loop/R24SYS_LOOPSD_REPLACE.BIN

# fifth item: LOOP[USB] stays, LOOP[SD] is added (needs/creates the relocated table)
python3 tools/loop_sd.py build/R24SYS_160.BIN --add -o build/loop/R24SYS_160_LOOPSD.BIN

# LOOP[USB] from the stick root as well (§4.2) — five items, both from root
python3 tools/loop_sd.py build/R24SYS_160.BIN --add --root --usb-root \
        -o build/loop/R24SYS_160_LOOPSD_ROOT_USBROOT.BIN

# either mode, browsing from the root of the SD card (§4.1)
python3 tools/loop_sd.py R24SYS.BIN --replace --root -o build/loop/R24SYS_LOOPSD_ROOT.BIN
python3 tools/loop_sd.py build/R24SYS_160.BIN --add --root -o build/loop/R24SYS_160_LOOPSD_ROOT.BIN

python3 tools/loop_sd.py IMAGE --check      # what is installed
python3 tools/test_loop_sd.py               # 33 checks
```

`--add` reserves an **extension area** after the relocated sample table
(`r24bank.py`: header `R24EXT01`, preserved by `patch_samples.py --repack` and
`r24bank.py relocate`). It holds the callback copy (2,160 bytes), 38 long-call
veneers for its 62 outgoing calls, the item record and the 24 × 32 pointer
array — 3,516 bytes, taken from trailing silence of the last sample.
`test_loop_sd.py` disassembles the copy at its new address and checks it
instruction by instruction against the original.

Using it: put WAVs in `ZOOM_R24\LOOP` on the SD card (sub-folders work), open a
track's TAKE SELECT, choose `LOOP[SD]`. The file is copied into the project's
`AUDIO` folder exactly as `LOOP[USB]` does from the stick.

`tools/loop_import.py` predates this analysis: its `--sd-import` / `--usb-root`
modes only rewrite the path strings and **do not produce a working import**
(§3). It is kept for inspection (`--list-sites`).

## 7. Status

| item | status |
|---|---|
| code runs at `file − 0x5B4 + 0x80000000` | PROVEN (boot copy) |
| TAKE SELECT built in RAM, 4 items × 24 tracks | PROVEN |
| `LOOP[USB]` copies USB → project `AUDIO` on SD | PROVEN |
| the three USB-specific calls | PROVEN (host mode / wait / off) |
| sub-folder browsing | hardware-confirmed (stock firmware) |
| path-only `LOOP[SD]` build | fails on device; cause explained (§3) |
| in-place SD import (§4) | **works on device** (`loop_sd.py --replace`, 2026-11-18) |
| browse from card root (§4.1) | **works on device** (with `--add`) |
| `LOOP[USB]` from stick root (§4.2) | built (`--usb-root`), untested on device |
| fifth item (§5) | **works on device** (`R24SYS_160_LOOPSD_ROOT.BIN`, 2026-11-18). The first `--add` build of 2026-11-18 (sha256 `e33283f2…`) had a bug: the veneer pass re-routed the 3 neutralised USB calls, so its `LOOP[SD]` still behaved like USB. Fixed; `test_loop_sd.py` now checks the copy's call sites |

## 8. Corrections to the previous version

| previous claim | actual |
|---|---|
| TAKE menu is a 12-entry table of 6-byte records at `0x43404` | that is a literal pool; the menu is built in RAM (§1) |
| `LOOP[USB]` label is unreferenced, pointer "derived from the record address" | loaded by `adr` at `0x80042DEE` |
| the loop lister reads loops "into the sampler" | it copies the file into the project `AUDIO` folder (§2) |
| `0x80006256` drives the USB LED; `0x800274B0` is the USB wait | wrong address space: those are *file* offsets `0x6256`/`0x274B0` (RAM `0x80005CA2`, a busy guard, and `0x80026EFC`, the USB wait); the LED comes from `0x8005B066` |
| the second path copy is the "Create Folder?" gate | it is `USB > STORAGE > INIT` (§2.1) |
| the `0xFF` gap is an allocated overlay (617 pointers) | it is never loaded into RAM (§0) |
| a working SD import needs new code with nowhere to put it | in place it needs four same-size patches (§4); a fifth item can live in the bank tail (§5) |

## 9. Slow folders in the browser — `sdk/patches/fast_browse.c` **[PROVEN by code + emulator; works on device 2026-11-19]**

Entering a folder with many files left the browser on "waiting" for seconds.

**Cause.** `fw_browse` (`0x800423C4`) lists through `0x80041DF2`
(`fw_browse_select`: list + menu) with flags `4` (`5` when deeper than 7
levels: bit 0 = no sub-folders). Flag 4 makes the lister (`0x8000205C`) end
with `0x80001D96`, which for **every** entry (files and folders) calls
`0x80001D4C` → `0x80001BEE`: open `dir\name` as a WAV (`0x80058F38`, header
read), stat it (`0x80054F8A`, a second directory lookup), close. N files =
N header reads at random places on the card + ~2N directory searches, each
longer the bigger the folder. The find-first/next pass and the selection sort
(`0x80001E46`, in RAM) are cheap by comparison.

The 28-byte records it fills are read for one entry only:

| reader | when | fields |
|---|---|---|
| row text `0x80041D72` | drawing the list | `+0x00` name, `+0x18 == 2` (folder icon) |
| event callback `0x80041B94`, event `0x10033` | preview of the highlighted entry | `+0x18`, `+0x08` rate type, name |
| `fw_browse` `0x800424CC..` | entry chosen | `+0x18` (enter folder), `+0x08` (rate vs project) |
| LOOP callback `0x80042746..` | after the browser | `+0x07`, `+0x08`, `+0x14` |

**Patch.** `fast_browse.c` replaces `0x80041DF2`. For the browser's flags
(4/5) it lists without flag 4, fills each record with just the name pointer
and the folder mark (files are entries `[0, files)`, folders
`[files, count)` after the lister's compaction), wraps the event callback so
a preview first fills the highlighted record, and fills the chosen record
before returning. Result: entering a folder = one directory pass + the sort;
one file is opened, the chosen (or previewed) one. Also affects `LOOP[USB]`
and the third `fw_browse` caller (`0x8002733A`). The other caller of
`0x80041DF2` (`0x80041E8E`, flags `0x1D`/`0x2D` with the rate filter) runs
exactly as the original.

The SDK trampoline goes through r3, so the 4th argument (`noback`) does not
arrive: it is derived from the flags (browser 4/5 → 1, `0x80041E8E` → 0),
and the three call sites are pinned by `R24_CHECK`.

`tools/test_fast_browse.py` (49 checks) runs the real lister in the emulator
on the stock and built images over a fake folder (long and 8.3 names, `.`/`..`,
hidden and non-WAV files, sub-folders): same names, order, folder marks, menu
record and results; stock opens every entry, built only the chosen/previewed
one, with an identical record; flags `0x1D`/`0x2D` identical incl. every open.
