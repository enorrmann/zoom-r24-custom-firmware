#!/usr/bin/env python3
"""r24emu.py — run R24 firmware code (and SDK patches) in an ARM926 emulator.

The image is laid out in memory exactly as the boot code does it:

    RAM 0x80000000  <- file 0x5B4 .. 0x1EE910   (code + data), then .bss (zero)
    RAM 0x87540000  <- the PCM bank (file 0x220040 .. EOF), 5 MiB window

Firmware code runs for real. Anything that touches hardware or the file
system is replaced by a Python *stub*: when execution reaches a stubbed
address, the stub receives the argument registers and its return value goes
to r0. File stubs (fopen/fread/fclose/feof) backed by a dict of virtual files
are installed by default — `emu.files["A:\\\\PATH\\\\FILE.WAV"] = bytes`.

    emu = R24Emu(open("build/x.BIN", "rb").read())
    emu.stub(0x8001D212, lambda e, a: e.cstring_at(...))   # your own stubs
    r0 = emu.call(0x8001D6A6, 7)                           # call any function

Needs `pip install unicorn` (2.x).
"""
from __future__ import annotations

import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                               UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR,
                               UC_ARM_REG_PC, UC_CPU_ARM_926)

DATA_RAM, DATA_FILE, DATA_LEN = 0x80000000, 0x5B4, 0x1EE910 - 0x5B4
BSS_END = 0x812DA28C
BANK_RAM, BANK_FILE, BANK_WINDOW = 0x87540000, 0x220040, 0x500000
RET = 0x90000000                    # return address of call(): stops the run
STACK_TOP = 0x91000000
SCRATCH = 0x92000000                # 1 MiB for strings/buffers from Python

FW_FOPEN, FW_FREAD, FW_FCLOSE, FW_FEOF = 0x80054E54, 0x80054EA2, 0x80054D56, 0x80054DEC
FW_FWRITE, FW_REMOVE = 0x80054FD2, 0x800550C6
FW_DCACHE = (0x800C4028, 0x800C4068, 0x800C408C, 0x800C40B0)   # sync/inv/flush/clean


class EmuError(Exception):
    pass


class R24Emu:
    def __init__(self, image: bytes, file_stubs: bool = True, cache_stubs: bool = True):
        uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        uc.ctl_set_cpu_model(UC_CPU_ARM_926)
        uc.mem_map(DATA_RAM, (BSS_END - DATA_RAM + 0xFFFF) & ~0xFFFF)
        uc.mem_write(DATA_RAM, image[DATA_FILE:DATA_FILE + DATA_LEN])
        uc.mem_map(BANK_RAM, BANK_WINDOW)
        uc.mem_write(BANK_RAM, image[BANK_FILE:])
        uc.mem_map(0, 0x10000)                       # low vectors (strcpy(NULL) etc.)
        uc.mem_map(RET, 0x10000)
        uc.mem_map(STACK_TOP - 0x100000, 0x100000)
        uc.mem_map(SCRATCH, 0x100000)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._fault)
        self.uc = uc
        self.stubs: dict[int, callable] = {}
        self._scratch = SCRATCH
        self.files: dict[str, bytes] = {}
        self.opened: list[str] = []
        self.handles: dict[int, list] = {}
        self._next_handle = 0x100
        self.cache_ops: list[tuple[int, int, int]] = []
        self.stub(RET, None)
        if file_stubs:
            self.stub(FW_FOPEN, R24Emu._fopen)
            self.stub(FW_FREAD, R24Emu._fread)
            self.stub(FW_FCLOSE, R24Emu._fclose)
            self.stub(FW_FEOF, R24Emu._feof)
            self.stub(FW_FWRITE, R24Emu._fwrite)
            self.stub(FW_REMOVE, R24Emu._remove)
        self.removed: list[str] = []
        if cache_stubs:                              # cp15 ops: record, do nothing
            for a in FW_DCACHE:
                self.stub(a, lambda e, r, a=a: e.cache_ops.append((a, r[0], r[1])) or 0)

    # ── stubs and calls ──
    def stub(self, addr: int, fn) -> None:
        """Run `fn(emu, [r0..r3])` instead of the code at `addr`; its result -> r0."""
        addr &= ~1
        if addr not in self.stubs:
            self.uc.hook_add(UC_HOOK_CODE, self._hit, None, addr, addr)
        self.stubs[addr] = fn

    def _hit(self, uc, addr, size, _):
        fn = self.stubs.get(addr)
        if addr == RET:
            uc.emu_stop()
            return
        if fn is None:
            return
        r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                                      UC_ARM_REG_R2, UC_ARM_REG_R3)]
        v = fn(self, r)
        uc.reg_write(UC_ARM_REG_R0, (v or 0) & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    def _fault(self, uc, access, addr, size, value, _):
        raise EmuError(f"unmapped access 0x{addr:08X} at pc 0x{uc.reg_read(UC_ARM_REG_PC):08X}")

    def call(self, addr: int, *args: int, thumb: bool = True, limit: int = 500_000_000) -> int:
        """Call the function at `addr` (Thumb unless thumb=False) with up to 4 args."""
        uc = self.uc
        for reg, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            uc.reg_write(reg, v & 0xFFFFFFFF)
        uc.reg_write(UC_ARM_REG_SP, STACK_TOP - 0x100)
        uc.reg_write(UC_ARM_REG_LR, RET | 1)
        uc.emu_start((addr & ~1) | (1 if thumb else 0), RET, count=limit)
        r0 = uc.reg_read(UC_ARM_REG_R0)
        return r0 - (1 << 32) if r0 >> 31 else r0

    # ── memory helpers ──
    def read(self, addr: int, n: int) -> bytes:
        return bytes(self.uc.mem_read(addr, n))

    def write(self, addr: int, b: bytes) -> None:
        self.uc.mem_write(addr, b)

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def cstring(self, addr: int, n: int = 256) -> str:
        return self.read(addr, n).split(b"\0")[0].decode("latin-1")

    def alloc(self, b: bytes) -> int:
        """Copy bytes into scratch memory; returns their address."""
        a = self._scratch
        self.write(a, b)
        self._scratch += (len(b) + 8) & ~3
        return a

    # ── virtual SD card ──
    def _fopen(self, r):
        path, mode = self.cstring(r[0]), self.cstring(r[1])
        self.opened.append(path)
        if mode.startswith("w"):                     # create / truncate
            self._next_handle += 1
            self.handles[self._next_handle] = [bytearray(), 0, path.upper()]
            return self._next_handle
        data = self.files.get(path.upper())
        if data is None:
            return 0
        self._next_handle += 1
        self.handles[self._next_handle] = [data, 0]
        return self._next_handle

    def _fwrite(self, r):
        buf, size, n, h = r
        ent = self.handles[h]
        ent[0] += self.read(buf, size * n)
        return n

    def _remove(self, r):
        path = self.cstring(r[0]).upper()
        self.removed.append(path)
        return 0 if self.files.pop(path, None) is not None else -1

    def _fread(self, r):
        buf, size, n, h = r
        data, pos = self.handles[h]
        chunk = data[pos:pos + size * n]
        chunk = chunk[:len(chunk) // size * size]
        self.write(buf, chunk)
        self.handles[h][1] += len(chunk)
        return len(chunk) // size

    def _fclose(self, r):
        ent = self.handles.pop(r[0], None)
        if ent is not None and len(ent) == 3:        # a file opened for writing
            self.files[ent[2]] = bytes(ent[0])
        return 0

    def _feof(self, r):
        data, pos = self.handles[r[0]]
        return 1 if pos >= len(data) else 0
