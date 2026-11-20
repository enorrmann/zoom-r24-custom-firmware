#!/usr/bin/env python3
"""test_kit_editor.py — prove sdk/patches/kit_editor.c in the emulator.

KIT SELECT's ENTER callback (record 0x800FD398 + 0x24) is called as the menu
engine would, with the firmware's own confirm code underneath: choosing CUSTOM
opens the 16-pad editor, any other kit does not. The editor is then driven
with scripted arrows / dial / ENTER / EXIT over a virtual SD card, with the
real custom_kit.c loader and kit_assign.c copy.

    python3 tools/test_kit_editor.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24sdk
import test_kit_assign as tka
from test_custom_kit import wav, tone, reference

ROOT = r24sdk.ROOT
PATCHES = [os.path.join(r24sdk.SDK, "patches", p)
           for p in ("custom_kit.c", "kit_assign.c", "kit_editor.c")]
SYM = tka.SYM
RATE = 44100
EXIT, ENTER, DIAL = 0x10019, 0x10024, 0x10000000
UP, DOWN, RIGHT, LEFT = 0x10020, 0x10021, 0x10022, 0x10023
HELD_DOWN = 0x20021
SEL_VAR, KIT_RECORD = 0x801EC3F0, 0x800FD398
ROWY = [0x0C + 9 * i for i in range(6)]
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


def build_image():
    data = bytearray(open(os.path.join(ROOT, "R24SYS.BIN"), "rb").read())
    b, _rel, _n = r24sdk.build(data, PATCHES, ["CUSTOM_KIT_RESERVE_SEC=6"])
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        return open(out, "rb").read(), b


class Editor(tka.Ui):
    def __init__(self, img, b, files, rate48=False):
        folder = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\AUDIO"
        super().__init__(img, b, files, folder)
        self.folder = folder
        self.listed = []
        self.pool = list(files)
        e = self.e
        e.stub(SYM["fw_list_files"], self._list)
        e.write(SYM["fw_project_settings"] + 0x38, struct.pack("<I", 1 if rate48 else 0))
        self.custom = r24sdk.kit_names(img).index("CUSTOM")

    def _list(self, e, r):
        lst, flags = r[0], r[1]
        self.listed.append((e.cstring(e.u32(lst)), flags))
        for i, n in enumerate(self.pool):
            e.write(SYM["fw_list_names"] + 260 * i, n.encode() + b"\0")
        e.write(lst + 8, struct.pack("<i", len(self.pool)))
        return 0

    def kit_enter(self, kit: int, events, deltas=()):
        """KIT SELECT: choose `kit`, press ENTER (the engine calls +0x24)."""
        e = self.e
        e.write(SEL_VAR, struct.pack("<i", kit))
        self.events, self.deltas = list(events), list(deltas)
        self.screens.clear()
        self.msgs.clear()
        self.calls.clear()
        e.call(e.u32(KIT_RECORD + 0x24))
        ok(not self.events and not self.deltas, "every scripted event consumed")
        ok(self.depth == 0 and not e.handles, "no gfx lock / file left open")
        ok(e.u32(SYM["fw_kit_index"]) == kit, "the original confirm still ran (set_kit)")

    def rows(self, k):
        """[(cursor, label, value)] of screen k."""
        out = []
        for y in ROWY[:5]:
            t = [s for s, x, yy in self.screens[k] if yy == y]
            if not t:
                continue
            cur = t[0] == ">"
            if cur:
                t = t[1:]
            out.append((cur, t[0], t[1] if len(t) > 1 else ""))
        return out

    def ks_rows(self, k):
        """KIT SETTINGS screen k: [(cursor, pad, [(selected, text) x3])]."""
        out = []
        for y in ROWY[1:5]:
            t = sorted([(x, s) for s, x, yy in self.screens[k] if yy == y])
            if not t:
                continue
            cur = t[0] == (1, ">")
            if cur:
                t = t[1:]
            pad, vals, sel = t[0][1], [], False
            for x, s in t[1:]:
                if s == ">":
                    sel = True
                    continue
                vals.append((sel, s))
                sel = False
            out.append((cur, pad, vals))
        return out

    def kit_slot(self, slot):
        """(level, pan, group) of the CUSTOM kit's pad in the live kit table."""
        tab = self.e.u32(SYM["fw_lit_kit_table"])
        a = tab + self.custom * 0x90 + 0x10 + slot * 8
        lv, pan, grp = self.e.read(a + 2, 3)
        return lv, pan, grp & 7

    def mono(self, slot):
        """MONO bit (byte 4 bit 4) of the CUSTOM kit's pad: 1 = POLY 0."""
        tab = self.e.u32(SYM["fw_lit_kit_table"])
        return self.e.read(tab + self.custom * 0x90 + 0x10 + slot * 8 + 4, 1)[0] >> 4 & 1

    def status(self, k):
        return [s for s, x, y in self.screens[k] if y == ROWY[5]]


def main() -> int:
    img, b = build_image()
    kick, snare = wav(tone(20000, 2), 48000), wav(tone(8000), RATE)
    files = {"KICK.WAV": kick, "SNARE.WAV": snare, "LONGNAMEFILE01.WAV": wav(tone(3000), RATE)}
    ed = Editor(img, b, files)
    kit = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT"

    # another kit: no editor, the original confirm only
    ed.kit_enter(0, [])
    ok(ed.screens == [] and ed.listed == [], "BASIC: no editor")
    ok(ed.e.u32(r24bank.KIT_COUNT_OFF - 0x5B4 + 0x80000000) == 11, "11 kits listed")

    # CUSTOM: editor opens, lists the project's WAVs (flag 0x01: no header reads)
    ed.kit_enter(ed.custom, [EXIT])
    ok(ed.listed == [(ed.folder, 0x01)], f"lister call {ed.listed}")
    r = ed.rows(0)
    ok(("CUSTOM KIT", -1, 0x20) in ed.screens[0], "title")
    ok([x[1] for x in r] == ["SETTINGS", "KICK", "SNARE", "CL HH", "OP HH"], f"first rows {r}")
    ok([x[0] for x in r] == [True, False, False, False, False], "cursor on SETTINGS")
    ok(r[0][2] == "" and all(x[2] == "-" for x in r[1:]), "all pads at their default")
    ok(all(len(t) * 6 + x <= 128 for t, x, y in ed.screens[0] if x >= 0), "rows fit 128 px")
    ok(all(len(n) <= 6 for n in ("KICK", "SNARE", "CL HH", "OP HH", "CRASH", "RIDE", "TOM1",
                                 "TOM2", "TOM3", "CLAP", "TAMB", "COWB", "CLAVE", "H CONG",
                                 "L CONG", "PERC")), "short labels")
    ok(ed.calls[0] == "screen 8003BC41" and ed.calls[-1] == "screen 8004ABCD", "screen restored")

    # RIGHT twice -> SNARE.WAV? on KICK, ENTER assigns it
    ed.kit_enter(ed.custom, [DOWN, RIGHT, RIGHT, ENTER, EXIT])
    ok(ed.rows(2)[1][2] == "KICK?", f"{ed.rows(2)[1]}")
    ok(ed.rows(3)[1][2] == "SNARE?", f"{ed.rows(3)[1]}")
    ok(ed.copies == [(ed.folder + "\\SNARE.WAV", kit + "\\SNARE.WAV")], f"{ed.copies}")
    ok(ed.rows(4)[1][2] == "SNARE" and ed.status(4) == ["Complete!"], "assigned + status")
    rel, ln = ed.entry(0)
    ok(ed.e.read(r24bank.RAM_BASE + rel, ln) == reference(snare, (6 * RATE * 2) & ~31),
       "pad 1 plays SNARE.WAV")

    # DOWN x6 (one held) scrolls; RIGHT continues after the file just assigned; dial
    ed.kit_enter(ed.custom, [DOWN, DOWN, DOWN, DOWN, HELD_DOWN, DOWN, RIGHT, DIAL, ENTER, EXIT],
                 [-1])
    r = ed.rows(6)
    ok([x[1] for x in r] == ["CL HH", "OP HH", "CRASH", "RIDE", "TOM1"], f"scrolled {r}")
    ok(r[4][0] and r[4][2] == "-", "cursor on TOM1 (pad 7), list scrolled")
    ok(ed.rows(7)[4][2] == "LONGNAMEFILE?", f"continues after SNARE: {ed.rows(7)[4]}")
    ok(ed.rows(8)[4][2] == "SNARE?", f"dial -1: {ed.rows(8)[4]}")
    ok(ed.copies[-1] == (ed.folder + "\\SNARE.WAV", kit + "\\SNARE.WAV"), "pad 7 assigned")
    ok(ed.e.u32(b.symbols["ke_row"]) == 7, "cursor remembered")

    # pending choice not applied on EXIT; ENTER without a choice does nothing
    n = len(ed.copies)
    ed.kit_enter(ed.custom, [RIGHT, EXIT])
    ed.kit_enter(ed.custom, [ENTER, EXIT])
    ok(len(ed.copies) == n, "EXIT discards, ENTER on nothing is a no-op")
    # the position survives leaving (LONGNAMEFILE, by name); wraps through none
    ed.kit_enter(ed.custom, [RIGHT, RIGHT, EXIT])
    ok(ed.rows(1)[4][2] == "SNARE", f"from LONGNAMEFILE to none: {ed.rows(1)[4]}")
    ok(ed.rows(2)[4][2] == "KICK?", f"then the first file: {ed.rows(2)[4]}")
    ed.kit_enter(ed.custom, [LEFT, LEFT, EXIT])
    ok(ed.rows(1)[4][2] == "SNARE" and ed.rows(2)[4][2] == "LONGNAMEFILE?",
       f"LEFT from none wraps to the last file: {ed.rows(2)[4]}")
    # the position is shared by every pad
    ed.kit_enter(ed.custom, [UP, LEFT, DOWN, EXIT])
    ok(ed.rows(2)[3][2] == "SNARE?", f"pad 6 continues from pad 7's position: {ed.rows(2)}")

    # protected project / bad WAV / position found again in a new list / no files
    ed.e.write(tka.PROTECT, struct.pack("<i", 1))
    ed.kit_enter(ed.custom, [RIGHT, RIGHT, RIGHT, ENTER, EXIT])
    ok(ed.status(4) == ["Project Protect"] and len(ed.copies) == n, "protected")
    ed.e.write(tka.PROTECT, struct.pack("<i", 0))
    ed.pool = ["BAD.WAV"]
    ed.e.files[(ed.folder + "\\BAD.WAV").upper()] = b"JUNK" + bytes(60)
    ed.kit_enter(ed.custom, [RIGHT, ENTER, EXIT])
    ok(ed.status(2) == ["Bad WAV Format"], f"{ed.status(2)}")
    ed.pool = ["AAA.WAV", "BAD.WAV", "ZZZ.WAV"]
    ed.kit_enter(ed.custom, [RIGHT, EXIT])
    ok(ed.rows(1)[4][2] == "ZZZ?", f"BAD.WAV found at its new place: {ed.rows(1)[4]}")
    ed.pool = []
    ed.kit_enter(ed.custom, [RIGHT, EXIT])
    ok(ed.status(1) == ["No File"], "no files")
    e48 = Editor(img, b, files, rate48=True)
    e48.kit_enter(e48.custom, [EXIT])
    ok(e48.listed[0][1] == 0x01, "48 kHz project: same list (the loader resamples)")

    # power off / on: a new R24 on the same card opens PRJ007 -> names from
    # KIT.TXT, sounds reloaded; an older SAMPLEnn.WAV kit shows its file name
    cfg = ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(cfg.endswith(b"\r\n01=SNARE.WAV\r\n07=BAD.WAV\r\n"), f"KIT.TXT {cfg!r}")
    ok((kit + "\\SNARE.WAV").upper() in ed.e.files, "SNARE.WAV kept: pad 1 still uses it")
    card = dict(ed.e.files)
    card[(kit + "\\SAMPLE03.WAV").upper()] = kick
    fresh = Editor(img, b, files)
    fresh.pool = list(files)
    fresh.e.files.update(card)
    fresh.e.stub(0x8001D212, lambda e_, r: e_.alloc(b"PRJ007\0"))
    fresh.e.stub(0x8001D63A, lambda e_, r: 0)
    fresh.e.call(0x8001D6A6, 7)                       # set_current_project(7)
    fresh.kit_enter(fresh.custom, [EXIT])
    r = fresh.rows(0)
    ok([x[2] for x in r[1:4]] == ["SNARE", "-", "SAMPLE03"], f"after reload {r}")
    rel, ln = fresh.entry(0)
    ok(fresh.e.read(r24bank.RAM_BASE + rel, ln) == reference(snare, (6 * RATE * 2) & ~31),
       "pad 1 sounds again after reload")
    # a listed file that is gone shows NAME!
    del fresh.e.files[(kit + "\\SNARE.WAV").upper()]
    fresh.e.call(0x8001D6A6, 7)
    fresh.kit_enter(fresh.custom, [EXIT])
    ok(fresh.rows(0)[1][2] == "SNARE!", f"missing file flagged {fresh.rows(0)[1]}")

    # ── SETTINGS: volume / pan / mute group per pad ──
    ked = b.symbols["ke_row"]
    ed.e.write(ked, struct.pack("<i", 0))
    ok(ed.kit_slot(0) == (14, 64, 0) and ed.kit_slot(2) == (7, 99, 1), "kit = BASIC values")
    ed.kit_enter(ed.custom, [ENTER, EXIT, EXIT])
    k = 1                                   # screen 0 = editor, 1 = KIT SETTINGS
    ok(("KIT SETTINGS", -1, 0x20) in ed.screens[k], "settings title")
    hdr = sorted((x, s) for s, x, y in ed.screens[k] if y == ROWY[0])
    ok([s for x, s in hdr] == ["PAD", "VOL", "PAN", "MUTE"], f"header {hdr}")
    r = ed.ks_rows(k)
    ok([x[1] for x in r] == ["KICK", "SNARE", "CL HH", "OP HH"], f"pads {r}")
    ok(r[0][0] and r[0][2] == [(True, "14"), (False, "C"), (False, "OFF")], f"KICK {r[0]}")
    ok(r[2][2] == [(False, "7"), (False, "R35"), (False, "1")], f"CL HH {r[2]}")
    ok(all(len(t) * 6 + x <= 128 for t, x, y in ed.screens[k] if x >= 0), "fits 128 px")
    ok(ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"] == cfg, "nothing changed: not saved")

    # KICK vol -3, pan +5, mute +2; SNARE mute +1; ENTER saves; changes are live
    ed.kit_enter(ed.custom, [ENTER, DIAL, RIGHT, DIAL, RIGHT, DIAL, DOWN, DIAL, ENTER, EXIT, EXIT],
                 [-3, 5, 2, 1])
    ok(ed.ks_rows(2)[0][2][0] == (True, "11"), f"vol 11 {ed.ks_rows(2)[0]}")
    ok(ed.ks_rows(4)[0][2][1] == (True, "R5"), f"pan R5 {ed.ks_rows(4)[0]}")
    ok(ed.ks_rows(6)[0][2][2] == (True, "2"), f"mute 2 {ed.ks_rows(6)[0]}")
    ok(ed.ks_rows(8)[1][2][2] == (True, "1") and ed.ks_rows(8)[1][0], f"snare {ed.ks_rows(8)[1]}")
    ok(ed.status(9) == ["Saved!"], f"saved {ed.status(9)}")
    ok(ed.kit_slot(0) == (11, 69, 2) and ed.kit_slot(1) == (13, 64, 1), "kit table updated live")
    cfg2 = ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(cfg2.endswith(b"01=SNARE.WAV\r\n07=BAD.WAV\r\nS01=11,69,2\r\nS02=13,64,1\r\n"),
       f"KIT.TXT {cfg2!r}")
    ok(ed.e.u32(b.symbols["ks_row"]) == 1 and ed.e.u32(b.symbols["ks_col"]) == 2,
       "settings cursor remembered")

    # limits; EXIT saves without ENTER; back to the default value drops the line
    ed.e.write(b.symbols["ks_row"], struct.pack("<i", 0))
    ed.e.write(b.symbols["ks_col"], struct.pack("<i", 0))
    ed.kit_enter(ed.custom, [ENTER, DIAL, RIGHT, DIAL, RIGHT, DIAL, EXIT, EXIT], [40, -200, 50])
    ok(ed.kit_slot(0) == (15, 0, 7), f"clamped {ed.kit_slot(0)}")
    ok(ed.ks_rows(6)[0][2] == [(False, "15"), (False, "L64"), (True, "7")], f"{ed.ks_rows(6)[0]}")
    cfg3 = ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=15,0,7\r\n" in cfg3, f"saved on EXIT {cfg3!r}")
    ed.e.write(b.symbols["ks_col"], struct.pack("<i", 0))
    ed.kit_enter(ed.custom, [ENTER, DIAL, RIGHT, DIAL, RIGHT, DIAL, EXIT, EXIT], [-1, 64, -7])
    ok(ed.kit_slot(0) == (14, 64, 0), "KICK back to the kit's values")
    cfg4 = ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=" not in cfg4 and b"S02=13,64,1\r\n" in cfg4, f"default -> no line {cfg4!r}")

    # protected project: not written
    ed.e.write(tka.PROTECT, struct.pack("<i", 1))
    ed.kit_enter(ed.custom, [ENTER, DIAL, ENTER, EXIT, EXIT], [1])
    ok(ed.status(3) == ["Project Protect"], f"protect {ed.status(3)}")
    ok(ed.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"] == cfg4, "KIT.TXT untouched")
    ed.e.write(tka.PROTECT, struct.pack("<i", 0))

    # reload on another R24: settings come back from KIT.TXT; a project
    # without settings (other project) gets the kit's own values
    card = dict(ed.e.files)
    fresh = Editor(img, b, files)
    fresh.e.files.update(card)
    fresh.e.stub(0x8001D212, lambda e_, r: e_.alloc(b"PRJ007\0"))
    fresh.e.stub(0x8001D63A, lambda e_, r: 0)
    fresh.e.call(0x8001D6A6, 7)
    ok(fresh.kit_slot(1) == (13, 64, 1), f"SNARE mute group back {fresh.kit_slot(1)}")
    fresh.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"] = (
        b"01=SNARE.WAV\r\nS03=9,10,3\r\nS04=99,1,1\r\nS05=1,2\r\nS06=x\r\n")
    fresh.e.call(0x8001D6A6, 7)
    ok(fresh.kit_slot(2) == (9, 10, 3), "S03 applied")
    ok(fresh.kit_slot(3) == (7, 99, 1) and fresh.kit_slot(4) == (9, 44, 0)
       and fresh.kit_slot(5) == (12, 19, 0), "bad lines ignored (kit values)")
    ok(fresh.kit_slot(1) == (13, 64, 0), "no S02 line: SNARE back to the kit's values")

    # ── SETTINGS page 2: PITCH (pad byte 5, signed, read by drum_pitch.c) ──
    def pitch(e_, slot):
        tab = e_.e.u32(SYM["fw_lit_kit_table"])
        return struct.unpack("<b", e_.e.read(tab + e_.custom * 0x90 + 0x10 + slot * 8 + 5, 1))[0]

    def page(e_, k):
        return sorted((x, s) for s, x, y in e_.screens[k] if y == ROWY[0])

    ok(all(pitch(fresh, s) == 0 for s in range(16)), "pitch 0 on every pad by default")
    fresh.e.write(b.symbols["ks_row"], struct.pack("<i", 0))
    fresh.e.write(b.symbols["ks_col"], struct.pack("<i", 0))
    fresh.e.write(b.symbols["ke_row"], struct.pack("<i", 0))
    # RIGHT x3 -> page 2; dial +5 on KICK; DOWN; dial -40 on SNARE (clamped -24); ENTER
    fresh.kit_enter(fresh.custom, [ENTER, RIGHT, RIGHT, RIGHT, DIAL, DOWN, DIAL, ENTER, EXIT, EXIT],
                    [5, -40])
    ok([s for x, s in page(fresh, 3)] == ["PAD", "VOL", "PAN", "MUTE"], "page 1 up to MUTE")
    ok([s for x, s in page(fresh, 4)] == ["PAD", "PITCH", "POLY"], f"RIGHT after MUTE: page 2 {page(fresh, 4)}")
    r = fresh.ks_rows(4)
    ok(r[0][2] == [(True, "0"), (False, "0")] and r[1][2] == [(False, "0")] * 2,
       f"pitch / poly columns {r}")
    ok(fresh.ks_rows(5)[0][2][0] == (True, "+5"), f"+5 {fresh.ks_rows(5)[0]}")
    ok(fresh.ks_rows(7)[1][2][0] == (True, "-24"), f"clamped -24 {fresh.ks_rows(7)[1]}")
    ok(pitch(fresh, 0) == 5 and pitch(fresh, 1) == -24, "kit byte 5 updated live")
    ok(fresh.kit_slot(0)[:3] == (14, 64, 0), "other values untouched")
    ok(all(len(t) * 6 + x <= 128 for t, x, y in fresh.screens[7] if x >= 0), "page 2 fits 128 px")
    ok(all(fresh.mono(s) for s in range(16)), "POLY 0 by default: MONO bit set on every pad")
    cfg5 = fresh.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=14,64,0,+5\r\n" in cfg5 and b"S02=13,64,0,-24\r\n" in cfg5, f"KIT.TXT {cfg5!r}")
    # LEFT from PITCH back to page 1 (MUTE)
    fresh.kit_enter(fresh.custom, [ENTER, LEFT, EXIT, EXIT])
    ok([s for x, s in page(fresh, 2)] == ["PAD", "VOL", "PAN", "MUTE"], "LEFT: page 1 again")
    ok(fresh.e.u32(b.symbols["ks_col"]) == 2, "cursor on MUTE")
    # +12 limit, and back to 0 drops the 4th value
    fresh.e.write(b.symbols["ks_col"], struct.pack("<i", 3))
    fresh.e.write(b.symbols["ks_row"], struct.pack("<i", 0))
    fresh.kit_enter(fresh.custom, [ENTER, DIAL, EXIT, EXIT], [99])
    ok(pitch(fresh, 0) == 19, "clamped +19 (DSP limit)")
    fresh.kit_enter(fresh.custom, [ENTER, DIAL, EXIT, EXIT], [-19])
    cfg6 = fresh.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=" not in cfg6 and b"S02=13,64,0,-24\r\n" in cfg6, f"pitch 0 = default: no line {cfg6!r}")
    # reload: pitch from KIT.TXT; bad pitch values reject the line
    card = dict(fresh.e.files)
    re_ = Editor(img, b, files)
    re_.e.files.update(card)
    re_.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"] = (
        b"S02=13,64,0,-12\r\nS03=9,10,3,+7\r\nS04=9,10,3,20\r\nS05=9,10,3,x\r\nS06=9,10,0, 3\r\n")
    re_.e.stub(0x8001D212, lambda e_, r: e_.alloc(b"PRJ007\0"))
    re_.e.stub(0x8001D63A, lambda e_, r: 0)
    re_.e.call(0x8001D6A6, 7)
    ok([pitch(re_, s) for s in range(1, 6)] == [-12, 7, 0, 0, 3], "pitch reloaded from KIT.TXT")
    ok(re_.kit_slot(3) == (7, 99, 1) and re_.kit_slot(2) == (9, 10, 3),
       "out-of-range pitch: line ignored; old 3-value lines still work")

    # ── POLY (page 2, second column): 0 = MONO bit set (default), 1 = overlap ──
    re_.e.write(b.symbols["ks_row"], struct.pack("<i", 0))
    re_.e.write(b.symbols["ks_col"], struct.pack("<i", 3))
    re_.e.write(b.symbols["ke_row"], struct.pack("<i", 0))
    re_.kit_enter(re_.custom, [ENTER, RIGHT, DIAL, RIGHT, EXIT, EXIT], [5])
    ok(re_.ks_rows(3)[0][2] == [(False, "0"), (True, "1")], f"POLY 1 {re_.ks_rows(3)[0]}")
    ok(re_.e.u32(b.symbols["ks_col"]) == 4, "RIGHT stops at the last column")
    ok(re_.mono(0) == 0 and re_.mono(1) == 1, "KICK MONO bit cleared live, others kept")
    ok(re_.kit_slot(0) == (14, 64, 0) and pitch(re_, 0) == 0, "other values untouched")
    cfg7 = re_.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=14,64,0,0,1\r\n" in cfg7 and b"S03=9,10,3,+7\r\n" in cfg7,
       f"KIT.TXT poly as 5th value, pitch kept {cfg7!r}")
    re_.e.write(b.symbols["ks_col"], struct.pack("<i", 4))
    re_.kit_enter(re_.custom, [ENTER, DIAL, EXIT, EXIT], [-3])
    ok(re_.mono(0) == 1, "POLY back to 0: MONO again")
    cfg8 = re_.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(b"S01=" not in cfg8, f"all defaults: no line {cfg8!r}")
    # reload with poly lines; bad poly rejects the line
    card = dict(re_.e.files)
    r2 = Editor(img, b, files)
    r2.e.files.update(card)
    r2.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"] = (
        b"S01=14,64,0,0,1\r\nS02=13,64,0,-3,1\r\nS03=9,10,3,0,2\r\nS04=7,99,1,+2\r\n")
    r2.e.stub(0x8001D212, lambda e_, r: e_.alloc(b"PRJ007\0"))
    r2.e.stub(0x8001D63A, lambda e_, r: 0)
    r2.e.call(0x8001D6A6, 7)
    ok([r2.mono(s) for s in range(5)] == [0, 0, 1, 1, 1], "poly reloaded (S03 poly 2: line ignored)")
    ok(pitch(r2, 1) == -3 and pitch(r2, 3) == 2 and r2.kit_slot(2) == (7, 99, 1),
       "pitch with poly; S03 back to kit values")


    # text-mode calls never used (tka.Ui fails the test if they are)
    print(f"test_kit_editor: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
