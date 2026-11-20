#!/usr/bin/env python3
"""End-to-end proof: extract a function, edit its assembly by hand, put it back.

This is the deliverable the whole exercise was for: a human edits ASSEMBLY and
produces a firmware image. The test does a real, verifiable edit -- it changes
an ARM `mov r0, r0` (nop) into an `add r0, r0, #0` ... no: it makes a change
whose effect is *observable in the bytes*, then rebuilds and re-verifies the
container.

The edit used here is deliberately trivial and safe: it rewrites the crt0
delay-loop counter constant. It proves the pipeline (extract -> edit ->
assemble -> splice -> re-seal -> verify) without pretending to implement a
feature.

Run:  python3 tools/test_asm_edit.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import r24image
import r24patch
from r24image import R24Image

REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, 'R24SYS.BIN')

FAILS = []


def check(name, cond, detail=''):
    print(f'  [{"PASS" if cond else "FAIL"}] {name}{"  " + detail if detail else ""}')
    if not cond:
        FAILS.append(name)


def main():
    img = R24Image.from_file(SRC)
    print(f'source: {SRC}')
    print()

    # --- 1. extract a real Thumb function -------------------------------
    va, size, thumb = 0x02080840, 80, True
    print(f'1. extract function at {va:#x} ({size} bytes, Thumb)')
    src = r24patch.disassemble_function(img, va, size, thumb)
    original = r24patch.function_bytes(img, va, size)
    check('disassembly produced text', len(src) > 100, f'{len(src)} chars')
    check('contains real mnemonics', 'push' in src and 'ldr' in src)

    # --- 2. re-assemble unmodified: must be byte-identical --------------
    print('\n2. reassemble unmodified')
    out = r24patch.assemble(src, va, thumb, size)
    check('unmodified reassembly is byte-identical', out == original,
          f'{len(out)}B vs {len(original)}B')

    # --- 3. make a real edit -------------------------------------------
    # `movs r4, #0` at 0x02080846 -> `movs r4, #1`. One nibble changes, the
    # instruction length is identical, so the function still fits.
    print('\n3. apply a human edit (movs r4,#0 -> movs r4,#1)')
    edited = src.replace('  movs r4, #0', '  movs r4, #1', 1)
    check('edit applied to source', edited != src)
    new = r24patch.assemble(edited, va, thumb, size)
    check('edited assembly still fits', len(new) <= size, f'{len(new)}B')
    check('edited code differs from original', new != original)

    # --- 4. splice into the image and re-seal --------------------------
    print('\n4. splice into the image and re-seal the container')
    patched = R24Image(img.data)
    r24patch.splice(patched, va, new, size)
    patched.refresh_checksum()
    patched.verify()
    check('patched image verifies', patched.checksum_ok())

    # --- 5. confirm the change landed exactly where intended -----------
    print('\n5. confirm the change landed where intended')
    off = va - r24image.LOAD_ADDR + r24image.HEADER_SIZE
    before = img.data[off:off + size]
    after = patched.data[off:off + size]
    diff = [i for i in range(size) if before[i] != after[i]]
    # `movs r4,#0` -> `movs r4,#1` only changes the imm8 field (one byte).
    check('exactly one byte changed', len(diff) == 1,
          f'{len(diff)} byte(s) differ at {[hex(off + d) for d in diff[:4]]}')
    check('the new halfword encodes movs r4,#1',
          after[0x06:0x08] == b'\x01\x24', after[0x06:0x08].hex())
    # bytes outside the function must be untouched
    a, b = img.data, patched.data
    hsize = r24image.HEADER_SIZE
    tail_start = off + size
    # compare the PAYLOAD only: the header's checksum is expected to change
    head_same = a[hsize:off] == b[hsize:off]
    tail_same = a[tail_start:] == b[tail_start:]
    hdr_diff = [i for i in range(r24image.HEADER_SIZE) if a[i] != b[i]]
    check('payload outside the function is untouched', head_same and tail_same,
          'head+tail identical')
    # The checksum is a byte sum, so only the low byte usually moves; the
    # guarantee is that no *other* header byte changed.
    check('header changes confined to the checksum field',
          all(0x34 <= i < 0x38 for i in hdr_diff),
          str([hex(i) for i in hdr_diff]))

    # --- 6. write it and verify with the CLI ---------------------------
    print('\n6. write the artifact and verify with the standalone tool')
    outdir = os.path.join(REPO, 'build-asm')
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, 'R24SYS.BIN')
    patched.write(outpath)
    r = subprocess.run([sys.executable, os.path.join(HERE, 'r24tool'),
                        'verify', outpath], capture_output=True, text=True)
    check('r24tool verify accepts the edited image', r.returncode == 0,
          r.stdout.splitlines()[0] if r.stdout else r.stderr[:80])
    check('artifact size unchanged', os.path.getsize(outpath) == img.size)

    print()
    if FAILS:
        print(f'FAILED: {len(FAILS)} check(s): {", ".join(FAILS)}')
        return 1
    print('ALL CHECKS PASSED')
    print(f'artifact: {outpath}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
