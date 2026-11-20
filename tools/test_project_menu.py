#!/usr/bin/env python3
"""test_project_menu.py — prove sdk/patches/project_menu.c in the emulator.

1. The firmware's own PROJECT-menu opener (0x80019610) hands the menu engine
   14 items: IMPORT SD, IMPORT USB, CLEAR UNUSED, DUMP 1, DUMP 2 after FILE.
2. IMPORT SD / USB with the (device-verified LOOP) browser and the lister
   stubbed, a virtual card + stick, scripted keys. IMPORT USB must make
   LOOP[USB]'s calls in LOOP[USB]'s order.
3. CLEAR UNUSED deletes exactly the files whose take map entry is -1.
4. DUMP 1 / DUMP 2 write every region byte for byte to A:\DUMP1 / A:\DUMP2, plus INFO.TXT.

    python3 tools/test_project_menu.py
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

PATCHES = [os.path.join(r24sdk.SDK, "patches", p) for p in
           ("custom_kit.c", "kit_assign.c", "kit_editor.c", "project_menu.c",
            "mem_dump.c")]
SYM = tka.SYM
EXIT, ENTER, DIAL = 0x10019, 0x10024, 0x10000000
AUDIO = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\AUDIO"
USB_SEQ = ["fw_audio_mode", "fw_usb_wait_box", "fw_save_project_now", "fw_close_audio_files",
           "fw_usb_host", "fw_usb_wait", "fw_rescan_drive", "fw_usb_off",
           "fw_reload_tracks_a", "fw_reload_tracks_b", "fw_audio_mode"]
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


def build_image():
    data = bytearray(open(os.path.join(r24sdk.ROOT, "R24SYS.BIN"), "rb").read())
    b, _rel, _n = r24sdk.build(data, PATCHES, ["CUSTOM_KIT_RESERVE_SEC=6", "PM_DUMP"])
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        return open(out, "rb").read(), b


class Pm(tka.Ui):
    def __init__(self, img, b, card: dict[str, bytes], takes: dict[str, int] | None = None):
        super().__init__(img, b, {}, AUDIO)
        self.e.files.update({k.upper(): v for k, v in card.items()})
        self.card = dict(card)
        self.takes = takes or {}
        self.pick = None
        self.listed = []
        self.progress = []
        self.seq = []
        self.usb_host_ret, self.usb_wait_ret = 0, 0
        e = self.e
        e.stub(SYM["fw_browse"], self._browse)
        e.stub(SYM["fw_list_files"], self._list)
        e.stub(SYM["fw_ui_progress"], lambda e_, r: self.progress.append(e_.cstring(r[0])) or 0)
        e.stub(SYM["fw_busy_guard"], lambda e_, r: self.seq.append(("guard", r[0])) or self.guard)
        e.stub(SYM["fw_sync_rec_guard"], lambda e_, r: self.seq.append(("sync",)) or 0)
        for n in USB_SEQ:
            e.stub(SYM[n], lambda e_, r, n=n: self._seq(n, r))
        e.stub(SYM["fw_audio_sync"], lambda e_, r: self.seq.append(("sync_audio",)) or 0)
        e.stub(SYM["fw_remove"], self._remove)
        e.write(SYM["fw_project_open_flag"], struct.pack("<i", 1))
        e.write(SYM["fw_rec_settings"] + 0x34, struct.pack("<I", 3))
        e.write(SYM["fw_track_sel"], struct.pack("<i", 5))

    def _seq(self, n, r):
        self.seq.append((n, r[0]) if n in ("fw_audio_mode", "fw_rescan_drive") else (n,))
        if n == "fw_usb_host":
            return self.usb_host_ret
        if n == "fw_usb_wait":
            return self.usb_wait_ret
        if n == "fw_reload_tracks_a":                 # clobbers it; must be restored
            self.e.write(SYM["fw_track_sel"], struct.pack("<i", 99))
        return 0

    def _remove(self, e, r):
        ok(("sync_audio",) in self.seq, "audio_sync before remove (as FILE DELETE)")
        return e._remove(r)

    def _browse(self, e, r):
        root = e.cstring(r[2])
        self.seq.append(("browse", root))
        ok(e.cstring(r[0]) == root, "browser starts at the root")
        idx, full = self.pick
        if full:
            e.write(r[1], full.encode() + b"\0")
        return idx & 0xFFFFFFFF

    def _list(self, e, r):
        d = e.cstring(e.u32(r[0]))
        self.listed.append((d, r[1]))
        files = {k.rsplit("\\", 1)[1]: k for k in self.e.files
                 if k.rsplit("\\", 1)[0] == d.upper() and k.endswith(".WAV")}
        names = sorted(n for n in files)
        for i, n in enumerate(names):
            orig = next((c.rsplit("\\", 1)[1] for c in self.card if c.upper() == files[n]), n)
            e.write(SYM["fw_list_names"] + 260 * i, orig.encode() + b"\0")
            e.write(SYM["fw_file_take"] + 4 * i, struct.pack("<i", self.takes.get(orig, -1)))
        e.write(r[0] + 8, struct.pack("<i", len(names)))
        return 0

    def run(self, cb, pick=None, events=()):
        self.pick = pick
        self.events = list(events)
        self.deltas = [1] * self.events.count(DIAL)
        for x in (self.screens, self.msgs, self.calls, self.progress, self.seq, self.listed):
            x.clear()
        self.e.call(self.sy[cb])
        ok(not self.events and self.depth == 0 and not self.e.handles, "clean exit")


def test_menu(img, b):
    from r24emu import R24Emu
    e = R24Emu(img)
    seen = {}
    e.stub(0x8003A776, lambda e_, r: seen.update(hdr=r[0]) or 0)
    e.call(0x80019610)
    hdr = seen["hdr"]
    n, arr = e.u32(hdr), e.u32(hdr + 0x20)
    ok(hdr == 0x800D44DC and n == 15 and arr == b.symbols["pm_items"], f"15 items ({n})")
    labels = [e.cstring(e.u32(e.u32(arr + 4 * i))) for i in range(n)]
    ok(labels == ["NEW", "SELECT", "INFO", "COPY", "DELETE", "RENAME", "PROTECT", "FILE",
                  "IMPORT SD", "IMPORT USB", "CLEAR UNUSED", "DUMP 1", "DUMP 2", "REC", "SEQ PLAY"],
       labels)
    cbs = [e.u32(e.u32(arr + 4 * i) + 0x10) for i in (8, 9, 10, 11, 12)]
    ok(cbs == [b.symbols[s] | 1 for s in ("pm_import_sd", "pm_import_usb", "pm_clear_unused",
                                          "pm_mem_dump1", "pm_mem_dump2")],
       "callbacks")


def test_import_sd(img, b):
    card = {"A:\\DRUMS\\KICK.WAV": b"k" * 100, "A:\\DRUMS\\snare.wav": b"s" * 50,
            "A:\\DRUMS\\HAT.WAV": b"h" * 70, "A:\\DRUMS\\README.TXT": b"x",
            AUDIO + "\\HAT.WAV": b"old hat"}
    u = Pm(img, b, card)
    u.run("pm_import_sd", (1, "A:\\DRUMS\\KICK.WAV"), [DIAL, ENTER])
    ok(u.listed == [("A:\\DRUMS", 0x13)], f"lister {u.listed}")
    sc = u.screens[0]
    ok(("IMPORT SD", -1, 0x20) in sc and ("DRUMS", 0x3E, 0x0C) in sc and ("3 WAV", 0x3E, 0x15) in sc,
       f"confirm {sc}")
    ok(sorted(d for s, d in u.copies) == sorted([AUDIO + "\\KICK.WAV", AUDIO + "\\snare.wav"]),
       f"copied {u.copies}")
    ok(u.e.files[(AUDIO + "\\HAT.WAV").upper()] == b"old hat", "existing file kept")
    ok(u.msgs == ["Imported 2 Skip 1"] and u.progress == ["Importing 2/3", "Importing 3/3"], u.msgs)
    ok(("guard", 9) in u.seq and not any(s[0] == "fw_usb_host" for s in u.seq), "SD: no USB calls")
    u.copies.clear()
    u.run("pm_import_sd", (0, "A:\\DRUMS\\HAT.WAV"), [EXIT])
    ok(u.copies == [] and u.msgs == [], "EXIT")
    u.run("pm_import_sd", (-2, ""), [])
    ok(u.msgs == ["No File"], "no file")
    u.run("pm_import_sd", (-1, ""), [])
    ok(u.msgs == [] and u.screens == [], "browser left")


def test_import_usb(img, b):
    card = {"B:\\SAMPLES\\808\\BD.WAV": b"b" * 40, "B:\\SAMPLES\\808\\SD.WAV": b"d" * 30}
    u = Pm(img, b, card)
    u.run("pm_import_usb", (0, "B:\\SAMPLES\\808\\BD.WAV"), [ENTER])
    names = [s[0] for s in u.seq]
    ok(names[:3] == ["guard", "sync", "fw_audio_mode"] and u.seq[0] == ("guard", 0xFFFF),
       f"LOOP[USB] guards first: {u.seq[:3]}")
    usb = [s for s in u.seq if s[0].startswith("fw_")]
    ok([s[0] for s in usb] == USB_SEQ, f"LOOP[USB] order: {[s[0] for s in usb]}")
    ok(u.seq.index(("browse", "B:")) == names.index("fw_usb_wait") + 1, "browse after the stick is up")
    ok(("fw_rescan_drive", 0x41) in u.seq, "rescan A:")
    ok(u.e.u32(SYM["fw_track_sel"]) == 5, "track selection restored")
    ok(u.e.u32(SYM["fw_project_open_flag"]) == 1, "project flag set again")
    audio_modes = [s for s in u.seq if s[0] == "fw_audio_mode"]
    ok(audio_modes == [("fw_audio_mode", 0), ("fw_audio_mode", 3)], f"audio mode {audio_modes}")
    ok(u.listed == [("B:\\SAMPLES\\808", 0x13)], f"{u.listed}")
    ok(sorted(d for s, d in u.copies) == [AUDIO + "\\BD.WAV", AUDIO + "\\SD.WAV"], f"{u.copies}")
    ok(u.msgs == ["Imported 2"], f"{u.msgs}")
    ok(u.progress[-1] == "Please Wait...", "cleanup box")
    # no stick: no browser, full cleanup
    u.usb_wait_ret = -1
    u.run("pm_import_usb")
    ok(not any(s[0] == "browse" for s in u.seq) and [s[0] for s in u.seq if s[0].startswith("fw_")]
       == USB_SEQ, "no stick: cleanup only")
    # host mode error: message, no cleanup (as LOOP[USB])
    u.usb_wait_ret, u.usb_host_ret = 0, 1
    u.run("pm_import_usb")
    ok(u.msgs == ["USB Setup Error"] and "fw_usb_off" not in [s[0] for s in u.seq], "setup error")
    # project not saved when no project is open
    u.usb_host_ret = 0
    u.e.write(SYM["fw_project_open_flag"], struct.pack("<i", 0))
    u.run("pm_import_usb", (-1, ""), [])
    ok("fw_save_project_now" not in [s[0] for s in u.seq], "nothing to save")


def test_clear_unused(img, b):
    card = {AUDIO + "\\TRK01_00.WAV": b"1", AUDIO + "\\TRK02_00.WAV": b"2",
            AUDIO + "\\KICK.WAV": b"k", AUDIO + "\\old take.wav": b"o", AUDIO + "\\NOTES.TXT": b"t"}
    u = Pm(img, b, card, takes={"TRK01_00.WAV": 0, "TRK02_00.WAV": 3})
    u.run("pm_clear_unused", events=[EXIT])
    ok(u.listed == [(AUDIO, 0x03)], f"all WAVs + take map: {u.listed}")
    sc = u.screens[0]
    ok(("CLEAR UNUSED", -1, 0x20) in sc and ("2 WAV", 0x3E, 0x0C) in sc and
       ("CANNOT BE UNDONE!", 1, 0x1E) in sc, f"warning {sc}")
    ok(u.e.removed == [] and u.msgs == [], "EXIT: nothing deleted")
    u.e.write(SELECTED := SYM["fw_selected_file"], b"KICK.WAV\0")
    u.run("pm_clear_unused", events=[ENTER])
    ok(sorted(u.e.removed) == sorted([(AUDIO + "\\KICK.WAV").upper(), (AUDIO + "\\old take.wav").upper()]),
       f"deleted {u.e.removed}")
    ok((AUDIO + "\\TRK01_00.WAV").upper() in u.e.files and (AUDIO + "\\NOTES.TXT").upper() in u.e.files,
       "used takes and non-WAV kept")
    ok(u.msgs == ["Deleted 2"] and u.progress == ["Deleting..."], f"{u.msgs}")
    ok(u.e.read(SELECTED, 1) == b"\0", "FILE selection cleared")
    u.run("pm_clear_unused")
    ok(u.msgs == ["No Unused File"] and u.screens == [], "nothing left")
    u.e.write(tka.PROTECT, struct.pack("<i", 1))
    u.run("pm_clear_unused")
    ok(u.msgs == ["Project Protect"], "protected")


DUMP_REGIONS = [("DSP_DDR", 0x87B00000, 0x500000), ("NOR", 0x02000000, 0x1000000),
                ("DSP_L2", 0x11800000, 0x10000), ("DSP_L1D", 0x11F04000, 0xC000),
                ("SYSMOD", 0x01C40000, 0x100), ("DSP_L1P", 0x11E08000, 0x8000),
                ("ARM_LOW", 0x00000000, 0xC000)]


def test_mem_dump(img, b):
    u = Pm(img, b, {})
    e = u.e
    heap = []
    e.stub(SYM["fw_malloc"], lambda e_, r: heap.append(r[0]) or 0x93000000)
    e.stub(SYM["fw_free"], lambda e_, r: heap.append(("free", r[0])) or 0)
    e.uc.mem_map(0x93000000, 0x10000)
    pat = {}
    for name, addr, size in DUMP_REGIONS:
        if addr:                                       # 0..0x10000 is mapped already
            e.uc.mem_map(addr & ~0xFFFF, (size + (addr & 0xFFFF) + 0xFFFF) & ~0xFFFF)
        data = bytes((i * 7 + len(name) + (i >> 16)) & 0xFF for i in range(size))
        e.write(addr, data)
        pat[name] = data
    e.write(0x01C40028, struct.pack("<I", 0x0B70002F))     # JTAGID
    e.write(0x01C40008, struct.pack("<I", 0x11800000))     # DSPBOOTADDR
    pat["SYSMOD"] = e.read(0x01C40000, 0x100)
    rescans = []
    e.stub(SYM["fw_rescan_drive"], lambda e_, r: rescans.append(r[0]) or 0)
    e.stub(SYM["fw_ui_progress"], lambda e_, r: u.progress.append(e_.cstring(r[0])) or 0)
    # DUMP 1: DDR off with the dial, so the small files are checked first
    u.run("pm_mem_dump1", events=[EXIT])
    ok(not any(k.startswith("A:\\DUMP") for k in e.files) and u.msgs == [], "EXIT: nothing written")
    sc = u.screens[0]
    ok(("DUMP 1", -1, 0x20) in sc and ("A:\\DUMP1", 0x3E, 0x0C) in sc, f"confirm {sc}")
    ok(("ON", 0x3E, 0x1E) in sc, "DDR on by default")
    u.run("pm_mem_dump1", events=[DIAL, ENTER])
    ok(("OFF", 0x3E, 0x1E) in u.screens[-1], f"dial turns DDR off {u.screens[-1]}")
    ok(u.msgs == ["Dump Done"], f"{u.msgs}")
    ok("A:\\DUMP1" in u.dirs, f"dir {u.dirs}")
    for name, addr, size in DUMP_REGIONS:
        got = e.files.get(f"A:\\DUMP1\\{name}.BIN")
        ok(got == pat[name], f"{name}.BIN: {None if got is None else len(got)} bytes")
    order = [p.rsplit("\\", 1)[1] for p in e.opened if p.upper().startswith("A:\\DUMP1")]
    ok(order == ["INFO.TXT"] + [n + ".BIN" for n, _a, _s in DUMP_REGIONS], f"order {order}")
    info = e.files["A:\\DUMP1\\INFO.TXT"].decode()
    ok("JTAGID      0B70002F" in info and "DSPBOOTADDR 11800000" in info and
       "NOR         02000000 01000000" in info, info)
    ok(heap == [0x10000, ("free", 0x93000000)], f"buffer {heap}")
    ok(rescans == [0x41], "rescan A:")
    ok("NOR 15/16M" in u.progress, f"progress {u.progress[:4]}")
    ok("A:\\DUMP1\\DDR.BIN" not in e.files, "DDR off: no DDR.BIN")
    u.guard = 1
    u.run("pm_mem_dump2")
    ok(u.msgs == [] and u.screens == [], "busy guard")
    u.guard = 0
    # DUMP 2, DDR on (default): the whole 0x80000000..0x87FFFFFF as DDR.BIN, last
    from r24emu import BSS_END
    lo = (BSS_END + 0xFFFF) & ~0xFFFF
    e.uc.mem_map(lo, 0x87540000 - lo)
    e.uc.mem_map(0x87A40000, 0x87B00000 - 0x87A40000)
    e.write(0x86000000, b"DDR-MARK")
    e.opened.clear()
    u.run("pm_mem_dump2", events=[ENTER])
    ok(("DUMP 2", -1, 0x20) in u.screens[-1] and ("A:\\DUMP2", 0x3E, 0x0C) in u.screens[-1],
       f"DUMP 2 screen {u.screens[-1]}")
    ddr = e.files.get("A:\\DUMP2\\DDR.BIN")
    ok(ddr is not None and len(ddr) == 0x8000000, "DDR.BIN 128 MiB")
    ok(ddr[0x6000000:0x6000008] == b"DDR-MARK" and ddr[:0x1000] == e.read(0x80000000, 0x1000),
       "DDR.BIN content")
    order = [p.rsplit("\\", 1)[1] for p in e.opened]
    ok(all(p.upper().startswith("A:\\DUMP2\\") for p in e.opened), f"only DUMP2 {e.opened[:3]}")
    ok(order[-1] == "DDR.BIN", f"DDR last {order[-8:]}")
    ok("DDR         80000000 08000000" in e.files["A:\\DUMP2\\INFO.TXT"].decode(), "INFO lists DDR")
    ok(e.files["A:\\DUMP1\\NOR.BIN"] == pat["NOR"], "DUMP 1 kept")
    ok(u.msgs == ["Dump Done"], f"{u.msgs}")


def test_default_no_dump():
    """build_firmware.py without --dump: 13 items, no mem_dump code."""
    from r24emu import R24Emu
    data = bytearray(open(os.path.join(r24sdk.ROOT, "R24SYS.BIN"), "rb").read())
    b, _rel, _n = r24sdk.build(data, PATCHES[:-1], ["CUSTOM_KIT_RESERVE_SEC=6"])
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        e = R24Emu(open(out, "rb").read())
    seen = {}
    e.stub(0x8003A776, lambda e_, r: seen.update(hdr=r[0]) or 0)
    e.call(0x80019610)
    n, arr = e.u32(seen["hdr"]), e.u32(seen["hdr"] + 0x20)
    labels = [e.cstring(e.u32(e.u32(arr + 4 * i))) for i in range(n)]
    ok(n == 13 and "DUMP 1" not in labels and labels[10] == "CLEAR UNUSED", labels)
    ok(not any(k.startswith("md_") or k.startswith("pm_mem_dump") for k in b.symbols), "no dump code")


def main() -> int:
    test_default_no_dump()
    img, b = build_image()
    test_menu(img, b)
    test_import_sd(img, b)
    test_import_usb(img, b)
    test_clear_unused(img, b)
    test_mem_dump(img, b)
    print(f"test_project_menu: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
