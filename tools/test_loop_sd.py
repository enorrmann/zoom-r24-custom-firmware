#!/usr/bin/env python3
"""test_loop_sd.py — prove tools/loop_sd.py changes exactly what docs/10 says.

--replace on R24SYS.BIN:
  * exactly the 3 call sites, the path and the label change (+ checksum);
  * the patched words decode as `movs r0,#0; nop` / `nop; nop`.

--add on a relocated (160-entry) image:
  * the copy of the callback, disassembled at its new address, is
    instruction-for-instruction the original except: the 3 USB calls
    (neutralised) and every outgoing bl/blx, which now reaches a veneer
    whose stored target is the original target (Thumb |1, ARM even);
  * every branch inside the copy stays inside it; every literal/adr target
    is inside the copied block and holds the original bytes (path changed);
  * veneers are 4-aligned `bx pc; nop; ldr pc,[pc,#-4]; .word`;
  * item record = LOOP[USB]'s with new label + callback; pointer array rows
    hold the builder's 4 pointers + the new item;
  * builder: count 5, both strides #5, literal -> array; nothing else in the
    code region changes; the sample table still locates, every kit slot plays
    the same audio, and a samples repack keeps the extension intact.

    python3 tools/test_loop_sd.py
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile

from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import loop_sd as L  # noqa: E402
import r24bank  # noqa: E402
import r24image  # noqa: E402

PY = sys.executable
IMAGE = os.path.join(ROOT, "R24SYS.BIN")
passed = failed = 0


def check(name, cond):
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}")
    passed += bool(cond)
    failed += not cond


def run(*a):
    return subprocess.run([PY, *a], capture_output=True, text=True, cwd=ROOT)


def diff_ranges(a, b):
    out, s = [], None
    for i in range(len(a)):
        if a[i] != b[i]:
            if s is None:
                s = i
        elif s is not None:
            out.append((s, i)); s = None
    if s is not None:
        out.append((s, len(a)))
    return out


def main() -> int:
    stock = open(IMAGE, "rb").read()
    tmp = tempfile.mkdtemp(prefix="loopsd_")
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)

    # ═══════════════════════════════════════════════════════════════════════
    print("--replace")
    rep = os.path.join(tmp, "rep.BIN")
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), IMAGE, "--replace", "-o", rep)
    check("exits 0", r.returncode == 0)
    g = open(rep, "rb").read()
    check("checksum verifies", r24image.R24Image(g).checksum_ok())
    allowed = [(0x34, 0x38)] + [(L.r2f(a), L.r2f(a) + 4) for a, *_ in L.CALL_PATCHES]
    allowed += [(L.r2f(L.PATH_RAM), L.r2f(L.PATH_RAM) + 17),
                (L.r2f(L.LABEL_RAM), L.r2f(L.LABEL_RAM) + 12)]
    stray = [s for s, e in diff_ranges(stock, g)
             if not any(a <= s and e <= b for a, b in allowed)]
    check(f"only the documented bytes change ({len(stray)} stray ranges)", not stray)
    ins = [list(md.disasm(g[L.r2f(a):L.r2f(a) + 4], a)) for a, *_ in L.CALL_PATCHES]
    check("call sites decode as movs r0,#0;nop / nop;nop",
          [[i.mnemonic for i in x] for x in ins]
          == [["movs", "mov"], ["movs", "mov"], ["mov", "mov"]])
    check("path is A:\\ZOOM_R24\\LOOP",
          g[L.r2f(L.PATH_RAM):L.r2f(L.PATH_RAM) + 17] == L.NEW_PATH)
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), rep, "--replace", "-o",
            os.path.join(tmp, "x.BIN"))
    check("patching twice is refused", r.returncode != 0 and "error:" in r.stderr)

    # ═══════════════════════════════════════════════════════════════════════
    print("\n--add on a relocated image")
    b160 = os.path.join(tmp, "b160.BIN")
    run(os.path.join(ROOT, "tools", "r24bank.py"), "relocate", IMAGE, "--per-kit", "-o", b160)
    base_img = open(b160, "rb").read()
    add = os.path.join(tmp, "add.BIN")
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), b160, "--add", "-o", add)
    check("exits 0", r.returncode == 0)
    h = open(add, "rb").read()
    check("size unchanged, checksum verifies",
          len(h) == len(stock) and r24image.R24Image(h).checksum_ok())
    lay = r24bank.locate(h)
    check("bank still relocated, 160 entries, with an extension",
          lay.relocated and lay.count == 160 and lay.ext_size > 0)
    base = r24bank.RAM_BASE + lay.ext_rel + r24bank.EXT_HDR
    eoff = r24bank.BANK_BASE + lay.ext_rel + r24bank.EXT_HDR
    check("extension starts with LOOPSD01", h[eoff:eoff + 8] == L.EXT_TAG)

    def ram2file(ram):           # extension RAM -> file
        return ram - r24bank.RAM_BASE + r24bank.BANK_BASE

    code_ram = base + 0x370
    delta = code_ram - L.COPY_LO
    copy = h[ram2file(code_ram):ram2file(code_ram) + (L.COPY_HI - L.COPY_LO)]
    orig = stock[L.r2f(L.COPY_LO):L.r2f(L.COPY_HI)]
    check("copy keeps the original alignment mod 4", code_ram % 4 == L.COPY_LO % 4)

    # instruction walk over the callback
    patched = {a for a, *_ in L.CALL_PATCHES}
    bad, n_calls, n_ok_calls, n_branch_out = [], 0, 0, 0
    a = L.FUNC_LO
    while a < L.FUNC_HI:
        o = a - L.COPY_LO
        if 0x8004299C <= a < 0x80042A14:      # mid-function literal pool,
            a += 2                             # checked separately below
            continue
        blo = L.decode_bl(stock, L.r2f(a), a)
        if a in patched:
            a += 4
            continue
        if blo:
            t, blx = blo
            if L.COPY_LO <= t < L.COPY_HI:
                if copy[o:o + 4] != orig[o:o + 4]:
                    bad.append(a)
            else:
                n_calls += 1
                nb = L.decode_bl(copy, o, a + delta)
                if nb and not nb[1]:
                    vf = ram2file(nb[0])
                    v = h[vf:vf + 12]
                    word = struct.unpack_from("<I", v, 8)[0]
                    ok = (nb[0] % 4 == 0 and v[:8] == bytes.fromhex("7847c04604f01fe5")
                          and word == (t if blx else t | 1))
                    n_ok_calls += ok
            a += 4
            continue
        if copy[o:o + 2] != orig[o:o + 2]:
            bad.append(a)
        a += 2
    check(f"non-call instructions identical ({len(bad)} differ)", not bad)
    neut = [list(md.disasm(copy[x - L.COPY_LO:x - L.COPY_LO + 4], x + delta))
            for x, *_ in L.CALL_PATCHES]
    check("the 3 USB calls are neutralised IN THE COPY (movs r0,#0;nop / nop;nop)",
          [[i.mnemonic for i in n] for n in neut]
          == [["movs", "mov"], ["movs", "mov"], ["mov", "mov"]])
    check(f"all {n_calls} outgoing calls reach a correct veneer ({n_ok_calls})",
          n_calls > 0 and n_ok_calls == n_calls)
    # branches inside the copy stay inside the function
    for i in md.disasm(copy[L.FUNC_LO - L.COPY_LO:L.FUNC_HI - L.COPY_LO], L.FUNC_LO + delta):
        if i.mnemonic.startswith("b") and i.mnemonic not in ("bl", "blx", "bx", "bic", "bics") \
                and i.op_str.startswith("#"):
            t = int(i.op_str[1:], 16)
            if not L.FUNC_LO + delta <= t < L.FUNC_HI + delta:
                n_branch_out += 1
    check(f"no plain branch leaves the copied callback ({n_branch_out})", n_branch_out == 0)
    pools = (L.FUNC_HI - L.COPY_LO, len(orig))
    pool_ok = copy[pools[0]:] == orig[pools[0]:]
    mid = (0x8004299C - L.COPY_LO, 0x80042A14 - L.COPY_LO)
    path_o = L.PATH_RAM - L.COPY_LO
    mid_ok = (copy[mid[0]:path_o] == orig[mid[0]:path_o]
              and copy[path_o:path_o + 17] == L.NEW_PATH
              and copy[path_o + 17:mid[1]] == orig[path_o + 17:mid[1]])
    check("literal pools identical except the path (now A:)", pool_ok and mid_ok)

    # item + array
    item = h[ram2file(base + 0x310):ram2file(base + 0x310) + 80]
    usb_item_fields = (1, 0, 0x800D38DC)
    lbl, en, z, hnd, cb = struct.unpack_from("<5I", item, 0)
    check("item: label 'LOOP[SD] ', enabled, handler 0x800D38DC, callback = copy|1",
          h[ram2file(lbl):ram2file(lbl) + 9] == b"LOOP[SD] "
          and (en, z, hnd) == usb_item_fields
          and cb == (L.FUNC_LO + delta) | 1)
    rows_ok = all(
        struct.unpack_from("<8I", h, ram2file(base + 0x10) + t * 32)
        == tuple([0x811449DC + t * 320 + n * 80 for n in range(4)] + [base + 0x310, 0, 0, 0])
        for t in range(24))
    check("pointer array: 24 rows = builder's 4 + new item", rows_ok)

    # builder patches + nothing else in code/data changed
    check("builder: count 5, strides #5, literal -> array",
          h[L.r2f(0x800430A0):L.r2f(0x800430A0) + 2] == bytes.fromhex("0520")
          and h[L.r2f(0x80043090):L.r2f(0x80043090) + 2] == bytes.fromhex("6101")
          and h[L.r2f(0x800430DA):L.r2f(0x800430DA) + 2] == bytes.fromhex("6001")
          and struct.unpack_from("<I", h, L.r2f(L.PTR_LITERAL))[0] == base + 0x10)
    allowed = [(0x34, 0x38), (L.r2f(L.PTR_LITERAL), L.r2f(L.PTR_LITERAL) + 4)]
    allowed += [(L.r2f(a), L.r2f(a) + 2) for a, *_ in L.BUILDER_PATCHES]
    # the sample table moved down to make room: its two literals follow it
    allowed += [(o, o + 4) for o in r24bank.TABLE_LITERALS]
    stray = [s for s, e in diff_ranges(base_img[:r24bank.BANK_BASE], h[:r24bank.BANK_BASE])
             if not any(a <= s and e <= b for a, b in allowed)]
    check(f"code/data region: only the 4 builder words + 2 table literals change "
          f"({len(stray)} stray)", not stray)
    check("table literals point at the moved table",
          all(struct.unpack_from("<I", h, o)[0] == lay.literal
              for o in r24bank.TABLE_LITERALS))
    check("LOOP[USB] itself untouched",
          h[L.r2f(L.COPY_LO):L.r2f(L.COPY_HI)] == orig)

    # audio unchanged, repack keeps the extension
    t0, t1 = r24bank.read_table(base_img), r24bank.read_table(h)
    same = all(r24bank.sample_bytes(base_img, t0, i)[:t1[i][1]]
               == r24bank.sample_bytes(h, t1, i)
               and not any(r24bank.sample_bytes(base_img, t0, i)[t1[i][1]:])
               for i in range(160))
    check("every entry plays the same audio (only trailing silence cut)", same)
    ex = os.path.join(tmp, "ex")
    run(os.path.join(ROOT, "tools", "patch_samples.py"), add, "--extract", "-o", ex)
    rp = os.path.join(tmp, "rp.BIN")
    r = run(os.path.join(ROOT, "tools", "patch_samples.py"), add,
            os.path.join(ex, "samples.txt"), "-o", rp, "--repack")
    k = open(rp, "rb").read() if r.returncode == 0 else b""
    check("samples --repack keeps the extension byte-identical",
          r.returncode == 0
          and k[r24bank.BANK_BASE + lay.ext_rel:] == h[r24bank.BANK_BASE + lay.ext_rel:]
          and k[0x38:r24bank.BANK_BASE] == h[0x38:r24bank.BANK_BASE])
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), add, "--add", "-o",
            os.path.join(tmp, "y.BIN"))
    check("adding twice is refused", r.returncode != 0)
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), IMAGE, "--add", "-o",
            os.path.join(tmp, "z.BIN"))
    check("--add on the factory image works (relocates the 73-entry table)",
          r.returncode == 0)

    print("\n--root")
    for mode, src in (("--replace", IMAGE), ("--add", b160)):
        out = os.path.join(tmp, f"root{mode}.BIN")
        r = run(os.path.join(ROOT, "tools", "loop_sd.py"), src, mode, "--root", "-o", out)
        q = open(out, "rb").read() if r.returncode == 0 else b""
        if mode == "--replace":
            blk, off = q, L.r2f
        else:
            ly = r24bank.locate(q)
            bb = r24bank.RAM_BASE + ly.ext_rel + r24bank.EXT_HDR + 0x370
            fo = bb - r24bank.RAM_BASE + r24bank.BANK_BASE
            blk, off = q, (lambda ram, fo=fo: fo + ram - L.COPY_LO)
        sites = [x for x, *_ in L.CALL_PATCHES] + [L.ROOT_PATCH[0]]
        ok = r.returncode == 0 and all(
            blk[off(x):off(x) + 2] in (L.MOVS_R0_0, L.THUMB_NOP) and
            blk[off(x) + 2:off(x) + 4] == L.THUMB_NOP for x in sites)
        ok = ok and blk[off(L.PATH_RAM):off(L.PATH_RAM) + 17] == L.ROOT_PATH
        check(f"{mode} --root: path 'A:', 4 calls neutralised", ok)

    print("\n--usb-root")
    src = os.path.join(tmp, "root--add.BIN")
    ur = os.path.join(tmp, "usbroot.BIN")
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), src, "--usb-root", "-o", ur)
    check("--usb-root on an --add --root image exits 0", r.returncode == 0)
    u, w = open(src, "rb").read(), open(ur, "rb").read() if r.returncode == 0 else b""
    ok_ranges = [(0x34, 0x38), (L.r2f(L.ROOT_PATCH[0]), L.r2f(L.ROOT_PATCH[0]) + 4),
                 (L.r2f(L.PATH_RAM), L.r2f(L.PATH_RAM) + 17)]
    stray = [x for x, y in diff_ranges(u, w) if not any(a <= x and y <= b for a, b in ok_ranges)] if w else [1]
    check(f"only LOOP[USB]'s stat call and path change ({len(stray)} stray)", not stray)
    check("LOOP[USB] path is 'B:', stat neutralised, host-mode + wait intact",
          bool(w) and w[L.r2f(L.PATH_RAM):L.r2f(L.PATH_RAM) + 17] == L.USB_ROOT_PATH
          and w[L.r2f(L.ROOT_PATCH[0]):L.r2f(L.ROOT_PATCH[0]) + 4] == L.MOVS_R0_0 + L.THUMB_NOP
          and all(w[L.r2f(x):L.r2f(x) + 4].hex() == want for x, want, *_ in L.CALL_PATCHES))
    full = os.path.join(tmp, "full.BIN")
    run(os.path.join(ROOT, "tools", "loop_sd.py"), b160, "--add", "--root", "--usb-root", "-o", full)
    check("one-step --add --root --usb-root == two steps",
          bool(w) and open(full, "rb").read() == w)
    r = run(os.path.join(ROOT, "tools", "loop_sd.py"), IMAGE, "--replace", "--usb-root",
            "-o", os.path.join(tmp, "bad.BIN"))
    check("--replace with --usb-root is refused", r.returncode != 0)

    print(f"\n{'=' * 60}\n{passed + failed} checks: {passed} passed, {failed} failed")
    print("ALL CHECKS PASSED" if not failed else "SOME CHECKS FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
