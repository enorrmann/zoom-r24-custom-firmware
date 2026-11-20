# R24 C SDK — firmware patches in C

Change what the Zoom R24 (firmware v1.13) does by writing C, without
rewriting the firmware: the original code stays as it is, and only the
functions you choose are redirected to your code.

```
sdk/
  firmware.sym        firmware functions/variables: name, RAM address, kind, check bytes
  include/r24fw.h     their C prototypes (fw_fopen, fw_sprintf, fw_kit_ptr, ...)
  include/r24.h       hook macros (R24_REPLACE, R24_POINTER, R24_BYTES, R24_KIT) + helpers
  runtime/runtime.c   section header, memcpy/memset, kit/table helpers (always linked)
  patches/            real patches: custom_kit.c (per-project drum kit),
                      kit_assign.c (FILE > KIT ASSIGN; needs custom_kit.c),
                      kit_editor.c (KIT SELECT > CUSTOM editor; needs both),
                      project_menu.c (PROJECT > IMPORT SD/USB, CLEAR UNUSED,
                      DUMP MEMORY; needs mem_dump.c: flash/DSP dump to A:\DUMP),
                      drum_pitch.c (per-voice drum pitch, docs/19_drum_pitch.md),
                      fast_browse.c (LOOP browser without per-file header
                      reads, docs/10_loop_import.md §9),
                      step_pitch.c (per-step pitch in PATTERN EDIT,
                      docs/21_step_pitch.md)
  tests/selftest.c    exercises every mechanism (not for flashing)
tools/
  r24sdk.py           build / check / syms
  r24emu.py           run firmware + patches in an ARM926 emulator (unicorn)
  test_r24sdk.py      SDK proof;  test_custom_kit.py: custom_kit.c proof
```

Requirements: `arm-none-eabi-gcc` (Debian: `apt install gcc-arm-none-eabi`),
Python 3, and `pip install unicorn` for the tests.

## 1. Build

```bash
python3 tools/r24sdk.py syms                       # firmware.sym vs R24SYS.BIN
python3 tools/r24sdk.py build R24SYS.BIN sdk/patches/custom_kit.c mine.c \
        -D CUSTOM_KIT_RESERVE_SEC=6 -o build/R24SYS_MINE.BIN
python3 tools/r24sdk.py check build/R24SYS_MINE.BIN
```

All patches of an image go into **one** build (one section). To change the
set, rebuild from an image without an SDK build. `LOOP[SD]`
(`tools/loop_sd.py --add`) is independent and can be applied before or after.

A build also writes `OUT.syms.json`: the RAM address of every symbol of your
code — what you need to read a variable in the emulator or a RAM dump.

## 2. Writing a patch

```c
#include "r24.h"

/* 1. Replace a firmware function. Give >= 10 of its original bytes (8 if the
 *    address is a multiple of 4): they are checked, then overwritten with a
 *    long jump to your function. Same arguments, same return value. */
int my_set_current_project(int n);
R24_REPLACE(0x8001D6A6, "70b50400e34804602000", my_set_current_project);

int my_set_current_project(int n)
{
    /* the original cannot be called any more: redo what you want to keep */
    fw_cur_project = n;
    fw_strcpy(fw_cur_project_name, fw_project_name(n));
    return fw_save_sys();
}

/* 2. Repoint a callback / table pointer stored in data (old value checked). */
R24_POINTER(0x800FD3A4, 0x8004B69B, my_kit_getter);

/* 3. Raw bytes, same length: an immediate, a branch, a string. */
R24_BYTES(0x80042E7C, "4c4f4f505b5553425d", "4c4f4f505b5344205d");  /* LOOP[SD ] */

/* 4. Assert data you rely on but do not change. */
R24_CHECK(0x800EA174, "949f0e80e49f0e80");      /* FILE menu: INFO, COPY */

/* 5. A new drum kit in KIT SELECT (16 table entries of its own). */
R24_KIT("MYKIT");
```

Rules of thumb:

* **Addresses are RAM** (`0x80xxxxxx`), as in the docs and the disassembly
  (`file = RAM − 0x80000000 + 0x5B4`).
* **REPLACE overwrites the first 8–10 bytes** of the function. Make sure the
  function is at least that long (look at the disassembly) and that nothing
  jumps into those bytes.
* **Calling firmware**: anything in `r24fw.h`. Calls go through registers
  (`-mlong-calls`), Thumb/ARM interworking is automatic.
* **Not available**: the C library (use `fw_sprintf`, `fw_malloc`, …),
  floating-point hardware (soft-float is fine, just slow), static
  constructors. Integer division works (libgcc is linked).
* **Memory**: globals, `static` buffers and string literals live in your
  section in RAM; a large static buffer (like the custom kit's reserve)
  enlarges the section, and room is made by capping the longest samples of
  the bank (`--no-trim` to refuse). Keep stack use small (a few hundred
  bytes): your code runs on the stack of the firmware task that called it.
* **Heap**: `fw_malloc` is a 1 MiB RTOS pool shared with the firmware
  (FILE COPY uses 320 KiB): allocate tens of KiB at most and free them. RAM
  map: `docs/18_dump_analysis.md` §5.
* **The DSP reads RAM directly**: after writing audio it will play, call
  `fw_dcache_clean(ptr, len)`.
* **Kits and the sample table may move** (tools relocate them): use
  `r24_sample_table()`, `r24_kit(k)`, `r24_find_kit("NAME")`, never fixed
  addresses.

## 3. Testing in the emulator before flashing

```python
import json, r24emu
img = open("build/R24SYS_MINE.BIN", "rb").read()
sym = json.load(open("build/R24SYS_MINE.BIN.syms.json"))["symbols"]
emu = r24emu.R24Emu(img)                       # SD card = emu.files dict
emu.files["A:\\ZOOM_R24\\PROJECT\\PRJ001\\KIT\\SAMPLE01.WAV"] = open("k.wav", "rb").read()
emu.stub(0x8001D212, lambda e, r: e.alloc(b"PRJ001\0"))   # replace firmware calls
print(emu.call(0x8001D6A6, 1))                 # call through the patched firmware
print(emu.u32(sym["cust_status"]))
```

The emulator runs the real firmware code; stub whatever touches hardware
(LCD, codec, USB) or needs a mounted card. `tools/test_custom_kit.py` is a
complete example.

### UI from C

**The FILE menu and KIT SELECT are graphic screens**: draw with `fw_gfx_*`
inside `fw_gfx_lock()/unlock()` and follow FILE COPY's enter/exit sequence
(`r24fw.h`, `docs/13_kit_assign.md` §3; `kit_editor.c` is a full example).
`fw_lcd_text`/`fw_confirm` only work on the older text-mode screens — on a
graphic screen they draw nothing (the first KIT ASSIGN "hang").

Text-mode screens have two lines; dialogs are a loop on `fw_wait_event(-1)`:

```c
fw_lcd_text("TITLE", 0, 0);
for (;;) {
    fw_lcd_text(line, 1, 0);
    int ev = fw_wait_event(-1);
    if (ev == R24_EV_ENTER) break;           /* EXIT: R24_EV_EXIT */
    if (ev == R24_EV_DIAL) value += fw_dial_delta();
    if (R24_ARROW(ev) == R24_EV_UP) row--;   /* also R24_EV_DOWN/LEFT/RIGHT */
}
if (fw_confirm("PAD04", "Assign?")) ...
```

`sdk/patches/kit_assign.c` is a complete example, including adding an item to
an existing menu (`docs/13_kit_assign.md`).

## 4. Adding a firmware function to the SDK

1. Find and understand it (Ghidra, `tools/r24tool disasm`, the docs; who calls
   it, with which arguments, what it returns).
2. Add a line to `sdk/firmware.sym`: name, RAM address, `thumb`/`arm`/`data`,
   its first bytes as check (or `-` for data that tools change), and the
   evidence after `#`.
3. Add the prototype to `sdk/include/r24fw.h`.
4. `python3 tools/r24sdk.py syms` and `python3 tools/test_r24sdk.py`.

## 5. Status

| | |
|---|---|
| build pipeline, hooks, refusal paths | proven by `test_r24sdk.py` (181 checks) |
| patches executed in the emulator, incl. Thumb→ARM firmware calls | proven |
| code in the bank tail executing on a device | verified (LOOP[SD], 2026-11-18) |
| an SDK build on a device | **not yet** — first candidate: `custom_kit.c` |
