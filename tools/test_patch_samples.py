#!/usr/bin/env python3
"""test_patch_samples.py — verify tools/patch_samples.py and docs/09_sample_map.md.

Proves the properties the patch tool promises:

  * a list entry writes only inside its own slot — the byte range
    [BANK_BASE + rel_start, + length) — never into the neighbouring sample;
  * an oversized WAV is truncated to exactly the slot length, an undersized one
    is zero-padded, and the bytes written always equal the capacity;
  * in-place mode leaves the Master Sample Table and all code untouched, and
    only the header checksum moves;
  * --repack stores each WAV at its own length, rewrites the table, keeps
    untouched entries byte-identical, refuses an over-full bank, and
    reproduces the image exactly from an untouched extract;
  * the last slot stops at EOF and nothing is written past it;
  * every malformed list line is rejected instead of being guessed at;
  * docs/09_sample_map.md agrees with the binary, row for row.

    python3 tools/test_patch_samples.py
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "patch_samples.py")
GEN = os.path.join(ROOT, "tools", "gen_sample_map.py")
IMAGE = os.path.join(ROOT, "R24SYS.BIN")
DOC = os.path.join(ROOT, "docs", "09_sample_map.md")

SAMPLE_TABLE = 0x000E20EC
NUM_SAMPLES = 73
BANK_BASE = 0x00220040
KIT_TABLE = 0x000E2334
CHECKSUM = 0x34

passed = failed = 0


def check(name: str, cond: bool) -> None:
    global passed, failed
    if cond:
        print(f"  ok    {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}")
        failed += 1


def table(data: bytes) -> list[tuple[int, int]]:
    return [struct.unpack_from("<II", data, SAMPLE_TABLE + 8 * i)
            for i in range(NUM_SAMPLES)]


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True, cwd=ROOT)


def write_wav(path: str, seconds: float, rate: int = 44100) -> int:
    """Write a full-scale sine; return its sample count."""
    import math
    n = int(rate * seconds)
    s = [int(30000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(n)]
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n}h", *s))
    return n


def diff_outside(data_a: bytes, data_b: bytes, allowed: list[tuple[int, int]]):
    """Byte offsets that differ, excluding the checksum field and `allowed`."""
    out = []
    for i in range(len(data_a)):
        if data_a[i] == data_b[i]:
            continue
        if CHECKSUM <= i < CHECKSUM + 4:
            continue
        if any(s <= i < e for s, e in allowed):
            continue
        out.append(i)
    return out


def main() -> int:
    stock = open(IMAGE, "rb").read()
    tab = table(stock)
    tmp = tempfile.mkdtemp(prefix="r24patch_")

    def slot(i: int) -> tuple[int, int]:
        rel, ln = tab[i]
        return BANK_BASE + rel, BANK_BASE + rel + ln

    # ═══════════════════════════════════════════════════════════════════════
    print("docs/09_sample_map.md agrees with the binary")
    check("doc exists", os.path.isfile(DOC))
    doc = open(DOC, encoding="utf-8") if os.path.isfile(DOC) else None
    doc_text = doc.read() if doc else ""
    if doc:
        doc.close()
    rows = re.findall(
        r"^\| (\d+) \| `0x([0-9A-F]+)` \| (\d+) \| `0x([0-9A-F]+)` \| "
        r"`0x([0-9A-F]+)` \| ([\d.]+) \|",
        doc_text, re.M)
    check(f"doc lists all {NUM_SAMPLES} positions (found {len(rows)})",
          len(rows) == NUM_SAMPLES)
    ok_rows = 0
    for r in rows:
        i, rel, ln = int(r[0]), int(r[1], 16), int(r[2])
        fs, fe, ms = int(r[3], 16), int(r[4], 16), float(r[5])
        src_rel, src_len = tab[i]
        if (rel == src_rel and ln == src_len
                and fs == BANK_BASE + rel and fe == BANK_BASE + rel + ln
                and abs(ms - ln / 2 / 44100 * 1000) < 0.05):
            ok_rows += 1
    check(f"all {NUM_SAMPLES} doc rows match the sample table",
          ok_rows == NUM_SAMPLES)
    check("doc rows are contiguous and end at EOF",
          all(int(rows[i][3], 16) == int(rows[i - 1][4], 16)
              for i in range(1, len(rows)))
          and int(rows[-1][4], 16) == len(stock))

    # ═══════════════════════════════════════════════════════════════════════
    print("\nan oversize WAV is truncated at the slot end (was the failure mode)")
    huge = os.path.join(tmp, "huge.wav")
    nsmp = write_wav(huge, 10.0)                      # 10 s, far over any slot
    lst = os.path.join(tmp, "huge.txt")
    out = os.path.join(tmp, "huge.BIN")
    open(lst, "w").write(f"{huge} 72\n")              # last slot
    res = run([IMAGE, lst, "-o", out])
    check("tool exits 0", res.returncode == 0)
    check("reports a trim", "trim" in res.stdout)
    got = open(out, "rb").read()
    s, e = slot(72)
    check("output size unchanged", len(got) == len(stock))
    bad = diff_outside(stock, got, [slot(72)])
    check(f"nothing written outside slot 72 ({len(bad)} stray bytes)", not bad)
    check("slot 72 ends at EOF", e == len(got))
    check("nothing written past EOF", not any(i >= e for i in
                                              range(len(stock))
                                              if stock[i] != got[i]))
    check("wrote exactly the slot length",
          f"{e - s}" in res.stdout.replace("  ", " "))

    # ═══════════════════════════════════════════════════════════════════════
    print("\na short WAV is zero-padded, still inside its slot")
    short = os.path.join(tmp, "short.wav")
    write_wav(short, 0.01)
    lst2 = os.path.join(tmp, "short.txt")
    out2 = os.path.join(tmp, "short.BIN")
    open(lst2, "w").write(f"{short} 0\n")
    res = run([IMAGE, lst2, "-o", out2])
    got2 = open(out2, "rb").read()
    s0, e0 = slot(0)
    check("tool exits 0", res.returncode == 0)
    check("reports a pad", "pad" in res.stdout)
    check("only slot 0 changed",
          not diff_outside(stock, got2, [slot(0)]))
    check("slot 0 tail is silence",
          got2[e0 - 200:e0] == b"\x00" * 200)
    check("checksum refreshed", stock[CHECKSUM:CHECKSUM + 4]
          != got2[CHECKSUM:CHECKSUM + 4])

    # ═══════════════════════════════════════════════════════════════════════
    print("\nstructure is preserved")
    check("sample table byte-identical (in-place mode never edits it)",
          stock[SAMPLE_TABLE:SAMPLE_TABLE + NUM_SAMPLES * 8]
          == got2[SAMPLE_TABLE:SAMPLE_TABLE + NUM_SAMPLES * 8])
    check("code region untouched", stock[0x40:SAMPLE_TABLE]
          == got2[0x40:SAMPLE_TABLE])
    check("kit table untouched", stock[KIT_TABLE:KIT_TABLE + 1440]
          == got2[KIT_TABLE:KIT_TABLE + 1440])
    check("sample-table literals untouched (file 0x2E244, 0x2E624)",
          all(stock[o:o + 4] == got2[o:o + 4] for o in (0x2E244, 0x2E624)))

    # ═══════════════════════════════════════════════════════════════════════
    print("\npaths containing spaces are parsed correctly")
    spaced = os.path.join(tmp, "my kick 01.wav")
    write_wav(spaced, 0.05)
    lst3 = os.path.join(tmp, "spaced.txt")
    out3 = os.path.join(tmp, "spaced.BIN")
    open(lst3, "w").write(f"{spaced} 5\n")
    res = run([IMAGE, lst3, "-o", out3, "--dry-run"])
    check("space-containing path accepted", res.returncode == 0)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nmalformed lists are rejected, never guessed")
    cases = {
        "id out of range": "REVERB_X0X/606/Kick1.wav 99\n",
        "non-integer id": "REVERB_X0X/606/Kick1.wav abc\n",
        "duplicate id": ("REVERB_X0X/606/Kick1.wav 5\n"
                         "REVERB_X0X/606/Kick2.wav 5\n"),
        "missing wav": "REVERB_X0X/does-not-exist.wav 3\n",
        "one column only": "REVERB_X0X/606/Kick1.wav\n",
        "negative id": "REVERB_X0X/606/Kick1.wav -1\n",
    }
    for label, body in cases.items():
        p = os.path.join(tmp, "bad.txt")
        open(p, "w").write(body)
        r = run([IMAGE, p, "-o", os.path.join(tmp, "bad.BIN"), "--dry-run"])
        check(f"{label} -> rejected", r.returncode != 0 and "error:" in r.stderr)

    empty = os.path.join(tmp, "empty.txt")
    open(empty, "w").write("# only comments\n\n")
    r = run([IMAGE, empty, "-o", os.path.join(tmp, "bad.BIN"), "--dry-run"])
    check("empty list -> rejected", r.returncode != 0)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nguards")
    tiny = os.path.join(tmp, "tiny.bin")
    open(tiny, "wb").write(b"\0" * 1000)
    good = os.path.join(tmp, "good.txt")
    open(good, "w").write("REVERB_X0X/606/Kick1.wav 3\n")
    r = run([tiny, good, "-o", os.path.join(tmp, "x.BIN")])
    check("wrong image size -> refused", r.returncode != 0)
    dry = os.path.join(tmp, "dry.BIN")
    if os.path.exists(dry):
        os.remove(dry)
    r = run([IMAGE, good, "-o", dry, "--dry-run"])
    check("--dry-run writes no file", r.returncode == 0
          and not os.path.exists(dry))
    r = run([IMAGE, good, "-o", os.path.join(tmp, "ok.BIN")])
    check("successful run exits 0", r.returncode == 0)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nextract: every sample becomes sampleNN.wav")
    exdir = os.path.join(tmp, "ex")
    r = run([IMAGE, "--extract", "-o", exdir])
    check("extract exits 0", r.returncode == 0)
    wavs = sorted(f for f in os.listdir(exdir) if f.endswith(".wav"))
    check(f"wrote {NUM_SAMPLES} WAVs (found {len(wavs)})",
          len(wavs) == NUM_SAMPLES)
    check("names run sample00.wav … sample72.wav",
          wavs[0] == "sample00.wav" and wavs[-1] == "sample72.wav")
    check("list file written", os.path.isfile(os.path.join(exdir, "samples.txt")))

    # each WAV must hold exactly the bytes stored for that sample
    bad = []
    for i in range(NUM_SAMPLES):
        rel, ln = tab[i]
        p = os.path.join(exdir, f"sample{i:02d}.wav")
        with wave.open(p, "rb") as w:
            frames = w.readframes(w.getnframes())
            if (w.getnchannels() != 1 or w.getsampwidth() != 2
                    or w.getframerate() != 44100
                    or frames != stock[BANK_BASE + rel:BANK_BASE + rel + ln]):
                bad.append(i)
    check(f"all {NUM_SAMPLES} WAVs match the stored PCM exactly "
          f"({len(bad)} bad)", not bad)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nre-package from the generated list reproduces the image")
    repack = os.path.join(tmp, "repack.BIN")
    lst_ex = os.path.join(exdir, "samples.txt")
    r = run([IMAGE, lst_ex, "-o", repack])
    check("re-pack exits 0", r.returncode == 0)
    check("every sample reported 'exact' (no trim/pad)",
          r.stdout.count("exact") == NUM_SAMPLES)
    check("re-packed image is byte-identical to the source",
          open(repack, "rb").read() == stock)
    check("checksum matches the stock image",
          open(repack, "rb").read()[CHECKSUM:CHECKSUM + 4]
          == stock[CHECKSUM:CHECKSUM + 4])

    # the same must hold for a previously patched image
    custom = os.path.join(ROOT, "R24SYS_X0X.BIN")
    if os.path.isfile(custom):
        cdir = os.path.join(tmp, "exx0x")
        r1 = run([custom, "--extract", "-o", cdir])
        crepack = os.path.join(tmp, "crepack.BIN")
        r2 = run([custom, os.path.join(cdir, "samples.txt"), "-o", crepack])
        check("x0x image extracts", r1.returncode == 0)
        check("x0x image re-packs byte-identically",
              r2.returncode == 0
              and open(crepack, "rb").read() == open(custom, "rb").read())

    # ═══════════════════════════════════════════════════════════════════════
    print("\n--repack: samples keep their own length, the table is rewritten")
    r = run([IMAGE, lst_ex, "-o", os.path.join(tmp, "rp0.BIN"), "--repack"])
    check("untouched extract repacks byte-identically",
          r.returncode == 0
          and open(os.path.join(tmp, "rp0.BIN"), "rb").read() == stock)

    two = os.path.join(tmp, "two_s.wav")
    n2 = write_wav(two, 2.0)                  # 2 s into slot 23 (68.8 ms)
    lst_g = os.path.join(tmp, "grow.txt")
    open(lst_g, "w").write(f"{two} 23\n{short} 72\n")   # and shrink slot 72
    rp1 = os.path.join(tmp, "rp1.BIN")
    r = run([IMAGE, lst_g, "-o", rp1, "--repack"])
    check("repack with a longer and a shorter WAV exits 0", r.returncode == 0)
    g = open(rp1, "rb").read() if r.returncode == 0 else stock
    t2 = table(g)
    check("output size unchanged", len(g) == len(stock))
    check("slot 23 now holds the whole 2 s WAV (no trim)", t2[23][1] == n2 * 2)
    check("slot 72 is as short as its WAV (no padding)",
          t2[72][1] == int(44100 * 0.01) * 2)
    same = all(g[BANK_BASE + t2[i][0]:BANK_BASE + t2[i][0] + t2[i][1]]
               == stock[BANK_BASE + tab[i][0]:BANK_BASE + tab[i][0] + tab[i][1]]
               for i in range(NUM_SAMPLES) if i not in (23, 72))
    check("the other 71 samples are byte-identical audio", same)
    check("table stays in place (73 entries, literals untouched)",
          g[0x2E244:0x2E248] == stock[0x2E244:0x2E248])
    check("code region untouched", g[0x40:SAMPLE_TABLE] == stock[0x40:SAMPLE_TABLE])
    check("kit table untouched", g[KIT_TABLE:KIT_TABLE + 1440]
          == stock[KIT_TABLE:KIT_TABLE + 1440])

    big = os.path.join(tmp, "big.wav")
    write_wav(big, 40.0)
    lst_b = os.path.join(tmp, "big.txt")
    open(lst_b, "w").write(f"{big} 0\n{huge} 1\n")      # 50 s > 56 s bank - rest
    r = run([IMAGE, lst_b, "-o", os.path.join(tmp, "rpbig.BIN"), "--repack"])
    check("over-full bank -> refused with a size message",
          r.returncode != 0 and "too many" in r.stderr
          and not os.path.exists(os.path.join(tmp, "rpbig.BIN")))
    r = run([IMAGE, lst_b, "-o", os.path.join(tmp, "rpbig.BIN"), "--repack",
             "--max-ms", "500"])
    check("--max-ms makes it fit", r.returncode == 0)
    r = run([IMAGE, lst_b, "-o", os.path.join(tmp, "x.BIN"), "--max-ms", "5"])
    check("--max-ms without --repack -> refused", r.returncode != 0)

    # ═══════════════════════════════════════════════════════════════════════
    print("\nthe generator reproduces the committed doc")
    regen = os.path.join(tmp, "regen.md")
    r = subprocess.run([sys.executable, GEN, IMAGE, "-o", regen],
                       capture_output=True, text=True, cwd=ROOT)
    check("generator exits 0", r.returncode == 0)
    check("regenerated doc is identical to docs/09_sample_map.md",
          open(regen, "rb").read() == open(DOC, "rb").read())

    # ═══════════════════════════════════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print(f"{passed + failed} checks: {passed} passed, {failed} failed")
    if failed:
        print("SOME CHECKS FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
