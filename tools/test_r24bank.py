#!/usr/bin/env python3
"""test_r24bank.py — prove the sample-table findings and the relocation.

Firmware evidence (static, on R24SYS.BIN):
  * the boot copy is memcpy(0x87540000, 0x022A0000, 0x500000);
  * the voice-start code reads the table through exactly two literal words
    (file 0x2E244, 0x2E624) and four Thumb `ldr` instructions, and nothing
    else in the code region holds the table address;
  * the bank's load address and RAM address each appear exactly once.

Relocation (`r24bank.py relocate --per-kit`):
  * 160 entries, kit k slot s -> entry 16k+s, every slot distinct image-wide;
  * every slot still plays the same audio as before;
  * only the literals, the kit sample ids, the bank tail and the checksum move;
  * the table is 8-aligned, inside the bank and inside the 5 MiB boot copy;
  * relocation is idempotent, and works on R24SYS_X0X.BIN too.

Then the whole workflow on the relocated image: 160 distinct WAVs repacked,
in-place writes to shared audio refused, kits export/import with ids 0..159,
and the sample map generated.

    python3 tools/test_r24bank.py
"""
from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import r24bank  # noqa: E402
import r24image  # noqa: E402

PY = sys.executable
BANK = os.path.join(ROOT, "tools", "r24bank.py")
PATCH = os.path.join(ROOT, "tools", "patch_samples.py")
KITS = os.path.join(ROOT, "tools", "r24kits.py")
GEN = os.path.join(ROOT, "tools", "gen_sample_map.py")
IMAGE = os.path.join(ROOT, "R24SYS.BIN")
X0X = os.path.join(ROOT, "R24SYS_X0X.BIN")
OFF = 0x0207FFC0                     # VA = file offset + OFF

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}")
    if cond:
        passed += 1
    else:
        failed += 1


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, *args], capture_output=True, text=True, cwd=ROOT)


def u32(d: bytes, o: int) -> int:
    return struct.unpack_from("<I", d, o)[0]


def words_equal(d: bytes, value: int, end: int) -> list[int]:
    return [o for o in range(0, end, 4) if u32(d, o) == value]


def thumb_ldr_refs(d: bytes, lit: int) -> list[int]:
    out = []
    for o in range(max(0, lit - 1024), lit, 2):
        h = struct.unpack_from("<H", d, o)[0]
        if h >> 11 == 0b01001 and ((o + 4) & ~3) + (h & 0xFF) * 4 == lit:
            out.append(o)
    return out


def sine(path: str, freq: float, secs: float, rate: int = 44100) -> bytes:
    n = int(rate * secs)
    pcm = struct.pack(f"<{n}h", *(int(20000 * math.sin(2 * math.pi * freq * i / rate))
                                  for i in range(n)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return pcm


def main() -> int:
    stock = open(IMAGE, "rb").read()
    tmp = tempfile.mkdtemp(prefix="r24bank_")

    # ═══════════════════════════════════════════════════════════════════════
    print("firmware evidence")
    check("boot copy size: movs r2,#5; lsls r2,#20 at VA 0x020ADDFE",
          stock[r24bank.COPY_SITE:r24bank.COPY_SITE + 4] == r24bank.COPY_SITE_BYTES)
    check("boot copy src/dst literals 0x022A0000 / 0x87540000",
          u32(stock, 0x2E1FC) == 0x022A0000 and u32(stock, 0x2E200) == 0x87540000)
    check("0x022A0000 appears once in code/data (the memcpy source)",
          words_equal(stock, 0x022A0000, r24bank.BANK_BASE) == [0x2E1FC])
    check("0x87540000 appears once in code/data (the RAM base)",
          words_equal(stock, 0x87540000, r24bank.BANK_BASE) == [0x2E200])
    sites = words_equal(stock, r24bank.FACTORY_LITERAL, r24bank.BANK_BASE)
    check("table address 0x800E1B38 held by exactly the two known literals",
          sites == list(r24bank.TABLE_LITERALS))
    refs = [r for s in sites for r in thumb_ldr_refs(stock, s)]
    check(f"loaded by exactly 4 Thumb ldr instructions ({len(refs)})",
          len(refs) == 4)
    check("those loads are the voice-start / duration code (VA 0x020AE0xx/0x020AE50C)",
          sorted(r + OFF for r in refs)
          == [0x020AE01A, 0x020AE028, 0x020AE034, 0x020AE50C])
    check("file 0x0E20EC is RAM 0x800E1B38 (.data delta 0x5B4, same as kits)",
          r24bank.FACTORY_LITERAL == 0x800E1B38
          and words_equal(stock, 0x800E1D80, r24bank.BANK_BASE) != [])

    # ═══════════════════════════════════════════════════════════════════════
    print("\nfactory layout")
    lay = r24bank.locate(stock)
    check("R24SYS.BIN: factory table, 73 entries",
          not lay.relocated and lay.count == 73 and lay.table_off == 0x0E20EC)
    if os.path.isfile(X0X):
        lx = r24bank.locate(open(X0X, "rb").read())
        check("R24SYS_X0X.BIN: factory table, 73 entries",
              not lx.relocated and lx.count == 73)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nrelocate --per-kit")
    out = os.path.join(tmp, "r160.BIN")
    r = run(BANK, "relocate", IMAGE, "--per-kit", "-o", out)
    check("exits 0", r.returncode == 0)
    new = open(out, "rb").read()
    check("size unchanged", len(new) == len(stock))
    check("checksum verifies", r24image.R24Image(new).checksum_ok())
    nl = r24bank.locate(new)
    tab = r24bank.read_table(new, nl)
    check("relocated, 160 entries", nl.relocated and nl.count == 160)
    check("both literals = 0x87540000 + table_rel",
          all(u32(new, o) == 0x87540000 + nl.table_rel for o in r24bank.TABLE_LITERALS))
    check("table 8-aligned, inside the bank, inside the 5 MiB copy",
          nl.table_rel % 8 == 0
          and r24bank.BANK_BASE + nl.table_rel + 160 * 8 <= len(new)
          and nl.table_rel + 160 * 8 <= r24bank.RAM_COPY)
    check("every entry valid and below the header",
          all(l >= 2 and l % 2 == 0 and rel % 2 == 0 and rel + l <= nl.audio_limit
              for rel, l in tab))
    ids = r24bank.kit_sample_ids(new)
    check("kit k slot s -> 16k+s",
          all(ids[k][s] == 16 * k + s for k in range(10) for s in range(16)))
    check("160 kit slots, 160 different entries image-wide",
          len({i for kit in ids for i in kit}) == 160)
    old_tab = r24bank.read_table(stock)
    old_ids = r24bank.kit_sample_ids(stock)
    same = True
    for k in range(10):
        for s in range(16):
            a = r24bank.sample_bytes(stock, old_tab, old_ids[k][s])
            b = r24bank.sample_bytes(new, tab, ids[k][s])
            same &= a[:len(b)] == b and not any(a[len(b):])
    check("every slot plays the same audio as before (cut tail is silence)", same)
    allowed = [(0x34, 0x38)] + [(o, o + 4) for o in r24bank.TABLE_LITERALS]
    allowed += [(r24bank.kit_slot_off(k, s), r24bank.kit_slot_off(k, s) + 2)
                for k in range(10) for s in range(16)]
    allowed += [(r24bank.BANK_BASE + nl.audio_limit, len(new))]
    stray = [i for i in range(len(new)) if new[i] != stock[i]
             and not any(a <= i < b for a, b in allowed)]
    check(f"nothing else changed ({len(stray)} stray bytes)", not stray)
    check("factory audio region untouched below the table",
          new[r24bank.BANK_BASE:r24bank.BANK_BASE + nl.audio_limit]
          == stock[r24bank.BANK_BASE:r24bank.BANK_BASE + nl.audio_limit])

    again = os.path.join(tmp, "r160b.BIN")
    r = run(BANK, "relocate", out, "--per-kit", "-o", again)
    check("relocating the relocated image is idempotent",
          r.returncode == 0 and open(again, "rb").read() == new)
    if os.path.isfile(X0X):
        r = run(BANK, "relocate", X0X, "--per-kit", "-o", os.path.join(tmp, "x160.BIN"))
        check("R24SYS_X0X.BIN relocates too", r.returncode == 0)
    r = run(BANK, "relocate", IMAGE, "--entries", "50", "-o", os.path.join(tmp, "n.BIN"))
    check("--entries smaller than the ids kits use -> refused",
          r.returncode != 0 and "error:" in r.stderr)

    # ═══════════════════════════════════════════════════════════════════════
    print("\n160 distinct samples on the relocated image")
    lines, want = [], {}
    for i in range(160):
        p = os.path.join(tmp, f"s{i:03d}.wav")
        want[i] = sine(p, 100 + 7 * i, 0.05 + (i % 7) * 0.02)
        lines.append(f"{p} {i}")
    lst = os.path.join(tmp, "all160.txt")
    open(lst, "w").write("\n".join(lines) + "\n")
    rp = os.path.join(tmp, "rp160.BIN")
    r = run(PATCH, out, lst, "-o", rp, "--repack")
    check("repack of 160 WAVs exits 0", r.returncode == 0)
    if r.returncode == 0:
        g = open(rp, "rb").read()
        gl = r24bank.locate(g)
        gt = r24bank.read_table(g, gl)
        check("still relocated with 160 entries", gl.relocated and gl.count == 160)
        check("each entry holds exactly its own WAV",
              all(r24bank.sample_bytes(g, gt, i) == want[i] for i in range(160)))
        check("160 distinct audio ranges", len(set(gt)) == 160)
        check("kits untouched by the repack",
              r24bank.kit_sample_ids(g) == ids)

    two = os.path.join(tmp, "two.txt")
    sine(os.path.join(tmp, "long.wav"), 330, 2.0)
    open(two, "w").write("long.wav 16\n")          # resolved next to the list
    full = os.path.join(tmp, "full.BIN")
    r = run(PATCH, out, two, "-o", full, "--repack")
    check("new audio on a full bank -> refused with the overflow size",
          r.returncode != 0 and "too many" in r.stderr)
    r = run(PATCH, out, two, "-o", full, "--repack", "--max-ms", "300", "--trim-all")
    if r.returncode == 0:
        f = open(full, "rb").read()
        ft = r24bank.read_table(f)
        check("--max-ms 300 --trim-all: fits, every entry <= 300 ms",
              all(l <= int(44100 * 0.3) * 2 for _r, l in ft))
        check("entry 16 holds the listed WAV (cut), others keep their audio start",
              r24bank.sample_bytes(f, ft, 16)[:2000]
              == open(os.path.join(tmp, "long.wav"), "rb").read()[44:2044]
              and r24bank.sample_bytes(f, ft, 0)[:2000]
              == r24bank.sample_bytes(new, tab, 0)[:2000])
    else:
        check("--max-ms --trim-all repack exits 0", False)

    one = os.path.join(tmp, "one.txt")
    open(one, "w").write(f"{os.path.join(tmp, 's000.wav')} 9\n")
    r = run(PATCH, out, one, "-o", os.path.join(tmp, "x.BIN"))
    check("in-place write to shared audio -> refused",
          r.returncode != 0 and "shares audio" in r.stderr)

    ex = os.path.join(tmp, "ex")
    r = run(PATCH, out, "--extract", "-o", ex)
    check("extract writes 160 WAVs (sample000..sample159)",
          r.returncode == 0 and os.path.isfile(os.path.join(ex, "sample159.wav"))
          and len([f for f in os.listdir(ex) if f.endswith(".wav")]) == 160)
    rpx = os.path.join(tmp, "rpx.BIN")
    r = run(PATCH, out, os.path.join(ex, "samples.txt"), "-o", rpx, "--repack")
    if r.returncode == 0:
        h = open(rpx, "rb").read()
        ht = r24bank.read_table(h)
        check("untouched 160 extract repacks: same audio per entry, shared again",
              all(r24bank.sample_bytes(h, ht, i) == r24bank.sample_bytes(new, tab, i)
                  for i in range(160)) and len(set(ht)) == len(set(tab)))
    else:
        check("untouched 160 extract repacks", False)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nkits and sample map on the relocated image")
    kd = os.path.join(tmp, "kits")
    r = run(KITS, out, "--export", "-o", kd)
    check("kits export exits 0 and reports 160 entries",
          r.returncode == 0 and "160 entries" in r.stdout)
    kimg = os.path.join(tmp, "k.BIN")
    r = run(KITS, out, os.path.join(kd, "kits.json"), "-o", kimg)
    check("untouched kits import is byte-identical",
          r.returncode == 0 and open(kimg, "rb").read() == new)
    import json
    doc = json.load(open(os.path.join(kd, "kits.json")))
    doc["kits"][0]["slots"][0]["sample_id"] = 159
    open(os.path.join(kd, "k159.json"), "w").write(json.dumps(doc))
    r = run(KITS, out, os.path.join(kd, "k159.json"), "-o", kimg)
    check("sample_id 159 accepted", r.returncode == 0)
    doc["kits"][0]["slots"][0]["sample_id"] = 160
    open(os.path.join(kd, "k160.json"), "w").write(json.dumps(doc))
    r = run(KITS, out, os.path.join(kd, "k160.json"), "-o", kimg)
    check("sample_id 160 refused", r.returncode != 0)
    r = run(KITS, IMAGE, os.path.join(kd, "kits.json"), "-o", kimg, "--force")
    check("a 160-entry kits.json cannot be applied to the factory image",
          r.returncode != 0)
    r = run(GEN, out)
    check("sample map lists 160 rows",
          r.returncode == 0 and "| 159 |" in r.stdout and "relocated" in r.stdout)

    # ═══════════════════════════════════════════════════════════════════════
    print("\n16 kits (kit table moved into the extension area)")
    LOOPSD = os.path.join(ROOT, "tools", "loop_sd.py")
    withloop = os.path.join(tmp, "loop.BIN")
    run(LOOPSD, out, "--add", "--root", "-o", withloop)
    lp = open(withloop, "rb").read()
    k16 = os.path.join(tmp, "k16.BIN")
    r = run(BANK, "kits", withloop, "--count", "16", "-o", k16)
    check("kits --count 16 exits 0", r.returncode == 0)
    q = open(k16, "rb").read() if r.returncode == 0 else lp
    kb, kn = r24bank.locate_kits(q)
    sec = r24bank.find_section(q, r24bank.KITS_TAG)
    check("KIT SELECT count = 16, both kit literals -> the new table",
          kn == 16 and sec is not None
          and all(u32(q, o) == r24bank.RAM_BASE + sec[0] + 16 for o in r24bank.KIT_LITERALS))
    loop_before = r24bank.find_section(lp, b"LOOPSD01")
    loop_after = r24bank.find_section(q, b"LOOPSD01")
    check("LOOP[SD] section keeps its address and bytes",
          loop_before == loop_after and loop_before is not None
          and q[r24bank.BANK_BASE + loop_after[0]:] [:loop_after[1]]
          == lp[r24bank.BANK_BASE + loop_before[0]:][:loop_before[1]])
    ob, _ = r24bank.locate_kits(lp)
    same_params = all(
        q[kb + k * 144:kb + k * 144 + 16] == lp[ob + k * 144:ob + k * 144 + 16]
        and all(q[kb + k * 144 + 16 + s_ * 8 + 2:kb + k * 144 + 16 + s_ * 8 + 8]
                == lp[ob + k * 144 + 16 + s_ * 8 + 2:ob + k * 144 + 16 + s_ * 8 + 8]
                for s_ in range(16))
        for k in range(10))
    check("kits 1-10 keep names and slot parameters", same_params)
    new_ok = all(
        q[kb + k * 144:kb + k * 144 + 16] == f"KIT{k + 1}".encode().ljust(15) + b"\x00"
        and all(q[kb + k * 144 + 16 + s_ * 8 + 2:kb + k * 144 + 16 + s_ * 8 + 8]
                == lp[ob + (k - 10) * 144 + 16 + s_ * 8 + 2:ob + (k - 10) * 144 + 16 + s_ * 8 + 8]
                for s_ in range(16))
        for k in range(10, 16))
    check("kits 11-16 are named KIT11..KIT16, copies of kits 1-6", new_ok)
    qi, li = r24bank.kit_sample_ids(q), r24bank.kit_sample_ids(lp)
    qt, lt = r24bank.read_table(q), r24bank.read_table(lp)
    check("256 entries, kit k slot s -> 16k+s",
          len(qt) == 256 and all(qi[k][s_] == 16 * k + s_ for k in range(16) for s_ in range(16)))
    snd = True
    for k in range(16):
        src = k if k < 10 else k - 10
        for s_ in range(16):
            a = r24bank.sample_bytes(lp, lt, li[src][s_])
            b = r24bank.sample_bytes(q, qt, qi[k][s_])
            snd &= a[:len(b)] == b and not any(a[len(b):])
    check("every slot of all 16 kits plays the expected audio", snd)
    allowed = [(0x34, 0x38), (r24bank.KIT_COUNT_OFF, r24bank.KIT_COUNT_OFF + 4)]
    allowed += [(o, o + 4) for o in r24bank.KIT_LITERALS + r24bank.TABLE_LITERALS]
    stray = [i for i in range(r24bank.BANK_BASE) if q[i] != lp[i]
             and not any(a_ <= i < b_ for a_, b_ in allowed)]
    check(f"code/data region: only kit literals, kit count, table literals ({len(stray)} stray)",
          not stray)
    kd = os.path.join(tmp, "k16kits")
    run(KITS, k16, "--export", "-o", kd)
    import json as _j
    doc = _j.load(open(os.path.join(kd, "kits.json")))
    kimg = os.path.join(tmp, "k16b.BIN")
    r = run(KITS, k16, os.path.join(kd, "kits.json"), "-o", kimg)
    check("r24kits: 16 kits export/import byte-identical",
          len(doc["kits"]) == 16 and r.returncode == 0 and open(kimg, "rb").read() == q)
    doc["kits"][15]["slots"][0]["sample_id"] = 255
    doc["kits"][15]["name"] = "MYKIT"
    open(os.path.join(kd, "e.json"), "w").write(_j.dumps(doc))
    r = run(KITS, k16, os.path.join(kd, "e.json"), "-o", kimg)
    e = open(kimg, "rb").read() if r.returncode == 0 else b""
    check("r24kits: kit 16 renamed and slot set to id 255",
          r.returncode == 0 and e[kb + 15 * 144:kb + 15 * 144 + 6] == b"MYKIT ")
    doc["kits"][15]["slots"][0]["sample_id"] = 256
    open(os.path.join(kd, "e2.json"), "w").write(_j.dumps(doc))
    r = run(KITS, k16, os.path.join(kd, "e2.json"), "-o", kimg)
    check("r24kits: id 256 refused", r.returncode != 0)
    r = run(BANK, "kits", k16, "--count", "20", "-o", os.path.join(tmp, "k20.BIN"))
    check("expanding twice is refused", r.returncode != 0)
    ex16 = os.path.join(tmp, "ex16")
    run(PATCH, k16, "--extract", "-o", ex16)
    rp16 = os.path.join(tmp, "rp16.BIN")
    r = run(PATCH, k16, os.path.join(ex16, "samples.txt"), "-o", rp16, "--repack")
    w = open(rp16, "rb").read() if r.returncode == 0 else b""
    ly = r24bank.locate(q)
    check("samples --repack keeps both extension sections",
          bool(w) and w[r24bank.BANK_BASE + ly.ext_rel:] == q[r24bank.BANK_BASE + ly.ext_rel:])
    r = run(GEN, k16)
    check("sample map lists 256 rows and 16 kits",
          r.returncode == 0 and "| 255 |" in r.stdout and "KIT16" in r.stdout)
    r = run(BANK, "kits", IMAGE, "--count", "16", "-o", os.path.join(tmp, "f16.BIN"))
    check("kits --count 16 works from the factory image too", r.returncode == 0)

    print(f"\n{'=' * 60}")
    print(f"{passed + failed} checks: {passed} passed, {failed} failed")
    print("ALL CHECKS PASSED" if not failed else "SOME CHECKS FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
