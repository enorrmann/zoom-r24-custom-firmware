#!/usr/bin/env python3
"""r24bank.py — the R24 PCM sample bank and its Master Sample Table.

This module is the single source of truth for *where the sample table is* and
*what the firmware does with it*. patch_samples.py, r24kits.py and
gen_sample_map.py all go through it.

HOW THE FIRMWARE USES THE BANK  (docs/07_pcm_samples.md §2, all [PROVEN])
-------------------------------------------------------------------------
1. Boot copy, VA 0x020ADDFC (called from the main init at 0x020887F2):

       movs r2,#5 ; lsls r2,#20         r2 = 0x500000  (5 MiB)
       ldr  r1,=0x022A0000              source: bank in the image (file 0x220040)
       ldr  r0,=0x87540000              destination: RAM
       blx  memcpy

2. Voice start, VA 0x020AE016..0x020AE03E:

       start = 0x87540000 + T[id].rel_start
       end   = 0x87540000 + T[id].rel_start + T[id].length - 2

   and VA 0x020AE508 derives the voice duration from T[id].length.

   T is read through exactly two literal-pool words, file 0x2E244 and
   0x2E624, both holding 0x800E1B38 (RAM copy of the factory table at file
   0x0E20EC; the .data image maps RAM 0x80000000 <- file 0x5B4). No other
   code loads T, and no code addresses an individual sample by a fixed
   address: the only bank address in the code is the memcpy source above.

So the table is pure data. rel_start / length can be anything, entries may
share or overlap audio, and the number of entries is limited only by where
the table lives — which is set by those two literal words.

TWO LAYOUTS
-----------
factory     table at file 0x0E20EC, 73 entries, literals = 0x800E1B38.
            The kit table starts right after it, so it cannot grow in place.

relocated   table moved to the END of the bank, inside the 5 MiB that the
            boot code copies to RAM, so the literals become
            0x87540000 + table_rel. A 16-byte header in front of it lets the
            tools find the entry count (the firmware never reads it):

                hdr_rel      "R24SMPTB"  u32 count  u32 table_rel
                table_rel    count x <II rel_start, length>
                ...          zero up to the end of the bank (== EOF)

            Audio may use [0, hdr_rel).

            Optionally an EXTENSION area follows the table and runs to EOF,
            for new code/data that must live in RAM (docs/10 §5):

                ext_rel      "R24EXT01"  u32 size  u32 ndir
                             (ext_rel = table_rel + 8*count, 8-aligned)
                ndir == 0    one section: payload at ext_rel+16 to EOF; its
                             first 8 bytes are its tag
                ndir  > 0    ndir x {tag[8], u32 rel, u32 len} at ext_rel+16;
                             sections are stacked downward from EOF and never
                             move once placed (they hold absolute addresses)

            Repacking samples or relocating the table preserves it.

Whatever the layout, the file size never changes: the bank still ends at EOF.

CLI
---
    python3 tools/r24bank.py info     R24SYS.BIN
    python3 tools/r24bank.py relocate R24SYS.BIN --per-kit -o build/R24SYS_160.BIN
    python3 tools/r24bank.py relocate R24SYS.BIN --entries 96 -o out.BIN

`relocate --per-kit` gives every kit slot its own entry (kit k, slot s ->
entry 16*k + s, 160 entries). Each new entry initially points at the audio the
slot already played, so the image sounds exactly like its source; afterwards
`patch_samples.py --repack` gives each of the 160 entries its own audio.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24image

# ── geometry ───────────────────────────────────────────────────────────────
BANK_BASE = 0x00220040              # file offset of bank byte 0 (VA 0x022A0000)
BANK_VA = 0x022A0000
RAM_BASE = 0x87540000               # where the boot code copies the bank
RAM_COPY = 0x500000                 # bytes copied at boot (movs r2,#5; lsls #20)
COPY_SITE = 0x2DE3E                 # file offset of `movs r2,#5`  (VA 0x020ADDFE)
COPY_SITE_BYTES = bytes.fromhex("0522" "1205")   # movs r2,#5 ; lsls r2,r2,#20

TABLE_LITERALS = (0x2E244, 0x2E624)  # the only two references to the table
FACTORY_TABLE = 0x000E20EC
FACTORY_COUNT = 73
DATA_RAM, DATA_FILE = 0x80000000, 0x5B4    # .data: RAM 0x80000000 <- file 0x5B4
FACTORY_LITERAL = DATA_RAM + FACTORY_TABLE - DATA_FILE      # 0x800E1B38

KIT_TABLE = 0x000E2334
KIT_SIZE = 144
NUM_KITS = 10
SLOTS = 16

ENTRY = 8
HDR_MAGIC = b"R24SMPTB"
HDR_SIZE = 16
EXT_MAGIC = b"R24EXT01"
EXT_HDR = 16
MAX_ENTRIES = 4096                  # sanity cap; kits can address 160 at most
PER_KIT_ENTRIES = NUM_KITS * SLOTS  # 160


class BankError(Exception):
    pass


@dataclass
class Layout:
    relocated: bool
    table_off: int          # file offset of entry 0
    count: int
    audio_limit: int        # audio must end at or before this rel offset
    ext_rel: int = 0        # extension area (relocated only), 0 = none
    ext_size: int = 0

    @property
    def table_rel(self) -> int:
        return self.table_off - BANK_BASE

    @property
    def literal(self) -> int:
        if not self.relocated:
            return FACTORY_LITERAL
        return RAM_BASE + self.table_rel

    def describe(self) -> str:
        kind = "relocated" if self.relocated else "factory"
        ext = (f", extension {self.ext_size} bytes @ RAM "
               f"0x{RAM_BASE + self.ext_rel:08X}" if self.ext_size else "")
        return (f"{kind}: {self.count} entries @ file 0x{self.table_off:08X} "
                f"(literal 0x{self.literal:08X}){ext}")


def bank_size(data: bytes) -> int:
    return len(data) - BANK_BASE


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def check_image(data: bytes) -> None:
    """Refuse an image that is not the v1.13 layout these offsets belong to."""
    if len(data) <= BANK_BASE + 0x1000:
        raise BankError(f"image of {len(data)} bytes is too small to hold the "
                        f"sample bank at 0x{BANK_BASE:X}")
    if bank_size(data) > RAM_COPY:
        raise BankError("bank is larger than the 5 MiB the boot code copies")
    if data[COPY_SITE:COPY_SITE + 4] != COPY_SITE_BYTES:
        raise BankError("boot copy size instruction not found at file "
                        f"0x{COPY_SITE:X}; this is not the v1.13 image")
    if _u32(data, 0x2E1FC) != BANK_VA or _u32(data, 0x2E200) != RAM_BASE:
        raise BankError("bank copy literals not found at file 0x2E1FC/0x2E200; "
                        "this is not the v1.13 image")


def locate(data: bytes) -> Layout:
    """Find the sample table the firmware will actually read."""
    check_image(data)
    lits = {_u32(data, o) for o in TABLE_LITERALS}
    if len(lits) != 1:
        raise BankError("the two sample-table literals disagree: "
                        + ", ".join(f"0x{_u32(data, o):08X}" for o in TABLE_LITERALS))
    lit = lits.pop()
    size = bank_size(data)
    if lit == FACTORY_LITERAL:
        return Layout(False, FACTORY_TABLE, FACTORY_COUNT, size)
    if RAM_BASE <= lit < RAM_BASE + size and lit % 4 == 0:
        rel = lit - RAM_BASE
        hdr = BANK_BASE + rel - HDR_SIZE
        magic, count, trel = struct.unpack_from("<8sII", data, hdr)
        if magic != HDR_MAGIC or trel != rel:
            raise BankError(f"table literal points at bank+0x{rel:X} but no "
                            f"R24SMPTB header precedes it")
        if not 1 <= count <= MAX_ENTRIES or rel + count * ENTRY > size:
            raise BankError(f"relocated table header has bad count {count}")
        lay = Layout(True, BANK_BASE + rel, count, rel - HDR_SIZE)
        ext = rel + count * ENTRY
        if ext + EXT_HDR <= size and data[BANK_BASE + ext:BANK_BASE + ext + 8] == EXT_MAGIC:
            esize = _u32(data, BANK_BASE + ext + 8)
            if ext + esize != size:
                raise BankError(f"extension area at bank+0x{ext:X} claims {esize} "
                                f"bytes but {size - ext} remain to EOF")
            lay.ext_rel, lay.ext_size = ext, esize
        return lay
    raise BankError(f"sample-table literal 0x{lit:08X} is neither the factory "
                    f"table nor inside the RAM copy of the bank")


def read_table(data: bytes, lay: Layout | None = None) -> list[tuple[int, int]]:
    lay = lay or locate(data)
    return [struct.unpack_from("<II", data, lay.table_off + ENTRY * i)
            for i in range(lay.count)]


def validate_table(table: list[tuple[int, int]], lay: Layout) -> None:
    """Every rule the playback code implies (end = start + length - 2)."""
    for i, (rel, length) in enumerate(table):
        if length < 2 or length % 2:
            raise BankError(f"entry {i}: length {length} must be even and >= 2")
        if rel % 2:
            raise BankError(f"entry {i}: rel_start 0x{rel:X} is odd")
        if rel + length > lay.audio_limit:
            raise BankError(f"entry {i}: ends at 0x{rel + length:X}, past the "
                            f"audio area (0x{lay.audio_limit:X})")


def sample_bytes(data: bytes, table, i: int) -> bytes:
    rel, length = table[i]
    return bytes(data[BANK_BASE + rel:BANK_BASE + rel + length])


def relocated_layout(size: int, count: int, ext_size: int = 0) -> Layout:
    """Table (and extension, if any) packed against the end of the bank."""
    if not 1 <= count <= MAX_ENTRIES:
        raise BankError(f"entry count {count} out of range 1..{MAX_ENTRIES}")
    if ext_size:
        if ext_size < EXT_HDR:
            raise BankError("extension smaller than its header")
        ext_rel = (size - ext_size) & ~7
        ext_size = size - ext_rel
        hdr_rel = ext_rel - count * ENTRY - HDR_SIZE      # table ends at ext_rel
        if hdr_rel % 8:
            raise BankError("internal: misaligned table")
    else:
        ext_rel = 0
        hdr_rel = (size - HDR_SIZE - count * ENTRY) & ~7
    return Layout(True, BANK_BASE + hdr_rel + HDR_SIZE, count, hdr_rel,
                  ext_rel, ext_size)


def write_layout(data: bytearray, lay: Layout,
                 table: list[tuple[int, int]]) -> None:
    """Write `table` where `lay` says and point both literals at it."""
    if len(table) != lay.count:
        raise BankError("table length does not match the layout")
    validate_table(table, lay)
    if lay.relocated:
        hdr = BANK_BASE + lay.audio_limit
        end = BANK_BASE + lay.ext_rel if lay.ext_size else len(data)
        data[hdr:end] = b"\x00" * (end - hdr)
        struct.pack_into("<8sII", data, hdr, HDR_MAGIC, lay.count, lay.table_rel)
    for i, (rel, length) in enumerate(table):
        struct.pack_into("<II", data, lay.table_off + ENTRY * i, rel, length)
    for o in TABLE_LITERALS:
        struct.pack_into("<I", data, o, lay.literal)


def pack_bank(data: bytearray, pcms: list[bytes], count: int | None = None,
              force_relocate: bool = False) -> tuple[Layout, list[tuple[int, int]], int]:
    """Lay out `pcms` (one per entry) contiguously, sharing identical audio.

    Returns (layout, table, bytes of audio used). The factory table is
    rewritten in place when there are exactly 73 entries and the image still
    uses it; anything else relocates it to the end of the bank.
    """
    count = len(pcms) if count is None else count
    if count != len(pcms):
        raise BankError("one PCM block per entry is required")
    cur = locate(data)
    size = bank_size(data)
    if count == FACTORY_COUNT and not cur.relocated and not force_relocate:
        lay = Layout(False, FACTORY_TABLE, FACTORY_COUNT, size)
    else:
        lay = relocated_layout(size, count, cur.ext_size)

    where: dict[bytes, int] = {}
    blob = bytearray()
    table = []
    for i, pcm in enumerate(pcms):
        if len(pcm) < 2 or len(pcm) % 2:
            raise BankError(f"entry {i}: PCM length {len(pcm)} must be even and >= 2")
        key = hashlib.sha256(pcm).digest()
        if key not in where:
            where[key] = len(blob)
            blob += pcm
        table.append((where[key], len(pcm)))
    if len(blob) > lay.audio_limit:
        over = len(blob) - lay.audio_limit
        raise BankError(
            f"audio needs {len(blob)} bytes but the bank holds "
            f"{lay.audio_limit} — {over} bytes ({over / 2 / 44100:.2f} s at "
            f"44.1 kHz) too many. Shorten samples (e.g. --max-ms) or reuse one")
    data[BANK_BASE:BANK_BASE + len(blob)] = blob
    data[BANK_BASE + len(blob):BANK_BASE + lay.audio_limit] = (
        b"\x00" * (lay.audio_limit - len(blob)))
    write_layout(data, lay, table)
    return lay, table, len(blob)


def seal(data: bytearray, out: str) -> r24image.R24Image:
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    img = r24image.R24Image(bytes(data))
    img.refresh_checksum()
    img.write(out)
    return img


# ── kits ───────────────────────────────────────────────────────────────────
# Where the firmware reads the kit table and how many kits KIT SELECT lists
# (docs/08_drum_kits.md §5.2): two literals and one menu-record word.
KIT_LITERALS = (0x2EA28, 0x4BE74)
KIT_FACTORY_RAM = 0x800E1D80
KIT_COUNT_OFF = 0xFD950            # KIT SELECT item count (u32)
DATA_END_RAM = 0x801EE35C          # end of the reset copy of file 0x5B4..


def ram_to_file(data: bytes, addr: int) -> int:
    if DATA_RAM <= addr < DATA_END_RAM:
        return addr - DATA_RAM + DATA_FILE
    if RAM_BASE <= addr < RAM_BASE + bank_size(data):
        return addr - RAM_BASE + BANK_BASE
    raise BankError(f"RAM 0x{addr:08X} is not a loaded image address")


def locate_kits(data: bytes) -> tuple[int, int]:
    """(file offset, count) of the kit table the firmware uses."""
    lits = {_u32(data, o) for o in KIT_LITERALS}
    if len(lits) != 1:
        raise BankError("the two kit-table literals disagree")
    count = _u32(data, KIT_COUNT_OFF)
    if not 1 <= count <= 99:
        raise BankError(f"KIT SELECT count {count} is implausible")
    off = ram_to_file(data, lits.pop())
    if off + count * KIT_SIZE > len(data):
        raise BankError("kit table runs past the end of the image")
    return off, count


def kit_slot_off(k: int, s: int, base: int = KIT_TABLE) -> int:
    return base + k * KIT_SIZE + 16 + s * 8


def kit_sample_ids(data: bytes) -> list[list[int]]:
    base, n = locate_kits(data)
    return [[struct.unpack_from("<H", data, kit_slot_off(k, s, base))[0]
             for s in range(SLOTS)] for k in range(n)]


# ── relocation ─────────────────────────────────────────────────────────────
def relocate(data: bytearray, count: int | None, per_kit: bool,
             ext_size: int | None = None) -> tuple[Layout, list[str]]:
    """Move the table to the end of the bank without changing any sound.

    The table goes over the bank's tail. Any entry reaching into that tail is
    shortened, which is only allowed when the bytes cut off are silence
    (all zero) — the factory bank ends with 11,030 zero bytes, far more than a
    160-entry table needs.
    """
    cur = locate(data)
    old = read_table(data, cur)
    size = bank_size(data)
    notes: list[str] = []
    if per_kit:
        nkits = locate_kits(data)[1]
        count = count or nkits * SLOTS
        if count < nkits * SLOTS:
            raise BankError(f"--per-kit needs at least {nkits * SLOTS} entries")
    elif count is None:
        raise BankError("give --entries N or --per-kit")
    if count < cur.count and not per_kit:
        used = {sid for kit in kit_sample_ids(data) for sid in kit}
        if any(sid >= count for sid in used):
            raise BankError(f"kits still use ids >= {count}")

    lay = relocated_layout(size, count,
                           cur.ext_size if ext_size is None else ext_size)
    ids = kit_sample_ids(data)
    if per_kit:
        new = [old[ids[k][s]] for k in range(len(ids)) for s in range(SLOTS)]
        new += [old[0]] * (count - len(new))
    else:
        new = [old[i] if i < len(old) else old[0] for i in range(count)]

    fixed = []
    for i, (rel, length) in enumerate(new):
        end = rel + length
        if end > lay.audio_limit:
            cut = data[BANK_BASE + max(rel, lay.audio_limit):BANK_BASE + end]
            if any(cut):
                raise BankError(
                    f"entry {i} has audio in the last {size - lay.audio_limit} "
                    f"bytes of the bank, where the table/extension must go; "
                    f"repack with "
                    f"patch_samples.py --repack --entries {count} instead")
            new_len = (lay.audio_limit - rel) & ~1
            if new_len < 2:
                raise BankError(f"entry {i} lies entirely in the table area")
            notes.append(f"entry {i}: length {length} -> {new_len} "
                         f"(cut {length - new_len} bytes of trailing silence)")
            length = new_len
        fixed.append((rel, length))
    write_layout(data, lay, fixed)
    if per_kit:
        kbase = locate_kits(data)[0]
        for k in range(len(ids)):
            for s in range(SLOTS):
                struct.pack_into("<H", data, kit_slot_off(k, s, kbase), k * SLOTS + s)
    return lay, notes


def make_room(data: bytearray, ext_size: int) -> tuple[Layout, list[str]]:
    """Keep the table's entries and kits, reserve `ext_size` bytes at the end.

    Returns the new layout; the caller writes the extension (header + payload)
    at BANK_BASE + layout.ext_rel. Works from a factory or relocated image.
    """
    cur = locate(data)
    return relocate(data, cur.count, False, ext_size)


def ext_sections(data: bytes, lay: Layout | None = None) -> list[tuple[bytes, int, int]]:
    """[(tag, rel, length)] of the extension area's sections (bank-relative)."""
    lay = lay or locate(data)
    if not lay.ext_size:
        return []
    o = BANK_BASE + lay.ext_rel
    ndir = _u32(data, o + 12)
    if ndir == 0:
        rel = lay.ext_rel + EXT_HDR
        return [(bytes(data[BANK_BASE + rel:BANK_BASE + rel + 8]), rel,
                 bank_size(data) - rel)]
    out = []
    for i in range(ndir):
        tag, rel, ln = struct.unpack_from("<8sII", data, o + EXT_HDR + 16 * i)
        out.append((tag, rel, ln))
    return out


def find_section(data: bytes, tag: bytes) -> tuple[int, int] | None:
    """(rel, length) of the section whose tag is `tag`, or None."""
    for t, rel, ln in ext_sections(data):
        if t == tag:
            return rel, ln
    return None


def add_section(data: bytearray, tag: bytes, size: int, build) -> tuple[Layout, int]:
    """Place a new extension section of `size` bytes; `build(ram)` -> bytes.

    The payload must start with its 8-byte `tag`. Existing sections keep their
    addresses. Returns (layout, bank-relative offset of the new section).
    """
    if find_section(data, tag):
        raise BankError(f"section {tag.decode()} is already installed")
    cur = locate(data)
    secs = ext_sections(data, cur)
    if not secs:                                   # first one: compact v1 form
        lay, _ = make_room(data, EXT_HDR + size)
        rel = lay.ext_rel + EXT_HDR
        payload = build(RAM_BASE + rel)
        write_ext(data, lay, payload)
        return lay, rel
    lowest = min(rel for _t, rel, _l in secs)
    rel = (lowest - size) & ~7
    ndir = len(secs) + 1
    ext_rel = (rel - EXT_HDR - 16 * ndir) & ~7
    lay, _ = make_room(data, bank_size(data) - ext_rel)
    if lay.ext_rel != ext_rel:
        raise BankError("internal: extension did not land where planned")
    payload = build(RAM_BASE + rel)
    if len(payload) > size or payload[:8] != tag:
        raise BankError("section payload does not match its size or tag")
    o = BANK_BASE + ext_rel
    data[o:BANK_BASE + lowest] = b"\x00" * (lowest - ext_rel)
    struct.pack_into("<8sII", data, o, EXT_MAGIC, lay.ext_size, ndir)
    for i, (t, r_, ln) in enumerate(secs + [(tag, rel, size)]):
        struct.pack_into("<8sII", data, o + EXT_HDR + 16 * i, t, r_, ln)
    data[BANK_BASE + rel:BANK_BASE + rel + len(payload)] = payload
    return lay, rel


def write_ext(data: bytearray, lay: Layout, payload: bytes) -> None:
    if not lay.ext_size or EXT_HDR + len(payload) > lay.ext_size:
        raise BankError("extension area too small for the payload")
    o = BANK_BASE + lay.ext_rel
    blob = struct.pack("<8sII", EXT_MAGIC, lay.ext_size, 0) + payload
    data[o:o + lay.ext_size] = blob + b"\x00" * (lay.ext_size - len(blob))


# ── more kits ─────────────────────────────────────────────────────────────
KITS_TAG = b"R24KITS1"


def expand_kits(data: bytearray, count: int, per_kit: bool = True) -> list[str]:
    """Move the kit table into an extension section with `count` kits.

    The firmware reads the kit table through two literals and lists
    `[KIT_COUNT_OFF]` kits in KIT SELECT; nothing else bounds the index
    (docs/08_drum_kits.md §5.2). New kits start as copies of the first ones,
    named KIT11, KIT12, …; with `per_kit` the sample table is then laid out so
    that kit k slot s plays its own entry 16*k+s (same sound as the copy).
    """
    kbase, cur = locate_kits(data)
    if not cur < count <= 99:
        raise BankError(f"kit count must grow: now {cur}, asked {count} (max 99)")
    if find_section(data, KITS_TAG):
        raise BankError("the kit table was already moved; edit kits with r24kits.py")
    old = bytes(data[kbase:kbase + cur * KIT_SIZE])
    table = bytearray(old)
    for k in range(cur, count):
        rec = bytearray(old[(k - cur) % cur * KIT_SIZE:((k - cur) % cur + 1) * KIT_SIZE])
        rec[0:16] = f"KIT{k + 1}".encode().ljust(15, b" ") + b"\x00"
        table += rec
    size = 16 + len(table)

    def build(ram):
        return KITS_TAG + struct.pack("<II", count, 0) + bytes(table)

    _lay, rel = add_section(data, KITS_TAG, size, build)
    ram = RAM_BASE + rel + 16
    for o in KIT_LITERALS:
        struct.pack_into("<I", data, o, ram)
    struct.pack_into("<I", data, KIT_COUNT_OFF, count)
    notes = [f"kit table: {cur} -> {count} kits, now at RAM 0x{ram:08X} "
             f"(file 0x{BANK_BASE + rel + 16:08X})",
             f"literals file 0x{KIT_LITERALS[0]:X}, 0x{KIT_LITERALS[1]:X} -> 0x{ram:08X}",
             f"KIT SELECT count (file 0x{KIT_COUNT_OFF:X}) {cur} -> {count}"]
    if per_kit:
        _l, n2 = relocate(data, count * SLOTS, True)
        notes += n2
        notes.append(f"sample table: {count * SLOTS} entries, kit k slot s -> 16*k+s")
    return notes


def cmd_kits(args) -> int:
    data = bytearray(open(args.image, "rb").read())
    before = bytes(data)
    notes = expand_kits(data, args.count, not args.no_per_kit)
    # every pre-existing kit still plays exactly the same audio
    ob, on = locate_kits(before)
    old_tab, new_tab = read_table(before), read_table(data)
    oi, ni = kit_sample_ids(before), kit_sample_ids(data)
    for k in range(on):
        for s_ in range(SLOTS):
            a = sample_bytes(before, old_tab, oi[k][s_])
            b = sample_bytes(data, new_tab, ni[k][s_])
            if a[:len(b)] != b or any(a[len(b):]):
                raise BankError(f"kit {k} slot {s_} would change sound")
    print(f"source : {args.image}")
    for n in notes:
        print(f"  {n}")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0
    img = seal(data, args.output)
    print(f"wrote  : {args.output} ({img.size} bytes, checksum "
          f"0x{img.stored_checksum:08X}, verifies {img.checksum_ok()})")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────
def cmd_info(args) -> int:
    data = open(args.image, "rb").read()
    lay = locate(data)
    table = read_table(data, lay)
    validate_table(table, lay)
    distinct = {}
    for rel, length in table:
        distinct[(rel, length)] = True
    used = max(rel + length for rel, length in table)
    ids = kit_sample_ids(data)
    kb, kn = locate_kits(data)
    print(f"image      : {args.image} ({len(data)} bytes)")
    print(f"kits       : {kn} @ file 0x{kb:08X}")
    for t, rel, ln in ext_sections(data, lay):
        print(f"extension  : {t.decode(errors='replace')} {ln} bytes @ RAM "
              f"0x{RAM_BASE + rel:08X}")
    print(f"table      : {lay.describe()}")
    print(f"bank       : file 0x{BANK_BASE:08X} .. 0x{len(data):08X} "
          f"({bank_size(data)} bytes), copied to RAM 0x{RAM_BASE:08X} "
          f"(boot copy {RAM_COPY} bytes)")
    print(f"audio area : 0x0 .. 0x{lay.audio_limit:X} ({lay.audio_limit} bytes, "
          f"{lay.audio_limit / 2 / 44100:.2f} s @44.1k)")
    print(f"audio used : up to 0x{used:X}; {len(distinct)} distinct ranges "
          f"for {lay.count} entries")
    kbase, nk = locate_kits(data)
    for k in range(nk):
        off = kbase + k * KIT_SIZE
        name = data[off:off + 16].split(b"\x00")[0].decode("ascii", "replace").strip()
        print(f"  kit {k} {name:<10s} {ids[k]}"
              f"{'  (all different)' if len(set(ids[k])) == SLOTS else ''}")
    return 0


def cmd_relocate(args) -> int:
    data = bytearray(open(args.image, "rb").read())
    before = bytes(data)
    lay, notes = relocate(data, args.entries, args.per_kit)
    table = read_table(data, lay)
    # proof that nothing audible changed: every kit slot still plays the same bytes
    old_lay = locate(before)
    old_tab = read_table(before, old_lay)
    old_ids, new_ids = kit_sample_ids(before), kit_sample_ids(data)
    for k in range(len(old_ids)):
        for s in range(SLOTS):
            a = sample_bytes(before, old_tab, old_ids[k][s])
            b = sample_bytes(data, table, new_ids[k][s])
            if a[:len(b)] != b or any(a[len(b):]):
                raise BankError(f"kit {k} slot {s} would change sound")
    print(f"source : {args.image}")
    print(f"table  : {old_lay.describe()}")
    print(f"      -> {lay.describe()}")
    print(f"audio  : may use bank 0x0 .. 0x{lay.audio_limit:X}")
    for n in notes:
        print(f"  {n}")
    if args.per_kit:
        print("kits   : kit k slot s -> entry 16*k+s (160 entries, all different "
              "within every kit); sound unchanged")
    if args.dry_run:
        print("--dry-run: nothing written")
        return 0
    img = seal(data, args.output)
    print(f"wrote  : {args.output} ({img.size} bytes, checksum "
          f"0x{img.stored_checksum:08X}, verifies {img.checksum_ok()})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Inspect or relocate the R24 sample table.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="show where the table is and what kits use")
    p.add_argument("image")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("relocate", help="move the table to the end of the bank")
    p.add_argument("image")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--entries", type=int, default=None,
                   help="number of table entries (default 160 with --per-kit)")
    p.add_argument("--per-kit", action="store_true",
                   help="give every kit slot its own entry: kit k slot s -> 16k+s")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_relocate)
    p = sub.add_parser("kits", help="raise the number of kits (moves the kit table)")
    p.add_argument("image")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--count", type=int, required=True, help="new number of kits")
    p.add_argument("--no-per-kit", action="store_true",
                   help="do not give the kits their own sample-table entries")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_kits)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (BankError, r24image.ImageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
