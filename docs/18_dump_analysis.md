# 18 — What the memory dump shows: boot loader, flash layout, update rules, live DSP

> Added 2026-11-19, from the first PROJECT > DUMP MEMORY run on a device
> (`DUMP/`, image `build/R24SYS.BIN` sha256 `610b68c5…`). Every number below is
> checked by `python3 tools/r24dump.py DUMP/`; code addresses come from
> disassembling `NOR.BIN`. DUMP MEMORY itself (`17_dsp_and_dump.md` §5) is
> **verified on a device**: all eight files written, including the two "risky"
> regions (DSP L1P, ARM low memory) — no bus error.

## 1. The chip **[PROVEN]**

| | value | meaning |
|---|---|---|
| CP15 main ID | `0x41069265` | ARM926EJ-S, r0p5 |
| JTAGID (`0x01C40028`) | `0x1B70002F` | TI (`0x017`), part `0xB700` = **DM644x** (DM6446 family), variant 1 |
| DSPBOOTADDR | `0x11800000` | as written by `fw_dsp_boot` |

SYSMOD (system module, mirrored every 0x80): PINMUX0 `0x00000C1F` (the value
both crt0s write), PINMUX1 `0x00040494`, `+0x14` (boot config latch)
`0x7F`; the rest is not decoded here.

ARM low memory (`ARM_LOW.BIN`): `0x0000–0x3FFF` is 16 KiB of internal RAM
holding the **ARM exception vectors** (`b 0x4000`, then `ldr pc,[pc,#0x18]` ×7),
mirrored at `0x8000–0xBFFF` — which is why the main firmware's reset code copies
its 0x40-byte vector block to `0x8000` (`07_pcm_samples.md` §2.3).
`0x4000–0x5FFF` is 8 KiB of TI boot ROM, mirrored at `0x6000`.

## 2. The NOR flash: 8 MiB, layout **[PROVEN]**

`NOR.BIN` (16 MiB window) repeats after 8 MiB: the chip is **8 MiB**, AMD/CFI,
64 KiB sectors. Above `0x027FFFFF` the same chip answers again — so an address
`0x02800000` *is* `0x02000000`.

| flash | sectors | content |
|---|---|---|
| `0x02000000–0x02057BFB` | 0–5 | **boot loader "D211 BOOT 1.00"** (§3) |
| `0x02060000–0x0207FFFF` | 6–7 | erased |
| `0x02080000–0x02756633` | 8–0x75 | **main image** = `R24SYS.BIN[0x40:]`, byte for byte (no header) |
| `0x02760000–0x0279FFFF` | 0x76–0x79 | erased — room the main image may grow into |
| `0x027A0000` | 0x7A | **boot flag**: `"ZOOM/R16"` (8 bytes), rest erased |
| `0x027B0000` | 0x7B | version record BOOT: `"1.00"`, stamp `0x4BE3E08A` (2010-05-07) |
| `0x027C0000` | 0x7C | version record SYSTEM: `"1.13"`, stamp `0x5CD55628` (2019-05-10) |
| `0x027D0000` | 0x7D | record 2: 64 bytes of small integers (not identified) |
| `0x027E0000–0x027FFFFF` | 0x7E–0x7F | 128 KiB **effect patch memory** (names as the factory table at image file `0xD5918`, values differ) [INFERRED] |

The flashed main image is our `build/R24SYS.BIN` exactly, so **the updater
stores the payload unchanged at `0x02080000`** — confirmed, not inferred.

Version records are 12 bytes `{char ver[8]; u32 stamp}` at `0x027B0000 +
(kind << 16)`. The stamp is a build time (Unix seconds) compiled into each
program (`0x80056EE4` in the boot loader, `0x801EC50C` in the main image).
The main firmware rewrites its own record at boot when it differs
(`0x800082E6` → `0x80023A4C` compare, `0x80023A78` erase + write); the boot
loader reads them for its `SYSTEM %s->%s` screen (`0x8000421C`).

## 3. The boot loader **[PROVEN by code]**

Same build style as the main image: ARM reset code in flash, then a copy of
itself to RAM and a jump.

```
0x02000000  ldr pc,=0x02000008  -> CP15, PLL (0x01C40C00), PSC, DDR2 (0x20000000),
                                   PINMUX0 = 0xC1F
0x020000D4  copy NOR 0x02000680 (0x5753C bytes) -> RAM 0x80000000;
            0x40 bytes NOR 0x02057BBC -> 0x8000 (vectors); bss 0x8005753C.. (0x36BCF4)
0x020000E0  MMU on, TTB 0x80040000;  jump 0x80044C34 (RTOS start)
```

Its own RTOS (`initial task`, `stack pool`, `general pool` — the same one the
main image uses on the ARM), a FAT driver, the panel driver, a C runtime.
Addresses below are RAM (`NOR = RAM − 0x80000000 + 0x02000680`).

### 3.1 Normal boot vs update mode

At start a task reads the front panel (`0x80035990`, a 0x40-byte report from
the panel MPU) and stores a **mode** (`0x80011460` → `[0x80056F24]`):

| keys held at power-on (panel report) | mode | what runs |
|---|---|---|
| none | 0 | normal boot (below) |
| byte 9 = 4 and byte 10 bit 4 | 1 | boot menu: `D211 BOOT 1.00 / Press <ENTER> Key`, file list `*.bin` |
| byte 10 = 8 | 2 | **SYSTEM update** from `A:\R24SYS.BIN` (the vendor guide's key) |
| byte 10 = 1, byte 4 = 0x10 | 3 | BOOT update from `A:\R24BOOT.BIN` |
| byte 10 = 2, byte 4 = 0x10 | 4 | SUB SYSTEM update from `A:\R24SUB.BIN` |
| byte 10 = 0xB | 5 | TEST from `A:\R24TEST.BIN` |

Which physical keys those bits are is not decoded (the vendor guide shows the
SYSTEM key as an icon).

Normal boot (`0x80011480`):

```
if mode != 0: stay in the boot loader (update screens)
if memcmp(0x027A0000, "ZOOM/R16", 8) != 0: mode = 2     <- no flag: SYSTEM update
else: 0x80003C06(); blx 0x02080000                     <- start the main image
```

**There is no checksum or header check at boot** — only the flag. The main
image runs from `0x02080000` (its crt0 copies itself to RAM,
`02_analysis.md` §3.2).

### 3.2 What the updater accepts

`0x80000FBC(path)` reads the 64-byte header; `0x8000110C(path)` checks the sum:

| check | rule | in `R24SYS.BIN` |
|---|---|---|
| magic | `memcmp(hdr, "ZOOM/D211", 9) == 0` | `ZOOM/D211` |
| **kind** | `hdr[0x10]` (byte): 0 = SYSTEM, 1 = BOOT, 2 = RHYTHM; must match the mode's file | 0 |
| checksum flag | `hdr[0x30]` (byte): **0 = no checksum check at all** (`0x80001146` returns OK) | 1 |
| checksum | if the flag is set: `u32 hdr[0x34]` == byte sum of **everything after 0x40 to end of file**, read in 2 MiB chunks | `0x3197CC47` (stock) |
| version | `hdr[0x20]` is only **displayed** (`SYSTEM 1.13->x.yy`, `0x80001190`); no downgrade check | `1.13` |
| length | **not checked** — whatever the file holds is written | 7,169,652 |

This settles `02_analysis.md` §5 / `05_limitations.md` §2.2: the header
fields at `0x10` and `0x30` are the image kind and a "checksum present" byte;
the length is free. Our tools keep `0x30` = 1 and always write a correct sum.

### 3.3 How it writes (`0x80003C80(kind, path, 0x40, 1)`)

| kind | destination | before | after |
|---|---|---|---|
| 0 SYSTEM | `0x02080000` | **erase the flag sector 0x7A** | write `"ZOOM/R16"` back (`0x80003C16`) |
| 1 BOOT | `0x02000000` | – | – |
| 2 RHYTHM (`A:\RHYTHM.BIN`, from the boot menu) | `0x022A0000` = the PCM bank | – | – |

Then: erase `ceil(len/64K)` sectors from the destination, program, verify
(`Erasing Data… / Writing Data… / Verifying Data…`, "Keep Power ON").

Consequences:

* **Interrupted SYSTEM update = recoverable.** The flag is erased first and
  written last; with no flag the boot loader comes up in SYSTEM update mode by
  itself (§3.1). Only a damaged *boot loader* bricks the unit.
* **RHYTHM.BIN** rewrites only the PCM bank region (`0x022A0000..`, image file
  `0x220040..`) — a sample-bank update without touching code exists in the
  boot loader (kind byte 2, payload = bank).
* **Size limit — critical.** Nothing bounds the length. A SYSTEM payload over
  `0x720000` bytes (file > **7,471,168**) runs into the flag/record sectors;
  over `0x780000` it wraps to `0x02800000`, which *is* `0x02000000` (§2):
  **it erases the boot loader → brick.** Current images use `0x6D6634`: 301,516
  bytes of room (`r24dump.py` prints it). Never grow `R24SYS.BIN` past
  7,471,168 bytes — every tool here now refuses to write such a file
  (`R24Image.write`/`verify`, `tools/test_flash_safety.py`).

The main firmware's Test Mode updater (`fw_flash_update`, `17_dsp_and_dump.md`
§4) is the same code with `D211*.BIN` names.

## 4. The DSP, live **[PROVEN]**

`r24dump.py` compares every section of `fw_dsp_sections` with the dump:

* all **code/const sections are identical** (2, 4, 5, 6, 11, 12, 13): the DSP
  runs exactly what `R24SYS.BIN` carries — patching those bytes changes the
  DSP;
* the vector table (section 9) differs in **vector 14** only: the image has
  `b 0x118001C0`, the live table a DSP/BIOS HWI dispatcher stub (`stw b0 … mvk
  0xE, b0 …`) — DSP/BIOS plugs interrupt 14 (its CLK timer) at run time;
* the small L1D data sections (0, 3, 10) hold changed variables — expected.

Memory use at the time of the dump:

| region | use |
|---|---|
| L2 `0x11800000` 64 KiB | vectors + code to `0x7000`, data beyond (all 64 KiB in use) |
| L1D `0x11F04000` 48 KiB | 47 of 48 KiB non-zero: the ARM↔DSP area |
| L1P `0x11E08000` 32 KiB | 824/1024 32-byte lines are copies of DSP code: L1P runs as **cache** |
| DDR `0x87B00000–0x87CBFFFF` | program (`0x87B10000–0x87B25113`) + data |
| DDR `0x87CC0000–0x87D9FFFF` | zero (896 KiB unused at that moment) |
| DDR `0x87DA0000–0x87FFFFFF` | data (2.4 MiB: buffers, delay lines) |

## 5. RAM: 128 MiB DDR, what is used and what is free

The DDR is `0x80000000–0x87FFFFFF` (128 MiB; the MMU maps exactly that,
`17_dsp_and_dump.md` §3). The dump did not copy the ARM's DDR, so this map is
**static** (from the code); "free" means no fixed address in the firmware
points there.

| range | size | use | evidence |
|---|---|---|---|
| `0x80000000–0x801EE35B` | 1.93 MiB | code + initialised data (copy of the image) | reset copy **[PROVEN]** |
| `0x801EE35C–0x812DA28B` | 16.9 MiB | bss (zeroed by crt0) | reset code **[PROVEN]** |
| `0x812E1018–0x816E1017` | 5 MiB | the 4 RTOS memory pools, below | pool table `0x801C7C80` **[PROVEN]** |
| `0x817E1018–0x81C128AF` | 4.2 MiB | RTOS control blocks, more static data | addresses used by the RTOS **[STRONG]** |
| `0x81C128B0` | – | **end of all static data** (`_end`) — the flash driver reports `0x81C128B0..0x83FFFFFF` as its free work area (`0x8005AB16/1A`) | **[PROVEN]** |
| `0x81C128B0–0x82FFFFFF` | **19.9 MiB** | **no fixed user found** | [OPEN] |
| `0x83000000–0x830FFFFF` | 1 MiB | MMU-uncached window (DMA buffers) | MMU table **[PROVEN]**; users not traced |
| `0x83100000–0x86FFFFFF` | 63 MiB | **track audio buffers**: `0x83100000 + track × 0x2A0000` (2.625 MiB each, ×24 = 63 MiB, ends exactly at `0x87000000`), sub-blocks of `3 × 0x20000` | `0x80030FC6`, `0x8002FD7E` **[PROVEN]** formula; ×24 **[STRONG]** |
| `0x87000000–0x8753FFFF` | **5.25 MiB** | **no fixed user found** | [OPEN] |
| `0x87540000–0x87A3FFFF` | 5 MiB | PCM drum bank (boot copy) + CUSTOM-kit reserve in its tail | `07_pcm_samples.md`, `12_custom_kit.md` **[PROVEN]** |
| `0x87A40000–0x87AFFFFF` | **0.75 MiB** | **no fixed user found** | [OPEN] |
| `0x87B00000–0x87FFFFFF` | 5 MiB | the DSP's memory, uncached for the ARM: program + 3.3 MiB data in use at dump time, 896 KiB zero (`0x87CC0000–0x87D9FFFF`) | dump §4 **[PROVEN]** |

### RTOS memory pools (`0x801C7C80`, 4 × 16 bytes `{id, flags, size, base}`)

| id | name | size | base | used by |
|---|---|---|---|---|
| 1 | `stack pool` | 2 MiB | `0x812E1018` | task stacks |
| 2 | `general pool` | 1 MiB | `0x814E1018` | RTOS objects |
| 3 | – (flag 1) | 1 MiB | `0x815E1018` | **nobody** (§5.1) |
| 4 | – | **1 MiB** | `0x816E1018` | **`fw_malloc` / `fw_free`** (pool id stored at `0x801EC628` by `0x80058D00`) |
| 5 | – (created at run time) | 1 MiB | `0x818126A8` | the C library heap (§5.1) |

**For patches:**

* `fw_malloc` draws on a **1 MiB pool shared with the whole firmware** (FILE
  COPY takes 320 KiB of it while copying). Keep allocations to tens of KiB,
  free them, and never hold one across a menu exit. DUMP MEMORY takes 64 KiB.
* A patch's static buffers live in its own section in the bank tail (the SDK
  makes room by trimming samples, `sdk/README.md`) — that is memory taken from
  the 5 MiB bank, not from the heap.
* Candidate free DDR — **19.9 MiB** at `0x81C128B0`, **5.25 MiB** at
  `0x87000000`, **0.75 MiB** at `0x87A40000` — is free only in the sense that
  no literal points there. The 19.9 MiB block is what the firmware's own flash
  driver treats as free. **Not verified at run time** yet.

### 5.1 Who owns each zone (traced 2026-11-19)

Method: every 32-bit constant the code loads (`ldr rX, [pc, #…]`, Thumb and
ARM; one-off capstone scripts, not kept in the repo), callers in **both** instruction
sets (a Thumb-only scan wrongly showed the C-heap allocator as dead code),
and the tables the code walks. "No user" = no code constant, no data pointer
and no caller reaches it; a computed address could still (the DDR-ON dump
diff is the run-time check).

**After the bss: kernel area `0x812DA28C–0x812E1018`** **[PROVEN by code]**

| address | what |
|---|---|
| `0x812DA29C–0x812DA2EC` | CPU mode stacks, 16 B each (FIQ, ABT, UND, IRQ, SVC, SYS: set by the RAM start `0x800C4C34`) — only exception entry frames |
| `0x812DA2FC–0x812DC2FC` | 8 KiB start-up stack (config `0x801C7BFC`: size `0x2000`, top `0x812DC2FC`) |
| `0x812DC6FC…0x812E0A30` | kernel object arrays, `base + (id−1) × size` (accessors `0x800C8680..0x800C8BA0`, counts in config `0x801C7C30..0x801C7CD4`): 67 tasks × `0x58` at `0x812DC6FC`, then 20–50 each of the other object kinds (`0x38`…`0x60` bytes) |

**Pools `0x812E1018–0x817E1018`** — table above. The kernel allocates only
from pools 1 and 2 (their ids are kept at `0x801C7CC8/CA`); applications only
through `fw_malloc` → pool 4. **Pool 3 has no user**: no Thumb or ARM caller
of the allocators (`get_mpl 0x80074BC4`, `0x800B3C9C`, `rel_mpl 0x80074D70`)
passes id 3. It can be taken cleanly with `0x80074BC4(3, size, &p)` **[STRONG]**.

**High static area `0x817E1018–0x81C128B0`**

| address | size | what | |
|---|---|---|---|
| `0x817E1018` | 24 × `0x50` | mempool objects (`0x800886C4` creates pools 1–4 from the table) | **[PROVEN]** |
| `0x817E1798…0x817E2660` | ~5 KiB | more kernel object arrays, kernel globals | **[PROVEN]** |
| `0x817E2660–0x81812660` | 192 KiB | kernel area next to `0x817E2650` (users `0x80074ECA`, `0x80088B26`, `0x80099996`); not decoded | [OPEN] |
| `0x818126A8` | **1 MiB** | **pool 5 = the C library heap**: `0x80099D90` (ARM, called from the kernel start `0x80088BAA`) creates it with `cre_mpl 0x800B3C1C` (id at `0x801EE088`); `_malloc_r 0x8009A128` / `_free_r 0x8009A140` / `_realloc_r 0x8009A158` → C `malloc 0x800BA94C`, `free 0x800BA966`. About ten call sites (stdio, C library) | **[PROVEN by code]** |
| `0x819126A8` | **3 MiB** | **no user**: the only reference is a getter (`0x800AAE08`) called once at start-up (`0x80099598`) whose result is discarded; not zeroed (outside the bss) | **[STRONG]** |
| `0x81C126A8` | `0x208` | kernel control block, through the pointer `0x801ED574` (kernel code `0x8006E5xx`, `0x80084B–0x8008571E`) | [STRONG] |

**DDR diff on the device (2026-11-20, `DUMP1` power-on vs `DUMP2` after
8-track recording, `r24dump.py --diff`)**: the 19.9 MiB after `_end`, the
5.25 MiB below the bank and the 0.75 MiB after it are **unchanged** (still
power-on noise) → regions 3/4 now hold the CUSTOM kit (`20_kit_memory_plan.md`).
Writers not in this map: `0x83100000–0x844DFFFF` (8 × 1.5 MiB, stride
2.625 MiB), `0x84B40000–0x84BAFFFF`, `0x84DE0000–0x84E4FFFF` [OPEN].

**Uncached window `0x83000000–0x830FFFFF`**: audio interface set-up
`0x800584D6(fs)` uses `0x83000940/54/68` (three config blocks copied from
`0x800FDD1C`; period = fs × 320 / 1000), zeroes `0x830009C0` (30 KiB) and
`0x830081C0` (7.5 KiB) — DMA buffers; `0x83000968` is passed on to
`0x8005C6C2`, `0x8006C014`. **About 38 KiB used; the other ~984 KiB: no user
found.** **[PROVEN by code]**

**Inside the bss (`0x801EE35C–0x812DA28B`)**, the large objects (sizes from
strides, memsets and the next object's address):

| address | size | what | |
|---|---|---|---|
| `0x801EE3A8`, `0x80202588` | 80 KiB + ~1 MiB | FAT volume 1: work area + sector cache | [STRONG] |
| `0x80303D88`, `0x80317F68` | 80 KiB + ~1 MiB | FAT volume 2 (volume table `0x801EA8EC`: 2 × `{buf_a, buf_b, 0xC0800, 0x100, 6}`, used by the file-system start `0x80000050`); SD card and USB stick | [STRONG] |
| `0x80419C80` | 320 KiB | `fw_file_copy` copy buffer (static; the other copy paths `0x8000176A`/`0x80001808` take 320 KiB from `fw_malloc`) | **[PROVEN]** |
| `0x8046A080` | 1024 × 260 B | the file lister's names (`fw_list_files`), then `fw_file_take` | **[PROVEN]** |
| `0x804B5414` | ~19 KiB | mixer / track parameters (304 users; `TRACK1/2`, `INVERT`) | [STRONG] |
| `0x804DC3E0` | 64 KiB | `fw_flash_update` buffer | **[PROVEN]** |
| `0x804EC6C4` | ~38 KiB | input settings (`Input1`, `Input1/2`) | [STRONG] |
| `0x804F6F14` | 256 KiB | project data (56 users in the project code `0x8001DCxx`) | [STRONG] |
| `0x80537FB4` | 8 × `0x38` | drum voices (`08_drum_kits.md`) | **[PROVEN]** |
| `0x805398C0` | **10 × 1.125 MiB = 11.25 MiB** | recording buffers: `base + i × 0x120000 + j × 0x60000` (3 × 384 KiB each, `0x8002FC50`), ending exactly at `0x810798C0`; 10 = 8 inputs + 2 (stereo bounce/master?) | formula **[PROVEN]**, meaning [INFERRED] |
| `0x8107B0C0`, `0x8107C1D0` | `0xA8`, `0x30` per stream | stream / track descriptors | [STRONG] |
| `0x8107C3B0` | `0x71C8` per track (~740 KiB) | per-track file buffers (`0x80030A1A`) | [STRONG] |
| `0x8114A074` | 128 KiB | rhythm patterns (pattern engine `0x800483A0`) | [STRONG] |
| `0x8116EC98` | 256 KiB | rhythm import work buffer (`Import Error`, `0x8004CDDA`) | [STRONG] |
| `0x811B0A80` | 384 KiB | audio file I/O buffer (WAV/AIFF chunks `RIFF`, `LIST`, `COMM`, `SSND`: `0x80078E0A`, `0x80058DDA`) | [STRONG] |
| `0x81210A80` | 64 KiB | zeroed by `0x8005916E` (audio mode change) | [OPEN] |
| `0x8122CB48`, `0x8123BD48` | 60 + 100 KiB | module `0x80080AC0` (zeroes `0x19000`) | [OPEN] |
| `0x8125CC04`, `0x812706B4` | 73 + 91 KiB | modules `0x80089B..`, `0x8008AF..` (tables of 14-byte / 8-byte entries) | [OPEN] |
| `0x81291384`, `0x812B13B0` | 2 × 2 × 64 KiB | 32-byte-aligned buffer pairs (`0x80096F4C`) — DMA (USB?) | [OPEN] |

What adds up: of the 16.9 MiB bss, ~11.25 MiB are the recording buffers and
~2.2 MiB the FAT caches.

**Free for patches, by confidence:**

| zone | size | evidence |
|---|---|---|
| pool 3 `0x815E1018` | 1 MiB | no caller passes id 3; usable through the RTOS itself, `0x80074BC4(3, …)` |
| `0x819126A8` | 3 MiB | one getter, result discarded |
| `0x81C128B0–0x82FFFFFF` (after `_end`) | 19.9 MiB | no constant, no pointer; the flash driver's own work area (Test Mode updater only) |
| `0x8300A000–0x830FFFFF` | ~984 KiB | uncached; no user found |
| `0x87000000–0x8753FFFF` | 5.25 MiB | no constant, no pointer |
| `0x87A40000–0x87AFFFFF` | 0.75 MiB | no constant, no pointer |

All still to confirm at run time with the DDR-ON dump diff below. Plan to put the CUSTOM kit's audio there with a
region allocator (pool 3 first): `20_kit_memory_plan.md`.

### Measuring it: DUMP MEMORY with +DDR ON

`DDR.BIN` (PROJECT > DUMP MEMORY, dial → `+DDR 128MB ON`, ~150 MiB in total
on the card) is the whole DDR. DRAM is **not** zero at power-on, so "zero"
does not mean "unused". The method:

1. power on, open a project, DUMP MEMORY with DDR ON → `DUMP/` (copy away);
2. record and play on many tracks (all 24 if possible), use the drum machine,
   effects, FILE COPY; DUMP MEMORY with DDR ON again;
3. compare the two `DDR.BIN` per 64 KiB block: a block identical in both
   (and holding power-on noise, not a structure) was not touched in that
   session — a candidate for patches. The candidates of the table above are
   the ones to look at first.

## 6. Reproduce

```bash
python3 tools/r24dump.py DUMP/                # ids, layout, flag, records, image, DSP
python3 tools/r24dump.py DUMP/ --image R24SYS.BIN   # was the stock image flashed?
```

Boot loader disassembly: `NOR.BIN[0x680:0x57BBC]` is RAM `0x80000000`
(Thumb + ARM, same toolchain as the main image).
