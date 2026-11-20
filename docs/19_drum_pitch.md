# 19 — Drum pitch: the DSP already resamples, the ARM sets the step

> Added 2026-11-19. SDK patch `sdk/patches/drum_pitch.c`, in the default
> image (`tools/build_firmware.py`). Proven in the emulator
> (`tools/test_drum_pitch.py`, 907 checks). **PITCH −24..+19** per pad (pad
> byte 5), KIT SELECT > CUSTOM > SETTINGS page 2. Device results 2026-11-19:
> ±12 OK; **above +19 the DSP reads outside the sample** (§2), hence the +19
> limit and the validation in `drum_pitch.c`. Per-step pitch from the
> sequencer: `21_step_pitch.md` (added to the pad's).

## 1. The ARM side **[PROVEN by code]**

Every drum voice is started by `fw_dsp_voice` (`0x8006C9F6`), called by the
drum engine (`0x8002DAEC`, ch 0..7 = the 8 voices) and by the metronome
(`0x8002E6E4`, ch 8). It writes the mailbox slot `0x11F07970 + ch × 0x40`
through `fw_dsp_write` (`0x80083C84` → `0x80083BAE`):

| word | drums (11 words) | metronome (6 words) |
|---|---|---|
| 0 | **step**, Q29 | step |
| 1 | start (sample address) | start |
| 2 | 0 (phase fraction) | 0 |
| 3 | end | end |
| 4 | param block +8 (gain) | param +8 |
| 5 | 0 | 0 |
| 6..10 | param block +0x0C..+0x1C | – |

The step is `0x20000000` (1.0) — or `0x1D666666` = 44100/48000 when
`fw_audio_rate` (`0x801ED3C0`) is 48000: the bank is 44.1 kHz and a 48 kHz
project plays it slower to keep it in tune. **That is the pitch.** The
`+0x0C = 0x20000000` of the parameter block, guessed as the pitch in
`08_drum_kits.md` §3.2.1, is word 6 — something else.

## 2. The DSP side **[STRONG: L2 code, capstone C64x; compact packets decode partly]**

The voice loop at L2 `0x11805568..0x11805668` walks the 9 slots
(`a18 += 0x40`, `a19` to 9) and reads word 0 (step), 1 (position), 2
(fraction), 3 (end). Per output sample: phase += step (Q29), integer advance
= `phase >> 29`, ×2 for 16-bit samples (`shr`, `and -2`), linear
interpolation between the two samples (`ldh`, `sshl 0x10`, `ssub`, multiply
by the fraction). No compare or clamp on the step: the 3-bit integer part
allows step + fraction < 4.0, so a step up to ~3.0 is safe.

**Device-verified limit (2026-11-19):** +19 st (step 2.997 at 44.1 kHz) plays
correctly; from +20 the voice reads garbage addresses — the phase overflows
(arithmetic shift, as read from the code). Observed curiosity: some notes at
+20..+22 play **backwards** (the overflowed phase goes negative); not exposed,
noted as a possible effect. Downwards there is no limit problem: −24 = step
0.25.

**Validation.** `drum_pitch_step` clamps the semitone to −24..+19 and the
final step to `0x5FFF0000` (< 3.0), whatever the source — pad byte, KIT.TXT,
or later the sequencer — so no input can make the DSP read outside the
sample. In a 48 kHz project +19 is a step of 2.75 (also safe).

## 3. The patch

`drum_pitch.c` replaces `fw_dsp_voice` with the same code; for ch 0..7 word 0
becomes `step × 2^(n/12)` (Q16 table, n = −24..+24, used up to +19), with
`n = drum_pitch_semi(ch)` = `(int8_t)` voice `+5` (clamped −24..+19): **pad byte 5**
(`struct r24_slot` `reserved[0]`), copied into the voice with the rest of the
pad at note-on (`0x8002DFFE`). The stock firmware never reads that byte and it
is 0 in every factory kit (`08_drum_kits.md` §3.2.1), so factory kits sound as
before. The metronome and the 48 kHz ratio are unchanged. To drive it from
the sequencer, change only `drum_pitch_semi`.

A higher pitch shortens the sound (the voice reaches `end` sooner), a lower
one lengthens it.

### Reverse through the DSP: does NOT work (device test 2026-11-19, removed)

Tried as a REV parameter (pad byte 7 bit 0): word 0 = −step (two's
complement, pitch included), word 1 (position) = the sample's last address,
word 3 ("end") = its first. **On the device the pad plays nothing** (REV 0
fine, REV 1 silent): the DSP voice loop does not run a voice backwards with
a negative step — its end test presumably stops it at once. The backwards
sound heard at +20..+22 (§2) is the phase overflowing, not a usable reverse.
The parameter was removed (image identical to the one before it, sha256
`611ddd2e…`). A working reverse would have to flip the PCM in memory (only
for loaded WAVs; factory samples are shared) — not done.

### Setting it: KIT SELECT > CUSTOM > SETTINGS, page 2

```
KIT SETTINGS                 KIT SETTINGS
 PAD    VOL  PAN  MUTE   ->   PAD    PITCH  POLY  RIGHT after MUTE: page 2
>KICK   14   C   >OFF        >KICK  >+5     0     LEFT from PITCH: page 1
```

* **PITCH** −24..+19 (shown `-24`..`0`..`+19`), heard at once.
  **Verified on a device** 2026-11-19 (±12; the +19 limit from the +24 test).
* **POLY** 0..1, **default 0**: a new hit of the pad cuts the one still
  sounding; 1 = hits overlap (the factory kits' behaviour). It is the MONO
  bit (byte 4 bit 4) of `08_drum_kits.md` §3.2.1: the choke `0x8002DCE0`
  stops (state 4) the voice of the same pad once its attack is over (state
  ≥ 3, i.e. after one voice tick) and the allocator reuses it. With a mute
  group as well, the hit cuts the whole group including its own pad.

Stored in KIT.TXT as optional 4th/5th values, written only up to the last
one that is not 0: `S01=14,64,0,+5`, `S01=14,64,0,0,1` (older builds read the
first three and ignore the rest; missing values are 0). Code: `custom_kit.c`
(`cust_get/set_settings(slot, v[CUST_NSET])`, limits `cust_set_min/max`,
order `CUST_LEVEL..CUST_POLY`), `kit_editor.c` (`KS_NCOL = CUST_NSET`,
`KS_PAGE`: a new parameter = one enum entry, its limits, a column).

## 4. Proof

`tools/test_drum_pitch.py`: the real note-on in the emulator, stock vs
`build/R24SYS.BIN`, pads 0/1/9, byte 5 = −24..+24, 44.1 and 48 kHz: word 0 =
stock step × 2^(min(n, 19)/12), step < 3.0; any byte value (127, −128…) gives a safe step; every other word as stock (start/end moved by the
bank relocation, same length) and as pitch 0; the mute group no longer
changes the step; ch 8 and ch ≥ 9 identical. `tools/test_kit_editor.py`
(141 checks): page 2, dial, ±12 limits, live kit byte, KIT.TXT write/reload,
bad values rejected, 128 px; POLY column, MONO bit live, KIT.TXT 5th
value, reload (157 checks). `test_drum_pitch.py` also plays three hits of a
pad in the real engine: poly → 1, 2, 3 voices; POLY 0 → 1, 1, 1.

## 5. Device test

2026-11-19, first version (mute group 1..7 = +1..+7 st): OK on the device.
±12 OK on the device; +24 test: garbage above +19 (§2) → limit +19. REV via
a negative step: silent → removed (§3).
