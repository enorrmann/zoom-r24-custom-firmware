#!/usr/bin/env python3
"""build_firmware.py — the firmware to flash, from the stock image, in one step.

    python3 tools/build_firmware.py                 # -> build/R24SYS.BIN
    python3 tools/build_firmware.py --reserve-sec 2 mine.c other.c

Always the same chain, starting from R24SYS.BIN:

  1. r24bank.relocate --per-kit      every kit slot gets its own table entry
  2. loop_sd --add --root            TAKE SELECT item LOOP[SD], browsing the
                                     card root. The image after this step must
                                     be the one verified on a device
                                     (sha256 2af98550…, 2026-11-18) — checked.
  3. r24sdk build                    sdk/patches/custom_kit.c (kit CUSTOM,
                                     loaded per project; audio in free DDR:
                                     pool 3 + 3 + 19.9 + 5.25 MiB, ~5.8 min,
                                     -D CUSTOM_KIT_DDR=1) + kit_assign.c
                                     (FILE > KIT ASSIGN) + kit_editor.c
                                     (KIT SELECT > CUSTOM: 16-pad editor)
                                     + project_menu.c (PROJECT > IMPORT SD,
                                     IMPORT USB, CLEAR UNUSED) + drum_pitch.c
                                     (drum pitch; test: +1 st per mute group)
                                     + fast_browse.c (LOOP browser: no
                                     per-file header reads while listing)
                                     + step_pitch.c (PATTERN EDIT REC: soft
                                     key 4 = dial VEL/PITCH per step)
                                     + usb_midi.c (SYSTEM > USB MIDI: clock
                                     and start/stop to a USB-MIDI cable)
                                     + any extra patches

With the diagnostic PROJECT > DUMP 1 / DUMP 2 (flash + DSP + DDR to A:\DUMP1 /
A:\DUMP2, compare: r24dump.py DUMP1 --diff DUMP2; not in the default image):
  python3 tools/build_firmware.py --dump

Optional, not in the default image (device test pending):
  python3 tools/build_firmware.py sdk/patches/pattern_track.c
      a rhythm pattern uses ONE track, not a pair (docs/16_pattern_track.md)

The result is deterministic: same inputs, same bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import custom_kit
import loop_sd
import r24bank
import r24image
import r24sdk

ROOT = r24sdk.ROOT
STOCK = os.path.join(ROOT, "R24SYS.BIN")
OUT = os.path.join(ROOT, "build", "R24SYS.BIN")       # ready to copy to the card as is
LOOP_SD_VERIFIED = "2af985507c91d802cdf57a5ea4f7e4d03dfd8deda608d81152c60389a0c4df33"
PATCHES = [custom_kit.PATCH, custom_kit.MENU, custom_kit.EDITOR,
           os.path.join(r24sdk.SDK, "patches", "project_menu.c"),
           os.path.join(r24sdk.SDK, "patches", "drum_pitch.c"),
           os.path.join(r24sdk.SDK, "patches", "fast_browse.c"),
           os.path.join(r24sdk.SDK, "patches", "step_pitch.c"),
           os.path.join(r24sdk.SDK, "patches", "usb_midi.c")]


def sealed_sha(data: bytes) -> str:
    img = r24image.R24Image(bytes(data))
    img.refresh_checksum()
    return hashlib.sha256(img.data).hexdigest()


DUMP_PATCH = os.path.join(r24sdk.SDK, "patches", "mem_dump.c")


def build(extra: list[str], reserve_sec: float, trim: bool = True, dump: bool = False):
    """-> (image bytes, notes, r24sdk.Build)"""
    data = bytearray(open(STOCK, "rb").read())
    notes = []
    _lay, n = r24bank.relocate(data, None, True)
    notes.append("1. sample table relocated, 160 entries (kit k slot s -> 16k+s)")
    notes += [f"   {x}" for x in n]
    loop_sd.do_add(data, root=True)
    sha = sealed_sha(data)
    if sha != LOOP_SD_VERIFIED:
        raise r24sdk.SdkError(f"after LOOP[SD] the image is {sha[:16]}…, not the one "
                              f"verified on the device ({LOOP_SD_VERIFIED[:16]}…); "
                              f"is R24SYS.BIN the stock v1.13?")
    notes.append(f"2. LOOP[SD] added (5th TAKE SELECT item, card root) = "
                 f"device-verified image {sha[:8]}…")
    patches = PATCHES + ([DUMP_PATCH] if dump else []) + extra
    defines = ([custom_kit.reserve_define(reserve_sec, ddr=True), custom_kit.DDR_DEFINE]
               + (["PM_DUMP"] if dump else []))
    b, _rel, n = r24sdk.build(data, patches, defines, trim)
    notes.append("3. SDK build: " + ", ".join(os.path.basename(p) for p in patches)
                 + ("  (--dump: + DUMP 1 / DUMP 2)" if dump else ""))
    notes += [f"   {x}" for x in n]
    return data, notes, b


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the firmware to flash from R24SYS.BIN.")
    ap.add_argument("patches", nargs="*", help="extra SDK patches (.c)")
    ap.add_argument("-o", "--output", default=OUT)
    ap.add_argument("--reserve-sec", type=float, default=0.0,
                    help="extra CUSTOM kit memory in the bank tail, seconds of mono "
                         "44.1 kHz (default 0: the kit uses ~29 MiB of free DDR)")
    ap.add_argument("--no-trim", action="store_true")
    ap.add_argument("--dump", action="store_true",
                    help="add PROJECT > DUMP 1 / DUMP 2 (sdk/patches/mem_dump.c)")
    args = ap.parse_args(argv)
    try:
        data, notes, b = build(args.patches, args.reserve_sec, not args.no_trim,
                               args.dump)
    except (r24sdk.SdkError, r24bank.BankError, r24image.ImageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for n in notes:
        print(n)
    print(r24sdk.seal(data, args.output))
    with open(args.output, "rb") as f:
        print(f"sha256 : {hashlib.sha256(f.read()).hexdigest()}")
    with open(args.output + ".syms.json", "w") as f:
        json.dump({"section_ram": b.ram, "size": len(b.blob),
                   "sources": [os.path.relpath(p, ROOT) for p in
                               PATCHES + ([DUMP_PATCH] if args.dump else []) + args.patches],
                   "symbols": {k: v for k, v in sorted(b.symbols.items())
                               if b.ram <= v < b.ram + len(b.blob)}}, f, indent=1)
    print(f"symbols: {args.output}.syms.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
