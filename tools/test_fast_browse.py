#!/usr/bin/env python3
"""test_fast_browse.py — sdk/patches/fast_browse.c (docs/10_loop_import.md §9).

fw_browse_select (0x80041DF2) runs in the emulator on the stock image and on
build/R24SYS.BIN, with the REAL lister (fw_list_files: find-first/next, sort,
folder compaction) over a fake folder. Stubbed: find-first/next (the folder),
0x80001BEE (open + header + stat of one entry: recorded, fills the record from
a table), the WAV open/getters used by the rate filter, the menu (fw_menu_run:
records its arguments, "chooses" an entry) and ui_progress_end.

Checked, stock vs built:
  * browser flags 4/5: stock opens EVERY entry, built opens only the chosen one;
    names, order, folder marks, count, menu record and result identical;
    the chosen record (28 bytes) identical;
  * preview (event 0x10033) on the built image fills the highlighted record
    before the firmware callback reads it, identical to stock;
  * leaving (-1), empty folder (-2): identical;
  * any other flags (0x1D/0x2D, the other caller): identical, incl. every open.

    python3 tools/build_firmware.py && python3 tools/test_fast_browse.py
"""
from __future__ import annotations

import json
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
BUILT_PATH = os.path.join(ROOT, "build", "R24SYS.BIN")
BUILT = open(BUILT_PATH, "rb").read()
SYMS = json.load(open(BUILT_PATH + ".syms.json"))["symbols"]

SELECT, EVENT, MENU_RUN, PROG_END = 0x80041DF2, 0x80041B94, 0x8003C29A, 0x8003DA74
FIND_FIRST, FIND_NEXT, ENTRY_INFO = 0x80054F42, 0x80054F6A, 0x80001BEE
WAV_OPEN, WAV_CLOSE = 0x80058F38, 0x80058F4A
WAV_RATE, WAV_BITS, WAV_CH = 0x800C8044, 0x800C8022, 0x800C8036
LIST, SEL, MENU, ENTRIES, NAMES = 0x811496E8, 0x801EBEB4, 0x811495A8, 0x804AC080, 0x8046A080
HEAD_CB, ENTER, PREVIEW = 0x80042148, 0x10024, 0x10033
DTA_ATTR, DTA_SHORT, DTA_LONG = 0x22C, 0x22D, 0x23A
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def folder(n_files, n_dirs):
    """(name, attr) in directory order, with '.'/'..' and non-WAVs mixed in."""
    ents = [(".", 0x10), ("..", 0x10), ("README.TXT", 0x20), ("HIDDEN.WAV", 0x22)]
    for i in range(n_files):
        ents.append((f"Loop {(i * 37) % n_files:03d} take.wav" if i % 3 else
                     f"L{(i * 37) % n_files:03d}.WAV", 0x20))
        if i < n_dirs:
            ents.append((f"DIR{n_dirs - i:02d}", 0x10))
    return ents


def info_for(name):
    """what 0x80001BEE would read from the file: (+7 ch, +8 rate type, +0x14 len)"""
    h = sum(name.encode())
    return 1 + h % 2, 2 + h % 2, 1000 + h


def run(img, ents, flags, noback, choose=None, ret=ENTER, preview=None):
    e = r24emu.R24Emu(img)
    e.info, e.menu, e.queue = [], None, []

    def find_first(em, r):
        em.queue = list(ents)
        em.dta = r[2]
        return find_next(em, [r[2]])

    def find_next(em, r):
        if not em.queue:
            return -1
        name, attr = em.queue.pop(0)
        em.write(em.dta + DTA_ATTR, bytes([attr]))
        short = name if len(name) <= 12 and " " not in name else ""
        alias = name.replace(" ", "")[:6].upper() + "~1" + os.path.splitext(name)[1].upper()
        em.write(em.dta + DTA_SHORT, (short or alias).encode() + b"\0")
        em.write(em.dta + DTA_LONG, (b"" if short else name.encode()) + b"\0")
        return 0

    def entry_info(em, r):                       # (path, record)
        path = em.cstring(r[0])
        em.info.append(path)
        rec = r[1]
        name = path.rsplit("\\", 1)[1]
        if not name.upper().endswith(".WAV"):
            em.write(rec + 4, b"\1")
            em.write(rec + 0x18, b"\2")
            return 0
        ch, rt, ln = info_for(name)
        em.write(rec + 4, bytes([0, 1, 2, ch, rt]))
        em.write(rec + 0x14, struct.pack("<I", ln))
        em.write(rec + 0x18, b"\0")
        return 0

    def menu_run(em, r):
        em.menu = (struct.unpack("<14I", em.read(r[0], 56)), r[1], r[2])
        if choose is not None:
            em.write(SEL, struct.pack("<I", choose))
        return ret

    e.stub(FIND_FIRST, find_first)
    e.stub(FIND_NEXT, find_next)
    e.stub(ENTRY_INFO, entry_info)
    e.stub(MENU_RUN, menu_run)
    e.stub(PROG_END, lambda em, r: 0)
    e.stub(WAV_OPEN, lambda em, r: 0)
    e.stub(WAV_CLOSE, lambda em, r: 0)
    e.stub(WAV_RATE, lambda em, r: 48000 if flags & 0x20 else 44100)
    e.stub(WAV_BITS, lambda em, r: 16)
    e.stub(WAV_CH, lambda em, r: 2)
    d = e.alloc(b"A:\\LOOPS\0")
    res = e.call(SELECT, HEAD_CB | 1, d, flags, noback)
    n = e.u32(LIST + 8)
    out = {
        "ret": res, "count": n, "files": e.u32(LIST + 12),
        "names": [e.cstring(e.u32(ENTRIES + 28 * i)) for i in range(n)],
        "names_at": [e.u32(ENTRIES + 28 * i) - NAMES for i in range(n)],
        "dir": [e.read(ENTRIES + 28 * i + 0x18, 1)[0] == 2 for i in range(n)],
        "menu": e.menu, "info": list(e.info), "emu": e,
    }
    if choose is not None and 0 <= choose < n:
        out["chosen"] = e.read(ENTRIES + 28 * choose, 28)
    if preview is not None:
        snap = []
        e.write(SEL, struct.pack("<I", preview))
        e.stub(EVENT, lambda em, r: snap.append(em.read(ENTRIES + 28 * preview, 28)) or 0)
        e.call(e.menu[1], PREVIEW)
        out["preview"] = snap[0]
        out["info_after_preview"] = list(e.info)
    return out


def main() -> int:
    ok(SYMS.get("fb_select") is not None and SYMS.get("fb_event") is not None,
       "fast_browse.c is in build/R24SYS.BIN")
    ents = folder(40, 6)
    for flags in (4, 5):
        print(f"browser flags {flags}")
        for choose in (0, 7, 39, 40, 45):
            s = run(STOCK, ents, flags, 1, choose)
            b = run(BUILT, ents, flags, 1, choose)
            n = s["count"]
            if choose == 0:
                ok(n == (46 if flags == 4 else 40) and s["files"] == 40,
                   f"stock lists {n} entries (40 WAVs{', 6 folders' if flags == 4 else ''})")
                ok(len(s["info"]) == n, f"stock opens every entry ({len(s['info'])})")
                ok(b["names"] == s["names"] and b["names_at"] == s["names_at"]
                   and b["count"] == n and b["files"] == s["files"],
                   "built: same names, same order")
                ok(b["dir"] == s["dir"], "built: same folder marks (row icons)")
                ok(b["menu"][0] == s["menu"][0] and b["menu"][2] == s["menu"][2],
                   "built: same menu record")
                ok(b["menu"][1] == SYMS["fb_event"] | 1 and s["menu"][1] == EVENT | 1,
                   "built: event callback wrapped (fb_event)")
            if choose >= n:
                continue
            ok(b["ret"] == s["ret"] == choose, f"choose {choose}: same result")
            ok(b["info"] == s["info"][choose:choose + 1] and len(b["info"]) == 1,
               f"choose {choose}: built opens only {b['info'][0]!r}")
            ok(b["chosen"] == s["chosen"], f"choose {choose}: chosen record identical")
        print("preview (event 0x10033)")
        for k in (3, 39, n - 1):
            s = run(STOCK, ents, flags, 1, choose=0, ret=-1)
            b = run(BUILT, ents, flags, 1, choose=0, ret=-1, preview=k)
            ok(b["preview"] == s["emu"].read(ENTRIES + 28 * k, 28)
               and b["info_after_preview"] == [s["info"][k]],
               f"preview {k}: record filled before the firmware reads it")
    print("leave / empty")
    s, b = run(STOCK, ents, 4, 1, ret=-1), run(BUILT, ents, 4, 1, ret=-1)
    ok(s["ret"] == b["ret"] == -1 and b["info"] == [], "leaving: -1, nothing opened")
    empty = [(".", 0x10), ("..", 0x10), ("A.TXT", 0x20)]
    s, b = run(STOCK, empty, 4, 1), run(BUILT, empty, 4, 1)
    ok(s["ret"] == b["ret"] == -2 and s["menu"] is None and b["menu"] is None,
       "empty folder: -2, no menu")
    print("other caller (flags 0x1D/0x2D, noback 0): unchanged")
    for flags in (0x1D, 0x2D):
        for choose in (0, 12):
            s = run(STOCK, ents, flags, 0, choose)
            b = run(BUILT, ents, flags, 0, choose)
            same = all(s.get(k) == b.get(k) for k in ("ret", "count", "files", "names", "dir",
                                               "info", "menu", "chosen"))
            ok(same, f"flags {flags:#x} choose {choose}: identical "
                     f"({s['count']} entries, {len(s['info'])} opened)")
    print(f"\n{checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
