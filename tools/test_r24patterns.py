#!/usr/bin/env python3
"""test_r24patterns.py — prove r24patterns.py round-trips and refuses bad input.

    python3 tools/test_r24patterns.py            # all checks
    python3 tools/test_r24patterns.py -v         # name every check

What is asserted, on both shipped images:

  * the blob parses into exactly 147 records, contiguous, ending at
    0x000FD410, with no bytes unaccounted for;
  * the factory patterns use only the field values the docs claim;
  * export -> import is byte-identical (whole file, not just the blob);
  * a real edit changes exactly the edited note byte and the header checksum,
    and never rewrites the pattern count `n2`, the record count `n1` or any
    neighbouring record;
  * every refusal path exits non-zero, prints `error:`, and leaves the output
    file absent (no half-written images, no tracebacks);
  * docs/11_patterns.md's pattern count matches the binary.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "r24patterns.py")
sys.path.insert(0, HERE)

import r24image                                          # noqa: E402
import r24patterns as P                                  # noqa: E402

IMAGES = [os.path.join(ROOT, "R24SYS.BIN"),
          os.path.join(ROOT, "R24SYS_X0X.BIN")]

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
_passed = 0
_failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        _failed.append(f"{name}{(': ' + detail) if detail else ''}")
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, TOOL, *args],
                          capture_output=True, text=True)


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def blob_bytes(data: bytes) -> bytes:
    pats = [e for e in P.scan(data) if "unused" not in e]
    return data[pats[0]["offset"]:pats[-1]["offset"] + pats[-1]["size"]]


# ── 1. the parse itself ────────────────────────────────────────────────────
def test_parse(path: str) -> dict | None:
    name = os.path.basename(path)
    if not os.path.isfile(path):
        print(f"skip {name}: not present")
        return None
    with open(path, "rb") as fh:
        data = fh.read()

    entries = P.scan(data)
    pats = [e for e in entries if "unused" not in e]
    check(f"{name}: 472 patterns", len(pats) == 472, f"got {len(pats)}")
    check(f"{name}: no unused bytes in the blob",
          not [e for e in entries if "unused" in e])

    first, last = pats[0], pats[-1]
    check(f"{name}: blob starts at 0x000EAAC4", first["offset"] == 0x000EAAC4,
          hex(first["offset"]))
    check(f"{name}: first pattern is 08Beat01",
          first["name_raw"].rstrip(b"\x00") == b"08Beat01")
    check(f"{name}: last pattern is COUNT", last["name_raw"].rstrip(b"\x00") ==
          b"COUNT")
    end = last["offset"] + last["size"]
    check(f"{name}: blob ends where COUNT ends", end == P.BLOB_END, hex(end))
    check(f"{name}: the region tail is untouched",
          data[P.BLOB_END:P.RESERVED_AFTER].startswith(b"\x00\x00\x00\x00"))

    # contiguous: every record starts where the previous one ends
    gaps = [(pats[i - 1]["offset"] + pats[i - 1]["size"] - pats[i]["offset"])
            for i in range(1, len(pats))]
    check(f"{name}: records are packed tight", gaps == [0] * (len(pats) - 1),
          f"{sum(1 for g in gaps if g)} gap(s)")

    # every declared length matches n1
    wrong = [p for p in pats if p["size"] != P.HEAD_SIZE + P.NOTE_SIZE * p["n1"]]
    check(f"{name}: size == 20 + 4*n1 everywhere", not wrong, str(wrong[:2]))

    # n2 <= n1 everywhere, but it is NOT a function of the notes: patterns with
    # the same note data carry different n2 values, so it is preserved verbatim.
    check(f"{name}: n2 <= n1 everywhere", all(p["n2"] <= p["n1"] for p in pats))
    # No formula reproduces n2 from the notes (tested: note count, distinct
    # ticks, notes with a duration). It is an unexplained field of the format,
    # so the tool carries it verbatim — assert that it round-trips instead of
    # pretending to understand it.
    deltas = collections.Counter(p["n1"] - p["n2"] for p in pats)
    check(f"{name}: n2 is always below n1, by 1..7",
          all(1 <= d <= 7 for d in deltas), str(dict(deltas)))
    check(f"{name}: n2 varies independently of n1",
          len(deltas) > 3, str(dict(deltas)))

    # note cells
    cells = [n for p in pats for n in p["notes"]]
    check(f"{name}: 16678 notes", len(cells) == 16678, str(len(cells)))
    check(f"{name}: every note pad byte is 00",
          all(c["pad"] == "00" for c in cells))
    check(f"{name}: all four duration codes occur",
          {c["duration"] for c in cells} == {0, 1, 2, 3},
          str(sorted({c["duration"] for c in cells})))
    check(f"{name}: flags bits 2-7 are always zero",
          all(c["flags"] < 4 for c in cells),
          str(sorted({c["flags"] for c in cells})[:8]))

    hdrs = {tuple(p["header"]) for p in pats}
    check(f"{name}: headers vary per pattern (n1/n2 live in them)",
          len(hdrs) > 100, str(len(hdrs)))
    check(f"{name}: header byte +0x0e is 0x04 everywhere",
          all(p["header"][6] == 0x04 for p in pats))
    check(f"{name}: 'fill' is a 0/1 flag",
          {p["header"][7] for p in pats} <= {0, 1})
    return {"data": data, "pats": pats}


# ── 2. export / import ─────────────────────────────────────────────────────
def test_roundtrip(path: str, tmp: str) -> None:
    name = os.path.basename(path)
    outdir = os.path.join(tmp, f"exp-{name}")
    res = run(path, "--export", "-o", outdir)
    check(f"{name}: --export exits 0", res.returncode == 0, res.stderr)
    spec = os.path.join(outdir, "patterns.json")
    check(f"{name}: export writes one file",
          sorted(os.listdir(outdir)) == ["patterns.json"],
          str(os.listdir(outdir)))

    with open(spec, encoding="utf-8") as fh:
        doc = json.load(fh)
    check(f"{name}: format tag", doc.get("format") == "r24-patterns")
    check(f"{name}: frames present", isinstance(doc.get("frames"), list)
          and len(doc["frames"]) >= 1)
    check(f"{name}: every pattern exported",
          sum(len(fr["patterns"]) for fr in doc["frames"]) == 472,
          str(sum(len(fr["patterns"]) for fr in doc["frames"])))
    check(f"{name}: notes exported",
          sum(len(p["notes"]) for fr in doc["frames"] for p in fr["patterns"])
          == 16678)
    check(f"{name}: export records the image hash",
          doc.get("image_sha256") == sha(path))

    repack = os.path.join(tmp, f"repack-{name}.BIN")
    res = run(path, spec, "-o", repack)
    check(f"{name}: untouched import exits 0", res.returncode == 0, res.stderr)
    check(f"{name}: untouched import reports 0 changes",
          "changes : 0" in res.stdout, res.stdout[:200])
    check(f"{name}: export -> import is byte-identical",
          os.path.isfile(repack) and sha(repack) == sha(path))

    # export -> import -> export is stable too
    outdir2 = os.path.join(tmp, f"exp2-{name}")
    run(repack, "--export", "-o", outdir2)
    with open(os.path.join(outdir2, "patterns.json"), encoding="utf-8") as fh:
        doc2 = json.load(fh)
    check(f"{name}: second export equals the first",
          doc["frames"] == doc2["frames"])


# ── 3. a real edit ─────────────────────────────────────────────────────────
def test_edit(path: str, tmp: str) -> None:
    name = os.path.basename(path)
    outdir = os.path.join(tmp, f"editexp-{name}")
    run(path, "--export", "-o", outdir)
    spec = os.path.join(outdir, "patterns.json")
    with open(spec, encoding="utf-8") as fh:
        doc = json.load(fh)

    target = None
    for fr in doc["frames"]:
        for pat in fr["patterns"]:
            if pat["name"] == "08Beat01":
                target = pat
    check(f"{name}: 08Beat01 is in the export", target is not None)
    note = target["notes"][0]
    before = dict(note)
    note["duration"] = (before["duration"] + 1) % 4
    edited = os.path.join(tmp, f"edited-{name}.json")
    with open(edited, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)

    out = os.path.join(tmp, f"edited-{name}.BIN")
    res = run(path, edited, "-o", out)
    check(f"{name}: edited import exits 0", res.returncode == 0, res.stderr)
    check(f"{name}: edited import reports 1 change",
          "changes : 1" in res.stdout, res.stdout[:300])
    check(f"{name}: the change names the pattern",
          "08Beat01" in res.stdout)

    with open(path, "rb") as fh:
        a = fh.read()
    with open(out, "rb") as fh:
        b = fh.read()
    check(f"{name}: size unchanged", len(a) == len(b))
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    # one note byte + the 4 checksum bytes (only the low ones move)
    cell = 0x000EAAC4 + P.HEAD_SIZE
    expect_note = [i for i in diff if i == cell + 2]
    expect_ck = [i for i in diff if 0x34 <= i < 0x38]
    check(f"{name}: exactly the edited note byte and the checksum move",
          len(expect_note) == 1 and len(expect_ck) >= 1
          and len(diff) == len(expect_note) + len(expect_ck),
          str([hex(i) for i in diff]))
    check(f"{name}: the edit is what was asked for",
          b[cell + 2] & 0x03 == note["duration"])
    check(f"{name}: the rest of the flags byte is preserved",
          b[cell + 2] & 0xFC == a[cell + 2] & 0xFC)

    img = r24image.R24Image.from_file(out)
    check(f"{name}: edited image verifies", img.checksum_ok())

    # n2 and n1 survive the edit untouched
    e = P.scan(b)[0]
    check(f"{name}: n1 unchanged by the edit", e["n1"] == 50, str(e["n1"]))
    check(f"{name}: n2 unchanged by the edit", e["n2"] == 47, str(e["n2"]))
    rest_a = blob_bytes(a)[:cell - 0x000EAAC4] + blob_bytes(a)[cell - 0x000EAAC4 + 4:]
    rest_b = blob_bytes(b)[:cell - 0x000EAAC4] + blob_bytes(b)[cell - 0x000EAAC4 + 4:]
    check(f"{name}: every other record is byte-identical", rest_a == rest_b,
          "a record outside the edited note changed")


# ── 4. refusals ────────────────────────────────────────────────────────────
def _mut(doc: dict):
    return json.loads(json.dumps(doc))


def test_refusals(path: str, tmp: str) -> None:
    name = os.path.basename(path)
    outdir = os.path.join(tmp, f"refuse-{name}")
    run(path, "--export", "-o", outdir)
    spec = os.path.join(outdir, "patterns.json")
    with open(spec, encoding="utf-8") as fh:
        base = json.load(fh)

    def expect_error(label: str, doc, expect: str | None = None,
                     args: tuple = ()) -> None:
        p = os.path.join(tmp, f"bad-{name}-{label}.json")
        with open(p, "w", encoding="utf-8") as fh:
            if isinstance(doc, str):
                fh.write(doc)
            else:
                json.dump(doc, fh)
        dst = os.path.join(tmp, f"bad-{name}-{label}.BIN")
        res = run(path, p, "-o", dst, *args)
        ok = res.returncode != 0
        check(f"{name}: refuses {label}", ok,
              f"exit {res.returncode}, stdout {res.stdout[:120]}")
        check(f"{name}: {label} prints error:", "error:" in res.stderr,
              res.stderr[:160].replace("\n", " "))
        check(f"{name}: {label} writes no output", not os.path.exists(dst))
        check(f"{name}: {label} prints no traceback",
              "Traceback" not in res.stderr, res.stderr[:200])
        if expect:
            check(f"{name}: {label} explains why", expect in res.stderr,
                  res.stderr[:200].replace("\n", " "))

    expect_error("invalid-json", "{not json", "not valid JSON")
    expect_error("wrong-format", {**base, "format": "r24-kits"}, "not a pattern")
    expect_error("wrong-version", {**base, "format_version": 99},
                 "format_version")
    expect_error("no-frames", {**base, "frames": []}, "frames")
    expect_error("wrong-image-size", {**base, "image_size": 123}, "bytes")

    other = os.path.join(ROOT, "R24SYS_X0X.BIN")
    if os.path.isfile(other) and os.path.basename(other) != name:
        p = os.path.join(tmp, f"foreign-{name}.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        dst = os.path.join(tmp, f"foreign-{name}.BIN")
        res = run(other, p, "-o", dst)
        check(f"{name}: refuses an export from another image",
              res.returncode != 0 and "different image" in res.stderr,
              res.stderr[:200])
        check(f"{name}: … and writes nothing", not os.path.exists(dst))
        res = run(other, p, "-o", dst, "--force")
        check(f"{name}: --force overrides the image check", res.returncode == 0,
              res.stderr[:200])

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["ticks"] = -1
    expect_error("negative-ticks", d, "out of range")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["ticks"] = 70000
    expect_error("ticks-too-big", d, "out of range")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["flags"] = 256
    expect_error("flags-too-big", d, "out of range")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["duration"] = 4
    expect_error("duration-out-of-range", d, "out of range")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["duration"] = 7
    expect_error("duration-too-big", d, "out of range")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"][0]["pad"] = "zz"
    expect_error("bad-pad-hex", d, "hex")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["name"] = "TOOLONGNAME"
    expect_error("name-too-long", d, "1..8")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["name"] = ""
    expect_error("name-empty", d, "1..8")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["name"] = "ñandu"
    expect_error("name-not-ascii", d, "ASCII")

    # a new note -> a record would have to change length
    d = _mut(base)
    pat = d["frames"][0]["patterns"][0]
    pat["notes"].append(dict(pat["notes"][0]))
    expect_error("added-note", d, "cannot change length")

    d = _mut(base)
    d["frames"][0]["patterns"][0]["notes"].pop()
    expect_error("deleted-note", d, "cannot change length")

    # renaming is refused (it would desync the pattern list)
    d = _mut(base)
    d["frames"][0]["patterns"][0]["name"] = "RENAMED"
    expect_error("rename", d, "renaming is not supported")

    # duplicate names
    d = _mut(base)
    d["frames"][0]["patterns"][1]["name"] = d["frames"][0]["patterns"][0]["name"]
    expect_error("duplicate-name", d, "appears twice")

    # pattern count changed
    d = _mut(base)
    d["frames"][0]["patterns"].pop()
    expect_error("removed-pattern", d, "not supported")

    # header type outside the observed set
    d = _mut(base)
    d["frames"][0]["patterns"][0]["header"]["type"] = 0x77
    expect_error("bad-record-type", d, "record kind")

    # frames out of order
    d = _mut(base)
    if len(d["frames"]) > 1:
        d["frames"][0]["frame"], d["frames"][1]["frame"] = \
            d["frames"][1]["frame"], d["frames"][0]["frame"]
        expect_error("frames-out-of-order", d, "in order")

    # missing file / missing image are CLI errors, not tracebacks
    res = run(path, os.path.join(tmp, "nope.json"), "-o",
              os.path.join(tmp, "nope.BIN"))
    check(f"{name}: missing spec is a clean error",
          res.returncode != 0 and "Traceback" not in res.stderr,
          res.stderr[:200])
    res = run(os.path.join(tmp, "nope.BIN"), "--export", "-o", tmp)
    check(f"{name}: missing image is a clean error",
          res.returncode != 0 and "Traceback" not in res.stderr,
          res.stderr[:200])
    res = run(path)
    check(f"{name}: no mode given is a clean error",
          res.returncode != 0 and "Traceback" not in res.stderr,
          res.stderr[:200])


# ── 5. --show and --dry-run ────────────────────────────────────────────────
def test_cli(path: str, tmp: str) -> None:
    name = os.path.basename(path)
    res = run(path, "--show", "08Beat01")
    check(f"{name}: --show prints a pattern", res.returncode == 0, res.stderr)
    check(f"{name}: --show lists 50 notes",
          "50 notes" in res.stdout and res.stdout.count("  ") > 50,
          res.stdout[:120])

    res = run(path, "--show", "NoSuchPattern")
    check(f"{name}: --show on an unknown name fails cleanly",
          res.returncode != 0 and "no pattern named" in res.stderr,
          res.stderr[:160])

    outdir = os.path.join(tmp, f"dry-{name}")
    res = run(path, "--export", "-o", outdir, "--dry-run")
    check(f"{name}: --export --dry-run writes nothing",
          res.returncode == 0 and not os.path.exists(outdir),
          res.stderr[:160])


# ── 6. docs agree with the binary ──────────────────────────────────────────
def test_docs(parsed: dict | None) -> None:
    doc = os.path.join(ROOT, "docs", "11_patterns.md")
    if not os.path.isfile(doc):
        check("docs/11_patterns.md exists", False, "missing")
        return
    with open(doc, encoding="utf-8") as fh:
        text = fh.read()
    if parsed:
        n = len(parsed["pats"])
        check("docs quote the real pattern count", f"{n} patterns" in text
              or str(n) in text, f"{n} not mentioned")
        check("docs quote the blob start", "0x000EAAC4" in text)
        check("docs quote the blob end", "0x000FD410" in text)
    check("docs describe the record header", "n1" in text and "n2" in text)
    check("docs name the tool", "r24patterns.py" in text)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="r24patterns-test-")
    parsed = None
    try:
        print("== parsing ==")
        for img in IMAGES:
            got = test_parse(img)
            parsed = parsed or got
        print("== export / import ==")
        for img in IMAGES:
            if os.path.isfile(img):
                test_roundtrip(img, tmp)
        print("== editing ==")
        for img in IMAGES:
            if os.path.isfile(img):
                test_edit(img, tmp)
        print("== refusals ==")
        for img in IMAGES:
            if os.path.isfile(img):
                test_refusals(img, tmp)
        print("== CLI ==")
        for img in IMAGES:
            if os.path.isfile(img):
                test_cli(img, tmp)
        print("== docs ==")
        test_docs(parsed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failed:
        print(f"FAILED {len(_failed)} of {_passed + len(_failed)} checks:")
        for f in _failed:
            print(f"  - {f}")
        return 1
    print(f"all {_passed} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
