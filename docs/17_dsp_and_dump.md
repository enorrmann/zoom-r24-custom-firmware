# 17 — The DSP program, the NOR flash, and PROJECT > DUMP MEMORY

> Added 2026-11-19. Corrects `02_analysis.md` §4.2–4.3 and `05_limitations.md`
> §3: the DSP program **is** inside `R24SYS.BIN`, and DSP/BIOS runs on the DSP,
> not on the ARM. SDK patch `sdk/patches/mem_dump.c` (+ the menu item in
> `project_menu.c`), proven in the emulator (`tools/test_project_menu.py`).
> **Verified on a device** (2026-11-19): all eight files written; analysis in
> `18_dump_analysis.md`.

## 1. The chip: TI DM644x, ARM926 + C64x+ DSP **[PROVEN: JTAGID, 18_dump_analysis.md]**

The address map the firmware uses is DaVinci's (DM644x family):

| Address | What | Evidence |
|---|---|---|
| `0x01C40000` | system module; `+0x08` DSPBOOTADDR, `+0x28` JTAGID | DSP boot below; crt0 writes `+0x00` |
| `0x01C41000` | PSC (power/sleep controller); MDCTL39 = `+0xA9C` = the DSP (GEM) | DSP boot below |
| `0x01C67000` | GPIO | crt0 |
| `0x02000000` | EMIF CS2: **NOR flash** (AMD/CFI, 64 KiB sectors) | §4 |
| `0x11800000` | DSP L2 RAM (64 KiB) | DSP boot address, section 9 |
| `0x11F04000–0x11F0FFFF` | DSP L1D RAM | ~1,700 literal references from the ARM |
| `0x80000000–0x87FFFFFF` | DDR (128 MiB) | MMU table, `.data` copy |

The exception strings that earlier docs used to call the DSP a "C674x"
(`IERR`, "Fetch packet", "Loop buffer", "Missed stall") are the C64x+ set too
(the C674x inherits it); the DaVinci memory map says C64x+. Either way the
instruction set is the same for our purposes.

## 2. How the ARM boots the DSP **[PROVEN by code]**

Main init (`0x800081E4`) calls, right after the PCM bank copy (`0x8000827E`):

```
0x80008288  bl fw_dsp_boot (0x8006C868)
  PSC: wait PTSTAT, power domain 1 on, MDCTL39 = ENABLE with local reset held
       (bit 8 = 0), PTCMD, wait
  0x01C40008 (DSPBOOTADDR) = 0x23 << 23 = 0x11800000
  fw_dsp_load (0x8006C838): 14 x memcpy(dst, src, len) from fw_dsp_sections
  MDCTL39 |= 0x100      -> local reset released, the DSP starts at 0x11800000
```

`fw_dsp_sections` (`0x801EDB24`, 14 × `{src, dst, len}`) — the whole DSP
program, 115,655 bytes, stored in the ARM's `.data` at file `0x1C83E0..0x1E47A7`:

| # | src (RAM) | file | dst | len | what |
|---|---|---|---|---|---|
| 0 | `0x801C7E2C` | `0x1C83E0` | `0x11F0E1E8` | `0x40` | L1D data |
| 1 | `0x801C7E6C` | `0x1C8420` | `0x11F0DB58` | `0x200` | L1D data |
| 2 | `0x801C806C` | `0x1C8620` | `0x87B1D900` | `0x38E0` | DDR |
| 3 | `0x801CB94C` | `0x1CBF00` | `0x11F0E250` | `0xC` | L1D data |
| 4 | `0x801CB958` | `0x1CBF0C` | `0x87B248C0` | `0x4A0` | DDR; holds the reset target `0x87B24B80` (`c_int00`) |
| 5 | `0x801CBDF8` | `0x1CC3AC` | `0x11800200` | `0x6E00` | **L2 code** (fast path) |
| 6 | `0x801D2BF8` | `0x1D31AC` | `0x87B211E0` | `0x36D4` | DDR |
| 7 | `0x801D62CC` | `0x1D6880` | `0x11F0E190` | `0x58` | L1D data |
| 8 | `0x801D6324` | `0x1D68D8` | `0x11F0AEDC` | `0x4` | L1D data |
| 9 | `0x801D6328` | `0x1D68DC` | `0x11800000` | `0x200` | **interrupt vector table** |
| 10 | `0x801D6528` | `0x1D6ADC` | `0x11F0E228` | `0x18` | L1D data |
| 11 | `0x801D6540` | `0x1D6AF4` | `0x87B24D60` | `0x3B3` | DDR `.const`: the DSP/BIOS strings |
| 12 | `0x801D68F3` | `0x1D6EA7` | `0x87B19960` | `0x3FA0` | DDR |
| 13 | `0x801DA893` | `0x1DAE47` | `0x87B10000` | `0x9960` | DDR |

Evidence that this is C6000 code: section 9 disassembles cleanly as C64x
(capstone `CS_ARCH_TMS320C64X`): `mvk/mvklh 0x87B24B80, b0 ; b b0 ; ... mvc
b0, ier` — the standard DSP/BIOS reset vector. Section 5 starts with `mvk/mvklh
0x11F0xxxx` loads of L1D addresses. capstone stops at the C64x+ *compact*
(16-bit, header-based) fetch packets, so the big sections need TI's `dis6x`
(free C6000 code generation tools) to read in full — that is why the old
"C6x decode finds no code" test failed.

The strings `EXC_exceptionHandler`, `SYS abort called with message`, the IERR
list and `***LOCK NOT CALLED IN TSK CONTEXT` sit in section 11 (file
`0x1D6D95..0x1D6E80` → `0x87B25001..`): **DSP/BIOS is the DSP's OS.** The ARM's
own RTOS (`initial task`, `stack pool`, `PUH_OTG_Int_task`) is not identified.

The ARM↔DSP interface is the DSP's L1D RAM (`0x11F04000–0x11F0E25C`), which
the ARM reads and writes directly (e.g. the drum voice `start`/`end` mailbox at
`0x11F07970`, `12_custom_kit.md`). DDR `0x87B00000–0x87FFFFFF` (5 MiB) is
mapped **uncached** for the ARM; the DSP program uses its first ~85 KiB, the
rest is presumably DSP working memory (delay lines, buffers).

### What this means for changing the DSP

* **The normal updater flashes the DSP too.** Changing the DSP is patching
  bytes in that file range and re-sealing — the same path as every other
  patch here. No separate DSP updater exists or is needed.
* Every section is copied at boot, so a different DSP program could also be
  loaded **from the SD card** by an ARM patch on `fw_dsp_load` (fallback to
  the built-in sections if the file is missing) — the safe way to iterate.
* What is missing is tooling: TI C6000 CGT (`cl6x`, `dis6x`) and a map of the
  L1D interface. Nothing on the DSP side has been changed yet.

## 3. The MMU table **[PROVEN by code]**

crt0 (`0x02080038`) enables the MMU with TTB = `0x800C0000`, a static
4096-entry section table in `.data`; DACR = 1 (domain 0 client, AP checked),
SCTLR `0x5107D` (S = R = 0, so AP = 0 means no access):

| VA = PA | AP | cache |
|---|---|---|
| `0x00000000–0x000FFFFF` | rw | – |
| `0x00100000–0x01AFFFFF` | **none** | |
| `0x01B00000–0x09FFFFFF` | rw | – (peripherals, EMIF CS2–CS5: NOR) |
| `0x0A000000–0x0BFFFFFF` | **none** | |
| `0x0C000000–0x118FFFFF` | rw | – (incl. DSP L2 `0x11800000`) |
| `0x11900000–0x11DFFFFF` | **none** | |
| `0x11E00000–0x11FFFFFF` | rw | – (DSP L1P, L1D) |
| `0x12000000–0x1FFFFFFF` | **none** | |
| `0x20000000–0x200FFFFF` | rw | – (DDR controller) |
| `0x20100000–0x7FFFFFFF` | **none** | |
| `0x80000000–0x82FFFFFF` | rw | write-back |
| `0x83000000–0x830FFFFF` | rw | – |
| `0x83100000–0x87AFFFFF` | rw | write-back (PCM bank `0x87540000`) |
| `0x87B00000–0x87FFFFFF` | rw | – (the DSP's DDR) |
| `0x88000000–0xFFFFFFFF` | **none** | |

## 4. The NOR flash and the in-firmware updater **[PROVEN by code]**

`R24SYS.BIN`'s payload is linked at `0x02080000` because that is where it sits
in the NOR flash. The main firmware itself contains a version-up path (Test
Mode: "Enter Test Mode?", `0x80022AE8`), `fw_flash_update` (`0x8002269A`):

| kind | file | flash address |
|---|---|---|
| 0 | `A:\D211MAIN.BIN` (skip 0x40 header) | `0x02080000` |
| 1 | `A:\D211BOOT.BIN` | **`0x02000000`** — the boot loader |
| – | `A:\D211Flsh.bin`, `A:\D211BtFl.bin` | "VERSION-UP MAIN/BOOT" variants |

Erase/program are AMD/CFI command sequences (`0x555/0x2AA`, 64 KiB sectors at
`0x02000000 + (n << 16)`): `fw_nor_erase_sector` `0x8005AB2E`,
`fw_nor_program` `0x8005ABA6`. **Never call these from a patch**: erasing
`0x02000000–0x0207FFFF` destroys the boot loader, which is the only recovery
path. The boot loader is therefore 512 KiB at `0x02000000`, readable as plain
memory — which is what DUMP MEMORY copies.

## 5. PROJECT > DUMP MEMORY

**Not in the default image** (diagnostic). Build an image with it:

```bash
python3 tools/build_firmware.py --dump -o build/dumptest/R24SYS.BIN
```

(copy it to the card as is; same name as the normal image). Without `--dump` the PROJECT menu has
13 items and no dump code (`-D PM_DUMP` off, `mem_dump.c` not linked).

Menu: … FILE, IMPORT SD, IMPORT USB, CLEAR UNUSED, **DUMP 1**, **DUMP 2**, REC,
SEQ PLAY.

> 2026-11-20: DUMP MEMORY became two items, **DUMP 1** → `A:\DUMP1` and
> **DUMP 2** → `A:\DUMP2`, so two states can be dumped without reflashing
> (power-on, then after heavy use) and compared with
> `python3 tools/r24dump.py DUMP1 --diff DUMP2`. `+DDR 128MB` is now **ON** by
> default (dial = OFF). The screen below shows the old single item; the
> files are the same, in the chosen folder.

```
DUMP MEMORY
WRITE TO  A:\DUMP
FLASH+DSP 21MB
+DDR 128MB OFF        <- turn the dial: ON / OFF (OFF every time it opens)
ENTER:OK EXIT:BACK
```

ENTER writes, in this order (progress `NOR 3/16M`), then `Dump Done`:

| file | address | size | |
|---|---|---|---|
| `INFO.TXT` | – | – | CP15 ID, JTAGID, DSPBOOTADDR, this table |
| `DSP_DDR.BIN` | `0x87B00000` | 5 MiB | DSP program + working memory, live |
| `NOR.BIN` | `0x02000000` | 16 MiB | boot loader + firmware; if the chip is 8 MiB the second half repeats the first |
| `DSP_L2.BIN` | `0x11800000` | 64 KiB | vectors + L2 code, live |
| `DSP_L1D.BIN` | `0x11F04000` | 48 KiB | the ARM↔DSP area, live |
| `SYSMOD.BIN` | `0x01C40000` | 256 B | system module registers |
| `DSP_L1P.BIN` | `0x11E08000` | 32 KiB | never read by the firmware: last |
| `ARM_LOW.BIN` | `0x00000000` | 48 KiB | ARM internal RAM/ROM: last |
| `DDR.BIN` | `0x80000000` | 128 MiB | **only with +DDR ON** (added 2026-11-19, not yet run on a device): the whole DDR, for the RAM map (`18_dump_analysis.md` §5) |

Copy them to the PC with **USB STORAGE** or a card reader. Errors: `Card Full`,
`File Access Error`, `No Memory`.

* **Read only.** Plain 32-bit loads into a 64 KiB `fw_malloc` buffer, then
  `fwrite`. Nothing is written to the flash, the DSP or any register.
* The regions the firmware itself reads come first; `DSP_L1P` and `ARM_LOW` —
  the two it never touches — come last. If one of those causes a bus error the
  unit hangs, but every earlier file is already closed: power off/on.
* Refused while the recorder is busy (`busy_guard(9)`). The DSP keeps running
  during the dump, so the live regions are a snapshot in motion.

### What to look at first

1. `NOR.BIN[0:0x80000]` — the boot loader: what the SD updater checks
   (length, version, checksum — `02_analysis.md` §5 is open on this) and
   whether it can recover a bad main image.
2. `NOR.BIN[0x80000:]` against `R24SYS.BIN[0x40:]` — confirms how the image is
   stored.
3. `DSP_L2.BIN` / `DSP_DDR.BIN` against the section table — confirms the DSP
   runs what the image holds, and shows its live data.
4. `INFO.TXT` JTAGID — the exact part number.

## 6. Proof

`tools/test_project_menu.py` (`test_mem_dump`): the menu has 14 items with
DUMP MEMORY's callback; DDR OFF by default, the dial turns it ON and `DDR.BIN`
(128 MiB, last, listed in INFO.TXT) then matches the emulator's DDR; EXIT writes nothing; ENTER writes all eight files with
every region byte for byte, in the order above, `INFO.TXT` contents, one 64 KiB
buffer allocated and freed, `A:` rescanned, per-MiB progress; busy guard.
