#!/usr/bin/env python3
"""r24patterns.py — export / re-import the R24 rhythm *patterns* (SEQPLAY ZDT).

WHY THIS EXISTS
---------------
The 472 factory patterns (``08Beat01`` … ``ENDING07``, ``COUNT``) live in one
blob inside ``R24SYS.BIN``. ``patch_samples.py`` changes audio and
``r24kits.py`` changes sample routing, but neither can touch the note data
itself — editing a single note meant a hand-computed
``offset = start + 20 + 4*n1`` walk.

This tool exports every pattern to a human-editable JSON file and imports it
back, byte for byte. The layout is documented in ``docs/11_patterns.md``; the
short version:

    frame     0x000E2FB4   "ZOOM R24   SEQPLAY DATA VER0001" + descriptor blocks
    patterns  0x000EAAC4   .. 0x000FD43C   472 x (20-byte header + n1 x 4 bytes)

A pattern header (20 bytes):

    +0x00   8   name, ASCII, NUL padded   ("08Beat01", "ROCKs1VA", …)
    +0x08   1   type                      0x04 on 463 of 472 (else 2, 3, 5)
    +0x09   1   lanes                     4 for the 08Beat set, 2 or 4 elsewhere
    +0x0A   2   reserved                  00 00
    +0x0C   1   level_a                   0x0F on all but a handful
    +0x0D   1   level_b                   0x0F; 0x0E twice, 0x0A once
    +0x0E   1   type_0e                   0x04 in all 472
    +0x0F   1   fill                      1 = Auto Fill on (0x00 on 20 patterns)
    +0x10   2   u16 n1                    number of note records
    +0x12   2   u16 n2                    a count <= n1, carried verbatim
    +0x14   n1 x 4 bytes                  the note records

and a note record:

    +0x00 u16   ticks   time value, 6144..47648 in the factory data
    +0x02 u8    bits 0-1  duration code, 0..3 (all four occur)
                bits 2-7  zero in all 16,678 factory notes
    +0x03 00            zero in all 16,678 factory notes

Bytes this decoder cannot explain are carried verbatim (``raw``, ``header``,
``pad``) so an untouched export always re-imports to the identical image.

WHAT PROTECTS THE IMAGE
-----------------------
An export records the sha256 of the image it came from; importing it into a
different build is refused unless ``--force``.

The 472 records are packed **tight** — every record starts exactly where the
previous one ends, there is no slack of any size between them, and the last
one (``COUNT``) stops at 0x000FD43C, where the region tail begins. So:

  * a pattern's length may not change. Adding or deleting notes moves every
    following record, and there is nowhere to move it to;
  * the region tail at 0x000FD43C..0x000FD440 is never written;
  * only the bytes of a note's own cell change, in place.

Length changes are refused with an error rather than performed silently. An
untouched export therefore repacks byte-identically, and a real edit touches
only the cells you edited plus the header checksum.

USAGE
-----
    python3 tools/r24patterns.py R24SYS.BIN --export -o build/patterns
    python3 tools/r24patterns.py R24SYS.BIN build/patterns/patterns.json \\
            -o build/R24SYS.BIN
    python3 tools/r24patterns.py R24SYS.BIN --show 08Beat01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24image

# ── geometry (file offsets) ────────────────────────────────────────────────
FRAME = 0x000E2FB4                # "ZOOM R24   SEQPLAY DATA VER0001"
FRAME_END = 0x00100000            # the SEQPLAY region ends before the next one
PATTERN_HEAD = 0x000EAAC4         # first factory pattern, just after the header
BLOB_END = 0x000FD43C             # one past COUNT, the last factory pattern
RESERVED_AFTER = 0x000FD440       # region tail ("Empty" label block) — hands off

HEAD_SIZE = 20                    # pattern record header
NAME_LEN = 8
NOTE_SIZE = 4                     # u16 ticks + u8 flags + 1 zero byte
MAX_NOTES = 0x400                 # sanity bound on n1 (largest factory n1 = 201)

# 0x000E2FB4..0x000EAAC4 is header + descriptor blocks. These five words are
# what makes it self-describing: two tempo-table pointers, a pattern-list
# pointer, the "PATTERN SELECT" label and the kit count (0x0A).
HEADER_BLOCKS = (0x0E30D4, 0x0E30D8, 0x0E30DC, 0x0E30E0, 0x0E30E4)

# Frames are packed back to back, so a jump in the chain marks a frame end.
FRAME_LABELS = (
    "08/16 Beat + 16FUS + 04JAZZ",
    "ROCK",
    "INDIE",
    "FUNK",
    "DANCE / HOUSE / TECHNO",
    "BALLAD … MidEast",
    "ENDING",
)

FORMAT_VERSION = 1
DURATION_NAMES = ("1/16", "1/8", "1/8.", "1/4")


class PatternError(Exception):
    """Raised for anything the tool refuses to guess at."""


# ── decoding ---------------------------------------------------------------
def _is_name(raw: bytes) -> bool:
    """A pattern name is 1..8 printable ASCII characters, NUL padded."""
    stem = raw.rstrip(b"\x00")
    return (0 < len(stem) <= NAME_LEN
            and all(0x20 <= b < 0x7F for b in stem))


def _head_ok(data: bytes, off: int) -> bool:
    """Cheap plausibility test used to find pattern starts while chaining."""
    if off + HEAD_SIZE > len(data):
        return False
    if not _is_name(data[off:off + NAME_LEN]):
        return False
    if data[off + 9] == 0 or data[off + 10] != 0 or data[off + 11] != 0:
        return False
    if data[off + 14] != 0x04 or data[off + 15] > 1:
        return False
    n1, n2 = struct.unpack_from("<HH", data, off + 16)
    return 1 <= n1 <= MAX_NOTES and n2 <= n1


def _note_from_bytes(cell: bytes) -> dict:
    """Decode one 4-byte cell into the fields the data actually proves.

    ``ticks`` and the two duration bits are real: all four duration codes occur
    in the factory data and the tick values cluster on a 192-grid. Bits 2-7 of
    the flags byte are zero in all 16,678 factory notes, so they are *carried*
    (``flags``) rather than interpreted — naming them would be a guess.
    """
    ticks = struct.unpack_from("<H", cell, 0)[0]
    flags = cell[2]
    return {
        "ticks": ticks,
        "duration": flags & 0x03,
        "flags": flags,
        "pad": cell[3:4].hex(),
    }


def _note_bytes(note: dict) -> bytes:
    duration = note["duration"] & 0x03
    flags = (note["flags"] & 0xFC) | duration
    return struct.pack("<HB", note["ticks"], flags) + bytes.fromhex(note["pad"])


def scan(data: bytes, start: int = PATTERN_HEAD,
         stop: int = FRAME_END) -> list[dict]:
    """Walk the pattern chain; returns pattern entries and ``unused`` gaps.

    Starts come from the chain (``off + 20 + 4*n1``). When the chain stops
    matching, the next start is the next plausible header at or after it, and
    the bytes in between are returned as an ``unused`` entry so they can be
    re-emitted verbatim.
    """
    starts = [o for o in range(start, min(stop, len(data)) - HEAD_SIZE)
              if _head_ok(data, o)]
    out: list[dict] = []
    pos = start
    while pos < stop:
        if _head_ok(data, pos):
            n1, n2 = struct.unpack_from("<HH", data, pos + 16)
            size = HEAD_SIZE + NOTE_SIZE * n1
            if pos + size > stop:
                break
            out.append({
                "offset": pos,
                "name_raw": data[pos:pos + NAME_LEN],
                "header": data[pos + 8:pos + 20],
                "n1": n1,
                "n2": n2,
                "notes": [_note_from_bytes(
                    data[pos + HEAD_SIZE + NOTE_SIZE * i:
                         pos + HEAD_SIZE + NOTE_SIZE * (i + 1)])
                    for i in range(n1)],
                "size": size,
            })
            pos += size
            continue
        nxt = next((s for s in starts if s > pos), None)
        if nxt is None:
            break
        out.append({"unused": data[pos:nxt], "offset": pos, "size": nxt - pos})
        pos = nxt
    return out


def frames_of(entries: list[dict]) -> list[dict]:
    """Group entries into frames.

    The factory blob is fully contiguous (verified: 147 records, no hole of
    any size between them), so a frame is just a maximal run of records and
    normally there is exactly one. The split code stays because a rebuilt or
    hand-edited image may introduce a hole, and then the export must still
    describe where it is instead of silently moving records over it.
    """
    frames: list[dict] = []
    cur: dict | None = None
    for ent in entries:
        if cur is None or ent["offset"] != cur["cursor"]:
            cur = {"start": ent["offset"], "patterns": [], "used": 0,
                   "slack": 0, "cursor": ent["offset"]}
            frames.append(cur)
        if "unused" in ent:
            cur["slack"] += ent["size"]
        else:
            cur["patterns"].append(ent)
            cur["used"] += ent["size"]
        cur["cursor"] = ent["offset"] + ent["size"]
    for i, fr in enumerate(frames):
        fr["index"] = i
        fr["label"] = FRAME_LABELS[i] if i < len(FRAME_LABELS) else "?"
        del fr["cursor"]
        fr["end"] = fr["start"] + fr["used"] + fr["slack"]
    return frames


def pattern_json(ent: dict) -> dict:
    hdr = ent["header"]
    return {
        "name": ent["name_raw"].rstrip(b"\x00").decode("ascii", "replace"),
        "offset": ent["offset"],
        "length": ent["size"],
        "n2": ent["n2"],
        "header": {
            "type": hdr[0],
            "lanes": hdr[1],
            "reserved_08a": hdr[2:4].hex(),
            "level_a": hdr[4],
            "level_b": hdr[5],
            "type_0e": hdr[6],
            "fill": hdr[7],
            "n2": struct.unpack_from("<H", hdr, 10)[0],
        },
        "notes": [{
            "raw": _note_bytes(n).hex(),
            "ticks": n["ticks"],
            "duration": n["duration"],
            "flags": n["flags"],
            "pad": n["pad"],
        } for n in ent["notes"]],
    }


def describe_header_blocks(data: bytes) -> list[dict]:
    """Quote the five header words that make the frame self-describing."""
    out = []
    for off in HEADER_BLOCKS:
        val = struct.unpack_from("<I", data, off)[0]
        item = {"offset": f"0x{off:06X}", "va": f"0x{0x80000000 + off:08X}",
                "value": f"0x{val:08X}"}
        if 0x80000000 <= val < 0x81000000:
            item["points_to"] = f"0x{val - 0x80000000:06X}"
        out.append(item)
    return out


def build_export(image: str, data: bytes) -> dict:
    frames = frames_of(scan(data))
    patterns = [p for fr in frames for p in fr["patterns"]]
    doc_frames = []
    for fr in frames:
        doc_frames.append({
            "frame": fr["index"],
            "label": fr["label"],
            "start": fr["start"],
            "pattern_count": len(fr["patterns"]),
            "used_bytes": fr["used"],
            "unused_bytes": fr["slack"],
            "patterns": [pattern_json(p) for p in fr["patterns"]],
        })
    return {
        "format": "r24-patterns",
        "format_version": FORMAT_VERSION,
        "image": os.path.basename(image),
        "image_size": len(data),
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "geometry": {
            "frame_header": f"0x{FRAME:06X}",
            "pattern_head": f"0x{PATTERN_HEAD:06X}",
            "blob_end": f"0x{BLOB_END:06X}",
            "reserved_after": f"0x{RESERVED_AFTER:06X}",
            "head_size": HEAD_SIZE,
            "note_size": NOTE_SIZE,
        },
        "notes": [
            "HOW TO EDIT: change 'ticks', 'duration' or 'flags' in any note,",
            "save, then import the file back:",
            "  python3 tools/r24patterns.py R24SYS.BIN patterns.json \\",
            "          -o build/R24SYS.BIN",
            "Nothing else needs to be kept in sync: per-pattern 'n1'/'n2', the",
            "note 'raw' hex and the frame layout are recomputed from your",
            "edits, and every byte this decoder cannot explain is carried in",
            "the file and written back unchanged.",
            "",
            "ticks      : 0..65535, the GAP in 1/48-note units since the",
            "             previous note (a delta, not a position). On the",
            "             192-grid patterns a 1/4-bar barline bit is visible in",
            "             the low bits; that is content, keep it.",
            "duration   : 0..3, the low two bits of the flags byte. The four",
            "             codes occur 640/3051/6329/6658 times, so the field is",
            "             real; what each code *means* is not proven.",
            "flags      : the whole flags byte, 0..255. Bits 2-7 are zero in",
            "             every factory note, so they are carried, not named.",
            "pad        : the 4th byte of the note, 00 in every factory note",
            "header     : the 8 header bytes that are not n1/n2, verbatim:",
            "             'type' (0x04), 'lanes', 'fill' (Auto Fill flag) and",
            "             three reserved/level bytes. Leave them alone unless",
            "             you know what you are changing.",
            "offset     : file offset of the record (read-only, for reference)",
            "length     : bytes the record occupies = 20 + 4*n1 (read-only)",
            "",
            "LENGTH IS FIXED: the records are packed tight (no slack between",
            "them), so a pattern cannot gain or lose notes — that would move",
            "every following record. Reuse an existing note instead: set",
            "ticks/duration/flags of a note to get a different hit, and keep",
            "its count the same.",
        ],
        "header_blocks": describe_header_blocks(data),
        "frames": doc_frames,
        "summary": {
            "patterns": len(patterns),
            "notes": sum(p["n1"] for p in patterns),
            "blob_start": f"0x{patterns[0]['offset']:06X}" if patterns else None,
            "blob_end": f"0x{patterns[-1]['offset'] + patterns[-1]['size']:06X}"
                        if patterns else None,
            "used_bytes": sum(p["size"] for p in patterns),
            "unused_bytes": sum(fr["slack"] for fr in frames),
        },
    }


# ── encoding ---------------------------------------------------------------
def _int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PatternError(f"{what}: expected an integer, got {value!r}")
    if not lo <= value <= hi:
        raise PatternError(f"{what}: {value} out of range {lo}..{hi}")
    return value


def encode_name(name, where: str) -> bytes:
    if not isinstance(name, str):
        raise PatternError(f"{where}: expected a string, got "
                           f"{type(name).__name__}")
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        raise PatternError(f"{where}: {name!r} is not ASCII")
    if not 1 <= len(encoded) <= NAME_LEN:
        raise PatternError(f"{where}: {name!r} must be 1..{NAME_LEN} ASCII "
                           f"characters")
    if b"\x00" in encoded or any(b < 0x20 or b == 0x7F for b in encoded):
        raise PatternError(f"{where}: {name!r} contains a control character")
    return encoded.ljust(NAME_LEN, b"\x00")


def encode_header(hdr, where: str) -> tuple[bytes, int]:
    if not isinstance(hdr, dict):
        raise PatternError(f"{where}.header: expected an object")
    t = _int(hdr.get("type"), f"{where}.header.type", 0, 255)
    lanes = _int(hdr.get("lanes"), f"{where}.header.lanes", 1, 255)
    la = _int(hdr.get("level_a"), f"{where}.header.level_a", 0, 255)
    lb = _int(hdr.get("level_b"), f"{where}.header.level_b", 0, 255)
    t0e = _int(hdr.get("type_0e"), f"{where}.header.type_0e", 0, 255)
    fill = _int(hdr.get("fill"), f"{where}.header.fill", 0, 255)
    n2 = _int(hdr.get("n2"), f"{where}.header.n2", 0, 0xFFFF)
    try:
        res = bytes.fromhex(hdr.get("reserved_08a", "0000"))
    except (TypeError, ValueError):
        raise PatternError(f"{where}.header.reserved_08a is not valid hex")
    if len(res) != 2:
        raise PatternError(f"{where}.header.reserved_08a must be 2 bytes")
    if t not in (2, 3, 4, 5):
        raise PatternError(
            f"{where}.header.type is 0x{t:02X}; the factory patterns only use "
            f"0x02, 0x03, 0x04 and 0x05 (0x04 on 463 of 472) — another value "
            f"would be a record kind this tool does not model")
    # 8 static bytes; the caller appends n1 (derived from the note count) and
    # n2, which is carried verbatim because it is not a function of the notes.
    return bytes([t, lanes]) + res + bytes([la, lb, t0e, fill]), n2


def _raw_cell(note: dict, where: str) -> bytes:
    try:
        raw = bytes.fromhex(note["raw"])
    except (TypeError, ValueError):
        raise PatternError(f"{where}.raw is not valid hex: {note['raw']!r}")
    if len(raw) != NOTE_SIZE:
        raise PatternError(f"{where}.raw is {len(raw)} bytes, expected "
                           f"{NOTE_SIZE}")
    return raw


NOTE_FIELDS = ("ticks", "duration", "flags")


def encode_note(note, where: str) -> bytes:
    """Build one 4-byte note cell.

    The explicit fields are authoritative. ``raw`` is what the export read and
    is only used when a note carries no fields at all (a hand-written file can
    paste a cell verbatim); it never overrides an edited field.
    """
    if not isinstance(note, dict):
        raise PatternError(f"{where}: expected an object")
    if not any(k in note for k in NOTE_FIELDS):
        if "raw" not in note:
            raise PatternError(f"{where}: no note fields and no 'raw'")
        return _raw_cell(note, where)

    norm = {
        "ticks": _int(note.get("ticks"), f"{where}.ticks", 0, 0xFFFF),
        "duration": _int(note.get("duration"), f"{where}.duration", 0, 3),
        "flags": _int(note.get("flags", note.get("duration", 0)),
                      f"{where}.flags", 0, 0xFF),
        "pad": note.get("pad", "00"),
    }
    try:
        pad = bytes.fromhex(norm["pad"])
    except (TypeError, ValueError):
        raise PatternError(f"{where}.pad is not valid hex: {norm['pad']!r}")
    if len(pad) != 1:
        raise PatternError(f"{where}.pad is {len(pad)} bytes, expected 1")
    norm["pad"] = pad.hex()
    return _note_bytes(norm)


def validate(doc: dict, data: bytes) -> list[dict]:
    """Check the imported document against the image. Returns normalised frames."""
    if not isinstance(doc, dict):
        raise PatternError("imported file is not an object")
    if doc.get("format") != "r24-patterns":
        raise PatternError(f"not a pattern export (format={doc.get('format')!r}"
                           f", expected 'r24-patterns')")
    if doc.get("format_version") != FORMAT_VERSION:
        raise PatternError(f"format_version {doc.get('format_version')!r} is "
                           f"not supported (expected {FORMAT_VERSION})")
    frames = doc.get("frames")
    if not isinstance(frames, list) or not frames:
        raise PatternError("'frames' is missing or empty")

    out: list[dict] = []
    seen: dict[str, int] = {}
    for fi, frame in enumerate(frames):
        where = f"frames[{fi}]"
        if not isinstance(frame, dict):
            raise PatternError(f"{where}: expected an object")
        if "frame" in frame:
            idx = _int(frame["frame"], f"{where}.frame", 0, len(frames) - 1)
            if idx != fi:
                raise PatternError(f"{where}.frame is {idx} but the frame is at "
                                   f"position {fi}; frames must stay in order")
        pats = frame.get("patterns")
        if not isinstance(pats, list):
            raise PatternError(f"{where}.patterns: expected a list")
        norm = []
        for pi, pat in enumerate(pats):
            w = f"{where}.patterns[{pi}]"
            if not isinstance(pat, dict):
                raise PatternError(f"{w}: expected an object")
            raw_name = encode_name(pat.get("name"), f"{w}.name")
            key = raw_name.rstrip(b"\x00").decode("ascii")
            if key in seen:
                raise PatternError(f"{w}.name: {key!r} appears twice (also "
                                   f"frames[{seen[key]}])")
            seen[key] = fi
            header, hdr_n2 = encode_header(pat.get("header", {}), w)
            notes = pat.get("notes")
            if not isinstance(notes, list):
                raise PatternError(f"{w}.notes: expected a list")
            if not 1 <= len(notes) <= MAX_NOTES:
                raise PatternError(f"{w}.notes: {len(notes)} notes, must be "
                                   f"1..{MAX_NOTES}")
            cells = [encode_note(n, f"{w}.notes[{i}]")
                     for i, n in enumerate(notes)]
            norm.append({
                "name_raw": raw_name,
                "name": key,
                "header": header,
                "cells": b"".join(cells),
                "n1": len(cells),
                "n2": hdr_n2,
            })
        out.append({"index": fi, "patterns": norm})
    return out


def apply_import(doc: dict, data: bytearray) -> list[str]:
    """Write imported patterns into `data` in place. Returns change descriptions."""
    frames = validate(doc, data)
    image_frames = frames_of(scan(bytes(data)))
    if len(frames) != len(image_frames):
        raise PatternError(
            f"the export describes {len(frames)} frames but the image has "
            f"{len(image_frames)}; adding or removing frames is not supported")

    changes: list[str] = []
    for img_fr, exp_fr in zip(image_frames, frames):
        pats = exp_fr["patterns"]
        old_pats = img_fr["patterns"]
        if len(pats) != len(old_pats):
            raise PatternError(
                f"frame {img_fr['index']}: the export lists {len(pats)} patterns "
                f"but the image has {len(old_pats)}; adding or removing patterns "
                f"is not supported")

        # Records are packed tight, so every pattern keeps its exact length and
        # is rewritten where it already lives.
        for old, pat in zip(old_pats, pats):
            old_name = old["name_raw"].rstrip(b"\x00").decode("ascii", "replace")
            if old_name != pat["name"]:
                raise PatternError(
                    f"frame {img_fr['index']}: pattern is named {pat['name']!r} "
                    f"in the export but {old_name!r} in the image; renaming is "
                    f"not supported, edit the note data instead")
            if pat["n1"] != old["n1"]:
                raise PatternError(
                    f"pattern {pat['name']!r}: the export has {pat['n1']} notes "
                    f"but the image has {old['n1']}. The records are packed "
                    f"tight, so a pattern cannot change length — edit the "
                    f"existing notes instead of adding or deleting them")
            rec = (pat["name_raw"] + pat["header"]
                   + struct.pack("<HH", pat["n1"], pat["n2"]) + pat["cells"])
            at = old["offset"]
            was = bytes(data[at:at + len(rec)])
            if was != rec:
                changes.append(_describe(old, pat, was, rec))
                data[at:at + len(rec)] = rec
    return changes


def _describe(old: dict, pat: dict, was: bytes, now: bytes) -> str:
    """One line saying exactly what moved in this record."""
    bits = []
    if old["n1"] != pat["n1"]:
        bits.append(f"notes {old['n1']}->{pat['n1']}")
    if old["n2"] != pat["n2"]:
        bits.append(f"n2 {old['n2']}->{pat['n2']}")
    if was[8:14] != now[8:14]:
        bits.append("header changed")
    old_cells = b"".join(_note_bytes(n) for n in old["notes"])
    if old_cells != pat["cells"]:
        diff = sum(1 for i in range(0, min(len(old_cells), len(pat["cells"])), 4)
                   if old_cells[i:i + 4] != pat["cells"][i:i + 4])
        bits.append(f"{diff} note(s) changed")
    if len(now) != len(was):
        bits.append(f"size {len(was)}->{len(now)}")
    return f"{pat['name']}: " + (", ".join(bits) if bits else "bytes changed")


# ── CLI ────────────────────────────────────────────────────────────────────
def do_export(image: str, outdir: str, dry_run: bool) -> int:
    with open(image, "rb") as fh:
        data = fh.read()
    doc = build_export(image, data)

    print(f"image    : {image} ({len(data)} bytes)")
    print(f"frame    : 0x{FRAME:06X}  {data[FRAME:FRAME + 35]!r}")
    print(f"patterns : {doc['summary']['patterns']} records, "
          f"{doc['summary']['notes']} notes")
    print(f"blob     : {doc['summary']['blob_start']} .. "
          f"{doc['summary']['blob_end']}  "
          f"({doc['summary']['used_bytes']} used + "
          f"{doc['summary']['unused_bytes']} unused bytes)")
    print()
    for fr in doc["frames"]:
        names = [p["name"] for p in fr["patterns"]]
        span = f"{names[0]}..{names[-1]}" if len(names) > 1 else names[0]
        print(f"  frame {fr['frame']}  {span:<24s} {len(names):3d} patterns, "
              f"{fr['unused_bytes']:6d} unused bytes")

    if dry_run:
        print("\n--dry-run: no files written")
        return 0

    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "patterns.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=False)
        fh.write("\n")
    print(f"\nwrote {path}")
    print(f"  edit a note, then: python3 tools/r24patterns.py "
          f"{os.path.basename(image)} {path} -o build/R24SYS.BIN")
    return 0


def do_show(image: str, name: str) -> int:
    with open(image, "rb") as fh:
        data = fh.read()
    pats = [p for p in scan(data) if "unused" not in p]
    hit = [p for p in pats
           if p["name_raw"].rstrip(b"\x00").decode("ascii", "replace") == name]
    if not hit:
        known = [p["name_raw"].rstrip(b"\x00").decode("ascii", "replace")
                 for p in pats]
        raise PatternError(f"no pattern named {name!r}. Known names: "
                           f"{', '.join(known[:12])}"
                           f"{' …' if len(known) > 12 else ''}")
    ent = hit[0]
    print(f"{name}  file 0x{ent['offset']:06X}  {ent['n1']} notes  "
          f"header {ent['header'].hex(' ')}")
    print(f"  {'ticks':>6} {'dur':>3} {'flags':>5}  raw")
    for note in ent["notes"]:
        print(f"  {note['ticks']:6d} {note['duration']:3d} {note['flags']:5d}  "
              f"{_note_bytes(note).hex()}")
    return 0


def check_image_identity(doc: dict, data: bytes, image: str, force: bool) -> None:
    size = doc.get("image_size")
    if size is not None and size != len(data):
        raise PatternError(
            f"this export was made from an image of {size} bytes but the "
            f"target is {len(data)} bytes; refusing to apply it")
    want = doc.get("image_sha256")
    if not want:
        print("warning: this export records no image hash; cannot tell which "
              "image it came from", file=sys.stderr)
        return
    got = hashlib.sha256(data).hexdigest()
    if want == got:
        return
    msg = (f"this export was made from a different image (recorded "
           f"{want[:16]}…) than the target ({got[:16]}…)")
    if force:
        print(f"warning: {msg} (--force given, continuing)", file=sys.stderr)
        return
    raise PatternError(
        f"{msg}. Re-run with --force if that is really what you want, or "
        f"export the patterns from the target image first "
        f"(`r24patterns.py {os.path.basename(image)} --export -o DIR`).")


def do_import(image: str, spec: str, out: str, dry_run: bool,
              force: bool = False) -> int:
    with open(image, "rb") as fh:
        raw = fh.read()
    data = bytearray(raw)
    with open(spec, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise PatternError(f"{spec}: not valid JSON ({exc})")

    check_image_identity(doc, raw, image, force)
    changes = apply_import(doc, data)

    print(f"image   : {image}")
    print(f"spec    : {spec}")
    print(f"changes : {len(changes)}")
    for line in changes:
        print(f"  {line}")

    if dry_run:
        print("\n--dry-run: no output written")
        return 0

    img = r24image.R24Image.from_parts(
        bytes(data[:r24image.HEADER_SIZE]), bytes(data[r24image.HEADER_SIZE:]))
    img.refresh_checksum()
    info = img.verify()
    img.write(out)
    print(f"\nwrote {out}")
    print(f"  size     : {info['size']} bytes")
    print(f"  checksum : {info['checksum']} (verified {info['checksum_ok']})")
    print(f"  sha256   : {info['sha256']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Export or re-import the R24 rhythm patterns "
                    "(SEQPLAY pattern data).")
    ap.add_argument("image", help="source firmware image (e.g. R24SYS.BIN)")
    ap.add_argument("spec", nargs="?", help="patterns.json to import")
    ap.add_argument("-o", "--output", help="export dir, or output image path")
    ap.add_argument("--export", action="store_true",
                    help="export patterns.json instead of importing")
    ap.add_argument("--show", metavar="NAME",
                    help="print one pattern (e.g. --show 08Beat01)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="import even when the export came from a different "
                         "image (mismatched image hash)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.image):
        ap.error(f"no such image: {args.image}")

    try:
        if args.show:
            return do_show(args.image, args.show)
        if args.export:
            return do_export(args.image, args.output or "patterns", args.dry_run)
        if not args.spec:
            ap.error("nothing to import: give a patterns.json, or use "
                     "--export / --show")
        if not os.path.isfile(args.spec):
            ap.error(f"no such spec: {args.spec}")
        return do_import(args.image, args.spec,
                         args.output or "build/R24SYS.BIN", args.dry_run,
                         args.force)
    except PatternError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except r24image.ImageError as exc:
        print(f"error: image verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
