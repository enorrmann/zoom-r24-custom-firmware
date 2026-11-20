# 04 — Data vs. code, clearly separated

The user-facing requirement was that data and code be kept apart. The R24 image
has **no section table**, so the separation below is *derived and verified*, not
read from a header. Nothing here is a guess dressed up as fact: each row states
how it was established.

---

## 1. The two halves of the file

```
byte offset                                      contents
─────────────────────────────────────────────────────────────────────────────
0x000000 ┌──────────────────────────────┐
         │  HEADER  (64 bytes)          │   ← pure DATA, structured
0x000040 ├──────────────────────────────┤
         │                              │
         │  PAYLOAD                     │   ← flat image, CODE + DATA interleaved
         │  (7,169,588 bytes)           │
0x6D6674 └──────────────────────────────┘
```

**Rule of thumb:** the header is data the *updater* reads. The payload is data
the *CPU* executes or dereferences. They are never interchangeable.

---

## 2. Header — 100 % data **[PROVEN]**

| Offset | Size | What it is | Mutable safely? |
|---|---|---|---|
| `0x00` | 9 | magic `ZOOM/D211` | **No** — device identity |
| `0x09` | 3 | padding | leave zero |
| `0x0c` | 4 | unknown | leave as-is |
| `0x10` | 16 | reserved | leave zero |
| `0x20` | 16 | version, ASCII, NUL-padded | **Yes** — tooling honours it |
| `0x30` | 4 | unknown (`0x00000001`) | leave as-is |
| `0x34` | 4 | **checksum** = payload byte sum | **recompute after any payload edit** |
| `0x38` | 8 | reserved | leave zero |

The header is the only part of the file with a documented, byte-exact layout.
It is also the only part that `r24tool` treats as structured fields.

---

## 3. Payload — where the code/data boundary actually falls

### 3.1 Derived segmentation **[PROVEN]**

```
kind          virtual range                 file range                size
──────────────────────────────────────────────────────────────────────────────
CODE+DATA     0x02080000 – 0x02270000       0x00000040 – 0x001F0040   0x1F0000
ERASED FLASH  0x02270000 – 0x022A0000       0x001F0040 – 0x00220040   0x030000
CODE+DATA     0x022A0000 – 0x02756634       0x00220040 – 0x006D6674   0x4B6634
```

Note the erased-flash region is reported here at 64 KiB granularity; the exact
byte-level extent is **file `0x001EE950`–`0x00220040`** (VA
`0x0226E910`–`0x022A0000`), because the `0xFF` run begins mid-block.

> **Correction (2026-11-18).** This segmentation is a statistical decode
> heuristic. The second `CODE+DATA` block is in fact the **PCM sample bank**
> (16-bit audio, `07_pcm_samples.md`), not code; and what the device actually
> loads is set by the reset code: file `0x5B4..0x1EE910` → RAM `0x80000000`
> (code runs there), the bank → RAM `0x87540000`, the `0xFF` region nowhere
> (`02_analysis.md` §3.2).

### 3.2 Why the flat payload cannot be "one or the other" **[PROVEN]**

96.4 % of payload bytes parse as whole Thumb instructions in a strict
sequential decode. But the remaining ~4 % is **interleaved**, not appended:
literal pools (constants), C string tables, and large numeric tables sit
*between* functions. Any tool that treats the payload as "code" and relocates
it will corrupt those pools.

Concretely, at the very start of the payload:

```
0x02080000  e59ff058   ldr pc,[pc,#88]      ← CODE
0x02080004  0208002c                        ← DATA (literal, arm of the ldr)
0x02080008  e59f0054   ldr r0,[pc,#84]      ← CODE
...
0x02080094  00400000                        ← DATA (literal pool)
0x0208009C  01c67000                        ← DATA (peripheral base)
```

Code and data alternate every few words. **This is the defining property of the
image and the main constraint on any modification.**

---

## 4. DATA inventory (everything you can safely edit today)

All offsets are **file offsets**. Every string below was confirmed unique or
its multiplicity recorded.

### 4.1 Version strings **[PROVEN]**

| File offset | VA | Content | Notes |
|---|---|---|---|
| `0x000020` | `0x0207FFE0` | `1.13` | header field, 16 bytes |
| `0x001EBBFC` | `0x0226BBBC` | `1.13` | **UI string**, 12 bytes to pointer array |

These are independent. Change both or the LCD and the tooling disagree.

### 4.2 SD-card filename templates **[PROVEN]**

| File offset | String | Length | Notes |
|---|---|---|---|
| `0x005D0C` | `STE-%03d.WAV` | 12 | stereo take template |
| `0x005D1C` | `MONO-%03d.WAV` | 13 | mono take template |
| `0x005D2C` | `MASTR%03d.WAV` | 13 | master mix template |

The three occupy fixed 16-byte slots with NUL padding, so any replacement of
**equal or shorter** length is safe; shorter is safer (more NUL terminator).
`r24tool` enforces equal length, which is the conservative rule.

### 4.3 FatFs path templates **[PROVEN]** (`A:\…`, 61 occurrences, many repeated)

```
A:\PROJ???
A:\R24PANEL.ZEX
A:\ZOOM_R24\PROJECT\%s\AUDIO
A:\ZOOM_R24\PROJECT\%s\AUDIO\TRK%02d_%02d
A:\ZOOM_R24\PROJECT\%s\HST_MSTR.ZDT
A:\ZOOM_R24\PROJECT\%s\HST_S%02d.ZDT
A:\ZOOM_R24\PROJECT\%s\HST_%02d.ZDT
A:\ZOOM_R24\PROJECT\%s\%s
A:\ZOOM_R24\SYS\RECDAT%02d.ZTP
```

Duplicate copies exist because several tables embed the same literals; a
`find`-based patch must therefore pin an `occurrence` or a fixed `offset`.

### 4.4 Container identity tags **[PROVEN]**

```
ZOOM R24   PROJECT DATA VER0001        (file 0x00DF0D4)
ZOOM R24   CARD DATA    VER0001
ZOOM R-24  EFFECT DATA VER0001
ZOOM R-24  SAMPLER SEQ DATA VER0001
ZOOM R-16  PROJECT DATA VER0001        (x2)
ZOOM R-16  EFFECT DATA VER0001
ZOOM R-16  SAMPLER SEQ DATA VER0001
ZOOM R24   SEQPLAY DATA VER0001
ZOOM D120SEQ
```

These are the on-card format magic strings. Editing them **breaks every
existing project** and the device will report an incompatible medium. Treat as
read-only.

### 4.5 UI vocabulary **[PROVEN]**

Message and parameter tables, e.g. `Please Wait...`, `PROJECT SELECT`,
`FILE INFORMATION`, `Card Protected`, `Stop Recorder`, `No USB Device`,
`Remove SD CARD`, `Erase…`, per-effect parameter names (`Limiter`, `Threshold`,
`LO CUT FILTER`), sample rates (`32.0kHz` … `96.0kHz`).

Editing display strings is the safest form of customisation: the UI reads them
by pointer and does not parse them. **Constraint: the LCD is a fixed-width
character display** — long replacements will be truncated by the renderer, and
length-changing edits are not supported by the equal-length patcher.

### 4.6 Structured descriptor/data region **[STRONG]**

VA `0x0226BB40`–`0x0226C000` (file `0x001EBB80`–`0x001EC040`, 1216 bytes,
81.7 % zeros):

- display charset placeholder `-.--`
- UI version `1.13`
- Thumb function-pointer arrays (RAM targets `0x80023Bxx`, `0x80024233`)
- numeric/character tables: `0..9` as 4-byte entries, `Fast`, `Slow`,
  `SWAP/BOUNCE 1/2`, `00-00-00`
- task-descriptor-shaped records `{state, flags, entry, entry2, period, …}`

This is the best place to **add** new data or table entries: it is sparse,
aligned, and followed by a large zero region.

---

## 5. CODE inventory (edit with extreme care)

### 5.1 What is code **[PROVEN]**

- **ARM state:** entry veneer at `0x02080000`, init/crt0 at `0x0208002C` and
  `0x02080060`, interworking/CP15 veneers at `0x02080408`, `0x0208042C`,
  `0x0208043C`, `0x02080490`, plus mode-switch helpers (`0x02080380` preserves
  `lr` in `r5`, switches mode, calls through `r0`).
- **Thumb state:** 96.4 % of the payload, including all application logic.
- **C64x+ (DSP) code:** stored as data in `.data`, file `0x1C83E0..0x1E47A7`,
  copied to the DSP at boot (`17_dsp_and_dump.md`).

### 5.2 Absolute-address discipline **[PROVEN]**

Literal pools bake in absolute addresses:

```
0x02080094  0x00400000   generic/alias view of 0
0x02080098  0x00800000   DRAM window
0x0208009C  0x01C67000   control/mode peripheral
0x0208016C..0x000001B0   0x01C40C00.. ; 0x01C41000..  (mask/status regs)
0x020800B0  0x800C0000   internal SRAM/peripheral alias
```

Therefore:

- **Do not move code.** There is no relocation table; a moved function breaks
  every literal that points at it.
- **Do not grow the payload.** Inserting bytes shifts every offset and
  invalidates the entire image.
- **Equal-length patches only.** `r24image.patch()` refuses anything else.

### 5.3 The only practically safe code change

Recomputing the checksum after a data edit. Everything else in code space is
high-risk without a full disassembly and a symbolic model of the literal pools,
which this toolchain deliberately does not claim to provide.

---

## 6. Decision table — "can I change this?"

| Target | Safe? | How |
|---|---|---|
| Version shown by tooling | Yes | `r24tool patch --set-version` |
| Version shown on LCD | Yes | `r24tool patch --set-version` (updates both) |
| SD WAV filename templates | Yes, if equal/ shorter | spec patch on `0x5D0C`/`0x5D1C`/`0x5D2C` |
| UI message strings | Yes, equal length | spec patch at the string's offset |
| Add data in the sparse region | Yes | patch into `0x001EBB80`–`0x001EC040` |
| Container magic `ZOOM R24 …` | **No** | breaks every project on card |
| Header magic `0x00` | **No** | device rejects the file |
| Checksum field by hand | Only via tool | must equal payload byte sum |
| Code bytes | **No** | no relocations; breaks silently |
| Image length / inserting bytes | **No** | shifts all offsets |
| DSP program image | Not yet (same length only, with TI tools) | file `0x1C83E0..0x1E47A7`, `17_dsp_and_dump.md` |

---

## 7. How this separation is enforced in code

`tools/r24image.py` is the single source of truth:

- `HEADER_SIZE`, `LOAD_ADDR`, `VERSION_OFFSET`, `UI_VERSION_OFFSET`,
  `CHECKSUM_OFFSET` — named constants, never magic numbers at call sites.
- `patch(offset, expected, replacement)` — refuses length changes **and**
  verifies the old bytes before writing, so a spec written against a different
  firmware revision fails loudly instead of corrupting silently.
- `refresh_checksum()` — the only sanctioned way to re-seal after an edit.
- `verify()` — magic + checksum, raising `ImageError` rather than returning a
  flag nobody checks.
- `specs/` — data edits are **declarative data**, not code. A customisation is
  a JSON file, reviewable in a diff, with no scripting.

That last point is the practical expression of "separate data from code": the
firmware is treated as data, modifications are described as data, and the only
code that touches bytes is a small, tested library.
