#!/usr/bin/env python3
"""r24dump.py — check a PROJECT > DUMP MEMORY dump (docs/18_dump_analysis.md).

    python3 tools/r24dump.py DUMP/ [--image build/R24SYS.BIN]
    python3 tools/r24dump.py DUMP1/ --diff DUMP2/     which DDR stayed untouched

Prints and checks, from the files only:
  * INFO.TXT ids;
  * NOR size (aliasing) and its layout: boot loader, main image, boot flag,
    version records, effect area;
  * which image is flashed (payload == file[0x40:]);
  * the boot loader's version string and its update-file names;
  * the DSP: every section of fw_dsp_sections against DSP_L2/L1D/DDR.
Exit status 1 if a check fails.

--diff: both folders need DDR.BIN (DUMP 1 right after power-on, DUMP 2 after
heavy use: recording, bounce, effects, FILE COPY, USB). Prints the DDR map
per 64 KiB (same / changed) and, for each candidate free zone of
docs/18_dump_analysis.md §5, how many bytes changed and where. A candidate is
usable only if it is unchanged (DRAM is not zero at power-on, so "unchanged"
is the test, not "zero"). Exit status 1 if a candidate changed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOR_BASE = 0x02000000
MAIN_OFF, MAIN_MAX = 0x80000, 0x720000         # 0x02080000 .. 0x0279FFFF
FLAG_OFF, REC_OFF, FX_OFF = 0x7A0000, 0x7B0000, 0x7E0000
BOOT_FLAG = b"ZOOM/R16"
DSP_TABLE, DSP_COUNT = 0x801EDB24, 14
DUMP_BASE = {"DSP_DDR": 0x87B00000, "DSP_L2": 0x11800000, "DSP_L1D": 0x11F04000,
             "DSP_L1P": 0x11E08000}
CODE_SECTIONS = {2, 4, 5, 6, 11, 12, 13}     # must be identical (code/const)

fails = 0


def check(cond: bool, what: str) -> None:
    global fails
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails += 1


def ram_off(a: int) -> int:
    return a - 0x80000000 + 0x5B4


def segments(b: bytes, base: int, step: int = 0x10000):
    out, prev, start = [], None, 0
    for i in range(0, len(b), step):
        blk = b[i:i + step]
        k = "erased" if blk == b"\xff" * len(blk) else "zero" if not any(blk) else "data"
        if k != prev:
            if prev:
                out.append((base + start, base + i, prev))
            prev, start = k, i
    out.append((base + start, base + len(b), prev))
    return out


DDR_BASE = 0x80000000
# (name, start, end): the zones to clear before use (docs/20_kit_memory_plan.md §2)
CANDIDATES = [("19.9 MiB after _end", 0x81C128B0, 0x83000000),
              ("5.25 MiB below the bank", 0x87000000, 0x87540000),
              ("0.75 MiB after the bank", 0x87A40000, 0x87B00000)]
# in use by the CUSTOM kit (CUSTOM_KIT_DDR): changes there are expected
KIT_ZONES = [("pool 3 (CUSTOM kit)", 0x815E1018, 0x816E1018),
             ("3 MiB (CUSTOM kit)", 0x819126A8, 0x81C126A8)]


def ddr_diff(d1: str, d2: str) -> int:
    a = open(os.path.join(d1, "DDR.BIN"), "rb").read()
    b = open(os.path.join(d2, "DDR.BIN"), "rb").read()
    n = min(len(a), len(b))
    print(f"DDR diff {d1} -> {d2} ({n >> 20} MiB)")
    if len(a) != len(b):
        print(f"  note: sizes differ ({len(a)} / {len(b)}), compared the first {n}")
    step = 0x10000
    runs, prev, start = [], None, 0
    for i in range(0, n, step):
        k = "same" if a[i:i + step] == b[i:i + step] else "CHANGED"
        if k != prev:
            if prev:
                runs.append((start, i, prev))
            prev, start = k, i
    runs.append((start, n, prev))
    print("\nmap (64 KiB blocks)")
    for s0, e0, k in runs:
        print(f"  {DDR_BASE + s0:#010x}-{DDR_BASE + e0 - 1:#010x} {(e0 - s0) / 0x100000:8.2f} MiB  {k}")

    def zone(name, lo, hi, must):
        o, e = lo - DDR_BASE, min(hi - DDR_BASE, n)
        za, zb = a[o:e], b[o:e]
        if za == zb:
            print(f"  ok    {name:26s} {lo:#010x}-{hi - 1:#010x} unchanged")
            return 0
        diff = [i for i in range(0, e - o, 4096) if za[i:i + 4096] != zb[i:i + 4096]]
        changed = sum(x != y for x, y in zip(za, zb))
        first = next(i for i in range(diff[0], e - o) if za[i] != zb[i])
        last = max(i for i in range(diff[-1], min(diff[-1] + 4096, e - o)) if za[i] != zb[i])
        tag = "FAIL  " if must else "info  "
        print(f"  {tag}{name:26s} {lo:#010x}-{hi - 1:#010x} {changed} bytes changed in "
              f"{len(diff)} pages of 4 KiB, {lo + first:#010x}..{lo + last:#010x}")
        return 1 if must else 0

    print("\ncandidate free zones")
    bad = sum(zone(nm, lo, hi, True) for nm, lo, hi in CANDIDATES)
    print("\nCUSTOM kit zones (change if a kit was (re)loaded in between)")
    for nm, lo, hi in KIT_ZONES:
        zone(nm, lo, hi, False)
    print(f"\n{'ALL CANDIDATES UNCHANGED' if not bad else f'{bad} CANDIDATE(S) CHANGED'}")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dump", help="folder with NOR.BIN, DSP_*.BIN, INFO.TXT")
    ap.add_argument("--image", default=os.path.join(ROOT, "build", "R24SYS.BIN"),
                    help="the image believed to be flashed")
    ap.add_argument("--stock", default=os.path.join(ROOT, "R24SYS.BIN"))
    ap.add_argument("--diff", metavar="DUMP2",
                    help="compare DDR.BIN with this second dump folder (only that)")
    a = ap.parse_args(argv)
    if a.diff:
        return ddr_diff(a.dump, a.diff)
    rd = lambda n: open(os.path.join(a.dump, n), "rb").read()

    print("INFO.TXT")
    for line in rd("INFO.TXT").decode("latin-1").splitlines()[1:4]:
        print("  " + line)

    nor = rd("NOR.BIN")
    print("\nNOR flash (EMIF CS2, 0x02000000)")
    size = len(nor)
    while size > 0x100000 and nor[:size // 2] == nor[size // 2:size]:
        size //= 2
    check(size == 0x800000, f"chip size {size >> 20} MiB (the dump window repeats above it)")
    nor = nor[:size]
    for lo, hi, k in segments(nor, NOR_BASE):
        print(f"  {lo:#010x}-{hi - 1:#010x}  {k}")

    boot = nor[:MAIN_OFF]
    i = boot.find(b"D211 BOOT ")
    check(i >= 0, "boot loader at 0x02000000 (\"D211 BOOT %s\" screen)")
    for name in (b"A:\\R24SYS.BIN", b"A:\\R24BOOT.BIN", b"A:\\R24SUB.BIN",
                 b"A:\\R24TEST.BIN", b"A:\\RHYTHM.BIN"):
        check(boot.find(name) >= 0, f"boot loader update file {name.decode()}")
    check(nor[FLAG_OFF:FLAG_OFF + 8] == BOOT_FLAG,
          "boot flag 'ZOOM/R16' at 0x027A0000 (boot loader starts the main image)")
    for k, what in ((0, "BOOT"), (1, "SYSTEM"), (2, "kind 2")):
        rec = nor[REC_OFF + (k << 16):REC_OFF + (k << 16) + 12]
        if rec[0] != 0xFF:
            ver = rec[:8].split(b"\0")[0].decode("latin-1")
            print(f"  record {what:6s} {REC_OFF + NOR_BASE + (k << 16):#010x}: "
                  f"version {ver!r} stamp {struct.unpack('<I', rec[8:12])[0]:#010x}")

    print("\nmain image (0x02080000)")
    payload = nor[MAIN_OFF:FLAG_OFF]
    for p in (a.image, a.stock):
        if os.path.exists(p):
            img = open(p, "rb").read()
            same = payload[:len(img) - 0x40] == img[0x40:]
            check(same or p == a.stock,
                  f"{os.path.relpath(p, ROOT)} ({hashlib.sha256(img).hexdigest()[:8]}): "
                  + ("flashed, payload == file[0x40:]" if same else "not this one"))
            check(len(img) - 0x40 <= MAIN_MAX,
                  f"{os.path.relpath(p, ROOT)}: payload {len(img) - 0x40:#x} <= {MAIN_MAX:#x} "
                  f"(room left {MAIN_MAX - len(img) + 0x40} bytes)")

    print("\nDSP program (fw_dsp_sections) against the live DSP memory")
    stock = open(a.stock, "rb").read()
    mem = {n: rd(n + ".BIN") for n in DUMP_BASE if os.path.exists(os.path.join(a.dump, n + ".BIN"))}
    for k in range(DSP_COUNT):
        src, dst, n = struct.unpack_from("<3I", stock, ram_off(DSP_TABLE) + 12 * k)
        want = stock[ram_off(src):ram_off(src) + n]
        got = next((m[dst - DUMP_BASE[r]:dst - DUMP_BASE[r] + n] for r, m in mem.items()
                    if DUMP_BASE[r] <= dst and dst + n <= DUMP_BASE[r] + len(m)), None)
        if got is None:
            print(f"  --    section {k:2d} {dst:#010x} not in the dump")
            continue
        same = sum(x == y for x, y in zip(got, want))
        if k in CODE_SECTIONS:
            check(got == want, f"section {k:2d} {dst:#010x} {n:#7x} code/const identical")
        else:
            print(f"  data  section {k:2d} {dst:#010x} {n:#7x} {same}/{n} bytes as loaded")

    print(f"\n{'ALL CHECKS PASSED' if not fails else f'{fails} CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
