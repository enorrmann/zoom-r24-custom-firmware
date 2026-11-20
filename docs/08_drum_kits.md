# 08 — Drum Kits & Rhythm Sound Architecture

This chapter documents the internal architecture of the Zoom R24 rhythm machine
and drum kits in `R24SYS.BIN` (v1.13), including the master sample bank table,
the preset kit definitions, voice parameters, execution routines, and the
feasibility of modifying or adding custom drum kits.

Every claim below is labelled with its **evidence class**:
- **[PROVEN]** — directly demonstrated, reproducible from offsets and disassembly.
- **[STRONG]** — drawn from multiple consistent code and data structures.
- **[INFERRED]** — the most consistent explanation; not independently confirmed.
- **[OPEN]** — explicitly unresolved.

---

## 1. System Overview **[PROVEN]**

The drum machine subsystem consists of five tightly coupled structures:

```
File Offset Range         VA Range                  Contents
─────────────────────────────────────────────────────────────────────────────
0x000FD94C                (RAM 0x800FD398)          KIT SELECT menu record: count 10 at 0xFD950
0x0004BC4E                0x020CBC0E                Kit address resolver (kit * 144), called by the menu
0x000E20EC .. 0x000E2334  0x021620AC (0x800E1B38)   Master Sample Table (73 entries)
0x000E2334 .. 0x000E28D4  0x021622F4 (0x800E1D80)   Drum Kit Table (10 presets)
0x000E28D4 .. 0x000E2FB4  0x02162894 (0x800E2320)   Velocity / response curves
0x000E2FB4 .. 0x000E3C40  0x02162F74 (0x800E2A00)   Sequencer pattern data
0x00220040 .. 0x006D6674  0x022A0000 (0x87540000)   Linear PCM Audio Bank (4.71 MB)
```

The address in parentheses is the one the code uses at runtime. The
initialised-data image is mapped **RAM `0x80000000` ← file `0x5B4`**, i.e.
`RAM = 0x80000000 + file_offset - 0x5B4`: the voice code reads the sample table
at `0x800E1B38` (file `0x0E20EC`), the kit code reads kit 0 at `0x800E1D80`
(file `0x0E2334`) and the pan curve at `0x800E2320` (file `0x0E28D4`). The PCM
bank is copied separately, by `memcpy(0x87540000, 0x022A0000, 5 MiB)` at boot
(`07_pcm_samples.md` §2.1). Earlier revisions of this file gave
`0x80000000 + file_offset`, which is off by `0x5B4`.

---

## 2. Master Sample Table **[PROVEN]**

The master sample table is located at file offset **`0x000E20EC`** to **`0x000E2334`**
(584 bytes, 73 entries), RAM `0x800E1B38`. The voice-start code reads it on
every hit (`start = 0x87540000 + rel_start`, `end = start + length - 2`) through
two literal words at file `0x2E244` / `0x2E624` — so it is ordinary data: it can
be rewritten, and it can be **moved** to hold more entries
(`07_pcm_samples.md` §2–3, `tools/r24bank.py`).

### 2.1 Record Format
Each entry is an 8-byte little-endian record (`<II`):

| Offset | Type | Field | Description |
|---|---|---|---|
| `0x00` | `uint32_t` | `rel_start` | Byte offset relative to sample bank base (`0x00220040`) |
| `0x04` | `uint32_t` | `length` | Byte length of the 16-bit linear PCM mono sample |

### 2.2 Sample Geometry & Extent
* **Base file offset:** `0x00220040` (VA `0x022A0000`).
* **Entry 00:** `rel_start = 0x000000`, `length = 0x011FC0` (73,664 bytes, 0.835 s at 44.1 kHz).
* **Entry 72:** `rel_start = 0x480A1A`, `length = 0x035C1A` (220,186 bytes, 2.496 s at 44.1 kHz).
* **Total bank size:** `0x480A1A + 0x035C1A = 0x004B6634` (4,941,364 bytes ≈ 4.71 MB).
* **Exact file boundary:** `0x00220040 + 0x004B6634 = 0x006D6674`, which matches the
  exact final byte of `R24SYS.BIN`.
* In the factory image every sample is contiguous:
  `entry[n].rel_start + entry[n].length == entry[n+1].rel_start`. This is how
  the image was built, not something the firmware requires.
* Every one of the 73 samples in this table is referenced by at least one of the 10 preset kits (zero unused samples).

---

## 3. Drum Kit Table Layout **[PROVEN]**

Located at file offset **`0x000E2334`** to **`0x000E28D4`** (1,440 bytes).
It contains exactly **10 drum kit presets**, each occupying **144 bytes (`0x90`)**:

| Index | Kit Name | File Offset | RAM Address |
|:---:|---|:---:|:---:|
| 0 | `BASIC` | `0x000E2334` | `0x800E1D80` |
| 1 | `STUDIO` | `0x000E23C4` | `0x800E1E10` |
| 2 | `LIVE` | `0x000E2454` | `0x800E1EA0` |
| 3 | `ROCK` | `0x000E24E4` | `0x800E1F30` |
| 4 | `POP` | `0x000E2574` | `0x800E1FC0` |
| 5 | `FUNK` | `0x000E2604` | `0x800E2050` |
| 6 | `JAZZ` | `0x000E2694` | `0x800E20E0` |
| 7 | `ACOUSTIC` | `0x000E2724` | `0x800E2170` |
| 8 | `TECHNO` | `0x000E27B4` | `0x800E2200` |
| 9 | `URBAN` | `0x000E2844` | `0x800E2290` |

### 3.1 Kit Entry Layout (144 bytes)
* **`0x00 .. 0x0F` (16 bytes):** ASCII name string, padded with spaces (`0x20`) and NUL-terminated.
* **`0x10 .. 0x8F` (128 bytes):** 16 instrument slot descriptors (8 bytes each).

### 3.2 Instrument Slot Descriptor (8 bytes)
Each slot defines playback routing for one instrument:

| Byte Offset | Type | Field | Description |
|:---:|:---:|---|---|
| `0x00 .. 0x01` | `uint16_t` | `sample_id` | Entry in the Master Sample Table (`0 .. 72` factory, `0 .. 159` relocated per-kit) |
| `0x02` | `uint8_t` | `level` | Volume, 0..15 (factory 7..15); see §3.2.1 |
| `0x03` | `uint8_t` | `pan` | Stereo pan 0..126 (engine clamps to `0x7E`), `0x40` = 64 Center |
| `0x04` | `uint8_t` | `mute_group` | bits 0-2: choke group (`0`=none, `1`=Hi-Hat (kits 0..8), `2`=Cowbell/Clave, `3`=Congas; 1..7 work); **bit 4: MONO** (§3.2.1, unused by factory kits) |
| `0x05` | `uint8_t` | `reserved` | Zero in every factory kit; **not read** (§3.2.1) |
| `0x06` | `uint8_t` | `priority` | Zero in every factory kit; **voice-steal priority** 0..2 (§3.2.1) |
| `0x07` | `uint8_t` | `reserved` | Zero in every factory kit; **not read** (§3.2.1) |

### 3.2.1 Hidden / unused per-pad options **[PROVEN from code, 2026-11-19; not used]**

Checked in the drum engine (note-on `0x8002DEE6`, voice choke `0x8002DCE0`,
voice gain `0x8002D9B2`, amplitude `0x8002DC16`). Documented only; nothing
in the image uses them (decision 2026-11-19).

* **`level`** — amplitude ∝ velocity × (level+1) (`0x8002DC16`:
  `vel × (level+1) × 127 / 4`, then squared at `0x8002DAF4`). 0 = silent,
  15 = max; 16 would overflow the 16-bit product. Exposed by KIT SELECT >
  CUSTOM > SETTINGS (`14_kit_editor.md`).
* **MONO, bit 4 (`0x10`) of byte 4** — the choke test at `0x8002DCE0`
  (voices of the same channel class; only voices in state < 3) is:

  | bit 4 | group | a new note cuts |
  |---|---|---|
  | 0 | 0 | nothing |
  | 0 | 1..7 | voices of the group playing a **different** pad |
  | 1 | 0 | voices of the **same** pad (each hit cuts the previous one) |
  | 1 | 1..7 | voices of the group, same pad included |

  Useful for long samples / crashes / loops that should not overlap. The
  CUSTOM kit's SETTINGS keeps this bit as it is (`custom_kit.c` writes only
  bits 0-2); adding a MONO column would set it.
* **Byte 6 = voice-steal priority** (corrected 2026-11-19; an earlier note
  here said it was never read). The engine has **8 voices**
  (`0x80537FB4`, 0x38 bytes each). At note-on the byte is copied to voice
  +0x0D (`0x8002DFF2`). When no voice is free (state 0) or stopping (state 4),
  the allocator `0x8002DE3A` steals: for priority level 0, then 1, then 2 it
  looks at the voices in state ≥ 3 whose +0x0D equals the level
  (`0x8002DCBC`) and takes the oldest (lowest +0x10; ties via the LRU list
  `0x80538174`); it moves to the next level only while the NEW note's byte 6
  is higher (`0x8002DE8C`). If nothing qualifies it takes voice 0. So a pad
  with priority 2 (a crash, a long sample) cannot be cut by notes of priority
  0 when the voices run out; equal priorities on every pad change nothing.
* **Bytes 5 and 7** — not read by any code: the kit table is read only by
  the note-on copy (`0x8002E0B2`) and `kit_ptr` (`0x8004B6A0`, names), and no
  load in the engine reaches them.
* **Proof:** `python3 tools/test_reserved_bytes.py` (20 checks): a static scan
  of every load in the engine that can reach these bytes, then the real
  engine code in the emulator — 20 notes on the 8 voices with the voice tick —
  run with bytes 5/7 set to 0xFF and other values gives identical DSP writes
  and voices; byte 6 = 2 on the kick keeps it from being stolen.
* **Pitch** — corrected 2026-11-19: the playback step is word 0 of the DSP
  mailbox, set inside `0x8006C9F6` (1.0, or 44100/48000 in a 48 kHz project),
  not the `+0x0C` constant of the parameter block. `sdk/patches/drum_pitch.c`
  scales it per voice: `19_drum_pitch.md`.
* **Not available per pad**: decay (computed from the sample length at
  note-on, `0x8002DF94..0x8002DFEE`, into voice +8), filter, effect sends
  (those are per track/channel: `0x805382F4` / `0x80538324`).

### 3.3 Slot-to-Hardware Mapping
* **Slots 00–07:** Mapped to the **8 physical velocity pads** (Bank 1):
  * `Slot 00`: Kick Drum (`pan = 0x40` Center)
  * `Slot 01`: Snare Drum (`pan = 0x40` Center)
  * `Slot 02`: Closed Hi-Hat (`mute_group = 1` on kits 0..8; `0` on URBAN)
  * `Slot 03`: Open Hi-Hat (`mute_group = 1` on kits 0..8, choked by closed hat; `0` on URBAN)
  * `Slot 04`: Crash Cymbal
  * `Slot 05`: Ride Cymbal
  * `Slot 06`: Tom 1 (Hi Tom)
  * `Slot 07`: Tom 2 (Mid Tom)
* **Slot 08:** Floor Tom / Low Tom (Aux 1)
* **Slots 09–15:** Auxiliary / Latin percussion bank (Handclap, Tambourine, Cowbell, Clave, Congas, etc.).

---

## 4. Full Drum Kit Combination Matrix **[PROVEN]**

The table below details the exact sample ID assigned to each slot across all 10 kits:

| Slot | Instrument Role | BASIC | STUDIO | LIVE | ROCK | POP | FUNK | JAZZ | ACOUSTIC | TECHNO | URBAN |
|:---:|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **00** | **Kick Drum** | #56 | #21 | #13 | #25 | #20 | #03 | #24 | #43 | #65 | #60 |
| **01** | **Snare Drum** | #70 | #19 | #00 | #14 | #07 | #37 | #27 | #17 | #68 | #62 |
| **02** | **Closed Hi-Hat** *(Mute 1\*)* | #08 | #47 | #34 | #08 | #38 | #38 | #34 | #34 | #64 | #58 |
| **03** | **Open Hi-Hat** *(Mute 1\*)* | #10 | #50 | #46 | #10 | #39 | #39 | #46 | #46 | #66 | #61 |
| **04** | **Crash Cymbal** | #05 | #01 | #01 | #05 | #01 | #05 | #01 | #01 | #59 | #59 |
| **05** | **Ride Cymbal** | #55 | #57 | #57 | #55 | #57 | #55 | #57 | #57 | #72 | #71 |
| **06** | **Tom 1 (Hi Tom)** | #04 | #48 | #29 | #22 | #16 | #32 | #41 | #16 | #53 | #51 |
| **07** | **Tom 2 (Mid Tom)** | #06 | #49 | #30 | #36 | #18 | #33 | #42 | #18 | #54 | #52 |
| **08** | **Tom 3 (Low Tom)** | #69 | #44 | #28 | #02 | #11 | #31 | #40 | #11 | #67 | #63 |
| **09** | **Handclap / Rim** | #35 | #35 | #35 | #35 | #35 | #35 | #35 | #35 | #35 | #35 |
| **10** | **Tambourine** | #45 | #45 | #45 | #45 | #45 | #45 | #45 | #45 | #45 | #45 |
| **11** | **Cowbell** *(Mute 2)* | #26 | #26 | #26 | #26 | #26 | #26 | #26 | #26 | #26 | #26 |
| **12** | **Clave** *(Mute 2)* | #23 | #23 | #23 | #23 | #23 | #23 | #23 | #23 | #23 | #23 |
| **13** | **Hi Conga** *(Mute 3)* | #15 | #15 | #15 | #15 | #15 | #15 | #15 | #15 | #15 | #15 |
| **14** | **Low Conga** *(Mute 3)* | #09 | #09 | #09 | #09 | #09 | #09 | #09 | #09 | #09 | #09 |
| **15** | **Percussion Mute** | #12 | #12 | #12 | #12 | #12 | #12 | #12 | #12 | #12 | #12 |

*\* Note on Hi-Hat mute groups: In kits 0–8 (BASIC through TECHNO), Slots 02 and 03 have `mute_group = 1`. In URBAN (kit 9), both slots have `mute_group = 0` (no choking, allowing simultaneous open and closed electronic hats).*
*\*\* Note on auxiliary mute groups: Across all kits, Slots 11 & 12 share `mute_group = 2` (Cowbell / Clave choke pair), and Slots 13 & 14 share `mute_group = 3` (Hi / Low Conga choke pair; FUNK kit also sets `mute_group = 3` on Slot 15).*

### Architectural Patterns:
1. **Kick and Snare:** 10 distinct kick samples and 10 distinct snare samples — each kit has a unique primary rhythm pair.
2. **Hi-Hat Sets:**
   * `BASIC` and `ROCK` share hats `#08` / `#10`.
   * `POP` and `FUNK` share hats `#38` / `#39`.
   * `LIVE`, `JAZZ` and `ACOUSTIC` share hats `#34` / `#46`.
   * `STUDIO` uses dedicated hats `#47` / `#50`.
   * `TECHNO` (`#64` / `#66`) and `URBAN` (`#58` / `#61`) use electronic / synth hats.
3. **Cymbals:**
   * Crash `#05` and Ride `#55` are shared across `BASIC`, `ROCK`, `FUNK`.
   * Crash `#01` and Ride `#57` are shared across `STUDIO`, `LIVE`, `POP`, `JAZZ`, `ACOUSTIC`.
   * Electronic Crash `#59` is shared between `TECHNO` and `URBAN`.
4. **Auxiliary Percussion (Slots 09–15):** Uniform across all kits, providing consistent Latin / secondary percussion regardless of chosen kit.

---

## 5. Firmware Code Internals **[PROVEN]**

### 5.1 Kit Indexing Routine (VA `0x020CBC0E`, File `0x0004BC4E`)
```assembly
0x020cbc0e:  movs  r1, r0          ; r1 = kit_index (0..9)
0x020cbc10:  movs  r0, #0x90       ; r0 = 144 bytes per kit descriptor
0x020cbc12:  muls  r0, r1, r0      ; r0 = kit_index * 144
0x020cbc14:  ldr   r2, [pc, #0x21c]; r2 = 0x800E1D80 (literal at file 0x4BE74)
0x020cbc16:  adds  r0, r0, r2      ; r0 = base + (kit_index * 144)
0x020cbc18:  bx    lr              ; returns kit pointer
```

**The literal `0x800E1D80` is the kit table's own RAM address** — file
`0x0E2334` under the `.data` mapping of §1. (An earlier revision read the file
offset as the RAM address and concluded the routine pointed `0x5B4` bytes before
the table; it does not.) A second literal with the same value, at file
`0x2EA28`, is used by the slot reader at VA `0x020AE61E`
(`base + kit*0x90 + 0x10 + slot*8`, then an 8-byte copy of the slot).

### 5.2 Every use of the kit index **[PROVEN, 2026-11-18]**

Addresses are RAM (`file − 0x5B4 + 0x80000000`, `10_loop_import.md` §0). A scan of
all code for loads/stores to the three variables that hold a kit index, plus the
two kit-table literals, finds exactly these uses:

| where (RAM) | what | bound check |
|---|---|---|
| `0x8002E0AA` (file `0x2E65E`) | **voice trigger**: `slot = [0x800E1D80 + kit*0x90 + 0x10 + n*8]`, kit from `0x801EB86C` (literal file `0x2EA28`) | none |
| `0x8002E55A` | `set_kit(k)`: `[0x801EB86C] = k` | none |
| `0x8002E560` | `get_kit()` — no callers | — |
| `0x8004B69A` (file `0x4BC4E`) | `kit_ptr(i) = 0x800E1D80 + i*0x90` (literal file `0x4BE74`); the KIT SELECT list's item getter, called through the menu record, hence no `bl` callers | none |
| `0x8004B65E` / `66E` / `67A` / `686` | KIT SELECT enter / change (preview) / cancel / confirm: copy between `settings.kit` (`0x804C3DB8+0x2C`) and the menu variable `0x801EC3F0`, call `set_kit` | none |
| `0x8004B49C` ← `0x80005528` | project load: `set_kit(settings.kit)` among the other settings | none |

**KIT SELECT** is a menu record at file `0xFD94C` (RAM `0x800FD398`):

```
+0x00  0x801EA7B3   "KIT SELECT"
+0x04  0x0000000A   item count  <- the only "10 kits" in the firmware (file 0xFD950)
+0x08  0
+0x0C  0x8004B69B   item getter = kit_ptr (the list shows the 16-byte names)
+0x10  0x801EC3F0   selection variable
+0x14  0x8004B66F, 0x8004B65F, 0x8004B67B, flags 0x00010024, 0x8004B687
```

The same record format carries 511 items for `PATTERN COPY TO`, so the count is
generic menu data, not a code constant.

> **Correction.** Earlier revisions documented a "hardcoded bounds check
> `cmp r0,#9` at VA `0x020AF24A`" as the kit limit. That compare is in the
> loader of `A:\ZOOM_R24\SYS\SEQPLY%02d.ZDT`, the ten **SEQUENCE PLAY LIST**
> files (project playlists: menu `SEQUENCE PLAY LIST` / EDIT / DELETE / PLAY),
> and has nothing to do with drum kits. Likewise the `0x0A` at file `0x0E30E4`
> belongs to that menu, not to the kits.

### 5.3 Stereo Pan & Volume Mixer (VA `0x020ADFAC` / `0x020AE826`)
Calculates individual voice stereo attenuation by combining pad velocity, slot level,
and slot pan (`0..127`):
* Panning `0x40` (64) balances left and right equally.
* Mute groups choke overlapping voices before feeding the DSP mixer buffers.

---

## 6. Feasibility of Custom Kits & Space Analysis

### 6.1 In-Place Appending (at `0x000E28D4`) **[PROVEN: IMPOSSIBLE]**
Directly at `0x000E28D4` (the byte after `URBAN`), the velocity curve tables begin
immediately (`0x0000, 0x0104, 0x01B4, 0x0269...`). There is **zero padding**.
Appending an 11th kit directly in-place would overwrite pad velocity response data.

### 6.2 Customizing Existing Kits **[PROVEN: FULLY VIABLE]**
All 10 kits and their 160 slots can be customized in-place without moving code:
* Change the 16-character ASCII name of any kit.
* Reassign any of the 16 slots to any entry of the sample table (73 factory, 160 after relocation).
* Adjust voice volume level (`0x02`), stereo pan (`0x03`), or mute groups (`0x04`).
* Recalculate header checksum via `r24tool pack` or `r24image.refresh_checksum()`.

`tools/r24kits.py` does all of this from an editable file — see §7.

### 6.3 More than 10 kits **[BUILT: `r24bank.py kits`, untested on device]**

```bash
python3 tools/r24bank.py kits IMAGE --count 16 -o build/R24SYS_16KITS.BIN
python3 tools/r24bank.py info build/R24SYS_16KITS.BIN
python3 tools/r24kits.py build/R24SYS_16KITS.BIN --export -o build/kits   # edit 16 kits
```

It moves the kit table into an extension section (`R24KITS1`) in the PCM bank
tail, points both kit literals at it, sets the KIT SELECT count, and gives every
kit its own 16 sample-table entries (kit *k* slot *s* → entry `16k+s`, 256 for 16
kits). Kits 11–16 start as copies of kits 1–6, named `KIT11`…`KIT16`; every slot
plays exactly what it played before (checked by the tool). Changed in the code
and data region: the two kit literals, the count word and the two sample-table
literals — no instruction. `tools/test_r24bank.py` (57 checks) covers it,
including coexistence with the `LOOP[SD]` section (`10_loop_import.md`).

The analysis behind it:
To support more than 10 kits, the table must be relocated:
* **Space:** file bytes only reach RAM if they are in a copied region; the
  erased range at file `0x001EE950` .. `0x00220040` is never copied
  (`02_analysis.md` §3.2). Two regions are copied at
  boot: the initialised data, file `0x5B4..0x1EE910` → RAM `0x80000000`
  (`07_pcm_samples.md` §2.3; it holds the current kit table and the rhythm
  patterns, file `0xEAAC4..0xFD43C`, 76,152 bytes), and the **PCM bank**, file
  `0x220040..EOF` → RAM `0x87540000`. The bank's end already holds the
  relocated sample table (verified on a device); on `R24SYS_160.BIN` 9,730 zero
  bytes remain between the last audio byte and that table — room for 67 kits
  (144 bytes each) without touching any audio or pattern.
* **Required patches** (complete, from §5.2):
  1. Point **both** literals holding `0x800E1D80` (file `0x2EA28` and `0x4BE74`)
     at the new table's RAM address — the address itself, no offset (§5.1).
  2. Copy the 10 existing descriptors to the new location and add the new ones.
  3. Set the KIT SELECT item count at file `0xFD950` (u32) to the new number.
  No code instruction changes. Code placed in the bank tail was verified to run
  on a device (`10_loop_import.md` §5), and data there since the sample-table
  relocation.
* **Space used:** 16 × 144 + 16 = 2,320 bytes for the table and 96 × 8 = 768
  bytes more sample table, taken from trailing silence of the last sample.
* **Project compatibility:** the kit index is saved with the project. A project
  saved with kit 11–16 and opened on stock firmware reads past the 10-kit table
  (into the pan/velocity curves) and plays wrong samples; it does not affect
  projects using kits 1–10.

The kit names are *not* duplicated in the UI: `BASIC`…`URBAN` appear only in the
table, so the LCD list is driven by the descriptors themselves and no second
name list needs patching.

### 6.4 Modifying Sound Samples **[PROVEN]**
The PCM sound bank extends to the physical end of `R24SYS.BIN` (`0x006D6674`);
the file size is kept, so the audio budget is 4,941,364 bytes (56.0 s). Within
it, samples can be replaced in place at their stored length
(`patch_samples.py`) or **repacked at any length** with the table rewritten
(`patch_samples.py --repack`, `07_pcm_samples.md` §4).

### 6.5 Sixteen samples per kit, all different **[PROVEN, verified on device]**
The factory table holds 73 entries for 160 kit slots, so kits share samples
(slots 9–15 are identical in all ten). Relocating the sample table
(`07_pcm_samples.md` §3) lifts that:

```bash
python3 tools/r24bank.py relocate R24SYS.BIN --per-kit -o build/R24SYS_160.BIN
```

gives 160 entries, kit *k* slot *s* → entry `16k+s`. The image still sounds like
the factory one; each entry can then be given its own audio with
`patch_samples.py --repack`, within the shared 56 s budget.

---

## 7. Editing kits with `tools/r24kits.py` **[PROVEN]**

Assembling a kit by hand means editing 144 bytes at a computed offset and
recomputing the checksum. `r24kits.py` exports the whole table to a single
editable JSON file and imports it back, so a kit is changed by editing a number.

### 7.1 Export

```console
$ python3 tools/r24kits.py R24SYS.BIN --export -o build/kits
image   : R24SYS.BIN (7169652 bytes)
kits    : 10 x 144 bytes @ 0x000E2334
samples : 73 entries @ 0x000E20EC

  kit 0 BASIC      [56 70  8 10  5 55  4  6 69 35 45 26 23 15  9 12]
  kit 1 STUDIO     [21 19 47 50  1 57 48 49 44 35 45 26 23 15  9 12]
  ...
  kit 9 URBAN      [60 62 58 61 59 71 51 52 63 35 45 26 23 15  9 12]

wrote build/kits/kits.json
  edit a slot's sample_id / level / pan / group, then import it back with:
  python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN
```

One file. It holds the kit table, the master sample table for reference, and a
`notes` block that describes every field — so the file explains itself when
you open it. A slot looks like this:

```json
{
  "sample_id": 56,
  "level": 14,
  "pan": 64,
  "group": 0,
  "reserved": "000000"
}
```

`sample_id` is which sample-table entry the slot plays (0..72 on the factory
table, 0..159 after `r24bank.py relocate --per-kit`); `level`, `pan` and
`group` are the playback parameters. `reserved` is the slot's 3 padding bytes,
carried verbatim so the import never invents padding.

Each kit also carries `name`. It is the only name field: the 16 bytes in the
image are written as your name, **space-padded to 15 characters, then a NUL** —
which is exactly what every factory kit stores. So renaming is a one-value edit
and nothing else needs to be kept in sync.

`name` is validated on import:

| Rule | Why |
|---|---|
| ASCII only | the firmware reads the field as bytes, not UTF-8 |
| 1..**15** characters | the 16th byte is the NUL terminator |
| unique across the 10 kits (case-insensitive) | a duplicate is ambiguous in the kit list. A duplicate the image already had is only warned about, so that image still round-trips |
| no trailing space | spaces are the padding, so they would be stripped on read and the edit would silently do nothing |
| no NUL, no control characters | they would truncate or garble the field |

### 7.2 Import

```console
$ python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN
image   : R24SYS.BIN
spec    : build/kits/kits.json
changes : 2
  kit 3 slot 00: id 25->12 lvl 14->15 pan 64->20 grp 0->0
  kit 3 slot 01: id 14->14 lvl 13->13 pan 64->100 grp 0->3

wrote build/R24SYS.BIN
  size     : 7169652 bytes
  checksum : 0x3197cc36 (verified True)
```

Only the slots that actually differ are written, so the `changes :` list is an
exact account of the edit. Importing an untouched export reports `changes : 0`
and reproduces the image byte for byte.

### 7.3 What is editable, and what is not

| Field | Range | Meaning |
|---|---|---|
| `sample_id` | 0..count-1 | which sample-table entry the slot plays (count = 73 factory, 160 relocated) |
| `level` | 0..15 | volume/attenuation |
| `pan` | 0..127 | 64 = centre |
| `group` | 0..255 | choke group, 0 = none |
| kit name | see below | 16-byte ASCII field |

The name field holds 16 bytes: 15 characters, space-padded, then a NUL. Set it
with `name` alone, 1..15 ASCII characters — see §7.1 for the validation rules
and the one case (the X0X `TR-xxx` kits) that a name cannot reproduce verbatim.

Reassigning `sample_id` is a **routing** change: it points the kit at a
different sample in the bank. It does not move audio. Changing what a sample
*sounds* like is `patch_samples.py`'s job (`docs/07_pcm_samples.md`), and the
two compose — the test below repacks samples first, kits second.

Per-sample `rel_start` / `length` are **not** edited by this tool: they only make
sense together with the audio they describe, so that is `patch_samples.py
--repack`'s job. The sample rows are exported for reference and re-checked
against the image on import, so a `kits.json` from a different sample table
(e.g. a 160-entry one applied to the factory image) is refused.

### 7.4 Verification

```console
$ python3 tools/test_r24kits.py
```

171 checks. The load-bearing ones:

* export writes exactly one file, and an untouched export re-imports reporting
  `changes : 0`;
* export → re-import → repack is **byte-identical** for `R24SYS.BIN`; for
  `R24SYS_X0X.BIN` only the three `TR-xxx` name fields and the checksum differ,
  and the test asserts that no *other* byte moves;
* a re-seal changes **no byte outside the header checksum field** (plus those
  three name fields on the X0X image);
* renaming a kit writes exactly 15 chars + NUL, moves only that kit's name field
  and the checksum, leaves the whole slot table untouched, and reads back
  unchanged through export → import → export;
* the exported matrix equals the §4 table above, so docs and binary cannot
  drift apart silently;
* a real edit touches exactly the two edited slots plus the checksum, leaving
  each slot's reserved padding intact;
* malformed/unsafe imports (out-of-range, negative or boolean `sample_id`;
  level or pan out of range; 15 or 17 slots; a missing kit; a duplicate or
  missing kit index; a trimmed sample table; a moved sample length or start;
  empty, missing, non-string, non-ASCII, 16- or 40-char, trailing-space,
  control-character or NUL-bearing name; a newly introduced duplicate kit name
  (also across case); a leftover `name_hex`; bad `reserved` hex; unknown format
  tag) exit non-zero, print an `error:` message rather than a traceback, and
  write **no output file**. A duplicate name that the image *already* had is
  only warned about, so such an image still round-trips byte-identically;
* importing an export made from a **different image** is refused unless
  `--force` is given, and a wrong `image_size` is refused even when the hash has
  been deleted — that mistake would otherwise rewrite all 160 slots and still
  report `verified True`.

End-to-end, both images, through the existing sample tool and this one:

```console
$ python3 tools/patch_samples.py R24SYS.BIN --extract -o build/e2e/samples
$ python3 tools/r24kits.py       R24SYS.BIN --export  -o build/e2e/kits
$ python3 tools/patch_samples.py R24SYS.BIN build/e2e/samples/samples.txt -o build/e2e/s1.BIN
$ python3 tools/r24kits.py       build/e2e/s1.BIN build/e2e/kits/kits.json -o build/e2e/s2.BIN
$ cmp R24SYS.BIN build/e2e/s2.BIN        # identical
```

**Known limitation, stated plainly.** The factory kits store 15 characters + a
NUL. The three X0X kits instead fill all 16 bytes — `TR-606` followed by 10
spaces, *with no NUL terminator*. A name-only field cannot express "16 bytes, no
terminator" without extra machinery, so importing `R24SYS_X0X.BIN`'s own export
normalises those three fields to `TR-606` + 9 spaces + NUL: byte 15 of each of
the three name fields becomes `0x00` (3 bytes), and the header checksum follows
(its low byte and carry, 2 bytes) — **5 bytes in total, deterministically**.
Nothing else changes: every slot, every reserved byte and all audio stay
byte-identical, which the test asserts explicitly.

That is the deliberate trade for a single, unambiguous `name` field. On
`R24SYS.BIN` the round-trip is byte-identical with zero exceptions.

