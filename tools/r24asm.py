"""Assemble / disassemble ARM+Thumb functions for the R24 image.

Design notes (learned the hard way, recorded for the next maintainer)
---------------------------------------------------------------------
* The image mixes ARM and Thumb state **per function**, so a linear sweep in
  one mode will run through the other and produce nonsense. The function map
  produced by Ghidra (`functions.csv`) is the source of truth for the mode of
  each function; without it the output is garbage.
* GAS may pick an equivalent-but-different encoding (`adds r2,r2,#4` is
  `0x1d12` originally, `0x3204` from GAS). Since we must reproduce bytes
  exactly, any instruction whose assembled bytes differ from the original is
  re-emitted verbatim as `.inst`. This is a *self-correcting* loop, not a
  guess: it converges because forcing is monotonic.
* A Thumb `bl` to an address outside the assembled range cannot be encoded
  from an absolute symbol (GAS emits an ARM interworking stub). Those targets
  get a label placed at their exact address by a generated linker script,
  which requires assemble + **link**, not just assemble.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB
from capstone import CS_MODE_LITTLE_ENDIAN as LE
from capstone.arm import ARM_OP_IMM

MARCH = "armv5t"          # the image uses BLX, which armv4t cannot encode


class ToolchainError(Exception):
    pass


def _csv_functions(path: str) -> list[dict]:
    out = []
    with open(path) as fh:
        next(fh, None)
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 4:
                continue
            out.append({
                "addr": int(parts[0], 16),
                "is_thumb": parts[1] == "1",
                "size": int(parts[2]),
                "name": parts[3],
            })
    return out


def _decode(blob: bytes, vma: int, thumb: bool, cs_cache={}):
    """Decode linearly, stepping one halfword/word past undecodable data."""
    key = thumb
    if key not in cs_cache:
        cs = Cs(CS_ARCH_ARM, (CS_MODE_THUMB if thumb else CS_MODE_ARM) | LE)
        cs.detail = True
        cs_cache[key] = cs
    cs = cs_cache[key]
    step = 2 if thumb else 4
    out, p, n = [], 0, len(blob)
    while p + step <= n:
        got = list(cs.disasm(blob[p:p + 4], vma + p))
        if got:
            i = got[0]
            # Reject decodings that only fit by reading past the end, and
            # reject Thumb-2/NEON on an ARMv4T/v5 target: those are data that
            # happen to look like instructions.
            plausible = (len(i.bytes) >= step and p + len(i.bytes) <= n
                         and not _is_armv7_only(i))
            if plausible:
                out.append((i, p))
                p += len(i.bytes)
                continue
        out.append((None, p))
        p += step
    while p < n:
        out.append((None, p))
        p += 1
    return out


_ARMV7_MNEMONICS = ("v",)          # NEON: vadd, vshr, vrhadd, vldr, vmov ...


def _is_armv7_only(insn) -> bool:
    """True for instructions this CPU cannot execute (=> bytes are data)."""
    m = insn.mnemonic
    if m.startswith(_ARMV7_MNEMONICS):
        return True
    for suffix in (".w", ".n"):
        if m.endswith(suffix):
            base = m[: -len(suffix)]
            # .w/.n width suffixes are Thumb-2; a genuine ARMv4T/v5 image
            # should not need them, and when they appear at a Thumb address
            # the bytes are usually data.
            if suffix == ".w":
                return True
    return False


def _split_ops(ops: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in ops:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


IMAGE_LO = 0x02080000
IMAGE_HI = 0x02756634


def _render2(insn, blob, pos, vma, addrs, far, thumb):
    """Like _render, but returns None when the instruction is implausible."""
    return _render(insn, blob, pos, vma, addrs, far, thumb)


def _render(insn, blob, pos, vma, addrs, far, thumb):
    parts = _split_ops(insn.op_str)
    out = []
    for k, piece in enumerate(parts):
        if k < len(insn.operands):
            o = insn.operands[k]
            if o.type == ARM_OP_IMM and piece.startswith("#"):
                if o.imm in addrs:
                    out.append(f"L{o.imm:08x}")
                    continue
                if insn.mnemonic.startswith("b"):
                    if not (IMAGE_LO <= o.imm < IMAGE_HI):
                        # Not inside the mapped image => not a real call; the
                        # bytes are data. Render as data so the caller's
                        # mismatch/force loop pins them exactly.
                        return None
                    far.add(o.imm)
                    out.append(f"EXT{o.imm:08x}")
                    continue
        out.append(piece)
    return f"  {insn.mnemonic} {', '.join(out)}".rstrip()


def _force_bytes(blob: bytes, pos: int, length: int, thumb: bool) -> str:
    """Emit the original bytes verbatim, bypassing assembler choice."""
    if thumb:
        hw = [int.from_bytes(blob[pos + k:pos + k + 2], "little")
              for k in range(0, length, 2)]
        if len(hw) == 1:
            return f"  .short 0x{hw[0]:04x}"
        # `.inst.n` only accepts 16 bits, so a 32-bit Thumb-2 encoding must be
        # emitted as its two halfwords in file order. This is unconditional and
        # therefore always exact.
        return "\n".join(f"  .short 0x{h:04x}" for h in hw)
    w = int.from_bytes(blob[pos:pos + length], "little")
    return f"  .inst 0x{w:08x}"


def _build(blob, vma, thumb, forced):
    """Assemble + link one function. Returns (bytes|None, err, layout)."""
    items = _decode(blob, vma, thumb)
    addrs = {i.address for i, _ in items if i is not None}
    far: set[int] = set()
    lines, layout = [], []
    lineno = 6                        # 5 header lines, body starts at 6
    for insn, pos in items:
        addr = vma + pos
        lines.append(f"L{addr:08x}:")
        lineno += 1
        iline = lineno
        if insn is None:
            nbytes = 2 if thumb else 4
            nbytes = min(nbytes, len(blob) - pos)
            if pos in forced:
                # A rejected data line is already verbatim; the only way an
                # assembler can still object is the directive itself, so fall
                # back to individual bytes.
                for k in range(nbytes):
                    lines.append(f"  .byte 0x{blob[pos+k]:02x}")
            elif thumb:
                lines.append(f"  .short 0x{int.from_bytes(blob[pos:pos+2], 'little'):04x}")
            else:
                lines.append(f"  .word 0x{int.from_bytes(blob[pos:pos+4], 'little'):08x}")
            layout.append((pos, nbytes, None, iline))
        elif pos in forced:
            forced_text = _force_bytes(blob, pos, len(insn.bytes), thumb)
            nlines = forced_text.count("\n") + 1
            lines.append(forced_text)
            layout.append((pos, len(insn.bytes), insn, iline))
            lineno += nlines - 1
        else:
            rendered = _render2(insn, blob, pos, vma, addrs, far, thumb)
            ok = rendered is not None
            if not ok:
                # implausible branch target -> emit the original bytes
                rendered = _force_bytes(blob, pos, len(insn.bytes), thumb)
                forced.add(pos)
                lineno += rendered.count("\n")
            lines.append(rendered)
            layout.append((pos, len(insn.bytes), insn, iline))
        lineno += 1

    # Only targets OUTSIDE the assembled range need a placed label; anything
    # inside already has one, and aliasing it would make the linker fail.
    fva = vma + len(blob)
    far = {a for a in far if not (vma <= a < fva)}
    src = [".syntax unified", ".thumb" if thumb else ".arm", ".text",
           ".global _start", "_start:"] + lines
    for a in sorted(far):
        src += [f'.section .far{a:08x},"ax",%progbits', f"EXT{a:08x}:", "  nop"]
    ld = ["SECTIONS", "{", f"  .text 0x{vma:08x} : {{ *(.text) }}"]
    for a in sorted(far):
        ld.append(f"  .far{a:08x} 0x{a:x} : {{ *(.far{a:08x}) }}")
    ld.append("}")

    with tempfile.TemporaryDirectory() as td:
        p = lambda n: os.path.join(td, n)
        open(p("a.s"), "w").write("\n".join(src) + "\n")
        open(p("a.ld"), "w").write("\n".join(ld) + "\n")
        r = subprocess.run(["arm-none-eabi-as", f"-march={MARCH}", p("a.s"), "-o", p("a.o")],
                           capture_output=True, text=True)
        if r.returncode:
            bad = set()
            for ln in r.stderr.splitlines():
                if "Error" in ln and "a.s:" in ln:
                    try:
                        bad.add(int(ln.split("a.s:")[1].split(":")[0]))
                    except (IndexError, ValueError):
                        pass
            return None, ("AS", bad), layout
        r = subprocess.run(["arm-none-eabi-ld", "-T", p("a.ld"), p("a.o"), "-o", p("a.elf")],
                           capture_output=True, text=True)
        if r.returncode:
            return None, ("LD", r.stderr[:200]), layout
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text",
                        p("a.elf"), p("a.bin")], capture_output=True, text=True)
        return open(p("a.bin"), "rb").read(), None, layout


def reassemble_function(blob: bytes, vma: int, thumb: bool, rounds: int = 60):
    """Rebuild `blob` from disassembly. Returns (bytes, stats)."""
    forced: set[int] = set()
    for _ in range(rounds):
        got, err, layout = _build(blob, vma, thumb, forced)
        if got is not None and got == blob:
            readable = sum(1 for p, _l, i, _li in layout if i is not None and p not in forced)
            return got, {"forced": len(forced), "readable": readable}
        if got is None:
            if not (isinstance(err, tuple) and err[0] == "AS"):
                raise ToolchainError(str(err))
            added = 0
            for pos, _l, insn, iline in layout:
                if iline in err[1] and pos not in forced:
                    forced.add(pos)
                    added += 1
            if not added:
                raise ToolchainError("assembler rejected lines that cannot be forced")
            continue
        added = 0
        for pos, ln, insn, _li in layout:
            if insn is None or pos in forced:
                continue
            if got[pos:pos + ln] != blob[pos:pos + ln]:
                forced.add(pos)
                added += 1
        if not added:
            raise ToolchainError("encoding mismatch that forcing cannot fix")
    raise ToolchainError(f"did not converge in {rounds} rounds")


def to_assembly(blob: bytes, vma: int, thumb: bool, label_prefix: str = "L"):
    """Human-readable assembly for one function (no forcing, for reading)."""
    items = _decode(blob, vma, thumb)
    addrs = {i.address for i, _ in items if i is not None}
    lines = []
    for insn, pos in items:
        addr = vma + pos
        if insn is None:
            if thumb:
                lines.append(f"{label_prefix}{addr:08x}:  .short 0x{int.from_bytes(blob[pos:pos+2], 'little'):04x}")
            else:
                lines.append(f"{label_prefix}{addr:08x}:  .word 0x{int.from_bytes(blob[pos:pos+4], 'little'):08x}")
            continue
        far = set()
        lines.append(f"{label_prefix}{addr:08x}:")
        lines.append("  " + _render(insn, blob, pos, vma, addrs, far, thumb).strip())
    return lines
