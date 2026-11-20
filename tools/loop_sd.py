#!/usr/bin/env python3
"""loop_sd.py — import loops from the SD card in TAKE SELECT.

Background: docs/10_loop_import.md (§2 how LOOP[USB] works, §4–§5 this tool).

The LOOP[USB] callback (RAM 0x800425BA) copies a WAV chosen in
B:\\ZOOM_R24\\LOOP into the project's AUDIO folder on the SD card. Only three
calls in it are USB-specific: host mode (0x8005B066), wait for a USB device
(0x80026EFC) and USB off (0x8005AFC6).

  --replace   in place: neutralise those three calls and point the path at
              A:\\ZOOM_R24\\LOOP. LOOP[USB] becomes LOOP[SD]. 4 same-size
              patches + the label; no new code. Use it first to validate the
              SD import on the device.

  --root      (with either mode) start browsing at the root of the SD card
              instead of A:\\ZOOM_R24\\LOOP; every folder is reachable.

  --usb-root  (alone, or with --add) make LOOP[USB] itself browse from the
              root of the USB stick ("B:") instead of B:\\ZOOM_R24\\LOOP.
              Same two changes as --root, on the original callback; the USB
              host mode and the wait for the stick stay.

  --add       keep LOOP[USB] and add a fifth item LOOP[SD]:
              * a copy of the callback (with the --replace changes) in the PCM
                bank tail, which the boot code copies to RAM 0x87540000+; every
                bl/blx out of it goes through a 12-byte long-call veneer
                (bx pc; nop; ldr pc,[pc,#-4]; .word target), since the firmware
                is beyond Thumb bl range;
              * an 80-byte item record and a 24 x 32-byte pointer array there;
              * 4 same-size patches in the TAKE SELECT builder (count 4->5,
                pointer stride 16->32, pointer-array literal).
              The image must use the relocated sample table (r24bank.py).

    python3 tools/loop_sd.py IMAGE --replace -o OUT
    python3 tools/loop_sd.py IMAGE --add     -o OUT
    python3 tools/loop_sd.py IMAGE --check          # what is installed
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank

RAM0 = 0x80000000
DATA_FILE = 0x5B4


def r2f(ram: int) -> int:
    return ram - RAM0 + DATA_FILE


def f2r(off: int) -> int:
    return off - DATA_FILE + RAM0


# ── LOOP[USB] callback ────────────────────────────────────────────────────
FUNC_LO, FUNC_HI = 0x800425BA, 0x80042AD6       # code of the callback
COPY_LO, COPY_HI = 0x800425B8, 0x80042E28       # + both literal pools
PATH_RAM = 0x800429B4                            # "B:\ZOOM_R24\LOOP"
LABEL_RAM = 0x80042E7C                           # "LOOP[USB]" (12-byte slot)

THUMB_NOP = bytes.fromhex("c046")                # mov r8, r8
MOVS_R0_0 = bytes.fromhex("0020")                # movs r0, #0

# (RAM address, expected bytes, replacement, what)
CALL_PATCHES = [
    (0x80042604, "18f02ffd", MOVS_R0_0 + THUMB_NOP, "bl 0x8005B066  USB host mode"),
    (0x8004261A, "e4f76ffc", MOVS_R0_0 + THUMB_NOP, "bl 0x80026EFC  wait for USB device"),
    (0x8004271E, "18f052fc", THUMB_NOP + THUMB_NOP, "bl 0x8005AFC6  USB off"),
]
OLD_PATH = b"B:\\ZOOM_R24\\LOOP\x00"
NEW_PATH = b"A:\\ZOOM_R24\\LOOP\x00"
# --root: browse the whole card. The lister appends "\*.*" and folders are
# joined with "\", so the root is written "A:" (no trailing backslash), and
# the pre-check stat of the folder is skipped (the FAT root has no directory
# entry of its own; the check only exists to report a missing LOOP folder).
ROOT_PATH = b"A:" + b"\x00" * 15
USB_ROOT_PATH = b"B:" + b"\x00" * 15
ROOT_PATCH = (0x80042636, "12f0a8fc", MOVS_R0_0 + THUMB_NOP, "bl 0x80054F8A  stat of the start folder")
OLD_LABEL = b"LOOP[USB]\x00\x00\x00"
NEW_LABEL = b"LOOP[SD] \x00\x00\x00"

# ── TAKE SELECT builder (RAM 0x80042C1A) ──────────────────────────────────
BUILDER_PATCHES = [
    (0x80043090, "2101", "6101", "lsls r1,r4,#4 -> #5  pointer stride"),
    (0x800430DA, "2001", "6001", "lsls r0,r4,#4 -> #5  pointer stride"),
    (0x800430A0, "0420", "0520", "movs r0,#4 -> #5     item count"),
]
PTR_LITERAL = 0x80043294                         # holds 0x8114691C
PTR_ORIG = 0x8114691C
ITEMS = 0x811449DC                               # 24 x 4 x 80-byte records
LOOP_ITEM_OFF = 2 * 80                           # LOOP[USB] record in a track row
TRACKS = 4 * 6                                   # 24
ROW = 32                                         # new pointer stride

EXT_TAG = b"LOOPSD01"


class LoopError(Exception):
    pass


def u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def check_stock_bytes(data: bytes) -> None:
    """Refuse anything that is not the v1.13 code these offsets belong to."""
    for ram, want, _new, what in CALL_PATCHES:
        got = data[r2f(ram):r2f(ram) + 4].hex()
        if got != want:
            raise LoopError(f"{what}: expected {want} at RAM 0x{ram:08X}, found {got}"
                            f" (already patched, or not v1.13)")
    if data[r2f(PATH_RAM):r2f(PATH_RAM) + 17] != OLD_PATH:
        raise LoopError("LOOP path literal not found")
    if data[r2f(LABEL_RAM):r2f(LABEL_RAM) + 12] != OLD_LABEL:
        raise LoopError("LOOP[USB] label not found")
    for ram, want, _new, what in BUILDER_PATCHES:
        if data[r2f(ram):r2f(ram) + 2].hex() != want:
            raise LoopError(f"TAKE SELECT builder: {what} not found")
    if u32(data, r2f(PTR_LITERAL)) != PTR_ORIG:
        raise LoopError("TAKE SELECT pointer-array literal not found")


# ── Thumb decoding helpers ────────────────────────────────────────────────
def decode_bl(d: bytes, off: int, ram: int):
    """Return (target, is_blx) for a Thumb BL/BLX(imm) pair at `off`, else None."""
    h1, h2 = struct.unpack_from("<HH", d, off)
    if h1 >> 11 != 0b11110 or (h2 >> 11) not in (0b11111, 0b11101):
        return None
    disp = ((h1 & 0x7FF) << 12) | ((h2 & 0x7FF) << 1)
    if disp & (1 << 22):
        disp -= 1 << 23
    t = ram + 4 + disp
    blx = (h2 >> 11) == 0b11101
    if blx:
        t &= ~3
    return t, blx


def encode_bl(src: int, dst: int) -> bytes:
    disp = dst - (src + 4)
    if disp % 2 or not -(1 << 22) <= disp < (1 << 22):
        raise LoopError(f"bl 0x{src:08X} -> 0x{dst:08X} out of range")
    disp &= (1 << 23) - 1
    h1 = 0xF000 | (disp >> 12)
    h2 = 0xF800 | ((disp >> 1) & 0x7FF)
    return struct.pack("<HH", h1, h2)


def veneer(target: int, arm: bool) -> bytes:
    """Thumb entry, 4-aligned: bx pc ; nop ; (ARM) ldr pc,[pc,#-4] ; .word target."""
    word = target if arm else target | 1
    return bytes.fromhex("7847") + THUMB_NOP + struct.pack("<II", 0xE51FF004, word)


def scan_calls(d: bytes) -> list[tuple[int, int, bool]]:
    """All bl/blx in the callback whose target is outside the copied block."""
    out = []
    ram = FUNC_LO
    while ram < FUNC_HI:
        bl = decode_bl(d, r2f(ram), ram)
        if bl:
            t, blx = bl
            if not COPY_LO <= t < COPY_HI:
                out.append((ram, t, blx))
            ram += 4
        else:
            ram += 2
    return out


# ── the extension blob for --add ──────────────────────────────────────────
def build_ext_payload(stock: bytes, base: int, root: bool = False) -> tuple[bytes, dict]:
    """Payload placed at RAM `base` (the extension area's start + 16).

    Layout (all offsets from `base`, which is 8-aligned):
        +0x000   "LOOPSD01" tag, u32 version, u32 0
        +0x010   pointer array, 24 rows x 32 bytes (slot 4 = new item)
        +0x310   item record, 80 bytes
        +0x360   "LOOP[SD] " label, 12 bytes
        +0x370   copy of RAM 0x800425B8..0x80042E28 (same alignment mod 4)
        ...      veneers, 12 bytes each, 4-aligned
    """
    P_ARR, P_ITEM, P_LABEL, P_CODE = 0x10, 0x310, 0x360, 0x370
    code_ram = base + P_CODE
    assert code_ram % 4 == COPY_LO % 4
    code = bytearray(stock[r2f(COPY_LO):r2f(COPY_HI)])
    delta = code_ram - COPY_LO

    def at(ram):                      # offset in `code` of an original RAM addr
        return ram - COPY_LO

    # 1. the --replace changes, in the copy
    for ram, _want, new, _what in CALL_PATCHES + ([ROOT_PATCH] if root else []):
        code[at(ram):at(ram) + 4] = new
    code[at(PATH_RAM):at(PATH_RAM) + 17] = ROOT_PATH if root else NEW_PATH

    # 2. redirect every call out of the callback through a veneer
    # the neutralised call sites must stay neutralised: never re-route them
    neutral = {ram for ram, *_ in CALL_PATCHES + ([ROOT_PATCH] if root else [])}
    calls = [c for c in scan_calls(stock) if c[0] not in neutral]
    ven_base = code_ram + len(code)
    ven_base = (ven_base + 3) & ~3
    pad = ven_base - (code_ram + len(code))
    veneers = bytearray()
    ven_for: dict[tuple[int, bool], int] = {}
    for ram, target, blx in calls:
        key = (target, blx)
        if key not in ven_for:
            ven_for[key] = ven_base + len(veneers)
            veneers += veneer(target, arm=blx)
        code[at(ram):at(ram) + 4] = encode_bl(ram + delta, ven_for[key])

    # 3. item record: same as LOOP[USB]'s, new label and callback
    entry = (code_ram + (FUNC_LO - COPY_LO)) | 1
    empty = 0x80042E68                         # "" used by all four items
    item = struct.pack("<IIII", base + P_LABEL, 1, 0, 0x800D38DC)
    item += struct.pack("<I", entry) + b"\x00" * 0x1C
    item += struct.pack("<IIII", empty, empty, empty, empty) + b"\x00" * 0x10
    assert len(item) == 80

    # 4. pointer array: what the builder writes in slots 0-3, ours in slot 4
    arr = bytearray()
    for t in range(TRACKS):
        row = [ITEMS + t * 320 + n * 80 for n in range(4)] + [base + P_ITEM, 0, 0, 0]
        arr += struct.pack("<8I", *row)

    payload = bytearray(P_CODE)
    payload[0:16] = EXT_TAG + struct.pack("<II", 1, 0)
    payload[P_ARR:P_ARR + len(arr)] = arr
    payload[P_ITEM:P_ITEM + 80] = item
    payload[P_LABEL:P_LABEL + 12] = NEW_LABEL
    payload += code + b"\x00" * pad + veneers
    info = {"base": base, "array": base + P_ARR, "item": base + P_ITEM,
            "entry": entry, "code": code_ram, "calls": len(calls),
            "veneers": len(ven_for), "size": len(payload)}
    return bytes(payload), info


def payload_size(stock: bytes, root: bool = False) -> int:
    return len(build_ext_payload(stock, 0x87000000, root)[0])


# ── modes ─────────────────────────────────────────────────────────────────
def do_replace(data: bytearray, root: bool = False) -> list[str]:
    check_stock_bytes(data)
    if data[r2f(ROOT_PATCH[0]):r2f(ROOT_PATCH[0]) + 4].hex() != ROOT_PATCH[1]:
        raise LoopError("stat call site not found")
    notes = []
    for ram, want, new, what in CALL_PATCHES + ([ROOT_PATCH] if root else []):
        data[r2f(ram):r2f(ram) + 4] = new
        notes.append(f"file 0x{r2f(ram):06X}  {want} -> {new.hex()}  {what}")
    data[r2f(PATH_RAM):r2f(PATH_RAM) + 17] = ROOT_PATH if root else NEW_PATH
    notes.append(f"file 0x{r2f(PATH_RAM):06X}  path -> "
                 + ("A: (card root)" if root else "A:\\ZOOM_R24\\LOOP"))
    data[r2f(LABEL_RAM):r2f(LABEL_RAM) + 12] = NEW_LABEL
    notes.append(f"file 0x{r2f(LABEL_RAM):06X}  label LOOP[USB] -> LOOP[SD]")
    return notes


def do_add(data: bytearray, root: bool = False) -> list[str]:
    check_stock_bytes(data)
    stock = bytes(data)
    info = {}

    def build(base):
        payload, inf = build_ext_payload(stock, base, root)
        info.update(inf)
        return payload
    try:
        _lay, _rel = r24bank.add_section(data, EXT_TAG, payload_size(stock, root), build)
    except r24bank.BankError as exc:
        raise LoopError(str(exc))
    notes = []
    for ram, want, new, what in BUILDER_PATCHES:
        data[r2f(ram):r2f(ram) + 2] = bytes.fromhex(new)
        notes.append(f"file 0x{r2f(ram):06X}  {want} -> {new}  {what}")
    struct.pack_into("<I", data, r2f(PTR_LITERAL), info["array"])
    notes.append(f"file 0x{r2f(PTR_LITERAL):06X}  literal 0x{PTR_ORIG:08X} -> "
                 f"0x{info['array']:08X}  pointer array")
    notes.append(f"extension at RAM 0x{info['base']:08X} ({info['size']} bytes): "
                 f"callback copy at 0x{info['code']:08X}, entry 0x{info['entry']:08X}, "
                 f"{info['calls']} calls via {info['veneers']} veneers, "
                 f"item 0x{info['item']:08X}")
    return notes


def do_usb_root(data: bytearray) -> list[str]:
    """LOOP[USB] (the original callback) browses from the stick's root."""
    ram, want, new, what = ROOT_PATCH
    if data[r2f(PATH_RAM):r2f(PATH_RAM) + 17] != OLD_PATH:
        raise LoopError("LOOP[USB] path is not B:\\ZOOM_R24\\LOOP (already changed, "
                        "or turned into LOOP[SD] by --replace)")
    if data[r2f(ram):r2f(ram) + 4].hex() != want:
        raise LoopError("LOOP[USB] stat call site not found")
    data[r2f(ram):r2f(ram) + 4] = new
    data[r2f(PATH_RAM):r2f(PATH_RAM) + 17] = USB_ROOT_PATH
    return [f"file 0x{r2f(ram):06X}  {want} -> {new.hex()}  LOOP[USB]: {what}",
            f"file 0x{r2f(PATH_RAM):06X}  LOOP[USB] path -> B: (stick root)"]


def describe(data: bytes) -> str:
    out = []
    lab = data[r2f(LABEL_RAM):r2f(LABEL_RAM) + 12]
    name = lab.split(b"\0")[0].decode()
    out.append(f"label at 0x{r2f(LABEL_RAM):06X}: {name!r}")
    out.append("path : " + data[r2f(PATH_RAM):r2f(PATH_RAM) + 17].split(b"\0")[0].decode())
    cnt = data[r2f(0x800430A0)]
    out.append(f"TAKE SELECT items: {cnt}")
    lit = u32(data, r2f(PTR_LITERAL))
    out.append(f"pointer array: 0x{lit:08X}" + ("" if lit == PTR_ORIG else " (moved)"))
    try:
        lay = r24bank.locate(data)
        out.append("bank : " + lay.describe())
        sec = r24bank.find_section(data, EXT_TAG)
        if sec:
            out.append(f"LOOP[SD] section at RAM 0x{r24bank.RAM_BASE + sec[0]:08X}")
    except r24bank.BankError as exc:
        out.append(f"bank : {exc}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SD-card loop import for TAKE SELECT.")
    ap.add_argument("image")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--replace", action="store_true",
                   help="turn LOOP[USB] into LOOP[SD] in place")
    g.add_argument("--add", action="store_true",
                   help="add LOOP[SD] as a fifth TAKE SELECT item")
    g.add_argument("--check", action="store_true", help="show what is installed")
    ap.add_argument("--usb-root", action="store_true",
                    help="LOOP[USB] browses from the root of the stick (B:)")
    ap.add_argument("--root", action="store_true",
                    help="browse from the root of the SD card (A:) instead of "
                         "A:\\ZOOM_R24\\LOOP")
    ap.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    try:
        data = bytearray(open(args.image, "rb").read())
        if args.check:
            print(describe(data))
            return 0
        if not (args.replace or args.add or args.usb_root):
            raise LoopError("give --replace, --add, --usb-root or --check")
        if args.replace and args.usb_root:
            raise LoopError("--replace turns LOOP[USB] into LOOP[SD]; there is "
                            "no LOOP[USB] left for --usb-root")
        if args.root and not (args.replace or args.add):
            raise LoopError("--root applies to LOOP[SD] (--replace or --add)")
        if not args.output:
            raise LoopError("-o OUTPUT is required")
        notes = []
        if args.replace:
            notes += do_replace(data, args.root)
        if args.add:
            notes += do_add(data, args.root)      # copies the untouched callback
        if args.usb_root:
            notes += do_usb_root(data)
        print(f"source : {args.image}")
        for n in notes:
            print(f"  {n}")
        img = r24bank.seal(data, args.output)
        print(f"wrote  : {args.output} ({img.size} bytes, checksum "
              f"0x{img.stored_checksum:08X}, verifies {img.checksum_ok()})")
        return 0
    except (LoopError, r24bank.BankError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
