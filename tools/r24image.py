"""Zoom R24 firmware image format — reference implementation.

This module is the single source of truth for the R24SYS.BIN container.
Everything else in tools/ imports from here.

FORMAT (empirically derived, see docs/02_analysis.md)
=====================================================
    +0x00  8   magic        "ZOOM/D21"
    +0x08  4   unknown      observed 31 00 00 00
    +0x0c  4   unknown      zero
    +0x10  1   kind         0 = SYSTEM (boot loader: 1 BOOT, 2 RHYTHM)
    +0x11  15  reserved     zero
    +0x20  16  version      ASCII, NUL padded ("1.13")
    +0x30  4   u32 LE       checksum present (byte 0x30 != 0), observed 1
    +0x34  4   u32 LE       checksum = sum(payload bytes) mod 2**32
    +0x38  8   reserved     zero
    +0x40  N   payload      flat image, linked for VA 0x02080000

The payload is a *flat* image: file offset 0x40 corresponds to virtual
address 0x02080000.  There is no ELF/PE container, no relocation table and
no compressed blob.  The 0xFF runs are erased-flash fill left inside the
image, not padding the loader strips.

FLASH SAFETY (docs/18_dump_analysis.md §3.3): the boot loader writes the
payload at NOR 0x02080000 with NO length check.  A payload over 0x720000
bytes reaches the boot flag (0x027A0000); over 0x780000 it wraps onto the
boot loader at 0x02000000 (the 8 MiB chip repeats) and bricks the unit.
write() and verify() refuse such an image.
"""

from __future__ import annotations

import hashlib
import struct

MAGIC = b"ZOOM/D21"             # 8-byte field; "ZOOM/D211" is magic+u32(1)
MAGIC_FULL = b"ZOOM/D211"        # the 9 bytes the device actually checks
HEADER_SIZE = 0x40
LOAD_ADDR = 0x02080000          # VA of file offset 0x40
CHECKSUM_OFFSET = 0x34
VERSION_OFFSET = 0x20
VERSION_SIZE = 16

# Known file-offset landmarks (validated in test_roundtrip.py)
FF_GAP_START = 0x01EE950        # first byte of the erased-flash gap
FF_GAP_END = 0x0220040          # first byte after it
LAST_USED = 0x6D3B5D            # last non-zero byte before trailing zeros

# The string the R24 prints on its LCD at boot.  It is a SEPARATE copy from
# the header field at 0x20: it lives in a UI string table next to the display
# charset, followed by a descriptor/function-pointer array.  Changing the
# header field does not change what the device shows (see docs/02_analysis.md).
UI_VERSION_OFFSET = 0x1EBBFC
UI_VERSION_SIZE = 12            # 1.13 + NUL; field ends at the ptr array (0x1EBC08)
UI_CHARSET_OFFSET = 0x1EBBF0    # "-.--" -- the display's placeholder charset

# Field-update hook string; the device looks for A:\R24PANEL.ZEX and applies a
# "PANEL VERSIONUP" step.  Present twice in the payload.
PANEL_UPDATE_STR = b"PANEL VERSIONUP"


KIND_OFFSET = 0x10             # image kind byte, 0 = SYSTEM
CHECKSUM_FLAG_OFFSET = 0x30    # byte: 0 = the updater skips the sum
MAX_PAYLOAD = 0x720000         # NOR 0x02080000..0x0279FFFF
MAX_FILE_SIZE = HEADER_SIZE + MAX_PAYLOAD    # 7,471,168 bytes


class ImageError(Exception):
    """Raised when an image fails structural validation."""


def va_to_off(va: int) -> int:
    """Virtual address -> file offset."""
    return va - LOAD_ADDR + HEADER_SIZE


def off_to_va(off: int) -> int:
    """File offset -> virtual address."""
    return off - HEADER_SIZE + LOAD_ADDR


def compute_checksum(payload: bytes) -> int:
    """Header check field: byte sum of the payload, truncated to 32 bits."""
    return sum(payload) & 0xFFFFFFFF


class R24Image:
    """A parsed R24SYS.BIN firmware image."""

    def __init__(self, data: bytes):
        if len(data) < HEADER_SIZE:
            raise ImageError(f"file too short: {len(data)} bytes")
        self._data = bytearray(data)
        self._validate_magic()

    # ---- construction -------------------------------------------------
    @classmethod
    def from_file(cls, path) -> "R24Image":
        with open(path, "rb") as fh:
            return cls(fh.read())

    @classmethod
    def from_parts(cls, header: bytes, payload: bytes,
                   fix_checksum: bool = True) -> "R24Image":
        """Build an image from a raw 64-byte header and a payload."""
        if len(header) != HEADER_SIZE:
            raise ImageError(f"header must be {HEADER_SIZE} bytes, got {len(header)}")
        hdr = bytearray(header)
        if fix_checksum:
            struct.pack_into("<I", hdr, CHECKSUM_OFFSET, compute_checksum(payload))
        return cls(bytes(hdr) + payload)

    # ---- basic accessors ----------------------------------------------
    @property
    def data(self) -> bytes:
        return bytes(self._data)

    @property
    def header(self) -> bytes:
        return bytes(self._data[:HEADER_SIZE])

    @property
    def payload(self) -> bytes:
        return bytes(self._data[HEADER_SIZE:])

    @property
    def size(self) -> int:
        return len(self._data)

    @property
    def version(self) -> str:
        raw = self.header[VERSION_OFFSET:VERSION_OFFSET + VERSION_SIZE]
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace")

    @property
    def stored_checksum(self) -> int:
        return struct.unpack_from("<I", self._data, CHECKSUM_OFFSET)[0]

    @property
    def computed_checksum(self) -> int:
        return compute_checksum(self.payload)

    @property
    def magic(self) -> bytes:
        return self.header[:len(MAGIC_FULL)]

    # ---- validation ---------------------------------------------------
    def _validate_magic(self) -> None:
        got = bytes(self._data[:len(MAGIC_FULL)])
        if got != MAGIC_FULL:
            raise ImageError(f"bad magic: {got!r} (expected {MAGIC_FULL!r})")

    def checksum_ok(self) -> bool:
        return self.stored_checksum == self.computed_checksum

    def check_flash_safe(self) -> None:
        """Refuse an image the boot loader would flash into the wrong place."""
        n = len(self.payload)
        if n > MAX_PAYLOAD:
            raise ImageError(
                f"payload {n:#x} bytes > {MAX_PAYLOAD:#x}: the updater would overwrite "
                f"the boot flag{' and the boot loader (brick)' if n > 0x780000 else ''}; "
                f"file must be <= {MAX_FILE_SIZE} bytes (docs/18_dump_analysis.md §3.3)")
        if self._data[KIND_OFFSET] != 0:
            raise ImageError(f"header kind byte 0x10 = {self._data[KIND_OFFSET]}, "
                             f"must be 0 (SYSTEM) for R24SYS.BIN")
        if self._data[CHECKSUM_FLAG_OFFSET] == 0:
            raise ImageError("header byte 0x30 = 0: the updater would skip the checksum")

    def verify(self) -> dict:
        """Full structural verification. Raises ImageError on failure."""
        self.check_flash_safe()
        if not self.checksum_ok():
            raise ImageError(
                f"checksum mismatch: header={self.stored_checksum:#010x} "
                f"computed={self.computed_checksum:#010x}")
        if self.version and not self.version[0].isdigit():
            raise ImageError(f"implausible version string: {self.version!r}")
        return {
            "size": self.size,
            "payload_size": len(self.payload),
            "version": self.version,
            "magic": self.magic.decode("ascii", "replace"),
            "checksum": f"{self.stored_checksum:#010x}",
            "checksum_ok": True,
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }

    # ---- mutation -----------------------------------------------------
    def set_version(self, version: str) -> None:
        raw = version.encode("ascii")
        if len(raw) > VERSION_SIZE:
            raise ImageError(f"version too long ({len(raw)} > {VERSION_SIZE})")
        field = raw.ljust(VERSION_SIZE, b"\x00")
        self._data[VERSION_OFFSET:VERSION_OFFSET + VERSION_SIZE] = field

    def set_ui_version(self, version: str) -> None:
        """Set the string the device shows on its LCD at boot.

        This is a different copy than set_version(). The header field is read
        by the updater; this one is read by the UI. Keep them in sync unless
        you specifically want the two to disagree.
        """
        raw = version.encode("ascii")
        if len(raw) > UI_VERSION_SIZE:
            raise ImageError(
                f"UI version too long ({len(raw)} > {UI_VERSION_SIZE}); "
                f"the field abuts the descriptor array at {UI_VERSION_OFFSET:#x}")
        field = raw.ljust(UI_VERSION_SIZE, b"\x00")
        self._data[UI_VERSION_OFFSET:UI_VERSION_OFFSET + UI_VERSION_SIZE] = field

    @property
    def ui_version(self) -> str:
        raw = bytes(self._data[UI_VERSION_OFFSET:
                               UI_VERSION_OFFSET + UI_VERSION_SIZE])
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace")

    def refresh_checksum(self) -> None:
        """Recompute the header checksum after mutating the payload."""
        struct.pack_into("<I", self._data, CHECKSUM_OFFSET,
                         compute_checksum(self.payload))

    def patch(self, offset: int, expected: bytes, replacement: bytes) -> None:
        """Surgical patch. `expected` guards against patching the wrong build."""
        if len(expected) != len(replacement):
            raise ImageError(
                f"patch must preserve length ({len(expected)} -> {len(replacement)})")
        actual = bytes(self._data[offset:offset + len(expected)])
        if actual != expected:
            raise ImageError(
                f"patch precondition failed at {offset:#x}: "
                f"expected {expected!r}, found {actual!r}")
        self._data[offset:offset + len(replacement)] = replacement

    def write(self, path) -> None:
        self.check_flash_safe()
        with open(path, "wb") as fh:
            fh.write(self._data)

    # ---- reporting ----------------------------------------------------
    def regions(self) -> list[dict]:
        """Segment the payload into code / erased-flash-gap regions."""
        out = []
        pay = self.payload
        in_gap = False
        start = 0
        for i in range(0, len(pay), 0x1000):
            blk = pay[i:i + 0x1000]
            erased = blk and blk.count(0xFF) == len(blk)
            if erased != in_gap:
                if i > 0:
                    out.append(self._region(start, i, in_gap))
                start, in_gap = i, erased
        out.append(self._region(start, len(pay), in_gap))
        return out

    @staticmethod
    def _region(start: int, end: int, erased: bool) -> dict:
        return {
            "name": "erased-flash gap" if erased else "image contents",
            "off_start": HEADER_SIZE + start,
            "off_end": HEADER_SIZE + end,
            "va_start": off_to_va(HEADER_SIZE + start),
            "va_end": off_to_va(HEADER_SIZE + end),
            "size": end - start,
            "kind": "fill-ff" if erased else "data",
        }
