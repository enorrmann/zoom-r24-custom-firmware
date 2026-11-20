#!/usr/bin/env python3
"""test_flash_safety.py — no tool can write an image the boot loader would
flash over the boot flag or the boot loader (docs/18_dump_analysis.md §3.3).

    python3 tools/test_flash_safety.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
checks = 0


def ok(cond, what):
    global checks
    checks += 1
    if not cond:
        raise AssertionError(what)


def refused(data: bytes, out: str, word: str) -> bool:
    try:
        r24bank.seal(bytearray(data), out)
    except r24image.ImageError as exc:
        return word in str(exc) and not os.path.exists(out)
    return False


def main() -> int:
    stock = open(os.path.join(ROOT, "R24SYS.BIN"), "rb").read()
    ok(r24image.MAX_FILE_SIZE == 7_471_168, "limit = 0x40 + 0x720000")
    with tempfile.TemporaryDirectory() as t:
        out = lambda n: os.path.join(t, n)
        # every image in the repo is inside the limit and verifies
        for n in ("R24SYS.BIN", "R24SYS_X0X.BIN", "build/R24SYS.BIN"):
            p = os.path.join(ROOT, n)
            if os.path.exists(p):
                img = r24image.R24Image.from_file(p)
                img.verify()
                ok(len(img.payload) <= r24image.MAX_PAYLOAD, n)
        # exactly at the limit: written
        big = stock + b"\0" * (r24image.MAX_FILE_SIZE - len(stock))
        r24bank.seal(bytearray(big), out("max.BIN"))
        ok(os.path.getsize(out("max.BIN")) == r24image.MAX_FILE_SIZE, "at the limit: written")
        # one byte more: refused, nothing written
        ok(refused(big + b"\0", out("over.BIN"), "boot flag"), "limit + 1 refused")
        # into the wrap zone: the message names the boot loader
        ok(refused(stock + b"\0" * (0x780040 + 1 - len(stock)), out("wrap.BIN"), "brick"),
           "over 0x780000: brick warning")
        # header kind byte and checksum flag
        bad = bytearray(stock); bad[0x10] = 1
        ok(refused(bytes(bad), out("kind.BIN"), "kind"), "kind byte != 0 refused")
        bad = bytearray(stock); bad[0x30] = 0
        ok(refused(bytes(bad), out("flag.BIN"), "0x30"), "checksum flag 0 refused")
        # verify() refuses too (r24tool verify)
        img = r24image.R24Image(big + b"\0")
        img.refresh_checksum()
        try:
            img.verify()
            ok(False, "verify accepted an oversized image")
        except r24image.ImageError:
            ok(True, "verify refuses")
    print(f"test_flash_safety: {checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
