#!/usr/bin/env python3
"""test_kit_assign.py — prove sdk/patches/kit_assign.c in the emulator.

1. The firmware's own FILE-menu opener (RAM 0x800409F6) runs on the patched
   image: the menu header it builds must say 6 items and point at our array:
   INFO / COPY / DELETE / RENAME / DIVIDE (the original records) + KIT ASSIGN.
2. The KIT ASSIGN callback runs with scripted dial/key events, a virtual SD
   card and the real custom_kit.c loader: copy, folder creation, reload, every
   message and every way out.

    python3 tools/test_kit_assign.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24sdk
from test_custom_kit import wav, tone, reference

try:
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = r24sdk.ROOT
PATCHES = [os.path.join(r24sdk.SDK, "patches", p) for p in ("custom_kit.c", "kit_assign.c")]
RATE = 44100
EXIT, ENTER, DIAL = 0x10019, 0x10024, 0x10000000
UP, DOWN, RIGHT, LEFT = 0x10020, 0x10021, 0x10022, 0x10023
SYM = {s["name"]: s["addr"] for s in r24sdk.load_syms()}
SELECTED, PROTECT = SYM["fw_selected_file"], SYM["fw_project_protect"]
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


class Ui:
    """Scripted front panel + graphic screen + virtual SD, on top of r24emu."""

    def __init__(self, img, b, files: dict[str, str], folder: str, project="PRJ007"):
        self.e = e = r24emu.R24Emu(img)
        self.b, self.sy = b, b.symbols
        self.events: list[int] = []
        self.deltas: list[int] = []
        self.screens: list[list[tuple[str, int, int]]] = []   # one per lock..unlock
        self.cur = None
        self.depth = 0
        self.msgs: list[str] = []
        self.calls: list[str] = []
        self.dirs: list[str] = []
        self.copies: list[tuple[str, str]] = []
        self.copy_err = 0
        self.guard = 0
        e.write(SYM["fw_screen"], struct.pack("<I", 0x8004ABCD))     # "previous screen"
        e.stub(SYM["fw_wait_event"], self._event)
        e.stub(SYM["fw_dial_delta"], lambda e_, r: self.deltas.pop(0))
        e.stub(SYM["fw_gfx_lock"], self._lock)
        e.stub(SYM["fw_gfx_unlock"], self._unlock)
        e.stub(SYM["fw_gfx_clear"], lambda e_, r: self.calls.append(f"clear{r[0]}") or 0)
        e.stub(SYM["fw_gfx_title"], lambda e_, r: self._draw(e_.cstring(r[0]), -1, r[1]))
        e.stub(SYM["fw_gfx_text"], lambda e_, r: self._draw(e_.cstring(r[0]), r[1], r[2]))
        e.stub(SYM["fw_set_screen"], self._set_screen)
        for n in ("fw_ui_enter", "fw_ui_leave", "fw_edit_reset"):
            e.stub(SYM[n], lambda e_, r, n=n: self.calls.append(n) or 0)
        e.stub(SYM["fw_edit_active"], lambda e_, r: 0)
        e.stub(SYM["fw_ui_message"], lambda e_, r: self.msgs.append(e_.cstring(r[0])) or 0)
        e.stub(SYM["fw_ui_progress"], lambda e_, r: self.calls.append("progress " + e_.cstring(r[0])) or 0)
        e.stub(SYM["fw_ui_progress_end"], lambda e_, r: self.calls.append("progress_end") or 0)
        e.stub(SYM["fw_make_dir"], lambda e_, r: self.dirs.append(e_.cstring(r[0])) or 0)
        e.stub(SYM["fw_file_copy"], self._copy)
        e.stub(SYM["fw_busy_guard"], lambda e_, r: self.guard)
        for n in ("fw_lcd_text", "fw_confirm", "fw_wait_ms"):        # text mode: must not be used
            e.stub(SYM[n], lambda e_, r, n=n: ok(False, f"{n} used on the graphic screen"))
        for n, data in files.items():
            e.files[(folder + "\\" + n).upper()] = data
        if project is None:
            e.write(SYM["fw_cur_project"], struct.pack("<i", -1))
        else:
            e.write(SYM["fw_cur_project"], struct.pack("<i", 7))
            e.write(SYM["fw_cur_project_name"], project.encode() + b"\0")
        self.table = r24bank.locate(img).literal
        k = r24sdk.kit_names(img).index("CUSTOM")
        self.ids = r24bank.kit_sample_ids(img)[k]

    def _lock(self, e, r):
        self.depth += 1
        self.cur = []
        return 0

    def _unlock(self, e, r):
        ok(self.depth == 1, "gfx lock/unlock balanced")
        self.depth -= 1
        self.screens.append(self.cur)
        return 0

    def _draw(self, text, x, y):
        ok(self.depth == 1, f"drawing {text!r} inside gfx_lock")
        self.cur.append((text, x, y))
        return 0

    def _set_screen(self, e, r):
        self.calls.append(f"screen {r[0]:08X}")
        e.write(SYM["fw_screen"], struct.pack("<I", r[0]))
        return 0

    def _event(self, e, r):
        ok(r[0] == 0xFFFFFFFF, "waits forever")
        return self.events.pop(0)

    def _copy(self, e, r):
        s, d = e.cstring(r[0]), e.cstring(r[1])
        self.copies.append((s, d))
        if self.copy_err or s.upper() not in e.files:
            return self.copy_err or 5
        e.files[d.upper()] = e.files[s.upper()]
        return 0

    def assign(self, name: str, events, deltas=()):
        self.e.write(SELECTED, name.encode() + b"\0")
        self.events, self.deltas = list(events), list(deltas)
        self.screens.clear()
        self.msgs.clear()
        self.calls.clear()
        self.e.call(self.sy["ka_menu_cb"])
        ok(not self.events and not self.deltas, "every scripted event consumed")
        ok(not self.e.handles, "files closed")
        ok(self.depth == 0, "no gfx lock left")
        if any(c.startswith("screen") for c in self.calls):
            ok(self.calls[0] == "screen 8003BC41" and self.calls[-1] == "screen 8004ABCD",
               f"screen set like FILE COPY and restored: {self.calls}")
            ok(self.calls[1:3] == ["fw_ui_enter", "fw_edit_reset"] and "fw_ui_leave" in self.calls,
               "ui_enter / edit_reset / ui_leave as FILE COPY")

    def row(self, screen, y):
        return [t for t, x, yy in self.screens[screen] if yy == y]

    def entry(self, slot):
        return struct.unpack("<II", self.e.read(self.table + 8 * self.ids[slot], 8))


def test_menu(img, b):
    e = r24emu.R24Emu(img)
    seen = {}

    def menu(e_, r):                                 # 0x8003A776(header, 0, 0, 0)
        seen["hdr"] = e_.read(r[0], 0x30)
        return 0

    e.stub(0x8003A776, menu)
    title = e.alloc(b"TRK01_00.WAV\0")
    e.call(0x800409F6, title)
    count, t, = struct.unpack_from("<II", seen["hdr"], 0)
    arr = struct.unpack_from("<I", seen["hdr"], 0x20)[0]
    ok(count == 6, f"menu header says {count} items")
    ok(t == title, "title passed through")
    ok(arr == b.symbols["ka_items"], "header points at our pointer array")
    labels, cbs = [], []
    for i in range(count):
        rec = e.u32(arr + 4 * i)
        labels.append(e.cstring(e.u32(rec)))
        cbs.append(e.u32(rec + 0x10))
        ok(e.u32(rec + 4) == 1, f"item {i} enabled")
    ok(labels == ["INFO", "COPY", "DELETE", "RENAME", "DIVIDE", "KIT ASSIGN"], labels)
    ok(cbs[:5] == [0x8003DEC3, 0x8003E07D, 0x8003E285, 0x8003E3DB, 0x8003EBA9],
       "original callbacks")
    ok(cbs[5] == b.symbols["ka_menu_cb"] | 1, "KIT ASSIGN callback (Thumb)")
    info, ours = e.u32(arr), e.u32(arr + 20)
    a, c = e.read(info, 0x50), e.read(ours, 0x50)
    ok(a[4:0x10] == c[4:0x10] and a[0x14:] == c[0x14:], "our record = INFO's but label/callback")


def test_assign(img, b):
    folder = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\AUDIO"
    kick = wav(tone(20000, 2), 48000)
    files = {"KICK.WAV": kick, "snare.wav": wav(tone(8000), RATE),
             "NOTES.TXT": b"hello", "BAD.WAV": b"JUNK" + bytes(60)}
    u = Ui(img, b, files, folder)
    kit = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT"
    d0 = [u.entry(s) for s in range(16)]

    # dial +2, cursor right, ENTER -> pad 4: copied, loaded, message after exit
    u.assign("KICK.WAV", [DIAL, RIGHT, ENTER], [2])
    sc = u.screens
    ok(len(sc) == 3, f"3 redraws, got {len(sc)}")
    ok(("KIT ASSIGN", -1, 0x20) in sc[0], "title")
    ok(u.row(0, 0x0C) == ["FILE", "KICK.WAV"], f"file row {u.row(0, 0x0C)}")
    ok([u.row(k, 0x15) for k in range(3)] ==
       [["PAD 01", "KICK"], ["PAD 03", "CL HH"], ["PAD 04", "OP HH"]], "pad row follows the dial")
    ok(all(len(t) * 6 + x <= 128 for t, x, y in sc[0] if x >= 0), "every row fits 128 px")
    ok(u.dirs == [kit], f"KIT folder created: {u.dirs}")
    ok(u.copies == [(folder + "\\KICK.WAV", kit + "\\KICK.WAV")], f"copied, name kept: {u.copies}")
    cfg = u.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"]
    ok(cfg.startswith(b"# ZOOM R24 CUSTOM kit") and cfg.endswith(b"\r\n04=KICK.WAV\r\n"),
       f"KIT.TXT written: {cfg!r}")
    ok("progress Copying..." in u.calls and "progress_end" in u.calls, "progress box")
    ok(u.msgs == ["Complete!"], f"message {u.msgs}")
    ok(u.calls.index("screen 8004ABCD") < len(u.calls), "screen restored before the message")
    rel, ln = u.entry(3)
    ok(u.e.read(r24bank.RAM_BASE + rel, ln) == reference(kick, (6 * RATE * 2) & ~31),
       "pad 4 plays the converted WAV")
    ok(all(u.entry(s) == d0[s] for s in range(16) if s != 3), "other pads unchanged")

    # again: starts at the last pad, now marked *
    u.assign("snare.wav", [ENTER])
    ok(u.row(0, 0x15) == ["PAD 04", "OP HH *"], f"{u.row(0, 0x15)}")
    ok(u.msgs == ["Complete!"] and
       u.e.files[(kit + "\\SNARE.WAV").upper()] == files["snare.wav"], "replaced")
    ok((kit + "\\KICK.WAV").upper() not in u.e.files and
       u.e.removed == [(kit + "\\KICK.WAV").upper()], "old file of the pad deleted (unused)")
    ok(u.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"].endswith(b"\r\n04=snare.wav\r\n"),
       "KIT.TXT updated")
    # the same file on two pads: replacing one keeps the file for the other
    u.assign("snare.wav", [DOWN, ENTER])                    # pad 5 = SNARE.WAV too
    u.e.removed.clear()
    u.assign("KICK.WAV", [UP, ENTER])                       # pad 4 = KICK.WAV
    ok(u.e.removed == [] and (kit + "\\SNARE.WAV").upper() in u.e.files,
       "shared file kept")
    ok(u.e.files["A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT.TXT"].endswith(
       b"\r\n04=KICK.WAV\r\n05=snare.wav\r\n"), "both pads in KIT.TXT")

    # clamping, EXIT
    n_copies = len(u.copies)
    u.assign("KICK.WAV", [DIAL, EXIT], [-9])
    ok(u.row(1, 0x15)[0] == "PAD 01" and u.msgs == [] and len(u.copies) == n_copies,
       "clamped at pad 1, EXIT does nothing")
    u.assign("KICK.WAV", [DIAL, LEFT, EXIT], [99])
    ok(u.row(1, 0x15)[0] == "PAD 16" and u.row(2, 0x15)[0] == "PAD 15", "clamped at 16")

    # refusals before the screen opens
    u.assign("NOTES.TXT", [])
    ok(u.msgs == ["Not a WAV file"] and u.screens == [], "non-WAV")
    u.e.write(PROTECT, struct.pack("<i", 1))
    u.assign("KICK.WAV", [])
    ok(u.msgs == ["Project Protect"] and u.screens == [], "protected project")
    u.e.write(PROTECT, struct.pack("<i", 0))
    u.guard = 1
    u.assign("KICK.WAV", [])
    ok(u.msgs == [] and u.screens == [], "busy guard: silent abort")
    u.guard = 0
    u.assign("", [])
    ok(u.msgs == [] and u.screens == [], "no file selected")

    # errors
    u.assign("BAD.WAV", [DIAL, ENTER], [99])
    ok(u.msgs == ["Bad WAV Format"] and u.entry(15) == d0[15], "bad WAV keeps the default")
    u.copy_err = 0x1C
    u.assign("KICK.WAV", [ENTER])
    ok(u.msgs == ["Card Full"], f"{u.msgs}")
    u.copy_err = 3
    u.assign("KICK.WAV", [ENTER])
    ok(u.msgs == ["File Access Error"], f"{u.msgs}")

    u3 = Ui(img, b, {"KICK.WAV": kick}, folder, project=None)
    u3.assign("KICK.WAV", [])
    ok(u3.msgs == ["No Project"] and u3.copies == [], "no project")


def main() -> int:
    img, b = build_image()
    test_menu(img, b)
    test_assign(img, b)
    print(f"test_kit_assign: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
