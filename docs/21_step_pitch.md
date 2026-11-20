# 21 — Per-step drum pitch in PATTERN EDIT (`step_pitch.c`)

> Added 2026-11-19. SDK patch `sdk/patches/step_pitch.c`, in the default
> image (`tools/build_firmware.py`). Proven in the emulator
> (`tools/test_step_pitch.py`, 98 checks, stock vs built for every hook).
> **Not yet tried on a device.**

## 1. Use

PATTERN EDIT, REC. The soft keys read `EDIT  KIT  DELETE  ...`; the 4th one
(empty in the stock firmware, `A-Z` outside REC) now toggles the dial:

| soft key 4 | dial on a pad row |
|---|---|
| `  VEL  ` | stock: none → soft (79) → loud (127), and back |
| ` PITCH ` / `PIT+5  ` | semitone of that pad's note at the edit position, −24..+19; an empty step gets a soft note; the note is heard at once |

In PITCH mode the key shows the pitch of the note under the cursor
(`PIT+5  `, `PIT-12 `, `PIT 0  `) or ` PITCH ` when there is none. The step
pitch is **added** to the pad's own PITCH (KIT SELECT > CUSTOM > SETTINGS,
`19_drum_pitch.md`) and clamped to −24..+19. DELETE clears the note with its
pitch. Changing the velocity keeps the pitch.

## 2. Where it lives **[PROVEN by code]**

A user pattern event is one 32-bit word, built by the sequencer task's
type-8 handler (`0x80049114`, message from `0x8004AC2C`):

| bits | field |
|---|---|
| 0–7 | tick (written at commit, `0x80048E22`, which keeps bits 8–31) |
| 8–13 | note − 12 (63 = marker) |
| 14 | flag (message word bit 0) |
| 15–17 | velocity / 16 (at least 1); played as `v*16+15` |
| **18–23** | **step pitch, signed (this patch)** |
| 24–31 | unused |

Bits 18–31 are read by nothing in the stock firmware; the step cells
(`0x8116A190`, one word per note) take whole words from the pattern, and the
pattern file is saved and loaded as raw words (`0x8004B1DC`). Patterns from
the stock firmware have 0 there; a pattern with pitches plays unpitched on a
stock firmware.

## 3. The hooks

| site | what |
|---|---|
| `0x80049114` replace | type-8 handler: same words as stock; bits 18–23 kept from the cell, or taken from the message (word bit 8 set, pitch in bits 16–21) |
| `0x8004E68C` detour | REC dial on a pad row: PITCH mode → `sp_pitch_dial`, back to `0x8004E72A`; VEL → stock |
| `0x8004C06A` detour | REC default key handler: `0x10038` toggles VEL/PITCH (REC and not the ALL DEL state), else stock |
| `0x8004E1B4` replace | REC screen update: a changed soft-key-4 text forces a full redraw; after a full redraw the key is drawn (`0x800371A6`, text centred on its length, so always 7 chars) |
| `0x800485EC`, `0x80048F60`, `0x8004A0E0` detours | the three event → note-on sites (song playback ch = slot+2, audition on cursor move ch 1, REC playback ch 1): same queue entry as stock + the event's pitch |
| `0x8002E566` jump + pointer `0x8002E570` | the drum-engine queue writer (4 args: the jump saves r3 on the stack first); firmware callers write pitch 0 |
| `0x8002E084` replace | voice trigger: pad of the note, byte 5 += the ring entry of the queue slot being played (`QR − 4`) |

The drum-engine queue (`0x80538192`, 256 bytes, read index `0x801EB868`,
write index `0x801EB869`, 4 bytes per entry) has no room for a pitch, so a
256-byte ring in the patch holds one per queue index, written under the same
mutex as the entry. The reader (`0x8002E3F0`) advances the read index before
it calls note-on `0x8002E0D4` → `0x8002E084`, the only caller chain.

## 4. Known limits

- A **negative** pitch lengthens the sample but the voice envelope still ends
  at the original length (voice+8, `19_drum_pitch.md`, same as the pad PITCH).
- The VEL-mode preview (stock code) plays without the step pitch; playback,
  cursor audition and the PITCH preview use it.
- Real-time recording (pads, type-6 handler) records pitch 0.
- Device behaviour not yet seen: the soft-key text drawing, the REC flags
  (`0x801EC384`/`0x801EC388`) as read from the label code.

## 5. Proof

`tools/test_step_pitch.py`: queue writer identical to stock (bytes, indexes,
wake-up, full queue); each of the three detours gives the stock queue entry
for 5 slot/note/velocity cases and the event's pitch in the ring; the stock
firmware plays a pitched event normally; the voice gets the kit's pad with
byte 5 = clamp(pad + step) (3 pads × 7 cases); the type-8 handler gives the
stock cells for stock messages (incl. clear-all, out of range, memory full),
keeps the pitch on a velocity change, sets it from a pitch message; the dial
in VEL runs the stock path, in PITCH sends the right message, previews with
the pitch and asks for a redraw; the soft key toggles only in REC; the
screen update draws the right 7-char text only on a full redraw.
