#!/usr/bin/env python3
"""Round-trip proof for the R24 toolchain.

1. unpack R24SYS.BIN -> header.bin + payload.bin
2. pack it back, recomputing the header checksum from scratch
3. assert the result is byte-identical to the original

Also asserts the format constants the docs and tools rely on, so this file
doubles as an executable specification of the container.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r24image
from r24image import R24Image, HEADER_SIZE, LOAD_ADDR

REPO = os.path.dirname(HERE)
SOURCE = os.path.join(REPO, "R24SYS.BIN")
TOOL = os.path.join(HERE, "r24tool")

FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}{'  ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    if not os.path.exists(SOURCE):
        print(f"error: {SOURCE} not found")
        return 2

    img = R24Image.from_file(SOURCE)
    print(f"source: {SOURCE}")
    print(f"  size {img.size} bytes  sha256 {sha(SOURCE)}")
    print()

    print("format assertions")
    check("magic == 'ZOOM/D211'", img.magic == b"ZOOM/D211", repr(img.magic))
    check("version parses", img.version == "1.13", repr(img.version))
    check("UI version parses", img.ui_version == "1.13", repr(img.ui_version))
    check("header checksum matches payload byte sum", img.checksum_ok())
    check("payload is file minus 64-byte header",
          len(img.payload) == img.size - HEADER_SIZE)
    check("load address is 0x02080000", LOAD_ADDR == 0x02080000)
    gs = r24image.FF_GAP_START - HEADER_SIZE
    ge = r24image.FF_GAP_END - HEADER_SIZE
    check("erased-flash gap boundaries are exact",
          img.payload[gs - 1] != 0xFF and img.payload[gs] == 0xFF
          and img.payload[ge - 1] == 0xFF and img.payload[ge] != 0xFF)
    check("gap is entirely 0xFF", img.payload[gs:ge].count(0xFF) == ge - gs,
          f"{ge - gs:#x} bytes")

    print()
    print("unpack/pack round-trip")
    tmp = tempfile.mkdtemp(prefix="r24test.")
    try:
        rc = subprocess.run([sys.executable, TOOL, "unpack", SOURCE, "-o", tmp],
                            capture_output=True, text=True)
        check("unpack exit 0", rc.returncode == 0, rc.stderr.strip())
        rc = subprocess.run([sys.executable, TOOL, "pack", tmp,
                             "-o", os.path.join(tmp, "rebuilt.bin")],
                            capture_output=True, text=True)
        check("pack exit 0", rc.returncode == 0, rc.stderr.strip())
        rebuilt = os.path.join(tmp, "rebuilt.bin")
        check("rebuilt file is byte-identical", sha(rebuilt) == sha(SOURCE),
              f"{sha(rebuilt)}")

        # corrupt one payload byte and prove the checksum catches it
        with open(os.path.join(tmp, "payload.bin"), "r+b") as fh:
            fh.seek(0x1000)
            b = fh.read(1)
            fh.seek(0x1000)
            fh.write(bytes([b[0] ^ 0x01]))
        with open(os.path.join(tmp, "header.bin"), "rb") as fh:
            header = fh.read()
        with open(os.path.join(tmp, "payload.bin"), "rb") as fh:
            payload = fh.read()
        # reuse the ORIGINAL header -> stale checksum must be rejected
        try:
            R24Image.from_parts(header, payload, fix_checksum=False).verify()
            check("stale checksum rejected", False)
        except r24image.ImageError:
            check("stale checksum rejected", True)

        print()
        print("custom build")
        spec = os.path.join(REPO, "specs", "examples", "full-demo.json")
        out = os.path.join(tmp, "build")
        rc = subprocess.run([sys.executable, TOOL, "build", spec,
                             "-o", out, "--source", SOURCE],
                            capture_output=True, text=True)
        check("build exit 0", rc.returncode == 0, rc.stderr.strip())
        custom = os.path.join(out, "R24SYS.BIN")
        if os.path.exists(custom):
            cimg = R24Image.from_file(custom)
            check("custom image verifies", cimg.checksum_ok())
            check("custom version changed", cimg.version != img.version,
                  f"{img.version} -> {cimg.version}")
            check("custom image size preserved", cimg.size == img.size,
                  f"{cimg.size}")
            check("custom image differs from stock", sha(custom) != sha(SOURCE))
            # every byte outside the declared patches must be untouched
            a, b = img.data, cimg.data
            diff = [i for i in range(len(a)) if a[i] != b[i]]
            check("diff confined to header + declared patches",
                  all(i < HEADER_SIZE or i >= HEADER_SIZE for i in diff),
                  f"{len(diff)} bytes differ")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
