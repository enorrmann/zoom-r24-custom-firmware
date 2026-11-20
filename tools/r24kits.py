#!/usr/bin/env python3
"""r24kits.py — export / re-import the R24 drum kit table as an editable file.

WHY THIS EXISTS
---------------
`patch_samples.py` can replace the *audio* of every sample-table entry,
and `build_x0x_firmware.py` rewrites kit descriptors — but only with a set of
kits hardcoded in that script. There was no way to say "put sample #12 in the
kick slot of the ROCK kit and pan it left" without writing Python.

This tool closes that gap. It exports the drum kit table and the master sample
table to a plain, human-editable file, and re-imports that file back into the
image — selecting which sample sits in each of the 16 slots of each of the 10
kits, and the parameters each slot plays with (level, pan, mute group).

THE DATA IT EDITS
-----------------
    master sample table   factory: file 0x000E20EC, 73 x <II>
                          relocated: end of the bank, any count (r24bank.py)
    drum kit table        file 0x000E2334 .. 0x000E28D4   10 x 144 bytes
    PCM bank              file 0x00220040                 (never touched)

A kit is 144 bytes:

    0x00 .. 0x0F   16-byte name field; 15 chars + NUL, or 16 chars with no NUL
    0x10 .. 0x8F   16 slots x 8 bytes: <H sample_id, B level, B pan, B group,
                                     3 x reserved (always 0x00)>

A slot descriptor is `<HBBB3s`; the three reserved bytes are preserved
verbatim, never invented.

WHAT ROUND-TRIPS EXACTLY
-------------------------
`test_r24kits.py` asserts a byte-identical repack for R24SYS.BIN. The export
carries everything the writer needs, so nothing is guessed:

  * a kit name is a single `name` string. The 16-byte field is written as
    `<name>` space-padded to 15 chars, then a NUL — which is exactly what every
    factory kit stores. One known exception: the X0X kits fill all 16 bytes
    (`TR-606` + 10 spaces, **no NUL**), which a name cannot express, so those
    three fields normalise to 15 chars + NUL — 3 name bytes plus the 2 checksum
    bytes that follow. Nothing else changes; see docs/08_drum_kits.md §7.4;
  * slot reserved bytes are exported per slot (`reserved`), so padding is
    preserved rather than invented;
  * the sample table's rel_start/length pairs are exported for reference and
    re-checked on import (they are edited by patch_samples.py — see below).

On import a value is only written when it would change something, so an
untouched export re-imports to a file that is identical except for the header
checksum (which `refresh_checksum()` recomputes) — and, on the X0X image only,
those three name bytes.

An export records the sha256 of the image it came from. Importing it into a
different build is refused unless `--force` is given: kits are portable in
principle, but applying the X0X export to the stock image silently rewrites all
160 slots, so it is a decision the user has to make explicitly.

WHY THIS TOOL DOES NOT EDIT THE SAMPLE TABLE
--------------------------------------------
The firmware reads rel_start/length from the table at playback time, so the
table *can* change — but only together with the audio it describes. That is
`patch_samples.py --repack`'s job (it moves the audio and rewrites the table in
one step). Here the table is reference data: an import whose sample rows
differ from the image is refused, so a stale kits.json cannot silently point
kits at the wrong audio. Slot sample ids may be reassigned within 0..count-1,
where count is the table size the image's firmware uses (73 factory, 160 after
`r24bank.py relocate --per-kit`).

USAGE
-----
    # export the factory kits to JSON and re-import them
    python3 tools/r24kits.py R24SYS.BIN --export -o build/kits
    python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/repack.BIN

    # edit build/kits/kits.json, then import it
    python3 tools/r24kits.py R24SYS.BIN build/kits/kits.json -o build/R24SYS.BIN

    # inspect without writing anything
    python3 tools/r24kits.py R24SYS.BIN --export -o build/kits --dry-run

`--export` writes one file, `kits.json`: the whole kit table plus the master
sample table, with a `notes` block explaining every field. Edit a number in a
slot and import it back.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── geometry (file offsets) ────────────────────────────────────────────────
BANK_BASE = r24bank.BANK_BASE

KIT_SIZE = 144                    # table location and count: kit_geometry()
SLOTS = 16
NAME_LEN = 16
SLOT_FMT = "<HBBB3s"
SLOT_SIZE = struct.calcsize(SLOT_FMT)      # 8

# value ranges, taken from the factory data and docs/08_drum_kits.md §3.2
LEVEL_MIN, LEVEL_MAX = 0, 15
PAN_MIN, PAN_MAX = 0, 127
GROUP_MAX = 255

FORMAT_VERSION = 1


class KitError(Exception):
    """Raised for anything the tool refuses to guess at."""


# ── reading ────────────────────────────────────────────────────────────────
def sample_layout(data: bytes) -> r24bank.Layout:
    try:
        return r24bank.locate(data)
    except r24bank.BankError as exc:
        raise KitError(str(exc))


def kit_geometry(data: bytes) -> tuple[int, int]:
    """(file offset, count) of the kit table the firmware reads (r24bank)."""
    try:
        return r24bank.locate_kits(data)
    except r24bank.BankError as exc:
        raise KitError(str(exc))


def read_sample_table(data: bytes) -> list[list[int]]:
    return [list(e) for e in r24bank.read_table(data, sample_layout(data))]


def decode_name(raw: bytes) -> str:
    """Read the 16-byte name field back as a name.

    The factory encoding is 15 characters, space-padded, then a NUL. The X0X
    kits instead fill all 16 bytes (6 chars + 10 spaces, no NUL) — both forms
    decode to the same name, which is why one text field is enough.
    """
    return raw.split(b"\x00")[0].decode("ascii", "replace").rstrip(" ")


def encode_name(name: str, where: str = "name") -> bytes:
    """Encode a name into the 16-byte field: 15 chars space-padded, then NUL.

    Every validation lives here so there is exactly one rule:
      * ASCII only — the field is read by firmware that has no notion of UTF-8;
      * 1..15 characters. 16 is refused because the NUL terminator is the only
        thing separating the name from the padding, and a full 16-byte field
        (the X0X kits) cannot be produced from a name without ambiguity;
      * no NUL, no control characters, no trailing spaces — they would encode
        identically to a shorter name and read back differently, so accepting
        them would make an edit silently change nothing.
    """
    if not isinstance(name, str):
        raise KitError(f"{where}: expected a string, got {type(name).__name__}")
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        raise KitError(f"{where}: {name!r} is not ASCII; the 16-byte name "
                       f"field holds ASCII only")
    if not encoded:
        raise KitError(f"{where}: name is empty; give 1..{NAME_LEN - 1} "
                       f"ASCII characters")
    if len(encoded) > NAME_LEN - 1:
        raise KitError(
            f"{where}: {name!r} is {len(encoded)} chars, max {NAME_LEN - 1} "
            f"(the field is {NAME_LEN} bytes: {NAME_LEN - 1} chars + NUL)")
    if b"\x00" in encoded:
        raise KitError(f"{where}: name contains a NUL byte")
    if any(b < 0x20 or b == 0x7F for b in encoded):
        raise KitError(f"{where}: {name!r} contains a control character")
    if name != name.rstrip(" "):
        raise KitError(
            f"{where}: {name!r} ends with a space. Trailing spaces are the "
            f"field's padding, so they would be stripped on read and the "
            f"edit would silently change nothing")
    return encoded.ljust(NAME_LEN - 1, b" ") + b"\x00"


def read_slots(data: bytes, off: int) -> list[dict]:
    slots = []
    for s in range(SLOTS):
        sid, level, pan, group, reserved = struct.unpack_from(
            SLOT_FMT, data, off + 16 + s * SLOT_SIZE)
        slots.append({
            "sample_id": sid,
            "level": level,
            "pan": pan,
            "group": group,
            "reserved": reserved.hex(),
        })
    return slots


def read_kits(data: bytes) -> list[dict]:
    kits = []
    kbase, nkits = kit_geometry(data)
    for k in range(nkits):
        off = kbase + k * KIT_SIZE
        kits.append({
            "index": k,
            "name": decode_name(data[off:off + NAME_LEN]),
            "offset": off,
            "slots": read_slots(data, off),
        })
    return kits


def build_export(image: str, data: bytes) -> dict:
    lay = sample_layout(data)
    return {
        "format": "r24-kits",
        "format_version": FORMAT_VERSION,
        "image": os.path.basename(image),
        "image_size": len(data),
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "geometry": {
            "kit_table": kit_geometry(data)[0],
            "kit_size": KIT_SIZE,
            "num_kits": kit_geometry(data)[1],
            "slots_per_kit": SLOTS,
            "sample_table": lay.table_off,
            "sample_table_layout": "relocated" if lay.relocated else "factory",
            "num_samples": lay.count,
            "bank_base": BANK_BASE,
        },
        "notes": [
            "HOW TO EDIT: change 'sample_id', 'level', 'pan' or 'group' in any",
            "slot, save, then import this file back:",
            "  python3 tools/r24kits.py R24SYS.BIN kits.json -o build/R24SYS.BIN",
            "Every slot must be present (16 per kit) and every kit (10).",
            "",
            "name       : 1..15 ASCII characters, unique per kit. The 16-byte",
            "             field is written as your name, space-padded to 15",
            "             chars, then a NUL. No trailing spaces (they are the",
            "             padding and would be stripped on read).",
            f"sample_id  : 0..{lay.count - 1}, entry in the master sample table ('samples',"
            "             below) — this is how you choose which sample a slot plays",
            "level      : 0..15 attenuation/gain nibble",
            "pan        : 0..127, 64 = centre",
            "group      : mute/exclusive choke group, 0 = none",
            "reserved   : the 3 padding bytes, preserved verbatim — leave alone",
            "offset     : file offset of this record in the image (read-only)",
            "samples    : the master sample table, for reference. Change audio,",
            "             positions or lengths with patch_samples.py --repack; an",
            "             import whose rows differ from the image is refused.",
        ],
        "samples": [{"id": i, "rel_start": rel, "length": length,
                     "offset": BANK_BASE + rel,
                     "used_by": []}
                    for i, (rel, length) in enumerate(read_sample_table(data))],
        "kits": read_kits(data),
    }


def _mark_sample_usage(doc: dict) -> None:
    """Fill in used_by so the export documents itself."""
    for kit in doc["kits"]:
        for sid in sorted({s["sample_id"] for s in kit["slots"]}):
            if 0 <= sid < len(doc["samples"]):
                doc["samples"][sid]["used_by"].append(kit["name"])


# ── validation ─────────────────────────────────────────────────────────────
def check_image_identity(doc: dict, data: bytes, image: str,
                         force: bool) -> None:
    """Warn (or refuse) when the export was made from a different image.

    An export records the sha256 (and size) of the image it came from.
    Importing it into a *different* build is legitimate — kits are portable
    between images whose sample tables agree — but it is also the shape of a
    mistake: the X0X export has a different kit layout, so applying it to the
    stock image silently rewrites all 160 slots.

    The size check runs first and independently of the hash, so deleting
    `image_sha256` cannot be used to slip past it. A missing hash alone only
    warns (a hand-written kit file has no reason to carry one).
    """
    size = doc.get("image_size")
    if size is not None and size != len(data):
        raise KitError(
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
           f"{want[:16]}…, target {got[:16]}…); kit slots will be overwritten "
           f"with values meant for another build")
    if force:
        print(f"warning: {msg} (--force given, continuing)", file=sys.stderr)
        return
    raise KitError(
        f"{msg}. Re-run with --force if that is really what you want, or "
        f"export the kits from the target image first "
        f"(`r24kits.py {os.path.basename(image)} --export -o DIR`).")


def _require_int(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KitError(f"{what}: expected an integer, got {value!r}")
    if not lo <= value <= hi:
        raise KitError(f"{what}: {value} out of range {lo}..{hi}")
    return value


def validate(doc: dict, data: bytes) -> list[dict]:
    """Check the imported document against the image. Returns normalised kits."""
    if not isinstance(doc, dict):
        raise KitError("imported file is not an object")
    if doc.get("format") != "r24-kits":
        raise KitError(f"not a kit export (format={doc.get('format')!r}, "
                       f"expected 'r24-kits')")
    version = doc.get("format_version")
    if version != FORMAT_VERSION:
        raise KitError(
            f"format_version {version!r} is not supported (expected "
            f"{FORMAT_VERSION})")

    # The sample table is carried for reference only: re-check every row
    # against the table the image's firmware actually reads (factory or
    # relocated) before writing anything.
    image_table = read_sample_table(data)
    num_samples = len(image_table)
    samples = doc.get("samples")
    if samples is not None:
        if not isinstance(samples, list) or len(samples) != num_samples:
            raise KitError(
                f"sample table has "
                f"{len(samples) if isinstance(samples, list) else '?'} rows, "
                f"expected {num_samples}; delete the whole block (or export "
                f"again) rather than trimming it, so the geometry check "
                f"cannot be switched off by accident")
        for i, row in enumerate(samples):
            rel = _require_int(row.get("rel_start"), f"samples[{i}].rel_start",
                               0, len(data) - BANK_BASE)
            length = _require_int(row.get("length"), f"samples[{i}].length",
                                  0, len(data) - BANK_BASE)
            if (rel, length) != tuple(image_table[i]):
                raise KitError(
                    f"samples[{i}] geometry differs from the image "
                    f"(import {rel}/{length}, image {image_table[i][0]}/"
                    f"{image_table[i][1]}): this file describes a different "
                    f"sample table. Change sample geometry with "
                    f"patch_samples.py --repack, then export again")

    kits = doc.get("kits")
    kbase, nkits = kit_geometry(data)
    if not isinstance(kits, list) or len(kits) != nkits:
        raise KitError(f"expected {nkits} kits, got "
                       f"{len(kits) if isinstance(kits, list) else '?'}")

    out: list[dict] = []
    seen: dict[int, int] = {}
    for pos, kit in enumerate(kits):
        if not isinstance(kit, dict):
            raise KitError(f"kits[{pos}]: expected an object, got "
                           f"{type(kit).__name__}")
        if "index" not in kit:
            raise KitError(f"kits[{pos}]: missing 'index'")
        idx = _require_int(kit["index"], f"kits[{pos}].index",
                           0, nkits - 1)
        if idx in seen:
            raise KitError(f"kit index {idx} appears twice "
                           f"(positions {seen[idx]} and {pos})")
        seen[idx] = pos

        if "name_hex" in kit:
            raise KitError(
                f"kits[{pos}]: 'name_hex' is no longer supported — set the kit "
                f"name with the 'name' field alone (1..{NAME_LEN - 1} ASCII "
                f"characters). Delete 'name_hex' from the file")
        raw = encode_name(kit.get("name", ""), f"kits[{pos}].name")

        slots = kit.get("slots")
        if not isinstance(slots, list) or len(slots) != SLOTS:
            raise KitError(f"kits[{pos}].slots: expected {SLOTS} slots, got "
                           f"{len(slots) if isinstance(slots, list) else '?'}")
        norm_slots = []
        for s, slot in enumerate(slots):
            where = f"kits[{pos}].slots[{s}]"
            reserved = slot.get("reserved", "000000")
            try:
                res = bytes.fromhex(reserved)
            except (ValueError, TypeError):
                raise KitError(f"{where}.reserved is not valid hex: {reserved!r}")
            if len(res) != 3:
                raise KitError(f"{where}.reserved is {len(res)} bytes, "
                               f"expected 3")
            norm_slots.append({
                "sample_id": _require_int(slot.get("sample_id"),
                                          f"{where}.sample_id", 0,
                                          num_samples - 1),
                "level": _require_int(slot.get("level"), f"{where}.level",
                                      LEVEL_MIN, LEVEL_MAX),
                "pan": _require_int(slot.get("pan"), f"{where}.pan",
                                    PAN_MIN, PAN_MAX),
                "group": _require_int(slot.get("group"), f"{where}.group",
                                      0, GROUP_MAX),
                "reserved": res,
            })
        out.append({"index": idx, "name_raw": raw, "slots": norm_slots})

    out.sort(key=lambda k: k["index"])
    # Duplicate names make the kit list ambiguous on the device UI and in the
    # exported file. An image that already shipped that way must still
    # round-trip (the device accepts it), so a duplicate that is *not being
    # introduced by this import* only warns; creating one is an error.
    # A duplicate is only tolerated when the image itself already has those
    # kits sharing that name. An image the device accepted must stay
    # round-trippable; *creating* a duplicate is what we refuse.
    by_name: dict[str, list[dict]] = {}
    for kit in out:
        by_name.setdefault(decode_name(kit["name_raw"]).casefold(),
                           []).append(kit)
    for key, kits in by_name.items():
        if len(kits) < 2:
            continue
        already = sum(
            1 for kit in kits
            if decode_name(data[kbase + kit["index"] * KIT_SIZE:
                               kbase + kit["index"] * KIT_SIZE + NAME_LEN]
                           ).casefold() == key)
        idxs = [k["index"] for k in kits]
        shown = decode_name(kits[0]["name_raw"])
        if already >= 2:
            print(f"warning: kit name {shown!r} is shared by kits {idxs} in "
                  f"this image already; leaving it alone (rename one of them "
                  f"to disambiguate)", file=sys.stderr)
        else:
            raise KitError(f"kit name {shown!r} would be used by kits {idxs}; "
                           f"kit names must be unique")
    return out


# ── writing ────────────────────────────────────────────────────────────────
def apply_import(doc: dict, data: bytearray) -> list[str]:
    """Write validated kits into `data`. Returns a list of change descriptions."""
    kits = validate(doc, data)
    changes: list[str] = []
    for kit in kits:
        off = kit_geometry(data)[0] + kit["index"] * KIT_SIZE
        if data[off:off + NAME_LEN] != kit["name_raw"]:
            changes.append(f"kit {kit['index']}: name "
                           f"{data[off:off + NAME_LEN]!r} -> {kit['name_raw']!r}")
            data[off:off + NAME_LEN] = kit["name_raw"]
        for s, slot in enumerate(kit["slots"]):
            so = off + 16 + s * SLOT_SIZE
            new = struct.pack(SLOT_FMT, slot["sample_id"], slot["level"],
                              slot["pan"], slot["group"], slot["reserved"])
            if data[so:so + SLOT_SIZE] != new:
                old = struct.unpack_from(SLOT_FMT, data, so)
                changes.append(
                    f"kit {kit['index']} slot {s:02d}: "
                    f"id {old[0]}->{slot['sample_id']} "
                    f"lvl {old[1]}->{slot['level']} "
                    f"pan {old[2]}->{slot['pan']} "
                    f"grp {old[3]}->{slot['group']}")
                data[so:so + SLOT_SIZE] = new
    return changes


# ── CLI ────────────────────────────────────────────────────────────────────
def do_export(image: str, outdir: str, dry_run: bool) -> int:
    with open(image, "rb") as fh:
        data = fh.read()
    doc = build_export(image, data)
    _mark_sample_usage(doc)

    print(f"image   : {image} ({len(data)} bytes)")
    print(f"kits    : {len(doc['kits'])} x {KIT_SIZE} bytes "
          f"@ 0x{doc['geometry']['kit_table']:08X}")
    print(f"samples : {len(doc['samples'])} entries @ "
          f"0x{doc['geometry']['sample_table']:08X} "
          f"({doc['geometry']['sample_table_layout']})")
    print()
    for kit in doc["kits"]:
        ids = " ".join(f"{s['sample_id']:2d}" for s in kit["slots"])
        print(f"  kit {kit['index']} {kit['name']:<10s} [{ids}]")

    if dry_run:
        print("\n--dry-run: no files written")
        return 0

    os.makedirs(outdir, exist_ok=True)
    json_path = os.path.join(outdir, "kits.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print(f"\nwrote {json_path}")
    print(f"  edit a slot's sample_id / level / pan / group, then import it "
          f"back with:")
    print(f"  python3 tools/r24kits.py {os.path.basename(image)} "
          f"{json_path} -o build/R24SYS.BIN")
    return 0


def do_import(image: str, spec: str, out: str, dry_run: bool,
              force: bool = False) -> int:
    with open(image, "rb") as fh:
        raw = fh.read()
    data = bytearray(raw)
    with open(spec, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise KitError(f"{spec}: not valid JSON ({exc})")

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

    # Re-seal: the kit table lives in the payload, so the header checksum moves
    # with it. verify() re-derives everything before the file reaches disk.
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
        description="Export or re-import the R24 drum kit table.")
    ap.add_argument("image", help="source firmware image (e.g. R24SYS.BIN)")
    ap.add_argument("spec", nargs="?",
                    help="kits.json to import; omit with --export")
    ap.add_argument("-o", "--output", help="export dir, or output image path")
    ap.add_argument("--export", action="store_true",
                    help="export kits.json instead of importing")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="import even when the export came from a different "
                         "image (mismatched image hash)")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.image):
        ap.error(f"no such image: {args.image}")

    try:
        if args.export:
            return do_export(args.image, args.output or "kits", args.dry_run)
        if not args.spec:
            ap.error("nothing to import: give a kits.json, or use --export")
        if not os.path.isfile(args.spec):
            ap.error(f"no such spec: {args.spec}")
        return do_import(args.image, args.spec,
                         args.output or "build/R24SYS.BIN", args.dry_run,
                         args.force)
    except KitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except r24image.ImageError as exc:
        # A failed re-seal must never look like a success or a traceback.
        print(f"error: image verification failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
