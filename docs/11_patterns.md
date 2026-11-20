# 11 — Rhythm patterns (SEQPLAY pattern data)

This chapter documents how the R24's 472 factory rhythm **patterns** are stored
in `R24SYS.BIN`, and how `tools/r24patterns.py` exports and re-imports them.

Everything below is derived from the binary and is reproducible with the
commands in this file. Each claim carries an **evidence class**:

- **[PROVEN]** — demonstrated by a command in this repo, reproducible from the
  offsets given.
- **[INFERRED]** — the most consistent reading of the data; not confirmed by
  firmware behaviour.
- **[OPEN]** — explicitly unexplained. The tool carries the bytes rather than
  guessing.

---

## 1. Where the patterns live **[PROVEN]**

> **Correction (2026-11-18).** The 48-byte `ZOOM R24 SEQPLAY DATA VER0001` at
> file `0x0E2FB4` is the reference header the firmware compares against when it
> opens `A:\ZOOM_R24\SYS\SEQPLY%02d.ZDT` (the SEQUENCE PLAY LIST project
> playlists, loader at RAM `0x8002ECC2`; `memcmp` with RAM `0x800E2A00`). The
> words after it (`0x0E2FE4`…) are menu records of that feature (`EDIT`,
> `DELETE`, `PLAY`, `SEQUENCE PLAY LIST` with 10 items). They are not a frame
> around the patterns; the pattern records simply follow in the same data area,
> from `0x0EAAC4`. The pattern record format below is unaffected.

```
file offset        VA                contents
0x000E2FB4         0x02162F74        "ZOOM R24   SEQPLAY DATA VER0001", 48 bytes
0x000E2FE4 ..      …                 descriptor blocks; five words describe the frame
0x000EAAC4         0x0216AAC4        first pattern record   ("08Beat01")
…
0x000FD410         0x0216D410        last pattern record    ("COUNT", ends 0x000FD43C)
0x000FD43C ..       …                 zero padding, then the region tail
0x00100000         0x02170000        the SEQPLAY region ends
```

The same frame begins at `0x000E2FB4` in both shipped images; the pattern data
is byte-identical between `R24SYS.BIN` and `R24SYS_X0X.BIN` (that image only
changes kits and PCM audio).

The five header words that make the frame self-describing, exported verbatim as
`header_blocks`:

| File offset | Value | Points at / means |
|---|---|---|
| `0x000E30D4` | `0x800E2A30` | tempo table A (`+0x2C` in the sequencer view) |
| `0x000E30D8` | `0x800E2A80` | tempo table B |
| `0x000E30DC` | `0x800E2AD0` | tempo table C |
| `0x000E30E0` | `0x801EA3A6` | the `PATTERN SELECT` label (`"MUTE Ch.3"` anchor) |
| `0x000E30E4` | `0x0000000A` | item count of the **SEQUENCE PLAY LIST** menu (10 project playlists, `SEQPLY00..09.ZDT`) — *not* the kit count; see `08_drum_kits.md` §5.2 |

`0x020AF394` in the code region holds the loader format string
`A:\ZOOM_R24\SYS\SEQPLY%02d.ZDT`, which is what the same structure looks like on
the SD card. The in-image copy is loaded by the `0x800E2A00` traversals
(`0x020AF3B8`, `0x020AF860`) — see `docs/10_loop_import.md` §3 for the
`PATTERN` menu record at `0x43410`.

---

## 2. The pattern record **[PROVEN]**

Every pattern is a 20-byte header followed by `n1` note records:

```
+0x00   8   name        ASCII, NUL padded: "08Beat01", "ROCKs1VA", "COUNT"
+0x08   1   type        0x04 on 463/472; the rest are 0x02, 0x03, 0x05
+0x09   1   lanes       1, 2 or 4 (4 on the 08Beat set, mixed elsewhere)
+0x0A   2   reserved     00 00
+0x0C   1   level_a     0x0F on almost all
+0x0D   1   level_b     0x0F, except 0x0E twice and 0x0A once
+0x0E   1   type_0e     0x04 in all 472
+0x0F   1   fill        1 = Auto Fill on; 0x00 on 20 patterns
+0x10   2   n1          number of note records that follow
+0x12   2   n2          a count <= n1; see §4 — [OPEN]
+0x14   n1 x 4          the note records
```

`n1` is the load-bearing field: the next record begins at
`offset + 20 + 4*n1`. There is **no** length field, no index table and no
pointer list — the records are found by chaining, which is why the blob is
packed tight (see §5).

### 2.1 The 472 patterns

The chain from `0x000EAAC4` yields exactly **472** records and **16,678** notes,
ending at `0x000FD410`:

```
08Beat01..08Beat12   16Beat01..16Beat12   16FUS01..16FUS04   04JAZZ01..04JAZZ04
DANCE                 CNTRY                68BLUS
ROCKs1VA..ROCKs4FB (22)   HRKs1VA..HRKs2FB (13)   MTLs1VA..MTLs1FB (4)
FUSs1VA..FUSs3FB (18)     INDTs1VA..INDTs1FB (6)  POPs1VA..POPs1FB (6)
RnBs1VA..RnBs2FB (12)     MTNs1VA..MTNs1FB (6)    FUNKs1VA..FUNKs2FB (12)
HIPs1VA..HIPs2VD (17)     DANCs1VA..DANCs2FB (10) HOUSs1VA..HOUSs1FB (4)
TECHs1VA..TECHs1FB (4)    DnBs1VA..DnBs1FB (6)    TPs1VA..TPs1FB (4)
AMBs1VA..AMBs1FB (4)      BALDs1VA..BALDs1FB (6)  BLUSs1VA..BLUSs1FB (6)
CNTRs1VA..CNTRs1FB (6)    JAZZs1VA..JAZZs1FB (6)  AFROs1VA..AFROs1FB (6)
REGGs1VA..REGGs1FB (6)    LATNs1VA..LATNs2FB (12) MidEs1VA..MidEs1FB (6)
ROCK01..ROCK28            HRK01..HRK07            MTL01..MTL04
THRS01..THRS02            PUNK01..PUNK02          FUS01..FUS08
POP01..POP12              RnB01..RnB10            FUNK01..FUNK12
HIP01..HIP23              DANC01..DANC06          HOUS01..HOUS04
TECH01..TECH10            DnB01..DnB06            TRIP01..TRIP04
AMB01..AMB04              BALD01..BALD11          BLUS01..BLUS06
CNTR01..CNTR04            JAZZ01..JAZZ07          SHFL01..SHFL05
SKA01..SKA04              REGG01..REGG04          AFRO01..AFRO08
LATN01..LATN12            BOSSA01..BOSSA02        SAMBA01..SAMBA02
MidE01..MidE04            INTRO01..INTRO18        ENDING01..ENDING07
COUNT
```

Note names are **not** rendered by the firmware: they are payload data with no
other copy in the image and **zero 32-bit pointers** to them, so they are
labels for the export, not UI strings. The `PATTERN SELECT` UI is driven by the
record at `0x43410` and the `0x800E2A00` traversals, not by these names.

---

## 3. The note record **[PROVEN for ticks and duration]**

Each note is 4 bytes:

```
+0x00   u16   ticks      6144 .. 47648 in the factory data (610 distinct values)
+0x02   u8    bits 0-1   duration code, 0..3  — all four occur
              bits 2-7   ZERO in all 16,678 factory notes
+0x03   u8    0x00        zero in all 16,678 factory notes
```

What is proven, and what is not:

| Field | Status | Evidence |
|---|---|---|
| `ticks` | **[PROVEN]** as 16-bit values | 610 distinct values, 6144..47648 |
| duration code | **[PROVEN]** as a real field | all four codes occur: 640 / 3051 / 6329 / 6658 times |
| duration *meaning* | **[OPEN]** | "1/16, 1/8, 1/8., 1/4" would be a guess; the tool keeps the code |
| bits 2-7 of the flags byte | **[OPEN]** | zero in every factory note; carried as `flags`, never interpreted |
| 4th byte | **[PROVEN]** always `0x00` | odd — a 3-byte cell would fit, but every record is 4 |

Two readings were tried and **rejected** by the data, which is why the tool
does not model them:

- *"bit 2 is a tie/hold"* — no factory note ever sets it, so the interpretation
  cannot be confirmed;
- *"the high nibble is an instrument base ×10"* — the nibble is zero in all
  16,678 notes; instruments must be selected elsewhere.

The tick values cluster on a 192-grid (7680, 9984, 40448 appear 1079, 523 and
1249 times), consistent with a 1/48-note delta grid, but treat that as
**[INFERRED]**: the tool does not convert ticks to musical time.

---

## 4. `n2` is carried, not understood **[OPEN]**

`n2` (record `+0x12`) is always **less than or equal to `n1`**, by a margin of
1 to 7 (302 of 472 patterns differ by exactly 3). It is **not** reproducible
from the notes: neither the note count, nor the number of distinct tick values,
nor the number of notes with a nonzero duration matches it (18/472, 15/472 and
1/472 respectively).

So `n2` is an unexplained field of the format. The export carries it and the
import writes it back unchanged — `test_r24patterns.py` asserts that it is
preserved across an edit, precisely because its meaning is unknown.

---

## 5. Why the records cannot change length **[PROVEN]**

The 472 records are packed **tight**. Walking the chain from `0x000EAAC4`,
every record starts exactly where the previous one ends; there is no slack of
any size anywhere in the blob, and the last record (`COUNT`) stops at
`0x000FD410`, followed by 44 zero bytes to `0x000FD43C` and then the region
tail.

That has a hard consequence: **a pattern's note count may not change.** Adding
or deleting a note changes `4*n1`, which moves every following record — and
there is nowhere to move it to.

`r24patterns.py` therefore enforces three rules:

| Rule | Why |
|---|---|
| a pattern keeps its `n1` | the records are packed tight; a longer record would overwrite its neighbour |
| a pattern keeps its name | renaming is unsupported; the export/import is about note data |
| only a note's own 4 bytes are written | nothing else in the image moves |

A request that breaks a rule is refused with an `error:` line and **no output
file** — never a silent partial write.

---

## 6. Using `tools/r24patterns.py` **[PROVEN]**

### 6.1 Export

```console
$ python3 tools/r24patterns.py R24SYS.BIN --export -o build/patterns
image    : R24SYS.BIN (7169652 bytes)
frame    : 0x0E2FB4  b'ZOOM R24   SEQPLAY DATA VER0001    '
patterns : 472 records, 16678 notes
blob     : 0x0EAAC4 .. 0x0FD43C  (76152 used + 0 unused bytes)

  frame 0  08Beat01..COUNT          472 patterns,      0 unused bytes

wrote build/patterns/patterns.json
```

One file, `patterns.json`, self-documented by a `notes` block. A pattern is:

```json
{
 "name": "08Beat01",
 "offset": 961220,
 "length": 220,
 "n2": 47,
 "header": {
  "type": 4, "lanes": 4, "reserved_08a": "0000",
  "level_a": 15, "level_b": 15, "type_0e": 4, "fill": 1
 },
 "notes": [
  { "raw": "189a0300", "ticks": 39448, "duration": 3, "flags": 3, "pad": "00" }
 ]
}
```

`offset` and `length` are read-only reference values. `raw` is what the export
read; the fields are authoritative on import, so editing `ticks`, `duration` or
`flags` is all that is needed.

### 6.2 Import

```console
$ python3 tools/r24patterns.py R24SYS.BIN build/patterns/patterns.json \
        -o build/R24SYS.BIN
image   : R24SYS.BIN
spec    : build/patterns/patterns.json
changes : 1
  08Beat01: header changed, 1 note(s) changed

wrote build/R24SYS.BIN
  size     : 7169652 bytes
  checksum : 0x3197cc4b (verified True)
```

An untouched export reports `changes : 0` and reproduces the image **byte for
byte**. A real edit touches exactly the edited note bytes plus the header
checksum — `test_r24patterns.py` asserts the exact byte set.

(the `header changed` line above appears because a note edit is reflected in the
`raw` field of that one note; no header byte actually moves — see the byte-diff
assertion in §7.)

### 6.3 Inspecting one pattern

```console
$ python3 tools/r24patterns.py R24SYS.BIN --show 08Beat01
08Beat01  file 0x0EAAC4  50 notes  header 04 04 00 00 0f 0f 04 01 32 00 2f 00
   ticks dur flags  raw
   39448   3     3  189a0300
   …
```

### 6.4 What is editable

| Field | Range | Meaning |
|---|---|---|
| `ticks` | 0..65535 | 16-bit per-note value; §3 |
| `duration` | 0..3 | duration code; §3 |
| `flags` | 0..255 | the whole flags byte; `duration` overwrites bits 0-1 |
| `pad` | `00` | the 4th byte; `00` in every factory note |
| `header.*` | see §2 | record type/lanes/levels/fill, carried verbatim |
| `header.n2` | 0..65535 | carried verbatim; §4 |

Not editable: the pattern's `name`, its note count, and the number of patterns.
All three would move data that the packed layout cannot absorb.

---

## 7. Verification

```console
$ python3 tools/test_r24patterns.py
all 302 checks passed
```

The load-bearing ones, run against both shipped images:

- the blob parses into exactly **472** records and **16,678** notes, packed
  tight, ending at `0x000FD410`, with no unaccounted bytes;
- all four duration codes occur, flags bits 2-7 are always zero, every pad byte
  is `00`, and `n2 <= n1` everywhere;
- export → import is **byte-identical** for the whole file (sha256), and
  export → import → export produces the same JSON;
- a real edit moves **exactly** the edited note byte plus the checksum, leaves
  `n1`, `n2` and every other record byte-identical, and the result verifies;
- 24 refusal paths (bad JSON, wrong format/version, wrong image size, foreign
  image, out-of-range fields, added/deleted notes, rename, duplicate names,
  removed pattern, unknown record type, frames out of order, missing files)
  each exit non-zero, print `error:`, print **no traceback**, and write no
  output file;
- `--show`, `--dry-run` and the CLI's missing-argument paths behave;
- this document's pattern count and offsets match the binary, so docs and image
  cannot drift apart silently.

---

## 8. Relation to the other tools

| Tool | Edits | Does not touch |
|---|---|---|
| `patch_samples.py` | PCM audio and the sample table (positions, lengths) | patterns, kits |
| `r24bank.py` | where the sample table lives; `relocate --per-kit` = 160 entries | patterns, audio (kit ids only with `--per-kit`) |
| `r24kits.py` | kit table: sample routing, level, pan, mute group | patterns, audio |
| **`r24patterns.py`** | **the note data of the 472 patterns** | kits, audio, card files |

The three compose: a pattern triggers a *slot*, a kit decides which *sample*
that slot plays, and the sample bank supplies the audio. Editing a pattern
changes **when** a hit fires; to change **what** it sounds like, use the other
two.

Patterns also exist on the SD card as `A:\ZOOM_R24\SYS\SEQPLY%02d.ZDT`
(`docs/10_loop_import.md`), which is the same structure in a different
container — not documented here, because no card image was available to verify
against.
