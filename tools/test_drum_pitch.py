#!/usr/bin/env python3
"""test_drum_pitch.py — sdk/patches/drum_pitch.c (docs/19_drum_pitch.md).

The real drum engine (note-on 0x8002E0D4) runs in the emulator on the stock
image and on build/R24SYS.BIN; every DSP mailbox write is captured
(0x80083BAE stubbed). For each PITCH -24..+24 (pad byte 5) on a pad:
  * word 0 (the DSP playback step, Q29) = stock step * 2^(min(pitch, 19)/12), step < 3.0 (device-verified DSP limit);
  * words 1..10 identical to the stock firmware;
in 44.1 and 48 kHz projects. Also: the metronome slot (ch 8) and channels
>= 9 behave exactly as the original fw_dsp_voice.

    python3 tools/build_firmware.py && python3 tools/test_drum_pitch.py
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK = open(os.path.join(ROOT, "R24SYS.BIN"), "rb").read()
BUILT = open(os.path.join(ROOT, "build", "R24SYS.BIN"), "rb").read()
NOTEMAP, VOICES, RATE = 0x800E2D64, 0x80537FB4, 0x801ED3C0
NOTE_ON, KIT_PTR, DSP_VOICE, DSP_WRITE = 0x8002E0D4, 0x8004B69A, 0x8006C9F6, 0x80083BAE
MBOX = 0x11F07970
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def emu(img, rate):
    e = r24emu.R24Emu(img)
    e.dsp = []
    e.stub(DSP_WRITE, lambda em, r: em.dsp.append(
        (r[0], struct.unpack(f"<{r[2]}I", em.read(r[1], 4 * r[2])))) or 0)
    e.write(RATE, struct.pack("<I", rate))
    return e


def hit(img, rate, pad, pitch, group=None):
    """note-on of pad (kit 0) with byte 5 = pitch; -> the voice's mailbox write."""
    e = emu(img, rate)
    kit = e.call(KIT_PTR, 0) & 0xFFFFFFFF
    a = kit + 0x10 + pad * 8
    e.write(a + 5, bytes([pitch & 0xFF]))
    if group is not None:
        e.write(a + 4, bytes([(e.read(a + 4, 1)[0] & ~7) | group]))
    e.call(NOTE_ON, 1, e.read(NOTEMAP, 16)[pad], 100)
    w = [(d, v) for d, v in e.dsp if len(v) == 11 and MBOX <= d < MBOX + 8 * 0x40]
    assert len(w) == 1, e.dsp
    return w[0]


def main() -> int:
    for rate, step in ((44100, 0x20000000), (48000, 0x1D666666)):
        print(f"{rate} Hz")
        for pad in (0, 1, 9):          # not trimmed by the build (4, 5 are)
            base = hit(STOCK, rate, pad, 0)
            ok(base[1][0] == step, f"pad {pad}: stock step {step:#x}")
            built0 = hit(BUILT, rate, pad, 0)
            for g in range(-24, 25):
                ref = hit(STOCK, rate, pad, g)
                got = hit(BUILT, rate, pad, g)
                n = min(g, 19)                     # validated: above +19 the DSP reads garbage
                want = min(step * round(2 ** (n / 12) * 65536) >> 16, 0x5FFF0000)
                # the built image relocates the bank: start/end move, the length does not
                ok(got[0] == ref[0] and got[1][3] - got[1][1] == ref[1][3] - ref[1][1]
                   and got[1][2] == ref[1][2] and got[1][4:] == ref[1][4:],
                   f"pad {pad} pitch {g:+d}: same slot, length and words 2, 4..10 as stock")
                ok(got[1][1:] == built0[1][1:], f"pad {pad} pitch {g:+d}: only word 0 changes")
                ok(got[1][0] == want,
                   f"pad {pad} pitch {g:+d}: step {got[1][0]:#x} = x{got[1][0] / step:.4f}"
                   f" (2^({g}/12) = {2 ** (g / 12):.4f})")

    print("validation: any pad byte gives a safe step")
    for raw in (127, 100, 20, -128, -25):
        st = hit(BUILT, 44100, 0, raw)[1][0]
        ok(0x08000000 <= st <= 0x5FFF0000, f"byte 5 = {raw}: step {st:#x} within 0.25..3.0")
    ok(hit(BUILT, 44100, 0, 127)[1][0] == hit(BUILT, 44100, 0, 19)[1][0], "127 plays as +19")
    ok(hit(BUILT, 44100, 0, -128)[1][0] == hit(BUILT, 44100, 0, -24)[1][0], "-128 plays as -24")
    print("mute group no longer changes the pitch; factory kits unchanged")
    for g in range(8):
        ok(hit(BUILT, 44100, 2, 0, g)[1][0] == 0x20000000, f"pad 2 group {g}: step 1.0")
    print("POLY 0 = MONO bit (byte 4 bit 4): a new hit of the pad cuts its voice")
    def hits(mono):
        e = emu(BUILT, 44100)
        kit = e.call(KIT_PTR, 0) & 0xFFFFFFFF
        a = kit + 0x10 + 3 * 8 + 4
        e.write(a, bytes([(e.read(a, 1)[0] & ~0x17) | (0x10 if mono else 0)]))
        n, out = e.read(NOTEMAP, 16)[3], []
        for _ in range(3):
            e.call(NOTE_ON, 1, n, 100)
            e.call(0x8002E100)                         # one voice tick
            out.append(sum(e.read(VOICES + v * 0x38 + 0xC, 1)[0] != 0 for v in range(8)))
        return out
    ok(hits(False) == [1, 2, 3], "poly: three hits, three voices sounding")
    ok(hits(True) == [1, 1, 1], "mono: each hit cuts the previous, one voice")
    print("metronome (ch 8) and ch >= 9")
    for rate in (44100, 48000):
        blk = list(range(0x1000, 0x1008))
        res = []
        for img in (STOCK, BUILT):
            e = emu(img, rate)
            p = e.alloc(struct.pack("<8I", *blk))
            e.call(DSP_VOICE, 8, p)
            e.call(DSP_VOICE, 9, p)
            e.call(DSP_VOICE, 200, p)
            res.append(e.dsp)
        ok(res[0] == res[1] and len(res[0]) == 1 and len(res[0][0][1]) == 6,
           f"{rate} Hz: ch 8 = 6 words, identical; ch 9/200 write nothing")
    print(f"\ntest_drum_pitch: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
