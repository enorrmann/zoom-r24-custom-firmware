"""Human-facing workflow: extract, edit and re-insert real functions.

The point of everything else in this toolchain is this file: a human edits
assembly for one function and gets a firmware image back. The rules that make
it safe are enforced here, not left to the operator.

Why patching a function in place (and not re-linking the image) is the only
option
--------------------------------------------------------------------------
The payload is position-dependent and has no relocation table: every literal
pool holds absolute addresses. Relinking the whole image would move functions
and silently break thousands of literals. So a modification must be spliced
back at the *same* virtual address and occupy *no more* bytes than the
original.

Execution address. The reset code copies file 0x5B4..0x1EE910 to RAM
0x80000000 and runs it there (docs/10_loop_import.md §0). Branches are
PC-relative, so assembling at the load VA (0x0208xxxx) gives the right bytes
for branches inside the image; but any *absolute* address the new code
produces (a function pointer, `ldr rX,=label`) must be a RAM address
(file - 0x5B4 + 0x80000000), not a VA.

There is no in-image room for code that does not fit in place: the erased
0xFF region (file 0x1EE950..0x220040) is never copied to RAM, so a branch into
it lands in zeroed bss at runtime. See trampoline_patch().
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

import r24image
from r24image import R24Image, HEADER_SIZE, LOAD_ADDR

MARCH = "armv5t"
GAP_START = 0x0226E910
GAP_END = 0x022A0000


class PatchError(Exception):
    pass


def function_bytes(img: R24Image, va: int, size: int) -> bytes:
    off = va - LOAD_ADDR + HEADER_SIZE
    blob = img.payload[off - HEADER_SIZE: off - HEADER_SIZE + size]
    if len(blob) != size:
        raise PatchError(f"function at {va:#x} runs past the end of the image")
    return blob


def assemble(source: str, vma: int, thumb: bool, size: int) -> bytes:
    """Assemble `source` and require it to fit exactly in `size` bytes.

    `EXT<addr>` symbols (branch targets outside the function) are placed at
    their real addresses by the generated linker script, exactly as the bulk
    reassembler does, so calls keep their original offsets.
    """
    header = [".syntax unified", ".thumb" if thumb else ".arm", ".text"]
    text = "\n".join(header) + "\n" + source
    ext = sorted({int(m, 16) for m in re.findall(r"\bEXT([0-9a-fA-F]{8})\b", source)})
    for a in ext:
        text += f"\n.section .far{a:08x},\"ax\",%progbits\nEXT{a:08x}:\n  nop\n"
    ld = f"SECTIONS\n{{\n  .text 0x{vma:08x} : {{ *(.text) }}\n"
    for a in ext:
        ld += f"  .far{a:08x} 0x{a:x} : {{ *(.far{a:08x}) }}\n"
    ld += "}\n"
    with tempfile.TemporaryDirectory() as td:
        p = lambda n: os.path.join(td, n)
        open(p("a.s"), "w").write(text)
        open(p("a.ld"), "w").write(ld)
        r = subprocess.run(["arm-none-eabi-as", f"-march={MARCH}", p("a.s"), "-o", p("a.o")],
                           capture_output=True, text=True)
        if r.returncode:
            errs = [l for l in r.stderr.splitlines() if "Error" in l]
            raise PatchError("assembly failed:\n  " + "\n  ".join(errs[:6]))
        r = subprocess.run(["arm-none-eabi-ld", "-T", p("a.ld"), p("a.o"), "-o", p("a.elf")],
                           capture_output=True, text=True)
        if r.returncode:
            raise PatchError("link failed: " + r.stderr.strip()[:300])
        subprocess.run(["arm-none-eabi-objcopy", "-O", "binary", "-j", ".text",
                        p("a.elf"), p("a.bin")], capture_output=True, text=True)
        out = open(p("a.bin"), "rb").read() if os.path.exists(p("a.bin")) else b""
    if len(out) > size:
        raise PatchError(
            f"new function is {len(out)} bytes but only {size} are available at "
            f"{vma:#x}; the image cannot be grown (no relocation table). "
            f"Shorten the code.")
    return out


def splice(img: R24Image, va: int, code: bytes, original_size: int) -> None:
    """Write `code` over the function, NOP-filling any leftover bytes."""
    if len(code) > original_size:
        raise PatchError("code larger than the original function")
    off = va - LOAD_ADDR + HEADER_SIZE
    body = bytearray(code)
    # pad the remainder with a run of no-op instructions so control flow that
    # falls through into the pad still executes something harmless
    pad = original_size - len(code)
    if pad:
        if va & 1:
            nop = b"\xc0\x46"          # Thumb-1: mov r8, r8 (0x46C0), the NOP
                                       # the firmware itself uses; nop.n
                                       # (0xBF00) is Thumb-2, undefined here
        else:
            nop = (0xE1A00000).to_bytes(4, "little")   # ARM: mov r0, r0
        body += (nop * (pad // len(nop) + 1))[:pad]
    img._data[off:off + original_size] = body


def trampoline_patch(img: R24Image, va: int, thumb: bool, code: bytes,
                     original_size: int) -> int:
    """DISABLED. Was: place `code` in the erased 0xFF region and branch to it.

    That region (file 0x1EE950..0x220040) is not copied to RAM by the reset
    code, which copies only file 0x5B4..0x1EE950 to 0x80000000/0x8000 and then
    zeroes the bss that starts right where the code copy ends. A branch from
    running code (RAM 0x80xxxxxx) into the "gap" lands at RAM 0x801EE39C+,
    i.e. in zeroed bss: the device would crash. Code that needs more room has
    to live in a region that IS copied to RAM, e.g. the PCM bank tail
    (RAM 0x87540000+), reached through a long-call veneer — see
    docs/10_loop_import.md §5.
    """
    raise PatchError(
        "trampoline_patch() is disabled: the 0xFF region is not loaded into "
        "RAM at boot, so code placed there would never exist at runtime "
        "(docs/10_loop_import.md §0)")


def _branch_bytes(va: int, target: int, thumb: bool) -> bytes:
    src = f"{'  b' if not thumb else '  b'} #0x{target:x}\n"
    return assemble(src, va, thumb, 8 if thumb else 4)


def disassemble_function(img: R24Image, va: int, size: int, thumb: bool) -> str:
    import r24asm
    blob = function_bytes(img, va, size)
    return "\n".join(r24asm.to_assembly(blob, va, thumb)) + "\n"


def load_annotations(path: str) -> dict[str, str]:
    """Read an optional names file: `0xADDR name` per line."""
    names = {}
    if not path or not os.path.exists(path):
        return names
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"(0x[0-9a-fA-F]+)\s+(\S+)", line)
            if m:
                names[int(m.group(1), 16)] = m.group(2)
    return names
