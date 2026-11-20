#!/usr/bin/env python3
"""test_reserved_bytes.py — do the "reserved" bytes 5..7 of a kit pad do anything?

A pad descriptor is 8 bytes (docs/08_drum_kits.md §3.2): sample_id, level,
pan, mute group, then bytes 5, 6, 7, zero in every factory kit.

A. Static, on the stock image:
   * the kit table is read in exactly two places: kit_ptr (KIT SELECT names)
     and the note-on, which copies the 8 bytes into the voice (+0..+7);
   * in the drum engine (0x8002D8E8..0x8002E470, every user of the voice
     array 0x80537FB4) the only loads that can reach bytes 5..7 of a pad
     copy or +0x0D of a voice are the known ones listed in KNOWN.
B. Dynamic, the real engine code in the emulator: the same sequence of notes
   (more than the 8 voices, with the voice tick 0x8002E100 between them) for
   several values of the bytes; everything written to the DSP and the whole
   voice array (minus the copies of the bytes themselves) is compared.

Result: bytes 5 and 7 do nothing. Byte 6 is the pad's VOICE-STEAL PRIORITY:
when the 8 voices are busy, a new note steals the oldest decaying voice of
priority 0, then 1, then 2 — but only up to its own priority.

    python3 tools/test_reserved_bytes.py
"""
from __future__ import annotations

import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import capstone
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn capstone")
    raise SystemExit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = open(os.path.join(ROOT, "R24SYS.BIN"), "rb").read()
D = 0x5B4
KITS, NOTEMAP, VOICES = 0x800E1D80, 0x800E2D64, 0x80537FB4
VSIZE, NVOICE = 0x38, 8
NOTE_ON, TICK, DSP_WRITE = 0x8002E0D4, 0x8002E100, 0x80083BAE
ENGINE = (0x8002D8E8, 0x8002E470)
# loads in the engine that overlap pad bytes 5..7 / voice +0x0D, explained
KNOWN = {
    0x8002DAC4: "ldr [sample_table+id*8+4] = sample length (not a pad byte)",
    0x8002DF9C: "ldr [sample_table+id*8+4] = sample length (not a pad byte)",
    0x8002DFF2: "ldrb pad[6] -> voice+0x0D (priority of the new voice)",
    0x8002DE3C: "ldrb pad[6] of the NEW note: highest priority it may steal",
    0x8002DCD2: "ldrb voice+0x0D: steal candidate's priority",
}
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def ram(a, n):
    o = a - 0x80000000 + D
    return IMG[o:o + n]


def static_part():
    print("A. static")
    pools = {o - D + 0x80000000 for o in range(D, 0x1EE910, 4)
             if struct.unpack_from("<I", IMG, o)[0] == KITS}
    sites = []
    for o in range(D, 0x1EE910, 2):
        h = struct.unpack_from("<H", IMG, o)[0]
        if h >> 11 == 0b01001:
            a = o - D + 0x80000000
            if ((a + 4) & ~3) + (h & 0xFF) * 4 in pools:
                sites.append(a)
    ok(sites == [0x8002E0B2, 0x8004B6A0], f"kit table read at {[hex(s) for s in sites]}: "
       "note-on copy + kit_ptr only")
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    md.skipdata = True
    a0, a1 = ENGINE
    found = {}
    for i in md.disasm(ram(a0, a1 - a0), a0):
        if not i.mnemonic.startswith("ldr") or "sp" in i.op_str or "pc" in i.op_str:
            continue
        m = re.search(r"#(0x[0-9a-f]+|\d+)\]", i.op_str)
        if not m:
            continue
        off = int(m.group(1), 0)
        w = {"ldr": 4, "ldrh": 2, "ldrsh": 2}.get(i.mnemonic, 1)
        if any(off <= b < off + w for b in (5, 6, 7, 0xD)):
            found[i.address] = f"{i.mnemonic} {i.op_str}"
    ok(set(found) == set(KNOWN), "engine loads reaching bytes 5..7 / voice+0x0D: "
       + ", ".join(f"{a:#x} {found.get(a, '?')}" for a in sorted(found)))
    ok(not any(re.search(r"#[57]\]", t) for t in found.values()),
       "no load of byte 5 or byte 7 anywhere in the engine")
    # every user of the voice array is inside the scanned range
    vp = {o - D + 0x80000000 for o in range(D, 0x1EE910, 4)
          if struct.unpack_from("<I", IMG, o)[0] == VOICES}
    users = [o - D + 0x80000000 for o in range(D, 0x1EE910, 2)
             if struct.unpack_from("<H", IMG, o)[0] >> 11 == 0b01001
             and (((o - D + 0x80000000 + 4) & ~3)
                  + (struct.unpack_from("<H", IMG, o)[0] & 0xFF) * 4) in vp]
    ok(users and all(a0 <= u < a1 for u in users), f"{len(users)} voice-array users, all scanned")


def run(pad_bytes: dict[int, tuple[int, int, int]]):
    """Play the sequence with kit 0 pads' bytes 5..7 set; -> (dsp writes, voices, notes)."""
    e = r24emu.R24Emu(IMG)
    dsp = []
    e.stub(DSP_WRITE, lambda em, r: dsp.append((r[0], em.read(r[1], 4 * r[2]))) or 0)
    for p, (b5, b6, b7) in pad_bytes.items():
        e.write(KITS + 0x10 + p * 8 + 5, bytes([b5, b6, b7]))
    notes = e.read(NOTEMAP, 16)
    seq = [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 3, 0, 9, 1, 2]
    owners = []
    for i, p in enumerate(seq):
        e.call(NOTE_ON, 1, notes[p], 100 - i)
        for _ in range(3):
            e.call(TICK)
        owners.append([e.read(VOICES + v * VSIZE + 0xA, 1)[0] for v in range(NVOICE)])
    raw = bytearray(e.read(VOICES, VSIZE * NVOICE))
    copies = [bytes(raw[v * VSIZE + 5:v * VSIZE + 8]) + bytes([raw[v * VSIZE + 0xD]])
              for v in range(NVOICE)]
    for v in range(NVOICE):                 # mask the copies of the bytes
        for b in (5, 6, 7, 0xD):
            raw[v * VSIZE + b] = 0
    return dsp, bytes(raw), owners, copies


def dynamic_part():
    print("B. dynamic (real engine code, 20 notes on 8 voices)")
    base = run({})
    ok(len(base[0]) > 40, f"{len(base[0])} DSP writes captured")
    ok(any(len(set(o)) == NVOICE for o in base[2]), "all 8 voices busy, stealing happens")
    for name, val in (("byte 5 = 0xFF", (0xFF, 0, 0)), ("byte 7 = 0xFF", (0, 0, 0xFF)),
                      ("bytes 5,7 = 0x5A,0xA5", (0x5A, 0, 0xA5))):
        r = run({p: val for p in range(16)})
        ok(r[3][0][:1] == bytes([val[0]]) and r[3][0][2:3] == bytes([val[2]]),
           f"{name}: the voice really holds the pad's bytes")
        ok(r[0] == base[0], f"{name}: identical DSP writes")
        ok(r[1] == base[1] and r[2] == base[2], f"{name}: identical voices / allocation")

    print("C. byte 6 = voice-steal priority")
    r = run({p: (0, 1, 0) for p in range(16)})
    ok(r[0] == base[0] and r[2] == base[2], "same priority on every pad: no difference")
    kick = run({0: (0, 2, 0)})
    ok(kick[3][0][3] == 2, "voice+0x0D = 2 for the kick")
    n_kick = r24emu.R24Emu(IMG).read(NOTEMAP, 1)[0]
    i = next(i for i, (a, b) in enumerate(zip(base[2], kick[2])) if a != b)
    ok(n_kick in base[2][i - 1] and n_kick not in base[2][i],
       f"note {i}: priority 0 steals the kick's voice")
    ok(n_kick in kick[2][i], f"note {i}: with priority 2 the kick keeps sounding")
    ok(kick[0] != base[0], "so the DSP receives a different allocation")


def main() -> int:
    static_part()
    dynamic_part()
    print(f"\ntest_reserved_bytes: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
