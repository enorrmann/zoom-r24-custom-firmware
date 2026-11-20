#!/usr/bin/env python3
"""loop_import.py — path-string patches for TAKE SELECT > LOOP[USB].

!! These builds do NOT give a working SD import. !!
------------------------------------------------------
Corrected analysis: docs/10_loop_import.md (rewritten 2026-11-18). The
LOOP[USB] callback (RAM 0x800425BA) switches the USB port to host mode
(0x8005B066), waits for a USB device (0x80026EFC) and copies the chosen file
into the project's AUDIO folder on the SD card. Changing only the path string,
as this tool does, still runs the USB host mode and the USB wait — the USB LED
lights and nothing is listed, which is exactly what the device showed.

The working change is described in docs/10 §4 (three call sites plus the path).
This tool is kept to inspect the literals (`--list-sites`) and because its
tests document the string layout:

    file 0x42f68  "B:\\ZOOM_R24\\LOOP" used by the LOOP[USB] callback (3 adr)
    file 0x281b4  "B:\\ZOOM_R24\\LOOP" used by USB > STORAGE > INIT (1 adr)
    file 0x43430  "LOOP[USB]" — a string in the literal pool of the TAKE SELECT
                  builder (RAM 0x80042C1A), loaded with `adr`; it is not a
                  record of a menu table (the menu is built in RAM at boot).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24image
from r24image import R24Image

# ────────────────────────────────────────────────────────────────────────────
# The fixed addresses.  These are file offsets in R24SYS.BIN (v1.13); the
# builder verifies the expected bytes before it writes, so they are safe to
# carry as constants (a different build fails loudly instead of silently).
# ────────────────────────────────────────────────────────────────────────────
LOOP_STR_LEN = 16
LOOP_STR_LIST = 0x042F68        # listing / stat / recursive merge
LOOP_STR_MKDIR = 0x0281B4       # "Create Folder?" gate
MENU_LABEL = 0x043430           # packed 6-byte-record table, entry 3
MENU_LABEL_LEN = 9              # "LOOP[USB]" + NUL -- must keep this length

# 15 characters + the NUL that already lives at +15.  The two factory
# spellings are exactly the slot's 16 bytes; the two root forms are truncated
# at the drive root and zero-filled, which the C string readers treat as the
# same path (docs/10_loop_import.md 5.3).
USB_LOOP = b"B:\\ZOOM_R24\\LOOP"
SD_LOOP = b"A:\\ZOOM_R24\\LOOP"
USB_ROOT = b"B:\\"
SD_ROOT = b"A:\\"
KNOWN_PATHS = (USB_LOOP, SD_LOOP, USB_ROOT, SD_ROOT)

# Other strings of the same family, listed so the report can show what was
# deliberately left alone.
RELATED = [
    (0x02759C, b"CARD READER"),
    (0x0275CC, b"USB Setup Error"),
    (0x0275FC, b"USB Stop Error"),
    (0x02760C, b"B:\\ZOOM_R24\\0"),          # pool tail of the mount strings
    (0x027614, b"B:\\ZOOM_R24"),
    (0x027620, b"B:\\ZOOM_R24\\PROJECT"),
    (0x027634, b"B:\\ZOOM_R24\\AUDIO"),
    (0x028180, b"Create Folder?"),
    (0x028198, b"Creating..."),
]

# String helpers found in the loop-import path (docs/10_loop_import.md §3.6).
HELPERS = [
    (0x8001E15C, "path exists?                   (Create Folder? gate)"),
    (0x80027446, "check B:\\ZOOM_R24 + \\PROJECT"),
    (0x8002746C, "check B:\\ZOOM_R24 + \\AUDIO"),
    (0x800274B0, "wait for the USB device"),
    (0x80042978, "count '\\' in a path, trim to last separator"),
    (0x800429FA, "same trim, string form"),
    (0x8005553E, "open/stat a directory -> handle"),
    (0x800BCB4C, "strcpy"),
    (0x8003A770, "dialog(title, message)"),
    (0x8003DF8E, "close/refresh dialog"),
    (0x80020C20, "mark sampler slot dirty"),
    (0x80020C84, "select / refresh sampler bank"),
]


class LoopImportError(Exception):
    pass


def _head(raw: bytes) -> bytes:
    """The C string inside a fixed-width NUL-padded slot."""
    return raw.split(b"\x00", 1)[0]


# ────────────────────────────────────────────────────────────────────────────
# Catalog: re-derive the reference sites from the image itself.
# ────────────────────────────────────────────────────────────────────────────
def _adr_thumb(hw: int) -> tuple[int, int]:
    """Decode `adr rd, #imm` -> (rd, imm).  Caller checks the opcode range."""
    return (hw >> 8) & 7, (hw & 0xFF) * 4


def find_string_refs(img: R24Image) -> list[dict]:
    """Every Thumb `adr`/`ldr`-pool reference to the two loop-path literals."""
    import struct

    data = img.data
    base = 0x80000000                       # runtime VA of file offset 0 is not
    lo, hi = r24image.HEADER_SIZE, len(data) - 4   # used: everything is relative
    hits = []
    for off in range(lo, hi, 2):
        hw = struct.unpack_from("<H", data, off)[0]
        if 0xA000 <= hw <= 0xAFFF:                       # adr rd, #imm8*4
            rd, imm = _adr_thumb(hw)
            target = ((off - 0x40 + 0x02080000) + 4) & ~3
            va = (target & ~0x80000000) + imm
            # recompute in VA space, then map back to a file offset
            va = target + imm
            fo = va - 0x02080000 + 0x40
            kind = f"adr r{rd}"
        elif 0x4800 <= hw <= 0x4FFF:                     # ldr rd, [pc, #imm8*4]
            rd, imm = _adr_thumb(hw)
            pc = ((off - 0x40 + 0x02080000) + 4) & ~3
            pool = pc + imm - 0x02080000 + 0x40
            if not (0 <= pool <= len(data) - 4):
                continue
            va = struct.unpack_from("<I", data, pool)[0]
            if not (0x80000000 <= va < 0x81000000):
                continue
            fo = va - 0x80000000
            kind = f"ldr r{rd}, [pool 0x{pool:06x}]"
        else:
            continue
        if fo in (LOOP_STR_LIST, LOOP_STR_MKDIR):
            hits.append({"site_file": off, "site_va": 0x80000000 + off,
                         "kind": kind, "target_file": fo})
    hits.sort(key=lambda h: (h["target_file"], h["site_file"]))
    return hits


def catalog(img: R24Image) -> dict:
    """Summarise everything the patch set depends on, for the report."""
    d = img.data
    return {
        "loop_list": d[LOOP_STR_LIST:LOOP_STR_LIST + LOOP_STR_LEN],
        "loop_mkdir": d[LOOP_STR_MKDIR:LOOP_STR_MKDIR + LOOP_STR_LEN],
        "menu_label": d[MENU_LABEL:MENU_LABEL + MENU_LABEL_LEN],
        "refs": find_string_refs(img),
        "related": [(o, d[o:o + len(b)].split(b"\x00")[0]) for o, b in RELATED],
    }


# ────────────────────────────────────────────────────────────────────────────
# Patches
# ────────────────────────────────────────────────────────────────────────────
def _fit_string(text: bytes, length: int = LOOP_STR_LEN) -> bytes:
    """NUL-terminate and zero-fill to exactly `length` bytes.

    Trailing NULs are the point, not a shortcut: the path is only ever read by
    C string functions, so `B:\\` + NULs *is* the root path, and the 13 spare
    bytes stay in place where the `Create Folder?` pool already uses them as
    scratch (docs/10_loop_import.md §5).
    """
    if len(text) < length:
        text = text + b"\x00"        # the slot's own terminator
    if len(text) > length:
        raise LoopImportError(
            f"{text!r} does not fit in {length} bytes (the slot is fixed)")
    return text.ljust(length, b"\x00")


def _fit_label(text: bytes, length: int = MENU_LABEL_LEN) -> bytes:
    """Pad a menu label to exactly 9 bytes: 8 characters + NUL."""
    if not text:
        raise LoopImportError("label must not be empty")
    if len(text) > length - 1:
        raise LoopImportError(
            f"label {text!r} is {len(text)} chars; the record holds {length - 1}")
    return text.ljust(length - 1, b" ") + b"\x00"


def apply(img: R24Image, *, usb_root: bool = False, sd_import: bool = False,
          label: bytes | None = None, verbose: bool = False) -> list[str]:
    """Apply the requested media switch to `img` in place. Returns the log."""
    if usb_root and sd_import:
        raise LoopImportError("--usb-root and --sd-import are mutually exclusive")

    cur_list = bytes(img.data[LOOP_STR_LIST:LOOP_STR_LIST + LOOP_STR_LEN])
    cur_mkdir = bytes(img.data[LOOP_STR_MKDIR:LOOP_STR_MKDIR + LOOP_STR_LEN])

    if usb_root:
        want, what = USB_ROOT, "USB root (B:\\)"
    elif sd_import:
        want, what = SD_ROOT, "SD card root (A:\\)"
    else:
        want, what = None, None

    log: list[str] = []
    if want is not None:
        new = _fit_string(want)
        changed = False
        for off, cur, name in (
            (LOOP_STR_LIST, cur_list, "listing / stat / merge"),
            (LOOP_STR_MKDIR, cur_mkdir, "Create Folder? gate"),
        ):
            if cur == new:
                log.append(f"  = 0x{off:06x}  {name}: already {want!r}")
                continue
            # both slots are any of the known spellings; refuse anything else
            if cur.split(b"\x00")[0] not in KNOWN_PATHS:
                raise LoopImportError(
                    f"0x{off:06x} holds {cur!r}, which is not a known loop path; "
                    "this is not the v1.13 image the patch set was derived from")
            img.patch(off, cur, new)
            changed = True
            log.append(f"  -> 0x{off:06x}  {name}: {_head(cur)!r} -> "
                       f"{_head(want)!r}")
        log.insert(0, f"loop source -> {what}" + ("" if changed else " (no change)"))

    if label is not None:
        cur = bytes(img.data[MENU_LABEL:MENU_LABEL + MENU_LABEL_LEN])
        new = _fit_label(label)
        if not cur.startswith(b"LOOP["):
            raise LoopImportError(
                f"0x{MENU_LABEL:06x} holds {cur!r}, which is not the loop menu "
                "entry of the v1.13 image")
        if cur == new:
            log.append(f"  = 0x{MENU_LABEL:06x}  menu label: already {label!r}")
        else:
            img.patch(MENU_LABEL, cur, new)
            log.append(f"  -> 0x{MENU_LABEL:06x}  menu label: "
                       f"{_head(cur)!r} -> {label!r}")

    if not log:
        raise LoopImportError("nothing to do: pass --usb-root, --sd-import "
                              "and/or --label")

    img.refresh_checksum()
    img.verify()
    if verbose:
        for line in log:
            print(line)
    return log


# ────────────────────────────────────────────────────────────────────────────
# Reporting
# ────────────────────────────────────────────────────────────────────────────
def report(img: R24Image) -> None:
    cat = catalog(img)
    print(f"image           {img.size} bytes, version {img.version!r}, "
          f"checksum {img.stored_checksum:#010x}"
          f" ({'ok' if img.checksum_ok() else 'BAD'})")
    print()
    print("loop-import literals")
    for name, off, raw in (("listing / stat / merge", LOOP_STR_LIST, cat["loop_list"]),
                           ("Create Folder? gate   ", LOOP_STR_MKDIR, cat["loop_mkdir"])):
        print(f"  {name}  0x{off:06x}  {raw.split(chr(0).encode())[0]!r}")
    print()
    print("references taken by the code")
    for h in cat["refs"]:
        where = ("listing" if h["target_file"] == LOOP_STR_LIST else "mk-dir ")
        print(f"  0x{h['site_file']:06x} (0x{h['site_va']:08x})  {h['kind']:24s}"
              f" -> 0x{h['target_file']:06x}  [{where}]")
    print()
    print("menu entry (packed 6-byte records at 0x43404)")
    raw = cat["menu_label"]
    print(f"  0x{MENU_LABEL:06x}  {raw!r}  ({len(raw)} bytes, must stay {MENU_LABEL_LEN})")
    print()
    print("left alone on purpose")
    for off, s in cat["related"]:
        print(f"  0x{off:06x}  {s!r}")
    print()
    print("helpers used by this path")
    for va, what in HELPERS:
        print(f"  0x{va:08x}  {what}")
    print()
    print("patchable sites (same-length data edits only)")
    print(f"  0x{LOOP_STR_LIST:06x}  16 bytes  loop path (listing side)")
    print(f"  0x{LOOP_STR_MKDIR:06x}  16 bytes  loop path (Create Folder? side)")
    print(f"  0x{MENU_LABEL:06x}   9 bytes  menu label (8 chars + NUL)")
    print()
    print("for a *second* simultaneous entry, new code has to live in the")
    print(f"erased-flash gap 0x{r24image.va_to_off(0x0226E910):06x}"
          f"..0x{r24image.va_to_off(0x022A0000):06x}"
          " (docs/10_loop_import.md section 6)")


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Move the R24 sampler's loop import between USB and SD.")
    ap.add_argument("image", help="input R24SYS.BIN")
    ap.add_argument("-o", "--out", help="write the patched image here")
    ap.add_argument("--usb-root", action="store_true",
                    help=r"list loops from the USB stick's root (B:\)")
    ap.add_argument("--sd-import", action="store_true",
                    help=r"list loops from the SD card's root (A:\)")
    ap.add_argument("--label", default=None,
                    help="menu label, max 8 chars (default for --sd-import: LOOP[SD])")
    ap.add_argument("--list-sites", action="store_true",
                    help="print the analysis report and exit")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        img = R24Image.from_file(args.image)
    except (OSError, r24image.ImageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list_sites:
        report(img)
        return 0

    label = args.label
    if label is None and args.sd_import:
        label = "LOOP[SD]"          # 8 chars: same shape as the factory label
    if label is None and args.usb_root:
        label = "LOOP[F]"          # 7 chars + the 8th slot stays a space

    try:
        log = apply(img, usb_root=args.usb_root, sd_import=args.sd_import,
                    label=label.encode("ascii") if label else None,
                    verbose=not args.quiet)
    except LoopImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.out:
        print("note: --out not given, nothing written", file=sys.stderr)
        return 0

    img.write(args.out)
    if not args.quiet:
        print()
        print(f"wrote {args.out}  ({img.size} bytes, checksum "
              f"{img.stored_checksum:#010x})")
        print("install: copy to the root of a FAT32 SD card as R24SYS.BIN and")
        print("follow R24_Firmware_Update_Guide_E.pdf.  NOT tested on hardware.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
