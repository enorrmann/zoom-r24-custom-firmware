#!/usr/bin/env python3
"""test_pattern_track.py — prove sdk/patches/pattern_track.c in the emulator.

Runs the REAL firmware code of the built image (build_firmware.py chain +
pattern_track.c) with the hardware-facing calls stubbed:

 1. old project (pair format) is converted on the first engine update;
 2. assign a pattern to track 4 (odd, 1-based) -> per-track mask/array,
    engine message for slot 1, pair unlinked, slot takes track 4's mixer;
 3. the mixer setters only feed the slot from the owner track;
 4. assigning to the partner removes the first pattern (one per pair);
 5. firmware is_pattern_track() (0x80004298) and the engine loops
    (0x8004830C, 0x80048342) see one track, not the pair;
 6. the stereo-link display test (0x8004185A) still covers the pair.

    python3 tools/test_pattern_track.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_firmware
import r24bank

try:
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

PATCH = os.path.join(build_firmware.r24sdk.SDK, "patches", "pattern_track.c")
PS = 0x804C45FC
PMASK, PAT, PMAGIC = PS + 4, PS + 8, PS + 0x68
ENGMASK, LINK = 0x801EBECC, 0x804C2478 + 0x30
LEVEL, PAN, ON = 0x805382C4, 0x80538294, 0x80538354
FADER, PANS, PLAY = 0x804C1E48, 0x804C1EA8, 0x804C1E3C
REC = 0x804C46C4
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def image():
    data, _notes, _b = build_firmware.build([PATCH], 6.0)   # optional extra
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        return open(out, "rb").read()


def w32(emu, a, v):
    emu.write(a, struct.pack("<I", v & 0xFFFFFFFF))


def main() -> int:
    img = image()
    emu = r24emu.R24Emu(img)
    posts, links = [], []
    emu.stub(0x800534B8, lambda e, r: posts.append(r[1]) or 0)
    emu.stub(0x8004AB28, lambda e, r: (e.write(r[1] + 8, bytes([4, 2])), 0)[1])
    emu.stub(0x80004A90, lambda e, r: links.append((r[0], r[1])) or 0)
    for a in (0x800204A8, 0x800204AE, 0x80019E6A, 0x80019EBA, 0x8001F170,
              0x8002D01A, 0x8002066C):
        emu.stub(a, lambda e, r: 0)

    print("1. old project converted")
    w32(emu, PMASK, 0b101)              # pairs 0 and 2
    w32(emu, PAT + 0, 5)
    w32(emu, PAT + 8, 7)
    w32(emu, PMAGIC, 0)
    emu.call(0x800488B6, 0, 5)
    ok(emu.u32(PMASK) == (1 << 0) | (1 << 4), "pair bits 0,2 -> track bits 0,4")
    ok(emu.u32(PAT + 0) == 5 and emu.u32(PAT + 16) == 7 and emu.u32(PAT + 8) == 0,
       "pattern[0]=5, pattern[4]=7, pattern[2] cleared")
    ok(emu.u32(PMAGIC) == 0x31545450, "project marked converted")
    ok(posts[-1] == 0x15000000 | (0 << 9) | 5, "engine slot 0 <- pattern 5")

    print("2. assign pattern 9 to track 4 (index 3)")
    posts.clear()
    w32(emu, LINK, 1 << 1)              # pair 1 (tracks 3/4) linked
    w32(emu, FADER + 4 * 3, 77)
    w32(emu, PANS + 4 * 3, 20)
    w32(emu, PLAY, 1 << 3)
    emu.call(0x8004B6A6, 3, 9)
    m = emu.u32(PMASK)
    ok(m >> 3 & 1 and not m >> 2 & 1, "mask: track index 3 only")
    ok(emu.u32(PAT + 12) == 9, "pattern[3] = 9")
    ok(emu.u32(ENGMASK) == m, "engine mask = project mask")
    ok(0x15000000 | (1 << 9) | 9 in posts, "engine slot 1 <- pattern 9")
    ok((2, 0) in links, "pair 1 unlinked")
    ok(emu.u32(REC + 3 * 0xD8 + 0x10) == 1, "track record 3 marked")
    ok(emu.u32(REC + 2 * 0xD8 + 0x10) == 0, "track record 2 untouched")
    ok(emu.u32(LEVEL + 4) == 77 and emu.u32(PAN + 4) == 20 and emu.u32(ON + 4) == 1,
       "slot 1 takes track 4's level/pan/on")

    print("3. setters only from the owner")
    emu.call(0x8002E5F4, 2, 11)
    ok(emu.u32(LEVEL + 4) == 77, "fader of track 3 (partner) ignored")
    emu.call(0x8002E5F4, 3, 55)
    ok(emu.u32(LEVEL + 4) == 55, "fader of track 4 drives the drums")
    emu.call(0x8002E5F4, 0, 66)
    ok(emu.u32(LEVEL + 0) == 66, "pair without odd owner: first track as stock")

    print("4. one pattern per pair")
    emu.call(0x8004B6A6, 2, 4)
    m = emu.u32(PMASK)
    ok(m >> 2 & 1 and not m >> 3 & 1, "track 3 assigned, track 4 removed")
    ok(emu.u32(REC + 3 * 0xD8 + 0x10) == 0, "track 4 record cleared")
    emu.call(0x8004B6A6, 3, 9)          # back to track 4 for the next checks

    print("5. firmware predicates per track")
    ok(emu.call(0x80004298, 3) == 1, "is_pattern_track(track 4) = 1")
    ok(emu.call(0x80004298, 2) == 0, "is_pattern_track(track 3) = 0 (audio track)")
    emu.call(0x8004830C, 1)
    ok(emu.u32(0x81149E14 + 4) == 1, "engine loop: slot 1 active")
    w32(emu, 0x81149E14 + 4, 0)
    emu.call(0x80048342, 1, 1 << 2)     # partner track 3 toggled
    ok(emu.u32(0x81149E14 + 4) == 0, "toggling the partner does not start the pattern")
    emu.call(0x80048342, 1, 1 << 3)
    ok(emu.u32(0x81149E14 + 4) == 1, "toggling track 4 does")

    print("6. stereo-link test covers the pair")
    # 0x8004185A..0x80041862 is inline (no return): check the new opcodes and
    # evaluate them for both tracks of the pair, as the CPU would.
    off = 0x8004185C - 0x80000000 + 0x5B4
    ok(img[off:off + 6] == bytes.fromhex("d140d1408907"),
       "lsrs r1,r2 ; lsrs r1,r2 ; lsls r1,r1,#30")
    mask = emu.u32(PMASK)
    for t in (2, 3):
        k = t >> 1
        z = ((mask >> k >> k) << 30) & 0xFFFFFFFF == 0
        ok(not z, f"track index {t}: pattern in the pair -> link shows '-'")

    print(f"\n{checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
