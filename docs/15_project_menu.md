# 15 — PROJECT menu: IMPORT SD, IMPORT USB, CLEAR UNUSED

> Added 2026-11-19. SDK patch `sdk/patches/project_menu.c` (was
> `import_folder.c`). Proven in the emulator (`tools/test_project_menu.py`,
> 55 checks). **IMPORT SD (then "IMPORT FOLDER") verified on a device**
> (2026-11-19, image `e6f577e2…`); IMPORT USB and CLEAR UNUSED **verified on a device** (2026-11-19, `build/R24SYS.BIN` sha256 `a4f1c0a2…`).

Menu: … FILE, **IMPORT SD**, **IMPORT USB**, **CLEAR UNUSED**, REC, SEQ PLAY
(count 13). Only with `build_firmware.py --dump`: **DUMP 1** and **DUMP 2** after CLEAR
UNUSED (count 14, `mem_dump.c`, `17_dsp_and_dump.md` §5).

## IMPORT SD / IMPORT USB

## Use

**IMPORT SD** opens the LOOP[SD] browser at the card root (`A:`), **IMPORT
USB** at the stick root (`B:`). Go into the folder and choose **any WAV in it**; a screen shows the
folder and how many WAVs it holds:

```
IMPORT SD
FOLDER    DRUMS
FILES     12 WAV
ENTER:OK EXIT:BACK
```

ENTER copies them into the project's pool, `PROJECT\<prj>\AUDIO`
(`Importing 3/12`), then `Imported 9 Skip 3`. They show in FILE and in the
KIT SELECT > CUSTOM editor at once.

* Which files: the WAVs FILE would show for this project — the firmware lister
  with FILE SELECT's flags (`0x13` 44.1 kHz / `0x23` 48 kHz project).
* A name already in `AUDIO` is skipped (never overwritten) and counted in Skip.
* Refused while the recorder is busy (`busy_guard(9)`, as FILE COPY) or when
  the project is protected.

IMPORT USB runs LOOP[USB]'s own sequence (`0x800425BA`) around it, call for
call: `busy_guard(0xFFFF)`, sync-rec guard, `audio_mode(0)`, wait box, save the
project if open, close audio files, **USB host mode** (`0x8005B066(1,2)`; error →
"USB Setup Error", as LOOP[USB]), **wait for the stick** (`0x80026EFC`; none →
straight to cleanup), browse/copy, then cleanup: "Please Wait...", rescan `A:`,
**USB off**, track state reload, `audio_mode(settings+0x34)`, project flag
back. The browser starting at `B:` (not `B:\ZOOM_R24\LOOP`) is untested on a
device (`10_loop_import.md` §4.2).

## CLEAR UNUSED

Lists `AUDIO` with the lister flags `0x03` (all WAVs; bit `0x2` fills the
file → take map `0x804AB080`, the `*` FILE shows for files on a track) and
warns:

```
CLEAR UNUSED
DELETE    7 WAV
NOT USED BY TRACKS
CANNOT BE UNDONE!
ENTER:OK EXIT:BACK
```

ENTER deletes exactly the files whose map entry is −1 — what FILE DELETE does
for an unused file: `0x80020712()` then `remove()`, and the FILE selection is
cleared. Files on a track, other files (non-WAV) and folders are kept.
Message: `Deleted 7` (`Error n` if a delete failed).

## How

The PROJECT menu header is data at `0x800D44DC` (count 10; item pointer array
at `+0x20` → `0x800D44B4`), opened by `0x80019610` → `0x8003A776(header)` (the
engine of FILE and TAKE SELECT).

| site | change |
|---|---|
| `0x800D44DC` | count `10` → `13` (`14` with `--dump`) |
| `0x800D44FC` | `0x800D44B4` → `pm_items`: the 10 originals with the 3 new items after FILE (+ DUMP 1, DUMP 2 with `--dump`) |
| `0x800D44B4`, FILE record `0x800D43C4` | checked only |

Browser: `0x800423C4(cur, full, "A:")` — LOOP's (`10_loop_import.md` §2,
§4.1), verified on a device with LOOP[SD]; `full` is the chosen file's path,
its folder is what gets listed. Copy: `0x800016C0`, one file at a time, with
the firmware's progress box.
