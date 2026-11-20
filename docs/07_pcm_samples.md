# 07 — Embedded PCM sample bank

The payload carries a bank of **16-bit signed little-endian mono PCM** that the
rhythm section plays as drum one-shots, plus a **Master Sample Table** that
says where each sample starts and how long it is. This chapter documents where
both are, exactly how the firmware uses them, and what can therefore be
changed.

Tools: **`tools/patch_samples.py`** (extract / patch in place / repack),
**`tools/r24bank.py`** (inspect, relocate the table), `tools/gen_sample_map.py`.
Tests: `tools/test_patch_samples.py`, `tools/test_r24bank.py`.

> **Correction (2026-11-18).** Earlier revisions of this file, of the tools and
> of the README said the sample table was *frozen* — that positions, lengths and
> the entry count could not change because "168 literal-pool words hold hard
> addresses into the bank". Disassembly of the code that actually uses the bank
> (§2) shows that is wrong: the firmware reads every sample's position and
> length from the table at playback time, and no code addresses an individual
> sample. §2.4 explains what those 168 words really are.

---

## 1. Where it is **[PROVEN]**

```
bank            file 0x00220040 .. 0x006D6674 (EOF)   4,941,364 bytes
                VA   0x022A0000 .. 0x02756634
                RAM  0x87540000 ..                    copied there at boot
format          16-bit signed LE, mono, 44.1 kHz, no header
table           file 0x000E20EC .. 0x000E2334         73 x <u32 rel_start, u32 length>
                RAM  0x800E1B38                       (.data copy, see §2.3)
```

Entry *n* covers bank bytes `[rel_start, rel_start + length)`. In the factory
image the 73 entries are contiguous, in id order, and the last one ends exactly
at EOF; that is how the factory image was built, not a rule the firmware
enforces. Every position is listed in [`09_sample_map.md`](09_sample_map.md).

## 2. How the firmware uses it **[PROVEN]**

### 2.1 Boot copy — VA `0x020ADDFC`

Called once from the main initialisation (`bl` at VA `0x020887F2`), before the
rhythm engine starts:

```
020addfc  push {r4, lr}
020addfe  movs r2, #5
020ade00  lsls r2, r2, #0x14        ; r2 = 0x500000  (5 MiB)
020ade02  ldr  r1, =0x022A0000      ; source: the bank in the image
020ade04  ldr  r0, =0x87540000      ; destination: RAM
020ade06  blx  memcpy
```

The whole bank — and anything placed inside it — ends up in RAM at
`0x87540000`. The copy is 5 MiB, 301,516 bytes more than the bank, so the RAM
area is larger than the file needs.

### 2.2 Voice start — VA `0x020AE016`

`r5` is the voice being started; `[r5]` is the u16 `sample_id` copied from the
kit slot:

```
020ae016  ldrh r0, [r5]             ; id
020ae018  lsls r0, r0, #3           ; id * 8
020ae01a  ldr  r1, =0x800E1B38      ; T  (the table)
020ae01c  ldr  r0, [r1, r0]         ; T[id].rel_start
020ae01e  ldr  r1, =0x87540000
020ae020  adds r0, r0, r1
020ae022  str  r0, [sp, #4]         ; start = RAM_BASE + rel_start
  ...
020ae034  ldr  r2, =0x800E1B38
020ae036  adds r1, r1, r2
020ae038  ldr  r1, [r1, #4]         ; T[id].length
020ae03a  adds r0, r0, r1
020ae03c  subs r0, r0, #2
020ae03e  str  r0, [sp, #8]         ; end = start + length - 2
  ...
020ae060  bl   0x020ECF6A           ; hand {start, end, gains} to the voice
```

VA `0x020AE508` reads `T[id].length` once more to compute the voice duration in
milliseconds: `(length / 2) * 1000 / 44100`, a 64-bit division whose divisor is
the literal `0x0000AC44` = 44100 at file `0x2E628`. There is no range check on
`id` in this path.

### 2.3 The table is reached through two words

`0x800E1B38` is stored at exactly two literal-pool words, **file `0x2E244` and
`0x2E624`**, and loaded by exactly four Thumb `ldr` instructions (the three
above and the one at `0x020AE50C`). Nothing else in the code region holds that
address, nor any address inside the table. (`0x800E1B00`, also used nearby, is
a separate 14-word table that ends where the sample table starts.)

`0x800E1B38` is the RAM copy of file `0x0E20EC`. The reset code at VA
`0x02080490` (ARM) copies the initialised-data image before anything else runs:

```
copy  dst 0x80000000  src VA 0x02080574 (file 0x0005B4)  0x1EE35C bytes  -> file 0x5B4 .. 0x1EE910
copy  dst 0x00008000  src VA 0x0226E8D0 (file 0x1EE910)  0x40 bytes
zero  dst 0x801EE35C                                     0x010EBF30 bytes (bss)
```

(parameters at file `0x594`–`0x5B0`). So RAM = `0x80000000 + file − 0x5B4` for
everything in file `0x5B4..0x1EE910`: the sample table, the kit table
(`0x800E1D80`, `08_drum_kits.md` §5.1) and the rhythm patterns
(`11_patterns.md`).

### 2.4 What the "168 literal pools" were

The old argument counted 4-byte words in the code region whose value falls in
the bank's *load* address range `0x022A0000..0x02756634`. Re-measured on the
whole data/code region there are 1,052 such words. Of them:

* only **18** are loaded by any Thumb `ldr` at all;
* of those, **one** is a pointer: `0x022A0000` at file `0x2E1FC`, the
  `memcpy` source in §2.1;
* the rest are constants or data decoded as code — e.g. `0x024E0000` at file
  `0x5F87C` is a multiplier (`(r4 - 0x4F) * 0x024E0000 + 0x20000000`),
  `0x025ED097` is a comparison bound, and most of the others sit in tables
  that only *look* like `ldr` when disassembled.

No code ever refers to a sample by its load address — playback never touches
the load address at all, only the RAM copy. In the RAM range only the base
`0x87540000` is used. `tools/test_r24bank.py` asserts each of these counts.

### 2.5 Consequences

| What | Free? | Limit |
|---|---|---|
| `rel_start`, `length` of any entry | **yes** | even, `length >= 2`, inside the bank |
| order of samples, gaps, sharing / overlapping audio | **yes** | — |
| total audio | — | 4,941,364 bytes (56.0 s @ 44.1 kHz) without growing the file |
| number of entries | 73 in place | the kit table follows the factory table, so more needs the table moved (§3) |
| bank base, file size | no | `0x022A0000` / `0x87540000` are literals; a longer file is untested with the loader (`05_limitations.md` §2) |

## 3. Two table layouts

**factory** — table at file `0x0E20EC`, 73 entries, literals `0x800E1B38`.
Repacking with exactly 73 entries keeps it there.

**relocated** — `tools/r24bank.py` (or `patch_samples.py --repack --entries N`)
moves the table to the **end of the bank**, which the boot code copies to RAM
with everything else, and sets both literals to `0x87540000 + table_rel`:

```
bank + hdr_rel     "R24SMPTB"  u32 count  u32 table_rel     (tools only)
bank + table_rel   count x <u32 rel_start, u32 length>       (8-byte aligned)
...                zero up to EOF
```

Audio may use `[0, hdr_rel)`. The 16-byte header exists so the tools can read
the entry count back; the firmware never reads it.

An **extension area** may follow the table, up to EOF, for new code and data
that must be in RAM (`r24bank.py`, `EXT_MAGIC` `R24EXT01`): with one section it
is `"R24EXT01", size, 0` + the payload; with several, a directory of
`{tag, rel, len}` and sections stacked downward from EOF, so a section never
moves once placed (sections contain absolute addresses). Repacking samples and
relocating the table keep it. In use: `LOOPSD01` (`10_loop_import.md` §5) and
`R24KITS1` (`08_drum_kits.md` §6.3). Data changed by a relocation:
the two literal words, the table area at the end of the bank, the header
checksum — and, with `--per-kit`, the 160 kit-slot `sample_id` fields. The old
table at `0x0E20EC` is left as it was and is no longer read.

### 3.1 160 entries, 16 per kit, all different **[confirmed on device]**

```bash
python3 tools/r24bank.py relocate R24SYS.BIN --per-kit -o build/R24SYS_160.BIN
python3 tools/r24bank.py info build/R24SYS_160.BIN
```

Kit *k*, slot *s* now plays entry `16k + s` — 160 entries, none shared between
slots or kits. Each new entry starts out pointing at the audio its slot already
played, so the image **sounds exactly like its source**; the only audio cut is
1,300 bytes of trailing silence of factory sample 72, where the table goes.
From there, every entry can get its own audio with `--repack`.

### 3.2 The space budget

All entries share the same 56.0 s. The factory audio already fills it, and in
the 160-entry image all of it is still in use, so **new audio fits only once old
audio stops being used or is cut**:

* replacing every entry that shared a factory sample frees that sample;
* `--max-ms N` cuts each listed WAV to N ms (64-sample fade);
* `--max-ms N --trim-all` also cuts the samples kept from the image.

160 distinct samples average **0.35 s** each (e.g. 160 × 300 ms = 42 s fits;
the tool reports the exact free space after each repack).

## 4. Using the tools

```bash
# where is the table, how full is the bank, what do the kits use?
python3 tools/r24bank.py info R24SYS.BIN

# every entry as a WAV + a list (sample00.wav…, or sample000.wav… for >100)
python3 tools/patch_samples.py IMAGE --extract -o build/samples

# in place: each WAV fitted to its entry's current length (trim/pad)
python3 tools/patch_samples.py IMAGE mylist.txt -o build/out.BIN

# repack: each WAV at its own length, table rewritten, identical audio stored once
python3 tools/patch_samples.py IMAGE mylist.txt -o build/out.BIN --repack
python3 tools/patch_samples.py IMAGE mylist.txt -o build/out.BIN --repack --max-ms 400
python3 tools/patch_samples.py IMAGE mylist.txt -o build/out.BIN --repack --entries 160
```

A list is `<wav path> <sample id> [ignored length]` per line; the path may
contain spaces; a relative path is looked up next to the list first, then under
the repository root; blank lines and `#` comments are ignored; a duplicate,
out-of-range or non-numeric id is an error. Entries not named in a `--repack`
list keep their current audio.

Input WAVs may be any channel count, 8/16/24/32-bit, any rate. They are folded
to mono and resampled (nearest-sample) to 44.1 kHz, the bank's rate.

**In place vs. repack.** In-place writes exactly `length` bytes at the entry's
position and never touches the table, so it can never reach a neighbour; it
refuses an entry whose bytes are shared with another one (a relocated per-kit
image starts that way), because the write would change both. `--repack`
rebuilds the whole bank and table, and stops with the overflow size if the
audio does not fit.

The web editor (`web/drumkit-editor*.html`) has a *Tabla de samples* selector:
with *Reubicada (160)* each kit exports to its own ids `16k..16k+15` and the ZIP
includes a `samples.txt` that `--repack` accepts as is.

### What the tests guarantee

`tools/test_patch_samples.py` (56 checks) and `tools/test_r24bank.py` (42):

* the firmware evidence of §2: copy size and literals, the two table words,
  the four loads, single occurrences of the bank's load and RAM addresses;
* extract → patch and extract → repack reproduce the factory image byte for byte;
* in place writes only inside the entry; trim/pad always equals the capacity;
* repack stores a longer WAV whole, a shorter one unpadded, keeps the other
  71 samples byte-identical and refuses an over-full bank;
* relocation: 160 entries, 8-aligned table inside the bank and the 5 MiB copy,
  every slot plays the same audio as before, nothing outside the documented
  bytes changes, idempotent, works on `R24SYS_X0X.BIN`;
* 160 distinct WAVs repack with each entry holding exactly its own audio;
  `r24kits.py` accepts ids 0..159 on that image and refuses 160.

## 5. How the PCM was identified **[PROVEN]**

There is no magic number. Before the table was found, the audio was located by
measuring the data: a percussive envelope (silence, sharp attack, exponential
decay per hit), a small step ratio (mean |Δ| / peak 0.004–0.04 vs 0.43–0.46 for
Thumb code), byte entropy of 6.5–6.8 bits/byte and a low normalised second
difference. Those measurements covered three dense runs inside the bank
(around file `0x231C80`, `0x2644C0` and `0x289340`); the table later gave the
exact extent, which starts at `0x220040` and runs to EOF.

## 6. What is **not** proven

* **On hardware.** The relocated table (`r24bank.py relocate --per-kit` on the
  stock image, sha256 `a98d8b66…`) was flashed and played on an R24 on
  2026-11-18: all kits sound as on the factory image. `--repack` output with
  new audio has not been played yet.
* **Other readers.** Only the four loads in §2.3 read the table in the ARM image.
  Something reading it from outside this image (the DSP) is not excluded, but
  the DSP is handed `start`/`end` pointers by `0x020ECF6A`, not ids.
* **A longer file** (more than 56 s of audio, using the extra 301,516 bytes the
  boot copy moves) depends on whether the updater accepts a different size.

## 7. Install

Same as any other custom image: copy the output to the **root** of an SD card as
`R24SYS.BIN` and follow `R24_Firmware_Update_Guide_E.pdf`. Keep the stock file
to flash back. See `05_limitations.md` for what the loader does and does not
enforce.
