#!/usr/bin/env python3
"""test_r24sdk.py — prove the C SDK (tools/r24sdk.py, sdk/).

* sdk/firmware.sym matches both images it is meant for;
* sdk/tests/selftest.c (every hook kind, Thumb and ARM firmware calls,
  runtime helpers) is built and RUN in the ARM926 emulator (tools/r24emu.py);
* both trampoline forms (entry at 4n and 4n+2) jump where they should;
* every refusal path leaves the image untouched;
* builds are deterministic.

    python3 tools/test_r24sdk.py
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24sdk

try:
    import r24emu
except ImportError:                                    # pragma: no cover
    print("SKIP: python3 -m pip install unicorn")
    raise SystemExit(0)

ROOT = r24sdk.ROOT
STOCK = os.path.join(ROOT, "R24SYS.BIN")
X0X = os.path.join(ROOT, "R24SYS_X0X.BIN")
SELFTEST = os.path.join(r24sdk.SDK, "tests", "selftest.c")
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


def built(src_image: str, sources: list[str], defines=()) -> tuple[bytes, r24sdk.Build, list[str]]:
    data = bytearray(open(src_image, "rb").read())
    b, _rel, notes = r24sdk.build(data, sources, list(defines))
    with tempfile.TemporaryDirectory() as t:
        out = os.path.join(t, "o.BIN")
        img = r24bank.seal(data, out)
        ok(img.checksum_ok(), "checksum verifies")
        return open(out, "rb").read(), b, notes


def refused(src_image: str, code: str, what: str, needle: str) -> None:
    data = bytearray(open(src_image, "rb").read())
    before = bytes(data)
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "p.c")
        open(p, "w").write('#include "r24.h"\n' + code)
        try:
            r24sdk.build(data, [p])
            ok(False, f"{what}: must be refused")
        except r24sdk.SdkError as exc:
            ok(needle in str(exc), f"{what}: message {exc}")
    ok(bytes(data) == before, f"{what}: image untouched")


def test_syms():
    syms = r24sdk.load_syms()
    ok(len(syms) >= 35, "symbol table loaded")
    ok(len({s["name"] for s in syms}) == len(syms), "unique names")
    for img in (STOCK, X0X):
        ok(r24sdk.verify_syms(open(img, "rb").read(), syms) == [], f"firmware.sym vs {img}")
    for s in syms:
        ok(s["name"].startswith("fw_"), f"{s['name']}: fw_ prefix")
        if s["kind"] == "thumb":
            ok(s["addr"] % 2 == 0, f"{s['name']}: even address in the file")
        if s["kind"] == "arm":
            ok(s["addr"] % 4 == 0, f"{s['name']}: ARM aligned")
    # every symbol in the header is bound, and every bound one is declared
    hdr = open(os.path.join(r24sdk.SDK, "include", "r24fw.h")).read()
    for s in syms:
        ok(s["name"] in hdr, f"{s['name']} declared in r24fw.h")


def test_selftest():
    src = open(STOCK, "rb").read()
    img, b, notes = built(STOCK, [SELFTEST])
    d = r24sdk.describe(img)
    ok(d and all(d["applied"]) and len(d["hooks"]) == 4, "4 hooks applied")
    ok("SELFTEST" in r24sdk.kit_names(img), "kit added")
    sy = b.symbols
    e = r24emu.R24Emu(img)
    ok(e.call(sy["st_probe"]) == 0x3FF, "st_probe: all 10 checks pass in the emulator")
    # REPLACE: calling the firmware's kit_ptr now runs st_kit_ptr
    kp = e.call(0x8004B69A, 3) & 0xFFFFFFFF
    ok(e.cstring(kp, 16).strip() == "ROCK", "kit_ptr(3) -> ROCK through the moved table")
    ok(e.u32(sy["st_calls"]) == 1, "replacement ran once")
    kp = e.call(0x8004B69A, 10) & 0xFFFFFFFF
    ok(e.cstring(kp, 16).strip() == "SELFTEST", "kit_ptr(10) -> SELFTEST")
    # POINTER and BYTES
    ok(e.u32(0x800FD3A4) == sy["st_kit_ptr"] | 1, "menu getter repointed (Thumb bit)")
    ok(e.cstring(0x80042E7C) == "SELFTEST!", "label bytes replaced")
    # the code region changed only at the hook sites and the known words
    allowed = set()
    for h in d["hooks"]:
        if h["kind"] == r24sdk.K_KIT:
            continue
        o = r24sdk.ram_to_file(h["addr"], img)
        n = {r24sdk.K_REPLACE: 10, r24sdk.K_POINTER: 4,
             r24sdk.K_BYTES: len(h["b"]) // 2}[h["kind"]]
        allowed |= set(range(o, o + n))
    for w in (*r24bank.TABLE_LITERALS, *r24bank.KIT_LITERALS, r24bank.KIT_COUNT_OFF):
        allowed |= set(range(w, w + 4))
    diff = {o for o in range(0x40, r24bank.BANK_BASE) if img[o] != src[o]}
    ok(diff <= allowed, f"unexpected changes {sorted(hex(x) for x in diff - allowed)[:8]}")
    # deterministic
    img2, _b, _n = built(STOCK, [SELFTEST])
    ok(img == img2, "build is deterministic")
    # one build per image
    try:
        r24sdk.build(bytearray(img), [SELFTEST])
        ok(False, "second build refused")
    except r24sdk.SdkError as exc:
        ok("already holds" in str(exc), "second build refused")


def test_trampoline_aligned():
    """REPLACE at a 4-byte aligned entry (fopen, 0x80054E54) and at 4n+2."""
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "t.c")
        open(p, "w").write('''#include "r24.h"
fw_file *t_fopen(const char *p, const char *m);
R24_REPLACE(0x80054E54, "30b58bb004000d00", t_fopen);
fw_file *t_fopen(const char *p, const char *m) { return (fw_file *)(0x1234 + (int)p[0]); }
int t_name(int n);
R24_REPLACE(0x8001D212, "0100002901da00207047", t_name);
int t_name(int n) { return n * 3; }
''')
        img, b, _ = built(STOCK, [p])
    o = 0x80054E54 - 0x80000000 + 0x5B4
    ok(img[o:o + 4] == bytes.fromhex("004b1847"), "4n form: ldr r3,[pc,#0]; bx r3")
    o2 = 0x8001D212 - 0x80000000 + 0x5B4
    ok(img[o2:o2 + 6] == bytes.fromhex("014b1847c046"), "4n+2 form")
    e = r24emu.R24Emu(img, file_stubs=False)
    a = e.alloc(b"A:\\x\0")
    ok(e.call(0x80054E54, a, a) == 0x1234 + ord("A"), "4n trampoline runs the C function")
    ok(e.call(0x8001D212, 5) == 15, "4n+2 trampoline runs the C function")


def test_refusals():
    refused(STOCK, 'int f(void){return 0;}\nR24_REPLACE(0x8001D6A6, "70b50400e3480460ffff", f);',
            "wrong original bytes", "patch expects")
    refused(STOCK, 'int f(void){return 0;}\nR24_REPLACE(0x8001D6A6, "70b50400", f);',
            "too few original bytes", "at least 10")
    refused(STOCK, 'int x;\nR24_POINTER(0x800FD3A4, 0x12345678, &x);',
            "wrong old pointer", "patch expects 0x12345678")
    refused(STOCK, 'R24_BYTES(0x80042E7C, "4c4f", "4c4f50");',
            "length mismatch", "differ in length")
    refused(STOCK, 'R24_BYTES(0x80042E7C, "4c4f4f50", "41414141");\n'
                   'R24_BYTES(0x80042E7E, "4f50", "4242");', "overlap", "overlap")
    refused(STOCK, 'R24_BYTES(0x90000000, "00", "01");', "address outside the image",
            "not part of the image")
    refused(STOCK, 'int f(void) { return undeclared_thing; }', "compile error", "compiling")
    refused(STOCK, 'R24_KIT("THIS NAME IS WAY TOO LONG");', "kit name", "1..15")


def test_symbol_mismatch():
    data = bytearray(open(STOCK, "rb").read())
    o = 0x80054E54 - 0x80000000 + 0x5B4
    data[o] ^= 0xFF                                  # "another firmware version"
    bad = r24sdk.verify_syms(bytes(data), r24sdk.load_syms())
    ok(len(bad) == 1 and "fw_fopen" in bad[0], "changed function detected")


def main() -> int:
    test_syms()
    test_selftest()
    test_trampoline_aligned()
    test_refusals()
    test_symbol_mismatch()
    print(f"test_r24sdk: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
