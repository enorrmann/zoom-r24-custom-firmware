# 20 — Plan: the CUSTOM kit's audio in free DDR, with a region allocator

> Added 2026-11-19. Builds on the memory map of `18_dump_analysis.md` §5.1.
>
> **Implemented 2026-11-20 (first version, device test pending):**
> `custom_kit.c` with `-D CUSTOM_KIT_DDR=1` (on in `build_firmware.py`) uses
> regions **1 + 2**: pool 3 via `fw_get_mpl(3, 0xF0000, &p)` (retried at each
> load until it succeeds) and the fixed 3 MiB `0x819126C0–0x81C12680`
> (`-D CUSTOM_KIT_DDR_FIXED=0` drops it). Region 0 (`reserve[]`) is 0 by
> default (`--reserve-sec`), so only 5 factory samples are trimmed (code room)
> instead of 23. Allocator = the "simpler first step" of §3: read + convert in
> the largest hole, `memcpy` to the best fit. Diagnostics in `cust_status`:
> `pool_err` (+44), `nreg` (+48), `reg_base/size/used[5]` (+52/+72/+92).
> Emulator test: `test_custom_kit.py` `test_ddr`.
>
> **Dump diff done on the device 2026-11-20** (`DUMP1` at power-on, `DUMP2`
> after 8-track recording — 8 of the 10 recording buffers changed —
> `r24dump.py DUMP1 --diff DUMP2`): regions 3, 4 and 5 **unchanged**, and they
> still hold power-on DRAM noise (mixed 00/FF bits), i.e. never written. On the
> device `get_mpl(3)` returned 0 (`pool_err 0`, no hang). Unexpected writers
> found **outside** the candidates: `0x83100000–0x844DFFFF` (8 × 1.5 MiB
> blocks, stride 2.625 MiB), `0x84B40000–0x84BAFFFF`, `0x84DE0000–0x84E4FFFF`
> — unmapped so far (not in §5.1); keep away from DDR above `0x83000000`
> except the bank. Not yet tried: a 48 kHz project, USB storage mode.
>
> So regions **3 + 4** were added (`-D CUSTOM_KIT_DDR_BIG`, on with
> `CUSTOM_KIT_DDR`): 19.9 MiB `0x81C128C0–0x82FFFFFF` and 5.25 MiB
> `0x87000000–0x8753FFFF`. Total ≈ 29.1 MiB ≈ **5.8 min** of mono 44.1 kHz;
> one sample up to ~3.7 min. Region 5 (0.75 MiB next to the DSP's memory) is
> left out. Test: `test_ddr_big`. `cust_status`: `reg_*[5]` at +52/+72/+92.

## 1. Today

`custom_kit.c` keeps the kit's audio in one static array, `reserve[]`, sized at
build time (`-D CUSTOM_KIT_RESERVE_SEC`, default 6 s = ~517 KiB of mono
44.1 kHz) inside the SDK section in the PCM bank tail. `cust_load` fills it in
pad order with a bump pointer: read the whole WAV at `w`, convert it in place
(mono fold, resample to 44.1 kHz, fade if cut), advance `w` by the result
rounded to 32 bytes; a reload resets `w`. A pad that no longer fits gets
`E_SPACE` (-4) and plays its default sound.

Costs:

* **the size is fixed at build time** — 6 s for all 16 pads together, however
  much RAM the R24 has free;
* **the room is taken from the 5 MiB bank**: the build trims the longest
  factory samples to 1.48 s to make it (23 table entries on today's image);
* one contiguous block: memory elsewhere cannot be used even when it is free.

## 2. Where the audio could go (`18_dump_analysis.md` §5.1)

The DSP reads samples straight from DDR by address, so any DDR region works,
cached (the loader already cleans the D-cache over what it wrote) or
uncached. The sample table stores `rel_start` relative to the bank base
`0x87540000` as a `uint32_t`, and the engine adds it in 32 bits
(`0x8002DAAA..AC`), so regions **below** the bank work too (the subtraction wraps).
`r24_bank_rel()` already computes it that way.

| # | region | size | how it is obtained | confidence |
|---|---|---|---|---|
| 0 | `reserve[]` in the bank tail | build time (could be small or 0) | static, as today | in use, device-verified |
| 1 | **pool 3** `0x815E1018` | 1 MiB (minus the pool's block header) | `0x80074BC4(3, size, &p)` — the RTOS's own allocator, so the kernel knows it is taken | **best**: no caller ever passes id 3 |
| 2 | `0x819126A8` | 3 MiB | fixed address | strong: only reference is a getter whose result is discarded |
| 3 | `0x81C128B0–0x82FFFFFF` (after `_end`) | 19.9 MiB | fixed address | no pointer, but the flash driver uses it as work area (Test Mode updater only) |
| 4 | `0x87000000–0x8753FFFF` | 5.25 MiB | fixed address | no pointer |
| 5 | `0x87A40000–0x87AFFFFF` | 0.75 MiB | fixed address | no pointer |

Recommended first version: regions **0 + 1 + 2** (~4.5 MiB ≈ 53 s mono), then
the larger ones only after the dump diff clears them.

## 3. The allocator: regions, not one block

Replace the bump pointer by a small table of free regions and place each
sample where it fits, so the total is the sum of all regions — fragmented
memory is used, not wasted.

```c
struct ck_region { uint8_t *base; uint32_t size, used; };
static struct ck_region ck_reg[N];     /* filled once at first load */
```

**Setup (first `cust_load`).** Region 0 = `reserve[]`; region 1 = pool 3,
allocated once with `get_mpl(3, size, &p)` and kept for the whole session
(never freed: the kit can be reloaded any time); regions 2.. = the fixed
addresses enabled by the build (`-D CK_REGIONS=…`), each checked once for
sanity (inside the MMU's mapped DDR, 32-byte aligned).

**Loading a kit.** Reset every `used` to 0, then per pad:

1. **Size first, from the WAV header.** Read the chunk headers only: `fmt `
   gives channels, bits, rate; the `data` chunk header gives its byte count.
   Other chunks (`LIST`, …) are skipped with `fseek(f, n, SEEK_CUR)` (below)
   or read into a small buffer and dropped. That gives the **exact output
   length** (frames → mono 16-bit at 44.1 kHz after resampling, capped at
   the pad's limit) before any audio is read.
2. **Best fit**: choose the region whose free space is the smallest that still
   holds that length. The large holes stay free for the next pads; small
   leftovers get filled.
3. **Stream-convert into place**: read the `data` chunk in 64 KiB pieces into
   one small scratch buffer, convert each piece (mono fold, resample, 16-bit)
   and append it to the destination. The raw file never has to fit (stereo /
   24-bit / 48 kHz sources are larger than the result). The resampler keeps
   its phase and the last input frame between pieces; the end fade is applied
   once the sample is complete. (Simpler first step: keep the current whole-file
   `cust_wav_to_pcm`, read into the region with the largest free space, then
   `memmove` to the best fit.)
4. Advance that region's `used` (32-byte aligned), clean the D-cache over the
   sample, write `{r24_bank_rel(p), len}` into the pad's table entry.

Optional: read all 16 headers first and place the **largest samples first**
to pack better.

**The firmware's `fseek`** (found 2026-11-19, not yet in `firmware.sym`):
file-service op 5, `0x80054ECE`, `int fseek(FILE *f, long off, int whence)`,
whence 0/1/2 = SET/CUR/END — e.g. `fseek(f, 0, 2)` at `0x8003036A`,
`fseek(f, -8, 1)` at `0x80079772`. Every caller only tests the result for
`>= 0`, so whether it returns the new position (`lseek`-style, which would
make `fseek(f, 0, SEEK_END)` the file size) is **not settled**; no `ftell`
among the ~35 file-service ops (dispatcher `0x800772B4`, op code at
request `+4`) has been identified. The header method above does not need it.
A third, plain option — read the file once into a small buffer just to count
the bytes — works too, at the cost of reading every file twice.

**Results.** `cust_status.used` becomes per region; the editor could show
`free: 41.2 s`. `E_SPACE` only when no region has room.

**Build.** `CUSTOM_KIT_RESERVE_SEC` becomes optional: with pool 3 present, 0
is possible, and then **no factory sample is trimmed** (the SDK section is
just code). The image stays one image: regions that fail the start-up checks
are skipped, never used.

## 4. Before using any fixed region: verify

1. Build with `python3 tools/build_firmware.py --dump -o build/dumptest/R24SYS.BIN`.
2. PROJECT > **DUMP 1** (DDR ON by default) right after power-on → `A:\DUMP1`.
3. Use the R24 hard: record 8 inputs at once, play all 24 tracks, bounce,
   drum machine, effects, FILE COPY, USB storage; then **DUMP 2** → `A:\DUMP2`.
4. `python3 tools/r24dump.py DUMP1 --diff DUMP2`: DDR map per 64 KiB, then each
   candidate zone (19.9 MiB, 5.25 MiB, 0.75 MiB) ok / FAIL with the changed
   range; a candidate must be identical in both dumps (DRAM is not zero at
   power-on, so "unchanged" is the test, not "zero").
5. Repeat once in a 48 kHz project (other buffer sizes).

Pool 3 does not need this (the RTOS hands it out), but the dump also shows
whether anything writes there.

## 5. Tests (emulator)

* region table: allocation across 3 regions, best fit, scratch move,
  `E_SPACE`, reload resets, D-cache clean per sample;
* `rel_start` for a region below the bank (wrap-around) plays the right bytes
  (drum engine in `r24emu`, as `test_drum_pitch.py` does);
* pool 3 allocated through the real `get_mpl` code in the emulator;
* `CUSTOM_KIT_RESERVE_SEC=0` build: no sample trimmed, kit still loads.

## 6. Also enabled

* Several CUSTOM kits, or longer samples (loops, one-shots of seconds).
* The lofi idea (half-rate samples played at step ×0.5, `19_drum_pitch.md`)
  becomes a sound choice, not a memory need.
