#!/usr/bin/env python3
"""gen_sample_map.py — emit the sample-position document from a firmware image.

The map in docs/09_sample_map.md is generated, never hand-edited: it is read
straight out of the Master Sample Table the firmware uses (factory table at
file 0x000E20EC, or a relocated one — see tools/r24bank.py), so it cannot
drift away from the binary.

    python3 tools/gen_sample_map.py R24SYS.BIN -o docs/09_sample_map.md
    python3 tools/gen_sample_map.py R24SYS.BIN            # to stdout
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank

BANK_BASE = r24bank.BANK_BASE
KIT_SIZE = 144
RATE = 44100


def read_table(data: bytes) -> list[tuple[int, int]]:
    return r24bank.read_table(data)


def read_kits(data: bytes) -> list[tuple[str, list[int]]]:
    kits = []
    kbase, nkits = r24bank.locate_kits(data)
    for k in range(nkits):
        off = kbase + k * KIT_SIZE
        name = data[off:off + 16].split(b"\x00")[0].decode("ascii", "replace").strip()
        sids = [struct.unpack_from("<H", data, off + 16 + s * 8)[0]
                for s in range(16)]
        kits.append((name, sids))
    return kits


def build_doc(image: str, data: bytes) -> str:
    # Use the basename in the doc so regenerating from a different path
    # produces byte-identical output; the doc must not depend on where the
    # image happens to live.
    image = os.path.basename(image)
    lay = r24bank.locate(data)
    table = read_table(data)
    n = len(table)
    kits = read_kits(data)
    total = max(rel + length for rel, length in table)
    distinct = len(set(table))

    used_by: dict[int, list[str]] = {i: [] for i in range(n)}
    for name, sids in kits:
        for sid in sorted(set(sids)):
            if 0 <= sid < n:
                used_by[sid].append(name)

    L: list[str] = []
    A = L.append
    A("# 09 — Sample positions inside the binary")
    A("")
    A("> **Generated file — do not edit by hand.**")
    A("> Regenerate with `python3 tools/gen_sample_map.py"
      f" {image} -o docs/09_sample_map.md`")
    A("")
    A("Every entry of the Master Sample Table, with its exact byte position in "
      "the image, its length, and the preset kits that use it. These are the "
      "coordinates the firmware plays from and that `tools/patch_samples.py` "
      "patches (in place) or rewrites (`--repack`).")
    A("")
    A("## Geometry")
    A("")
    A("```")
    A(f"image            {image}   ({len(data)} bytes)")
    kind = "relocated" if lay.relocated else "factory"
    A(f"sample table     {kind}: file 0x{lay.table_off:08X} .. "
      f"0x{lay.table_off + n * 8:08X}   "
      f"({n} entries x 8 bytes = <II rel_start,length>)")
    A(f"table literal    0x{lay.literal:08X}   (file 0x2E244 and 0x2E624)")
    A(f"bank base        file 0x{BANK_BASE:08X}   "
      f"(VA 0x{r24bank.BANK_VA:08X}, copied to RAM 0x{r24bank.RAM_BASE:08X})")
    A(f"bank size        {len(data) - BANK_BASE} bytes (0x{len(data) - BANK_BASE:X}), "
      f"ends at EOF")
    A(f"audio area       0x0 .. 0x{lay.audio_limit:X} "
      f"({lay.audio_limit / 2 / RATE:.2f} s at {RATE} Hz)")
    A(f"audio used       0x0 .. 0x{total:X}; {distinct} distinct ranges "
      f"for {n} entries")
    A("format           16-bit signed LE, mono, no header, even lengths")
    A("```")
    A("")
    A("A sample's bytes live at:")
    A("")
    A("```")
    A(f"start = 0x{BANK_BASE:08X} + rel_start")
    A("end   = start + length")
    A("")
    A("# what the firmware does at voice start (VA 0x020AE016):")
    A(f"#   ram_start = 0x{r24bank.RAM_BASE:08X} + rel_start")
    A("#   ram_end   = ram_start + length - 2")
    A("```")
    A("")
    A("## Positions")
    A("")
    A("| id | rel_start | length (B) | file start | file end | ms @44.1k | used by |")
    A("|---:|---:|---:|---:|---:|---:|---|")
    for i, (rel, length) in enumerate(table):
        start = BANK_BASE + rel
        end = start + length
        who = ", ".join(used_by[i]) if used_by[i] else "—"
        A(f"| {i} | `0x{rel:06X}` | {length} | `0x{start:08X}` | "
          f"`0x{end:08X}` | {length / 2 / RATE * 1000:.1f} | {who} |")
    A("")
    A("## Notes")
    A("")
    A("* Ids are **table entries**, not names. A kit slot stores an id; the "
      "firmware looks the id up in this table at playback time.")
    A("* **The table is data, not frozen.** The code reads it through two "
      "literal words only and never addresses a sample by a fixed address, "
      "so positions, lengths and the entry count can all change "
      "(`patch_samples.py --repack`, `r24bank.py relocate`). The only fixed "
      "things are the bank base and the file size.")
    A("* Entries may share audio (a relocated per-kit table starts that way); "
      "an in-place patch refuses an entry whose bytes are shared.")
    A("* The bank is 44.1 kHz mono 16-bit; the `ms` column uses that rate "
      "(the firmware's voice-duration code divides by 44100 as well).")
    A("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Emit the sample-position document from a firmware image.")
    ap.add_argument("image")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args(argv)
    data = open(args.image, "rb").read()
    doc = build_doc(args.image, data)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print(f"wrote {args.output} ({len(doc)} bytes, "
              f"{len(read_table(data))} sample positions)")
    else:
        sys.stdout.write(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
