# 00 — Quick start: custom samples, kits and repack

One page, all the commands. Everything here is run from the repository root.
Only Python 3 (standard library) is required — no compilers, no vendor toolchain.

Every command on this page was executed against `R24SYS.BIN` v1.13 before being
written down; the outputs shown are real, not illustrative.

---

## 0. The 60-second version

```bash
# 1. Check the image you are starting from
./tools/r24tool verify R24SYS.BIN

# 2. Replace sample #0 with your own WAV
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/R24SYS.BIN

# 3. Confirm the result is a valid image
./tools/r24tool verify build/R24SYS.BIN
```

Then copy `build/R24SYS.BIN` to the **root** of a FAT32 SD card named exactly
`R24SYS.BIN` and flash it with the vendor procedure
(`R24_Firmware_Update_Guide_E.pdf`). **Keep the stock file — it is your
rollback.** The toolchain never flashes for you.

---

## 1. Which tool does what

| Tool | Use it for | Writes an image? |
|---|---|---|
| `tools/patch_samples.py` | **the main tool for custom samples** — extract every entry, patch in place, or `--repack` at any length | yes |
| `tools/r24bank.py` | where the sample table is, how full the bank is; `relocate --per-kit` = 16 own samples per kit (160) | yes |
| `tools/r24kits.py` | drum kit table — which sample each slot plays, level, pan, mute group, kit name | yes |
| `tools/r24tool paste` | raw WAV → byte offset, **no validation** (advanced) | yes |
| `tools/r24tool verify` | structural check; non-zero exit = do not flash | no |
| `tools/r24tool info` | header, version, checksum summary | no |
| `tools/r24tool unpack` / `pack` | split into header+payload and reassemble | yes |
| `tools/gen_sample_map.py` | regenerate `docs/09_sample_map.md` (every table position) | no |
| `tools/build_x0x_firmware.py` | rebuild the TR-606/808/909 image | yes |
| `tools/loop_import.py` | move the sampler’s loop import between the USB stick and the SD card | yes |

**`patch_samples.py` is the only supported way to change drum audio.** It has
two ways of writing:

* **in place** (default) — each WAV is fitted to its entry's current length:
  longer audio is trimmed with a short fade, shorter audio zero-padded, the
  table is not touched and a patch can never bleed into a neighbour;
* **`--repack`** — each WAV keeps its own length; the bank is laid out again and
  the sample table rewritten. The firmware reads positions and lengths from the
  table at playback time (`07_pcm_samples.md` §2), so this is safe as long as
  everything fits in the bank: 4,941,364 bytes = 56.0 s at 44.1 kHz in total.

---

## 2. Custom samples — the everyday workflow

### 2.1 Extract everything, look, edit, repack

```bash
python3 tools/patch_samples.py R24SYS.BIN --extract -o build/samples
```

```
wrote 73 WAVs + build/samples/samples.txt
re-pack: python3 tools/patch_samples.py R24SYS.BIN build/samples/samples.txt -o <out>
```

You get `sample00.wav … sample72.wav` plus a ready-to-use list. Each WAV is mono
16-bit @ 44.1 kHz, holding the PCM **verbatim at its exact stored length**.

Repack untouched — this must come out byte-identical, and it is the check that
your setup is sane:

```bash
python3 tools/patch_samples.py R24SYS.BIN build/samples/samples.txt -o build/repack.BIN
cmp R24SYS.BIN build/repack.BIN && echo IDENTICAL
```

Then edit whichever WAVs you like in any editor and repack the same list —
in place (WAVs fitted to the old lengths) or with `--repack` (WAVs keep their
new lengths). Untouched, both reproduce the image byte for byte.

### 2.2 Patch one sample

The list is one line per sample: `<wav path> <sample id>`. The path may contain
spaces (the **last** token is always the id). Blank lines and `#` comments are
ignored.

```bash
# mylist.txt
# <wav path>              <sample id>
/abs/path/mykick.wav      0
build/samples/sample13.wav  13
```

```bash
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/R24SYS.BIN
```

```
wrote build/R24SYS.BIN  (7169652 bytes, unchanged size)
checksum 0x311C3DC7 (verifies: True)
```

Real result of patching a 100 ms 60 Hz kick into slot 0: **60,779 bytes changed**,
everything else untouched, checksum recomputed for you.

### 2.3 Check before you write

```bash
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/R24SYS.BIN --dry-run
```

Reports every trim/pad (or, with `--repack`, the space used and free) and
writes nothing.

### 2.3b Repack at any length

```bash
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/R24SYS.BIN --repack
```

```
audio    : 4941364 of 4941364 bytes (100.0%), 73 distinct for 73 entries, 0 bytes free
```

The factory audio fills the whole bank, so a longer sample only fits if others
get shorter. If it does not fit the tool stops and says by how much;
`--max-ms 500` cuts listed WAVs to 500 ms, `--trim-all` applies the same cut to
the samples kept from the image.

### 2.4 Which sample is which?

```bash
python3 tools/gen_sample_map.py R24SYS.BIN -o docs/09_sample_map.md
```

`docs/09_sample_map.md` lists every table position with byte offsets, lengths, a
duration in ms, and which factory kits use each one. That is how you find the id
to patch. Regenerate it rather than editing it by hand.

### 2.5 Sample rate

The bank is **44.1 kHz** mono 16-bit (the rhythm voice code also divides by
44100 to compute durations, `07_pcm_samples.md` §2.2). Input WAVs at any other
rate are resampled to 44.1 kHz automatically; extracted WAVs are written at
44.1 kHz. There is nothing to configure.

---

## 3. Drum kits — which sample plays where

`r24kits.py` exports the 10 kit descriptors to one editable JSON file and imports
them back. Any slot can be pointed at any entry of the sample table (73 on the
factory image, 160 after §3b), with its own level, pan and mute group.

```bash
python3 tools/r24kits.py R24SYS.BIN --export -o build/kits
```

```
  kit 0 BASIC      [56 70  8 10  5 55  4  6 69 35 45 26 23 15  9 12]
  kit 3 ROCK       [25 14  8 10  5 55 22 36  2 35 45 26 23 15  9 12]
  ...
wrote build/kits/kits.json
```

Edit a slot in `build/kits/kits.json`:

```json
{ "sample_id": 25, "level": 14, "pan": 64, "group": 0, "reserved": "000000" }
```

| Field | Range | Meaning |
|---|---|---|
| `sample_id` | 0..72 | which sample this slot plays |
| `level` | 0..15 | volume |
| `pan` | 0..127 | 64 = centre, lower = left |
| `group` | 0..255 | choke group, 0 = none |
| `name` | 1..15 ASCII | kit name, unique across kits |

Then repack (dry run first — it lists the exact changes and writes nothing):

```bash
python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN --dry-run
python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN
```

```
changes : 2
  kit 3: name bytearray(b'ROCK           \x00') -> b'ROCK-CUSTOM    \x00'
  kit 3 slot 00: id 25->3 lvl 14->15 pan 64->20 grp 0->0
```

`changes : 0` on an edited file means nothing will happen — investigate before
writing. Renaming a kit is a one-value edit: set `name` (nothing else to keep in
sync). Names that are already duplicated in the source image only warn; creating
a new duplicate is refused.

**`sample_id` changes *routing*, not audio.** To change how a sample *sounds*,
patch the audio (§2) — the two compose:

```bash
python3 tools/patch_samples.py R24SYS.BIN mylist.txt -o build/step1.BIN
python3 tools/r24kits.py build/step1.BIN build/kits/kits.json -o build/step2.BIN --force
```

The `--force` is needed because `step1.BIN` is no longer the image the kits were
exported from, so its hash differs. You get a warning, not a silent override:

```
warning: this export was made from a different image (recorded 71c7e3c5…, target 42f4b8d7…) …
```

That guard exists because importing the *wrong* kit file would otherwise rewrite
all 160 slots while still reporting a valid checksum.

---

## 3b. Sixteen own samples per kit (160 entries)

The factory table has 73 entries for 160 kit slots, so kits share sounds
(slots 9–15 are the same in all ten kits; changing one changes every kit).
Moving the table to the end of the bank lifts that:

```bash
python3 tools/r24bank.py relocate R24SYS.BIN --per-kit -o build/R24SYS_160.BIN
python3 tools/r24bank.py info build/R24SYS_160.BIN
```

```
table  : factory: 73 entries @ file 0x000E20EC (literal 0x800E1B38)
      -> relocated: 160 entries @ file 0x006D6170 (literal 0x879F6130)
  entry 133: length 220186 -> 218886 (cut 1300 bytes of trailing silence)
kits   : kit k slot s -> entry 16*k+s (160 entries, all different within every kit); sound unchanged
```

Kit *k*, slot *s* now plays entry `16k + s`. It still sounds like the factory
image. Then give each kit its own audio:

```bash
python3 tools/patch_samples.py build/R24SYS_160.BIN --extract -o build/s160   # sample000..159
# replace WAVs, or write a list with your own files and ids 16k+s
python3 tools/patch_samples.py build/R24SYS_160.BIN build/s160/samples.txt \
        -o build/R24SYS.BIN --repack --max-ms 400 --trim-all
```

**Budget:** all 160 share 56.0 s (≈ 0.35 s each on average). `--repack` only
keeps factory audio that some entry still uses, so when your list overwrites
all 160 entries the factory sounds disappear and the full 56 s is yours. While
only part is replaced, the remaining factory audio still takes its space
(`--max-ms`/`--trim-all` help in that case). The web editor
(`web/drumkit-editor.html`, *Tabla de samples → Reubicada (160)*) exports a ZIP
with `sampleNNN.wav` + `samples.txt` for one kit at a time; unzip it anywhere and
pass that `samples.txt` to `--repack`.

Status: the relocated image (factory audio) was **flashed and verified on an
R24** (2026-11-18): every kit sounds as before. Repacked new audio is not yet
tested on a device.

---

## 4. Raw offset write (advanced, no safety net)

If you know exactly where you want bytes and do not want the bounds rule or the
sample table consulted:

```bash
./tools/r24tool paste R24SYS.BIN --offset 0x231C80 --wav "REVERB_X0X/606/Kick1.wav" -o build/raw.bin
```

`--offset` accepts `0x…` or decimal. This uses your byte count **verbatim** —
it will happily overwrite whatever else lives at that offset, and it does not
know anything about the sample table. Prefer `patch_samples.py` unless you have
a specific reason.

---

## 5. Verify, always

```bash
./tools/r24tool verify build/R24SYS.BIN
```

```
verification passed
  size          : 7169652
  checksum      : 0x311C3DC7
  checksum_ok   : True
```

Non-zero exit means **do not flash this file**. For a quick header summary:

```bash
./tools/r24tool info build/R24SYS.BIN
```

To see exactly what you changed versus stock:

```bash
cmp -l R24SYS.BIN build/R24SYS.BIN | wc -l     # how many bytes differ
```

Editing kits should move only a handful of bytes plus the checksum; replacing
audio moves audio bytes only. If a kit edit changes tens of thousands of bytes,
something is wrong.

---

## 6. Prove your setup works

Run these before trusting anything you build. All must pass.

```bash
python3 tools/test_roundtrip.py        # container round-trip, byte-identical
python3 tools/test_patch_samples.py    # bounds rule, repack (56 checks)
python3 tools/test_r24bank.py          # firmware evidence + table relocation (42 checks)
python3 tools/test_r24kits.py          # kit export/reimport (171 checks)
python3 tools/test_asm_edit.py         # disassemble/edit/reassemble loop
```

Also worth running on any image you build:

```bash
python3 tools/patch_samples.py R24SYS.BIN --extract -o build/chk
python3 tools/patch_samples.py R24SYS.BIN build/chk/samples.txt -o build/chk.BIN
cmp R24SYS.BIN build/chk.BIN          # must be identical
```

---

## 7. Installing

1. Copy the built image to the **root** of a FAT32 SD card, named exactly
   `R24SYS.BIN` (the filename matters to the updater).
2. Insert it into the R24.
3. Power on while holding the documented button.
4. Select `OK` and confirm.
5. **Do not power off or remove the card during the update.**
6. Power off when complete.

Keep the stock `R24SYS.BIN`. To roll back, repeat from step 1 with the original.

---

## 8. Things that will trip you up

| Symptom | Cause |
|---|---|
| `error: sample id 12 already assigned on line 7` | duplicate id in a patch list — the later line would silently overwrite the earlier one |
| `error: sample id 99 out of range 0..72` | ids run 0..count-1 (73 factory, 160 relocated); see `r24bank.py info` |
| `error: audio needs … too many` | `--repack` overflow: shorten WAVs, `--max-ms`, `--trim-all` |
| `error: sample N shares audio with sample M` | in-place write on shared audio (relocated image); use `--repack` |
| Kit import refuses: *"different image"* | you are applying a kit export to a different build. Re-export from that build, or add `--force` |
| `changes : 0` after editing kits | nothing differs from the source image — check you edited the file you are importing |
| Renamed a kit, name unchanged on device | you edited `name_hex`-style leftovers or the wrong kit entry; only `name` counts now |
| Audio sounds cut short or padded | in-place mode fits WAVs to the existing length; use `--repack` to keep each WAV's own length |
| `verification failed` | do not flash. Rebuild; a stale checksum means the file was edited outside the toolchain |

---

## 9. Where the details live

| Document | Contents |
|---|---|
| [`docs/07_pcm_samples.md`](07_pcm_samples.md) | how the firmware plays the bank, table layouts, repack, relocation, budget |
| [`docs/08_drum_kits.md`](08_drum_kits.md) | kit table layout, slot descriptors, §7 = `r24kits.py` in full |
| [`docs/12_custom_kit.md`](12_custom_kit.md) | CUSTOM kit loaded per project from `KIT\SAMPLEnn.WAV` |
| [`docs/09_sample_map.md`](09_sample_map.md) | every sample position (generated) |
| [`docs/03_toolchain.md`](03_toolchain.md) | every `r24tool` subcommand, declarative build specs |
| [`docs/05_limitations.md`](05_limitations.md) | what is *not* proven — read before trusting a claim |
| [`docs/06_assembly_workflow.md`](06_assembly_workflow.md) | editing code, not data |
