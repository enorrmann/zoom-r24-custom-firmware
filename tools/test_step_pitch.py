#!/usr/bin/env python3
"""test_step_pitch.py — sdk/patches/step_pitch.c (docs/21_step_pitch.md).

Runs the real firmware code around every hook in the emulator, stock image vs
build/R24SYS.BIN (RTOS mutex/flag/mailbox calls stubbed and recorded):

  * queue writer 0x8002E566 (now a jump that keeps r3): same queue bytes and
    indexes as stock, pitch ring entry 0;
  * the three event -> note-on sites (song playback, audition, REC playback):
    same queue bytes as stock for every slot/note/velocity, and the ring holds
    the event's bits 18-23 (signed);
  * voice trigger 0x8002E084 (through the real note-on 0x8002E0D4): the pad
    handed to the voice is the kit's, byte 5 = clamp(pad pitch + step pitch);
  * type-8 handler 0x80049114: same cell words as stock for stock messages,
    keeps the pitch bits on a velocity change, takes them from a pitch message;
  * dial on a pad row: VEL mode runs the stock code, PITCH mode sends the
    pitch message, previews with the pitch and asks for a redraw;
  * soft key 4 (0x10038) toggles the mode only in REC; other keys and states
    run the stock handler;
  * the REC screen update draws soft key 4 = "  VEL  " / " PITCH " / "PIT+5  ".

    python3 tools/build_firmware.py && python3 tools/test_step_pitch.py
"""
from __future__ import annotations

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import r24emu
    from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R4,
                                   UC_ARM_REG_R5, UC_ARM_REG_R7, UC_ARM_REG_SP,
                                   UC_ARM_REG_LR)
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK = open(os.path.join(ROOT, "R24SYS.BIN"), "rb").read()
BUILT_PATH = os.path.join(ROOT, "build", "R24SYS.BIN")
BUILT = open(BUILT_PATH, "rb").read()
SYM = json.load(open(BUILT_PATH + ".syms.json"))["symbols"]

Q, QR, QW = 0x80538192, 0x801EB868, 0x801EB869
LOCK, UNLOCK, FLAG, MBOX, KICK = 0x800544C8, 0x80054470, 0x80053884, 0x80053C14, 0x8002BBB2
PUT, NOTE_ON, VOICE_ON = 0x8002E566, 0x8002E0D4, 0x8002DEE6
CELL_SET, CELLS, RUNNING, NEARLY_FULL, CHANGED = 0x80049114, 0x8116A190, 0x801EBF40, 0x800490D8, 0x80048D98
PADNOTES, KIT_PTR, KIT_INDEX = 0x800E2D64, 0x8004B69A, 0x801EB86C
DIAL, ROW, REDRAW, REC, ALLDEL = 0x80003B7A, 0x801EC390, 0x801EC414, 0x801EC384, 0x801EC388
KEY, KEY_PRE = 0x8004C06A, 0x8004C026
SCREEN, LCD_BEGIN, LCD_END, REC_DRAW, LABEL = 0x8004E1B4, 0x800321A0, 0x80032176, 0x8004E030, 0x800371A6
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def emu(img):
    e = r24emu.R24Emu(img)
    e.flags, e.msgs = [], []
    e.stub(LOCK, lambda em, r: 0)
    e.stub(UNLOCK, lambda em, r: 0)
    e.stub(FLAG, lambda em, r: em.flags.append((r[0], r[1])) or 0)
    e.stub(MBOX, lambda em, r: em.msgs.append(em.read(r[1], r[2])) or 0)
    e.stub(KICK, lambda em, r: 0)
    e.stub(0x80083BAE, lambda em, r: 0)            # DSP mailbox write (hardware)
    return e


def queue(e, n):
    """the last n queue entries (4 bytes each), ending at QW"""
    w = e.read(QW, 1)[0]
    return [e.read(Q + ((w - 4 * (n - i)) & 0xFF), 4) for i in range(n)]


def ring(e, n):
    """pitch ring entries of the last n queue entries (built image only)"""
    w = e.read(QW, 1)[0]
    return [struct.unpack("b", e.read(SYM["sp_ring"] + ((w - 4 * (n - i)) & 0xFF), 1))[0]
            for i in range(n)]


def run(e, start, until, regs, sp_words=()):
    """run firmware from `start` (Thumb) to `until` with r4/r5/r7 and a stack"""
    uc = e.uc
    sp = r24emu.STACK_TOP - 0x400
    for i, w in enumerate(sp_words):
        e.write(sp + 4 * i, struct.pack("<I", w))
    uc.reg_write(UC_ARM_REG_SP, sp)
    uc.reg_write(UC_ARM_REG_LR, r24emu.RET | 1)
    for reg, v in regs.items():
        uc.reg_write(reg, v)
    uc.emu_start(start | 1, until, count=2_000_000)


def word(idx, vel3, pitch=0, flag=0):
    return (flag << 14) | (idx << 8) | (vel3 << 15) | ((pitch & 0x3F) << 18) | 0x5A


def main() -> int:
    print("queue writer 0x8002E566 (firmware callers)")
    for args in ((1, 0, 36, 0x4F), (1, 7, 49, 127), (3, 1, 7, 0x5C), (0, 2, 40, 0)):
        res = []
        for img in (STOCK, BUILT):
            e = emu(img)
            e.write(QR, b"\x10"); e.write(QW, b"\x10")
            e.call(PUT, *args)
            res.append((queue(e, 1), e.read(QR, 2), e.flags))
            if img is BUILT:
                rg = ring(e, 1)
        ok(res[0] == res[1] and res[1][0][0] == bytes(args) and rg == [0],
           f"put{args}: same queue bytes, indexes and wake-up as stock, pitch 0")
    for img in (STOCK, BUILT):                       # full queue: nothing written
        e = emu(img)
        e.write(QR, b"\x14"); e.write(QW, b"\x10")
        e.call(PUT, 1, 0, 36, 99)
        ok(e.read(QW, 1) == b"\x10" and not e.flags, f"{'stock' if img is STOCK else 'built'}: full queue left alone")

    print("event -> note-on detours: same queue bytes as stock, pitch in the ring")
    sites = (("song playback", 0x800485EA, 0x80048606, "sp"),
             ("audition", 0x80048F5E, 0x80048F76, "sp"),
             ("REC playback", 0x8004A0DC, 0x8004A0F4, "r4"))
    for name, start, until, where in sites:
        for slot, idx, vel3, pitch in ((0, 24, 4, 5), (5, 30, 7, -12), (11, 0, 1, 19),
                                       (3, 62, 7, -24), (1, 25, 4, 0)):
            res = []
            for img in (STOCK, BUILT):
                e = emu(img)
                st = e.alloc(struct.pack("<8I", 0, 0, 0, 0, 0, 0, word(idx, vel3, pitch), 0))
                regs = {UC_ARM_REG_R5: idx, UC_ARM_REG_R7: vel3}
                if where == "sp":
                    regs[UC_ARM_REG_R4] = slot if name == "song playback" else 1
                    run(e, start, until, regs, (st,))
                else:
                    regs[UC_ARM_REG_R4] = st
                    run(e, start, until, regs)
                res.append(queue(e, 1)[0])
                if img is BUILT:
                    rg = ring(e, 1)[0]
                    regs_after = (e.uc.reg_read(UC_ARM_REG_R4), e.uc.reg_read(UC_ARM_REG_R5),
                                  e.uc.reg_read(UC_ARM_REG_R7))
            ch = slot + 2 if name == "song playback" else 1
            want = bytes([1, ch, idx + 12, vel3 * 16 + 15])
            ok(res[0] == res[1] == want and rg == pitch
               and regs_after[1:] == (idx, vel3),
               f"{name}: slot {slot} note {idx + 12} vel {vel3 * 16 + 15}: {want.hex()} as stock, pitch {pitch:+d}")
        e = emu(STOCK)                                   # the stock event ignores bits 18-31
        st = e.alloc(struct.pack("<8I", 0, 0, 0, 0, 0, 0, word(10, 4, -7), 0))
        if where == "sp":
            run(e, start, until, {UC_ARM_REG_R4: 0, UC_ARM_REG_R5: 10, UC_ARM_REG_R7: 4}, (st,))
        else:
            run(e, start, until, {UC_ARM_REG_R4: st, UC_ARM_REG_R5: 10, UC_ARM_REG_R7: 4})
        ok(queue(e, 1)[0][2:] == bytes([22, 0x4F]), f"{name}: stock firmware plays a pitched event normally")

    print("voice trigger: pad byte 5 = pad pitch + step pitch (clamped)")
    for pad in (0, 3, 9):
        for padp, step in ((0, 0), (0, 7), (5, -12), (-20, -10), (12, 12), (0, -24), (-3, 0)):
            got, slots = [], []
            for img in (STOCK, BUILT):
                e = emu(img)
                kit = e.call(KIT_PTR, e.u32(KIT_INDEX)) & 0xFFFFFFFF
                a = kit + 0x10 + pad * 8
                e.write(a + 5, struct.pack("b", padp))
                slot = e.read(a, 8)
                note = e.read(PADNOTES, 16)[pad]
                e.voices = []
                e.stub(VOICE_ON, lambda em, r: em.voices.append((r[0], r[1], r[2], em.read(r[3], 8))) or 0)
                if img is BUILT:                      # as the reader does: entry at QR, QR += 4
                    e.write(QR, b"\x20")
                    e.write(SYM["sp_ring"] + 0x20, struct.pack("b", step))
                    e.write(QR, b"\x24")
                e.call(NOTE_ON, 4, note, 100)
                got.append(e.voices)
                slots.append(slot)
            want5 = max(-24, min(19, padp + step)) & 0xFF
            ok(len(got[0]) == len(got[1]) == 1 and got[0][0][:3] == got[1][0][:3]
               and got[0][0][3] == slots[0] and got[1][0][3][:5] == slots[1][:5]
               and got[1][0][3][6:] == slots[1][6:] and got[1][0][3][5] == want5,
               f"pad {pad}: pad pitch {padp:+d} + step {step:+d} -> byte 5 = {struct.unpack('b', bytes([want5]))[0]:+d}, rest = kit slot")
    for img in (STOCK, BUILT):
        e = emu(img)
        e.voices = []
        e.stub(VOICE_ON, lambda em, r: em.voices.append(1) or 0)
        e.call(NOTE_ON, 4, 5, 100)                    # note 5 is no pad
        ok(not e.voices, f"{'stock' if img is STOCK else 'built'}: a note that is no pad starts nothing")

    print("type-8 handler 0x80049114 (step cells)")

    def cell(img, msgs, pre=None, nearly=0):
        e = emu(img)
        e.write(RUNNING, b"\x01")
        e.stub(NEARLY_FULL, lambda em, r: nearly)
        e.stub(CHANGED, lambda em, r: 0)
        e.stub(0x800489D4, lambda em, r: 0)
        if pre:
            for i, w in pre.items():
                e.write(CELLS + 4 * i, struct.pack("<I", w))
        for m in msgs:
            e.call(CELL_SET, *m)
        return [struct.unpack("<I", e.read(CELLS + 4 * i, 4))[0] for i in range(63)]

    stock_msgs = [[(0, 36, 0x4F)], [(0, 36, 0x7F)], [(1, 40, 100)], [(0, 74, 5)],
                  [(0, 36, 0x4F), (0, 36, 0)], [(0, 11, 0x4F)], [(0, 75, 0x4F)],
                  [(0, 50, 0x4F), (0xFFFFFFFF, 0xFFFFFFFF, 0)]]
    for msgs in stock_msgs:
        ok(cell(STOCK, msgs) == cell(BUILT, msgs), f"stock messages {msgs}: same cells as stock")
    ok(cell(STOCK, [(0, 36, 0x4F)], nearly=1) == cell(BUILT, [(0, 36, 0x4F)], nearly=1),
       "memory nearly full: note refused as stock")
    pitched = word(24, 4, -5) & ~0xFF
    c = cell(BUILT, [(0, 36, 0x7F)], pre={24: pitched})
    ok(c[24] == (24 << 8) | (7 << 15) | ((-5 & 0x3F) << 18), "velocity change keeps the pitch bits")
    for p in (-24, -1, 0, 1, 19):
        c = cell(BUILT, [(0x100 | ((p & 0x3F) << 16), 36, 0x40)], pre={24: word(24, 7, 3) & ~0xFF})
        ok(c[24] == (24 << 8) | (4 << 15) | ((p & 0x3F) << 18), f"pitch message {p:+d}: bits 18-23")
    c = cell(BUILT, [(0, 36, 0)], pre={24: pitched})
    ok(c[24] == 0, "DELETE (vel 0) clears the cell, pitch included")

    print("dial on a pad row")
    sp_mode = SYM["sp_mode"]

    def dial(mode, row, pre, d):
        e = emu(BUILT)
        e.write(sp_mode, struct.pack("<I", mode))
        e.write(ROW, struct.pack("<I", row))
        e.write(REDRAW, struct.pack("<I", 0))
        e.write(QR, b"\x40"); e.write(QW, b"\x40")
        note = e.read(PADNOTES, 16)[row - 1]
        if pre is not None:
            e.write(CELLS + 4 * (note - 12), struct.pack("<I", pre))
        e.stub(DIAL, lambda em, r: d)
        run(e, 0x8004E68C, 0x8004E72A if mode else 0x8004E696, {})
        return e, note

    e, note = dial(0, 3, None, 1)
    ok(e.uc.reg_read(UC_ARM_REG_R0) == 2 and e.uc.reg_read(UC_ARM_REG_R1) == PADNOTES
       and not e.msgs, "VEL: stock path (r0 = row - 1, r1 = pad note table)")
    for pre, d, wantp, wantv in ((None, 1, 1, 0x40), (None, -3, -3, 0x40),
                                 (word(0, 7, 4) & ~0xFF, 2, 6, 0x70),
                                 (word(0, 4, 18) & ~0xFF, 5, 19, 0x40),
                                 (word(0, 4, -22) & ~0xFF, -9, -24, 0x40)):
        e, note = dial(1, 3, pre, d)
        m = e.msgs[0] if e.msgs else b""
        wd = struct.unpack("<I", m[:4])[0] if m else 0
        ok(len(e.msgs) == 1 and m[4] == note and m[5] == wantv and m[7] == 8
           and wd & 0x100 and struct.unpack("b", bytes([((wd >> 16) & 0x3F) << 2]))[0] >> 2 == wantp,
           f"PITCH: cell {'empty' if pre is None else 'set'}, dial {d:+d} -> message pitch {wantp:+d} vel {wantv}")
        ok(queue(e, 1)[0] == bytes([1, 0, note, wantv | 0xF]) and ring(e, 1) == [wantp]
           and e.u32(REDRAW) == 1, f"  preview note {note} vel {wantv | 0xF} pitch {wantp:+d}, redraw")
    e, _ = dial(1, 3, None, 0)
    ok(not e.msgs and e.read(QW, 1) == b"\x40", "PITCH: no dial step, nothing sent")

    print("soft key 4")

    def key(ev, rec, alldel, mode=0):
        e = emu(BUILT)
        e.write(sp_mode, struct.pack("<I", mode))
        e.write(REC, struct.pack("<I", rec))
        e.write(ALLDEL, struct.pack("<I", alldel))
        e.pre = 0
        e.stub(KEY_PRE, lambda em, r: setattr(em, "pre", em.pre + 1) or 0)
        e.stub(0x8004BB5A, lambda em, r: 0)
        e.stub(0x800093EA, lambda em, r: 0)
        e.write(0x801EBF00, b"\x00")
        e.call(KEY, ev)
        return e.u32(sp_mode), e.pre, e.u32(REDRAW)

    ok(key(0x10038, 1, 0) == (1, 0, 1), "REC: 0x10038 -> PITCH, stock handler not run")
    ok(key(0x10038, 1, 0, 1)[0] == 0, "REC: 0x10038 again -> VEL")
    ok(key(0x10038, 0, 0)[:2] == (0, 1), "not REC: stock handler, mode unchanged")
    ok(key(0x10038, 1, 1)[:2] == (0, 1), "ALL DEL state: stock handler, mode unchanged")
    ok(key(0x10035, 1, 0)[:2] == (0, 1), "REC: other keys go to the stock handler")

    print("REC screen: soft key 4")

    def screen(mode, row, cellw, rec=1, redraw=0, shown=b""):
        e = emu(BUILT)
        e.write(sp_mode, struct.pack("<I", mode))
        e.write(REC, struct.pack("<I", rec))
        e.write(ALLDEL, struct.pack("<I", 0))
        e.write(ROW, struct.pack("<I", row))
        e.write(REDRAW, struct.pack("<I", redraw))
        e.write(SYM["sp_shown"], shown.ljust(8, b"\0"))
        if row:
            n = e.read(PADNOTES, 16)[row - 1]
            e.write(CELLS + 4 * (n - 12), struct.pack("<I", cellw))
        e.log = []
        e.stub(LCD_BEGIN, lambda em, r: em.log.append("begin") or 0)
        e.stub(LCD_END, lambda em, r: em.log.append("end") or 0)
        e.stub(REC_DRAW, lambda em, r: em.log.append(("draw", em.u32(REDRAW))) or 0)
        e.stub(LABEL, lambda em, r: em.log.append((r[0], em.cstring(r[1]), r[2])) or 0)
        e.call(SCREEN)
        return e.log

    for mode, row, cw, want in ((0, 0, 0, "  VEL  "), (1, 0, 0, " PITCH "), (1, 2, 0, " PITCH "),
                                (1, 2, word(0, 4, 5), "PIT+5  "), (1, 2, word(0, 4, -12), "PIT-12 "),
                                (1, 2, word(0, 7, 0), "PIT 0  "), (1, 2, word(0, 7, 19), "PIT+19 "),
                                (0, 2, word(0, 7, 5), "  VEL  ")):
        log = screen(mode, row, cw)
        ok(log == ["begin", ("draw", 1), (3, want, 0), "end"],
           f"mode {mode} row {row}: changed label -> full redraw + soft key 4 = {want!r}")
        ok(screen(mode, row, cw, shown=want.encode()) == ["begin", ("draw", 0), "end"],
           f"  same label, no redraw: stock update only")
    ok(screen(0, 0, 0, shown=b"  VEL  ", redraw=1)[2] == (3, "  VEL  ", 0),
       "firmware full redraw: soft key 4 drawn again")
    ok(screen(1, 0, 0, rec=0, redraw=1) == ["begin", ("draw", 1), "end"],
       "not REC: stock screen update, no soft key 4")

    print(f"\ntest_step_pitch: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
