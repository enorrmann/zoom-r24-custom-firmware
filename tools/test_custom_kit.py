#!/usr/bin/env python3
"""test_custom_kit.py — prove sdk/patches/custom_kit.c by running the built image.

The patched set_current_project and the loader in the bank tail are executed
in an ARM926 emulator (tools/r24emu.py) straight from the built firmware
image. Only the file system, project_name() and the ZOOM.ZDT writer are
stubbed; strcpy and everything else is the firmware's own code.

    python3 tools/test_custom_kit.py
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import custom_kit as ck
import r24bank
import r24sdk

try:
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATE = 44100
HOOK_RAM = 0x8001D6A6
PRJ_NAME, SAVE_SYS = 0x8001D212, 0x8001D63A
CUR_PRJ, CUR_NAME = 0x804C6E14, 0x804C6E24
ORIG = bytes.fromhex("70b50400e34804602000fff7affd05000100df4810309ef06cfffff7bbff70bd")

checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


# ── WAV builders and the reference conversion ─────────────────────────────
def wav(frames: list[list[float]], rate: int, bits: int = 16, fmt: int = 1,
        extensible: bool = False, extra: bytes = b"") -> bytes:
    ch = len(frames[0])
    body = bytearray()
    for fr in frames:
        for v in fr:
            if fmt == 3:
                body += struct.pack("<f", v)
            else:
                iv = max(-(1 << (bits - 1)), min((1 << (bits - 1)) - 1,
                                                 int(round(v * (1 << (bits - 1))))))
                body += iv.to_bytes(bits // 8, "little", signed=True)
    align = ch * bits // 8
    if extensible:
        fmtc = struct.pack("<HHIIHHHHI", 0xFFFE, ch, rate, rate * align, align, bits,
                           22, bits, 0) + struct.pack("<H", fmt) + bytes(14)
    else:
        fmtc = struct.pack("<HHIIHH", fmt, ch, rate, rate * align, align, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmtc)) + fmtc + extra
    chunks += b"data" + struct.pack("<I", len(body)) + bytes(body)
    if len(body) % 2:
        chunks += b"\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def s16(raw: bytes, bits: int, fmt: int) -> int:
    if fmt == 3:
        v = struct.unpack("<I", raw)[0]
        e = ((v >> 23) & 0xFF) - 127
        m = (v & 0x7FFFFF) | 0x800000
        if e < -16:
            return 0
        r = 32767 if e >= 0 else min(32767, m >> (8 - e))
        return -r if v >> 31 else r
    return struct.unpack("<h", raw[bits // 8 - 2:bits // 8])[0]


def c_div(a: int, b: int) -> int:                      # C truncating division
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def reference(data: bytes, cap: int) -> bytes:
    """What loader.c must produce (same integer arithmetic)."""
    off, fmt, dpos, dlen, trunc = 12, None, None, 0, False
    n = len(data)
    while off + 8 <= n:
        tag, ln = data[off:off + 4], struct.unpack_from("<I", data, off + 4)[0]
        if tag == b"fmt ":
            fmt = off + 8
        elif tag == b"data":
            dpos, dlen = off + 8, ln
            if dpos + dlen > n:
                dlen, trunc = n - dpos, True
            if fmt:
                break
        if ln > n - off - 8:
            break
        off += 8 + ln + (ln & 1)
    tagf, ch, rate, _br, align, bits = struct.unpack_from("<HHIIHH", data, fmt)
    if tagf == 0xFFFE:
        tagf = struct.unpack_from("<H", data, fmt + 24)[0]
    frames = dlen // align
    o = []
    for i in range(frames):
        p = dpos + i * align
        s = sum(s16(data[p + c * bits // 8:p + (c + 1) * bits // 8], bits, tagf)
                for c in range(ch))
        o.append(c_div(s, ch) if ch > 1 else s)
    if frames * 2 > cap:
        frames, trunc = cap // 2, True
        o = o[:frames]
    out = frames
    if rate != RATE:
        want = max(1, frames * RATE // rate)
        step = (rate << 16) // RATE
        if want > cap // 2:
            want, trunc = cap // 2, True
        res = []
        for i in range(want):
            pos = i * step
            k, f = pos >> 16, pos & 0xFFFF
            k = min(k, frames - 1)
            a = o[k]
            b = o[k + 1] if k + 1 < frames else a
            res.append(a + (((b - a) * f) >> 16))
        o, out = res, want
    o = [((v + 0x8000) & 0xFFFF) - 0x8000 for v in o]
    if trunc and out > 4:
        f = min(out // 4, 220)
        for i in range(f):
            k = out - f + i
            o[k] = c_div(o[k] * (f - 1 - i), f)
    return struct.pack(f"<{out}h", *o[:out])


def tone(n, ch=1, freq=440.0, rate=RATE, amp=0.6):
    return [[amp * math.sin(2 * math.pi * freq * (i / rate) + c) for c in range(ch)]
            for i in range(n)]


# ── tests ──────────────────────────────────────────────────────────────────
def build(src: str, reserve_sec: float = 6.0):
    data = bytearray(open(src, "rb").read())
    b, rel, notes = ck.install(data, reserve_sec, menu=False)   # menu: test_kit_assign.py
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        return open(out, "rb").read(), b


class Kit:
    """The installed patch, as the emulator sees it."""

    def __init__(self, img: bytes, b: r24sdk.Build, names: dict[int, str]):
        self.img, self.b, self.sy = img, b, b.symbols
        self.emu = e = r24emu.R24Emu(img)
        self.saves = 0
        e.stub(PRJ_NAME, self._name)
        e.stub(SAVE_SYS, self._save)
        self.names = names
        self.table = r24bank.locate(img).literal
        k = r24sdk.kit_names(img).index("CUSTOM")
        self.ids = r24bank.kit_sample_ids(img)[k]
        tab = r24bank.read_table(img)
        self.defaults = [tab[i] for i in self.ids]
        self.reserve = self.sy["reserve"]
        self.status = self.sy["cust_status"]

    def _name(self, e, r):
        n = r[0] - (1 << 32) if r[0] >> 31 else r[0]
        if n not in self.names:
            return 0
        return e.alloc(self.names[n].encode() + b"\0")

    def _save(self, e, r):
        self.saves += 1
        return 7

    def set_project(self, n: int) -> int:
        r = self.emu.call(HOOK_RAM, n)
        ok(not self.emu.handles, "every opened file was closed")
        return r

    def entry(self, i):
        return struct.unpack("<II", self.emu.read(self.table + 8 * i, 8))

    def play(self, i) -> bytes:
        rel, ln = self.entry(i)
        return self.emu.read((r24bank.RAM_BASE + rel) & 0xFFFFFFFF, ln)

    def result(self, s):
        return struct.unpack("<h", self.emu.read(self.status + 8 + 2 * s, 2))[0]

    def add(self, path, data):
        self.emu.files[path.upper()] = data


def test_static(img: bytes, src: bytes, b: r24sdk.Build):
    d = r24sdk.describe(img)
    ok(d is not None and all(d["applied"]), "SDK build present, hooks applied")
    ok(sorted(h["kind"] for h in d["hooks"]) == [r24sdk.K_REPLACE, r24sdk.K_KIT], "two hooks")
    o = HOOK_RAM - 0x80000000 + 0x5B4
    ok(img[o:o + 10] == r24sdk.trampoline(HOOK_RAM, b.symbols["cust_hook"] | 1), "trampoline")
    ok(img[o + 10:o + 32] == ORIG[10:], "rest of the old body untouched")
    ok(b.symbols["reserve"] % 32 == 0, "reserve 32-byte aligned (cache lines)")
    ok(b.symbols["reserve"] + 6 * RATE * 2 - 32 <= d["end"] <= d["ram"] + d["len"],
       "reserve inside the section")
    ok("CUSTOM" in r24sdk.kit_names(img), "kit CUSTOM in KIT SELECT")
    diff = [o for o in range(0x40, r24bank.BANK_BASE) if img[o] != src[o]]
    allowed = set(range(o, o + 10))
    for w in (*r24bank.TABLE_LITERALS, *r24bank.KIT_LITERALS, r24bank.KIT_COUNT_OFF):
        allowed |= set(range(w, w + 4))
    ok(set(diff) <= allowed, f"code/data changed outside the known words: "
       f"{[hex(x) for x in diff if x not in allowed][:8]}")
    s_tab, i_tab = r24bank.read_table(src), r24bank.read_table(img)
    s_ids, i_ids = r24bank.kit_sample_ids(src), r24bank.kit_sample_ids(img)
    for kk in range(len(s_ids)):
        for s in range(16):
            a = r24bank.sample_bytes(src, s_tab, s_ids[kk][s])
            c = r24bank.sample_bytes(img, i_tab, i_ids[kk][s])
            ok(len(c) <= len(a) and a[:len(c) - 2 * 882] == c[:len(c) - 2 * 882],
               f"kit {kk} slot {s} keeps its audio")


def test_run(img: bytes, b: r24sdk.Build, reserve_len: int):
    k = Kit(img, b, {0: "PRJ000", 7: "PRJ007", 9: "PRJ009"})
    e, ids, D = k.emu, k.ids, k.defaults
    base = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT\\"

    # 1. no KIT folder: defaults, original side effects, return value
    ok(k.set_project(0) == 7, "returns set_current_project's result")
    ok(e.u32(CUR_PRJ) == 0 and k.saves == 1, "current project stored, ZOOM.ZDT written")
    ok(e.read(CUR_NAME, 7) == b"PRJ000\0", "project name copied (firmware strcpy)")
    ok(len(e.opened) == 17 and e.opened[0] == "A:\\ZOOM_R24\\PROJECT\\PRJ000\\KIT.TXT"
       and e.opened[1] == "A:\\ZOOM_R24\\PROJECT\\PRJ000\\KIT\\SAMPLE01.WAV"
       and e.opened[16].endswith("\\SAMPLE16.WAV"), f"paths {e.opened[:3]}")
    ok([k.entry(i) for i in ids] == D, "defaults without files")
    ok(e.cache_ops == [], "nothing loaded, nothing cleaned")

    # 2. a project with a kit in many formats
    cases = {
        1: wav(tone(30000), RATE),                                   # exact
        2: wav(tone(20000, 2), RATE),                                # stereo
        3: wav(tone(9000), RATE, bits=24),
        4: wav(tone(9000, 2), RATE, bits=32, fmt=3),                 # float stereo
        5: wav(tone(12000), 48000),                                  # down-sample
        6: wav(tone(5000), 22050),                                   # up-sample
        7: wav(tone(7000, 2), 96000, bits=24, extensible=True),
        8: wav(tone(4001), RATE, extra=b"LIST" + struct.pack("<I", 5) + b"abcde\0"),
        9: b"JUNK" + bytes(60),                                      # not a WAV
        10: wav(tone(3000), RATE, bits=8),                           # unsupported
        11: b"RIFF\x10\0\0\0WAVEjunk\0\0\0\0",                      # no fmt
        12: wav(tone(9000), RATE)[:-1000],                           # cut short
        16: wav([[1.0], [-1.0], [0.5], [-0.25]] * 3000, RATE, fmt=3, bits=32),
    }
    for s, data in cases.items():
        k.add(base + f"SAMPLE{s:02d}.WAV", data)
    e.cache_ops.clear()
    k.set_project(7)
    expect_err = {9: -1, 10: -2, 11: -2}
    w = k.reserve
    for s in range(1, 17):
        i, res = ids[s - 1], k.result(s - 1)
        if s in cases and s not in expect_err:
            cap = k.reserve + reserve_len - w
            if cap < 64:
                ok(k.entry(i) == D[s - 1] and res == -4, f"slot {s}: no room -> default")
                continue
            want = reference(cases[s][:cap], cap)
            got = k.play(i)
            ok(got == want, f"slot {s}: {len(got)} bytes vs reference {len(want)}")
            ok(r24bank.RAM_BASE + k.entry(i)[0] == w, f"slot {s} at the next free line")
            w += (len(want) + 31) & ~31
            ok(res > 0, f"slot {s} status {res}")
        else:
            ok(k.entry(i) == D[s - 1], f"slot {s} default")
            ok(res == expect_err.get(s, 0), f"slot {s} status {res}")
    ok(e.cache_ops == [(0x800C40B0, k.reserve, w - k.reserve)], f"D-cache clean {e.cache_ops}")
    ok(e.u32(k.status + 4) == w - k.reserve, "used bytes reported")
    ok(k.play(ids[0]) == cases[1][44:44 + 60000], "slot 1 bit-exact")
    ok(len(k.play(ids[4])) == 2 * (12000 * RATE // 48000), "48k length")

    # 3. a file larger than the whole reserve is cut to fit, with a fade
    big = wav(tone(reserve_len // 2 + 50000), RATE)
    e.files.clear()
    k.add(base + "SAMPLE01.WAV", big)
    k.add(base + "SAMPLE02.WAV", wav(tone(1000), RATE))
    k.set_project(7)
    got = k.play(ids[0])
    ok(len(got) == reserve_len - 44, "big file fills the reserve (minus its header)")
    ok(got == reference(big[:reserve_len], reserve_len), "cut + fade")
    ok(struct.unpack_from("<h", got, len(got) - 2)[0] == 0, "fade ends at silence")
    ok(k.entry(ids[1]) == D[1] and k.result(1) == -4, "no room left: default")

    # 4. switching to a project without a kit restores every default
    k.set_project(9)
    ok([k.entry(i) for i in ids] == D, "defaults restored")
    # 5. no project (n < 0): defaults, no file access
    e.opened.clear()
    k.set_project(-1)
    ok(e.opened == [] and [k.entry(i) for i in ids] == D, "n < 0")
    ok(e.u32(k.status) == 5, "hook ran 5 times")
    # 6. other entries untouched
    tab = r24bank.read_table(img)
    ok(all(k.entry(i) == tab[i] for i in range(len(tab)) if i not in ids),
       "other entries untouched")


def test_config(img: bytes, b: r24sdk.Build):
    """PROJECT\\<prj>\\KIT.TXT maps pads to files of any name in KIT\\."""
    k = Kit(img, b, {7: "PRJ007"})
    e, ids = k.emu, k.ids
    base = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\"
    kick, snare, old = wav(tone(3000), RATE), wav(tone(2000, 2), 48000), wav(tone(1500), RATE)
    k.add(base + "KIT\\Kick Deep.wav", kick)
    k.add(base + "KIT\\SNARE.WAV", snare)
    k.add(base + "KIT\\SAMPLE03.WAV", old)               # older kit, no line for pad 3
    k.add(base + "KIT\\SAMPLE04.WAV", old)               # pad 4 has a line: ignored
    k.add(base + "KIT.TXT", b"# my kit\r\n 01 = Kick Deep.wav \r\n2=SNARE.WAV\n"
                           b"04=MISSING.WAV\r\n17=X.WAV\r\n05=..\\AUDIO\\T.WAV\r\n"
                           b"06=A:\\X.WAV\r\nbad line\r\n=Y.WAV\r\n")
    k.set_project(7)
    ok(k.play(ids[0]) == reference(kick, 1 << 30), "pad 1 = KIT\\Kick Deep.wav (spaces trimmed)")
    ok(k.play(ids[1]) == reference(snare, 1 << 30), "pad 2 = SNARE.WAV, LF-only line")
    ok(k.play(ids[2]) == reference(old, 1 << 30), "pad 3: no line -> SAMPLE03.WAV")
    ok(k.entry(ids[3]) == k.defaults[3] and k.result(3) == 0, "pad 4: listed file missing -> default")
    ok(base + "KIT\\SAMPLE04.WAV" not in e.opened, "pad 4 does not fall back to SAMPLE04")
    ok(all(k.entry(ids[s]) == k.defaults[s] for s in (4, 5)), "paths with folders/drives refused")
    ok(not any("AUDIO" in p or p.startswith("A:\\X") for p in e.opened), "nothing outside KIT\\ opened")
    names = [e.cstring(b.symbols["pad_file"] + 64 * s) for s in range(16)]
    ok(names[:4] == ["Kick Deep.wav", "SNARE.WAV", "SAMPLE03.WAV", "MISSING.WAV"] and
       not any(names[4:]), f"pad names {names[:6]}")
    # another project without KIT.TXT: names cleared
    k.names[9] = "PRJ009"
    k.set_project(9)
    ok(not any(e.cstring(b.symbols["pad_file"] + 64 * s) for s in range(16)), "names cleared")


POOL3, POOL3_ASK = 0x815E1018, 0xF0000
FIXED, FIXED_END = 0x819126C0, 0x81C12680
GET_MPL = 0x80074BC4


def ddr_kit(src: str, defines: list[str]):
    data = bytearray(open(src, "rb").read())
    b, _rel, _n = r24sdk.build(data, [ck.PATCH], [ck.reserve_define(0, ddr=True),
                                                 ck.DDR_DEFINE] + defines, True)
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        r24bank.seal(data, out)
        img = open(out, "rb").read()
    return img, b


BIG1, BIG1_END, BIG2, BIG2_END = 0x81C128C0, 0x83000000, 0x87000000, 0x87540000


def test_ddr_big(src: str):
    """default DDR build: + 19.9 MiB and 5.25 MiB; a 6 MB sample fits whole."""
    img, b = ddr_kit(src, [])
    e = r24emu.R24Emu(img)
    names = {7: "PRJ007"}
    e.stub(PRJ_NAME, lambda e_, r: e_.alloc(names[r[0]].encode() + b"\0") if r[0] in names else 0)
    e.stub(SAVE_SYS, lambda e_, r: 0)
    e.stub(GET_MPL, lambda e_, r: e_.write(r[2], struct.pack("<I", POOL3 + 8)) or 0)
    e.uc.mem_map(0x815E0000, 0x110000)
    e.uc.mem_map(0x81910000, BIG1_END - 0x81910000)       # 3 MiB + 19.9 MiB
    e.uc.mem_map(BIG2, BIG2_END - BIG2)                   # bank window starts at BIG2_END
    tab_lit = r24bank.locate(img).literal
    kk = r24sdk.kit_names(img).index("CUSTOM")
    ids = r24bank.kit_sample_ids(img)[kk]
    base = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT\\"
    huge = wav(tone(3_000_000), RATE)                     # 6 MB, 68 s: > 5.25 MiB
    mid = wav(tone(1_700_000), RATE)                      # 3.4 MB: 5.25 MiB is its best fit
    small = wav(tone(3000), RATE)
    e.files[(base + "SAMPLE01.WAV").upper()] = huge
    e.files[(base + "SAMPLE02.WAV").upper()] = mid
    e.files[(base + "SAMPLE03.WAV").upper()] = small
    e.call(HOOK_RAM, 7, limit=20_000_000_000)
    st = b.symbols["cust_status"]
    pbase = (POOL3 + 8 + 31) & ~31
    nreg = e.u32(st + 48)
    regs = [(e.u32(st + 52 + 4 * i), e.u32(st + 72 + 4 * i)) for i in range(nreg)]
    ok(regs == [(FIXED, FIXED_END - FIXED), (BIG1, BIG1_END - BIG1), (BIG2, BIG2_END - BIG2),
                (pbase, (POOL3_ASK - (pbase - POOL3 - 8)) & ~31)],
       f"regions {[(hex(a), n) for a, n in regs]}")
    ent = lambda i: struct.unpack("<II", e.read(tab_lit + 8 * i, 8))
    addr = lambda i: (r24bank.RAM_BASE + ent(i)[0]) & 0xFFFFFFFF
    ok(addr(ids[0]) == BIG1 and e.read(addr(ids[0]), ent(ids[0])[1]) == reference(huge, 1 << 30),
       "6 MB sample whole in the 19.9 MiB zone")
    ok(addr(ids[1]) == BIG2 and e.read(addr(ids[1]), ent(ids[1])[1]) == reference(mid, 1 << 30),
       "3.4 MB sample in the 5.25 MiB zone (best fit)")
    ok(e.read(addr(ids[2]), ent(ids[2])[1]) == reference(small, 1 << 30), "small sample")
    total = sum(n for _a, n in regs)
    ok(total > 29 * 0x100000, f"total {total / 0x100000:.1f} MiB")


def test_ddr(src: str):
    """-D CUSTOM_KIT_DDR=1 CUSTOM_KIT_DDR_BIG=0, no reserve: pool 3 + the fixed 3 MiB."""
    img, b = ddr_kit(src, ["CUSTOM_KIT_DDR_BIG=0"])
    ok("reserve" not in b.symbols, "no reserve in the bank tail")
    tab0 = r24bank.read_table(img)
    k = Kit.__new__(Kit)
    k.img, k.b, k.sy = img, b, b.symbols
    k.emu = e = r24emu.R24Emu(img)
    k.saves, k.names = 0, {7: "PRJ007", 9: "PRJ009"}
    e.stub(PRJ_NAME, k._name)
    e.stub(SAVE_SYS, k._save)
    k.table = r24bank.locate(img).literal
    kk = r24sdk.kit_names(img).index("CUSTOM")
    k.ids = ids = r24bank.kit_sample_ids(img)[kk]
    k.defaults = D = [tab0[i] for i in ids]
    k.status = st = b.symbols["cust_status"]
    e.uc.mem_map(0x815E0000, 0x100000 + 0x10000)          # pool 3
    e.uc.mem_map(0x81910000, 0x310000)                    # the 3 MiB
    calls = []

    def get_mpl(em, r):
        calls.append((r[0], r[1]))
        if len(calls) == 1:
            return (-25) & 0xFFFFFFFF                     # first time: refused (retry later)
        em.write(r[2], struct.pack("<I", POOL3 + 8))       # block after an 8-byte header
        return 0
    e.stub(GET_MPL, get_mpl)
    base = "A:\\ZOOM_R24\\PROJECT\\PRJ007\\KIT\\"

    # first load: pool refused -> only the 3 MiB region, still works
    k.add(base + "SAMPLE01.WAV", wav(tone(3000), RATE))
    k.set_project(7)
    ok(calls == [(3, POOL3_ASK)], f"get_mpl(3, 0xF0000) {calls}")
    ok(e.u32(st + 44) == (-25) & 0xFFFFFFFF and e.u32(st + 48) == 1, "pool error kept, 1 region")
    ok(r24bank.RAM_BASE + k.entry(ids[0])[0] & 0xFFFFFFFF == FIXED, "pad 1 in the fixed 3 MiB")
    ok(k.play(ids[0]) == reference(wav(tone(3000), RATE), 1 << 30), "pad 1 plays (rel wraps)")

    # second load: pool granted. Model of the allocator: read in the largest
    # hole, keep it in the smallest hole that holds it (regions: fixed, pool)
    e.files.clear()
    big = wav(tone(1_500_000), RATE)                      # 3.0 MB: only the 3 MiB holds it
    small = [wav(tone(4000 + 900 * s), RATE) for s in range(16)]
    k.add(base + "SAMPLE01.WAV", big)
    for s in range(2, 17):
        k.add(base + f"SAMPLE{s:02d}.WAV", small[s - 1])
    e.cache_ops.clear()
    k.set_project(7)
    ok(len(calls) == 2 and e.u32(st + 44) == 0 and e.u32(st + 48) == 2, "pool taken on retry")
    pbase = (POOL3 + 8 + 31) & ~31
    psize = (POOL3_ASK - (pbase - POOL3 - 8)) & ~31
    ok(e.u32(st + 56) == pbase and e.u32(st + 76) == psize, "pool region = the block, aligned")
    addr = lambda i: (r24bank.RAM_BASE + k.entry(i)[0]) & 0xFFFFFFFF
    regs = [[FIXED, FIXED_END - FIXED, 0], [pbase, psize, 0]]
    where = []
    for s in range(1, 17):
        pcm = reference(big if s == 1 else small[s - 1], 1 << 30)
        need = (len(pcm) + 31) & ~31
        free = [r[1] - r[2] for r in regs]
        fits = [i for i in range(2) if free[i] >= need]
        fit = min(fits, key=lambda i: free[i])
        ok(addr(ids[s - 1]) == regs[fit][0] + regs[fit][2],
           f"pad {s} in region {fit} at {addr(ids[s - 1]):08X}")
        ok(k.play(ids[s - 1]) == pcm, f"pad {s} audio")
        regs[fit][2] += need
        where.append(fit)
    ok(where[0] == 0 and 0 in where[1:] and 1 in where[1:], f"both regions used {where}")
    ok(sorted(e.cache_ops) == sorted([(0x800C40B0, r[0], r[2]) for r in regs]),
       f"cache {e.cache_ops}")
    ok(e.u32(st + 4) == regs[0][2] + regs[1][2], "used = both regions")
    ok([e.u32(st + 92 + 4 * i) for i in range(2)] == [r[2] for r in regs], "per-region used")

    # a sample bigger than everything: cut to the largest hole
    e.files.clear()
    huge = wav(tone(1_700_000), RATE)                     # 3.4 MB > 3 MiB
    k.add(base + "SAMPLE01.WAV", huge[:FIXED_END - FIXED])
    k.add(base + "SAMPLE02.WAV", small[1])
    k.set_project(7)
    ok(addr(ids[0]) == FIXED and len(k.play(ids[0])) == FIXED_END - FIXED - 44, "cut to the 3 MiB")
    ok(addr(ids[1]) == pbase and k.play(ids[1]) == reference(small[1], 1 << 30),
       "pad 2 goes to the pool")
    k.set_project(9)
    ok([k.entry(i) for i in ids] == D, "defaults restored")
    ok(len(calls) == 2, "pool taken once")


def main() -> int:
    for name in ("R24SYS.BIN", "R24SYS_X0X.BIN"):
        path = os.path.join(ROOT, name)
        src = open(path, "rb").read()
        img, b = build(path, 6.0)
        test_static(img, src, b)
        test_run(img, b, (6 * RATE * 2) & ~31)
        test_config(img, b)
        test_ddr(path)
        test_ddr_big(path)
    # combined with LOOP[SD] (verified on a device), when present
    loop = os.path.join(ROOT, "build", "loop", "R24SYS_160_LOOPSD_ROOT.BIN")
    if os.path.exists(loop):
        img, b = build(loop, 3.0)
        ok(len(r24bank.ext_sections(img)) == 3, "LOOP[SD] section kept")
        test_run(img, b, (3 * RATE * 2) & ~31)
    try:
        ck.install(bytearray(img), 1.0)
        ok(False, "second build must be refused")
    except r24sdk.SdkError:
        ok(True, "second build refused")
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "My Kick.wav")
        open(p, "wb").write(wav(tone(4800, 2), 48000))
        kitdir = os.path.join(t, "PRJ001", "KIT")
        ck.prepare([p, "-", p], kitdir)
        names = sorted(os.listdir(kitdir))
        ok(names == ["My Kick.WAV"], f"prepare keeps the name: {names}")
        cfg = open(os.path.join(t, "PRJ001", "KIT.TXT"), "rb").read()
        ok(cfg.endswith(b"\r\n01=My Kick.WAV\r\n03=My Kick.WAV\r\n"), f"KIT.TXT {cfg!r}")
        with wave.open(os.path.join(kitdir, "My Kick.WAV")) as w:
            ok((w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, RATE),
               "prepare format")
        ck.prepare([p], kitdir, first=5)              # adds pad 5, keeps 1 and 3
        cfg = open(os.path.join(t, "PRJ001", "KIT.TXT"), "rb").read()
        ok(b"01=" in cfg and b"03=" in cfg and b"05=My Kick.WAV" in cfg, "other pads kept")
    print(f"test_custom_kit: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
