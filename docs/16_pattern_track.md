# 16 — A rhythm pattern on ONE track (`pattern_track.c`)

Stock v1.13 assigns a rhythm pattern to a **pair** of tracks (1/2, 3/4, …),
forces STEREO LINK and leaves the second track dead. The R8 uses one track.
`sdk/patches/pattern_track.c` makes the R24 do the same. It is an **optional**
patch, not in the default image:

```bash
python3 tools/build_firmware.py sdk/patches/pattern_track.c -o build/pattern_track/R24SYS.BIN
```
Proof: `python3 tools/test_pattern_track.py` (25 checks, real
firmware code in the emulator).

**Status: built and emulator-tested, not yet tried on a device.**

## 1. How the stock firmware does it **[PROVEN]**

| what | where | layout |
|---|---|---|
| pattern mask | `0x804C4600` (project block `0x804C45FC` + 4) | bit k = pair k (12 bits) |
| pattern numbers | `0x804C4604` | `int[12]`, one per pair |
| unused | `0x804C4634..0x804C46C3` | 0x90 bytes, no code reads them |
| track records | `0x804C46C4 + t*0xD8` | 24 × 0xD8 |

The block `0x804C1E18[0x3CEC]` is what `PRJDATA.ZDT` stores after its 0x30-byte
header (15644 = 0x30 + 0x3CEC), so the table is saved with the project.

Every user computes `slot = track >> 1` inline (`asrs rX, rT, #1`).
`0x80004298` is the one shared predicate, `is_pattern_track(t) = mask & 1<<(t>>1)`.

**The drums never pass through the tracks' DSP channels.** The drum engine
(MIDI-like queue `0x80538192`, notes `(1, ch = slot+2, note, vel)`) has 12
channels for the sampler slots. Each voice takes the **level, pan, two sends
and on/off** of its slot (`0x8002D9B2` copies them into the voice;
gain = level × master × on). The mixer feeds those from the track faders
through five setters, `0x8002E5F4/60C/624/63C/654`:

```
if (track & 1) return;  slot_param[track/2] = value;
```

That is why only the pair's first fader works and pan/EQ of the track do not
reach the drums: the audio is the engine's own stereo bus, exactly as on the R8.

## 2. What the patch changes

- **Per-track project data.** Mask = 24 bits (bit t = track t), pattern
  numbers = `int[24]` at `0x804C4604..0x804C4663` (the first half of the
  unused range). 33 `asrs #1` sites become `movs` (2 bytes each).
- **The engine keeps 12 slots.** Slot k serves tracks 2k/2k+1 and plays the
  pattern of the one that has it (the *owner*). Only one pattern per pair:
  assigning to a track removes a pattern from its partner.
- **C replacements:** `0x800488B6` (send a slot's pattern to the engine; now
  takes a track), `0x8004B6A6` (assign, one track: record, length, rec→play,
  unlink the pair, push this track's mixer values to the slot), the five slot
  setters (owner track only).
- **Engine/mixer:** engine mask `0x801EBECC` is per track; the mixer's 7 tests
  use the track; the engine's three slot loops test the pair (`3 << 2k`) and
  start/stop a pattern only from its owner track.
- **Loops over the slots** (project load, refresh after a pattern edit):
  24 tracks, pass the track (`cmp #0xc → #0x18`, `lsls #1 → movs`).
- **Mute pushes** (`0x80019E9A`, `0x80019F12`, `0x8001A1C8`) visit all 24
  tracks instead of the even ones.
- **UI:** RHYTHM > ASSIGN + pad uses the pressed track (`0x8004F450`) and shows
  `TRACKn`; TAKE SELECT > PATTERN no longer links the pair (5 `bl` → nop);
  STEREO LINK shows `-` while either track of the pair has a pattern
  (`0x8004185C`: `lsrs r1,r2 ; lsrs r1,r2 ; lsls r1,r1,#30`).

## 3. Old projects

A project saved by stock firmware is converted the first time its patterns
are sent to the engine (project load): pair k → track 2k, then the word
`0x804C4664` is set to `"PTT1"`. The pair stays linked as it was; unlink it
(or re-assign the pattern) to use the second track. A project saved by this
firmware and opened with the **stock** firmware shows patterns on the wrong
pairs.

## 4. Known limits / not tested

- Max 12 patterns, one per pair (engine slots, polyphony unchanged).
- Assigning a **stereo** WAV to the partner of a pattern track (which uses
  both tracks) is not handled specially.
- TRACK EDIT exchange can put two patterns in one pair; the lower track wins.
- The RHYTHM ASSIGN pad blink still follows the stock code.
