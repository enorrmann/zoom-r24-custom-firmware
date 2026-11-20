#!/usr/bin/env python3
"""test_r24kits.py — verify tools/r24kits.py round-trips the kit table exactly.

The promises this asserts, in order:

  * the exported kit table matches the binary, slot for slot, including the
    three reserved bytes of every slot and every kit name;
  * the exported matrix agrees with docs/08_drum_kits.md §4 (the factory
    combination matrix), so a drift in either one is caught;
  * **export -> re-import -> repack is byte-identical** for R24SYS.BIN. For
    R24SYS_X0X.BIN it is identical except the three 16-byte `TR-xxx` name
    fields, which normalise to 15 chars + NUL (a name cannot express a 16-byte
    field with no terminator), plus the checksum that follows — 5 bytes, and
    the test asserts no *other* byte moves;
  * a re-seal touches nothing but the header checksum (plus, on X0X, those three
    name bytes);
  * a real edit changes exactly the bytes it should — the two edited slots plus
    the checksum — preserving slot padding, and never touching code or audio;
  * renaming a kit writes 15 chars + NUL, disturbs no slot, and reads back
    unchanged through export -> import -> export;
  * every malformed or unsafe import is rejected with a non-zero exit, an
    `error:` message rather than a traceback, and no output file: out-of-range
    id/level/pan, duplicate slots, missing kits, a moved sample length, and for
    names: empty, missing, non-string, non-ASCII, 16- or 40-char,
    trailing-space, control-character, NUL-bearing or duplicated; plus a
    leftover `name_hex`.

    python3 tools/test_r24kits.py
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import r24image  # noqa: E402  (used only to craft a valid test fixture)
TOOL = os.path.join(ROOT, "tools", "r24kits.py")
DOC = os.path.join(ROOT, "docs", "08_drum_kits.md")
IMAGES = [os.path.join(ROOT, "R24SYS.BIN"), os.path.join(ROOT, "R24SYS_X0X.BIN")]

SAMPLE_TABLE = 0x000E20EC
NUM_SAMPLES = 73
BANK_BASE = 0x00220040
KIT_TABLE = 0x000E2334
KIT_SIZE = 144
NUM_KITS = 10
SLOTS = 16
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


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True, cwd=ROOT)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def kits_of(data: bytes) -> list[tuple[bytes, list[tuple[int, int, int, int, bytes]]]]:
    """Independent decode of the kit table — deliberately not the tool's code."""
    out = []
    for k in range(NUM_KITS):
        off = KIT_TABLE + k * KIT_SIZE
        slots = []
        for s in range(SLOTS):
            so = off + 16 + s * 8
            sid, lvl, pan, grp = struct.unpack_from("<HBBB", data, so)
            slots.append((sid, lvl, pan, grp, data[so + 5:so + 8]))
        out.append((data[off:off + 16], slots))
    return out


def encode_name(name: str) -> bytes:
    """The name encoding, restated here independently of the tool's code.

    15 characters, space-padded, then a NUL — what every factory kit uses.
    """
    return name.encode("ascii").ljust(15, b" ") + b"\x00"


def matrix_from_doc() -> dict[str, list[int]]:
    """Parse the §4 combination matrix out of docs/08_drum_kits.md."""
    text = read(DOC).decode("utf-8")
    section = text.split("## 4. Full Drum Kit Combination Matrix", 1)[1]
    body = section.split("## 5.", 1)[0]
    rows = [ln for ln in body.splitlines() if ln.startswith("| **")]
    header = next(ln for ln in body.splitlines()
                  if ln.startswith("| Slot") or ln.startswith("| **Slot"))
    names = [c.strip().strip("*") for c in header.strip("|").split("|")][2:]
    kits: dict[str, list[int]] = {name: [] for name in names}
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        for name, cell in zip(names, cells[2:]):
            cell = cell.replace("*", "").replace("#", "").strip()
            kits[name].append(int(cell))
    return kits


def main() -> int:
    if not all(os.path.isfile(p) for p in IMAGES):
        print("missing firmware image(s); expected R24SYS.BIN and R24SYS_X0X.BIN")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1. export fidelity, both images ────────────────────────────────
        print("export fidelity")
        exports: dict[str, dict] = {}
        for image in IMAGES:
            base = os.path.basename(image)
            name = base.replace(".BIN", "").replace(".", "_")
            outdir = os.path.join(tmp, name)
            r = run([image, "--export", "-o", outdir])
            check(f"{base}: export exits 0", r.returncode == 0)
            jpath = os.path.join(outdir, "kits.json")
            check(f"{base}: kits.json written", os.path.isfile(jpath))
            check(f"{base}: export writes exactly one file (JSON only)",
                  os.path.isdir(outdir) and os.listdir(outdir) == ["kits.json"])
            doc = json.loads(read(jpath).decode())

            data = read(image)
            ref = kits_of(data)
            ok_name = ok_slot = True
            for k, kit in enumerate(doc["kits"]):
                # The exported name must decode from the image's 16-byte field,
                # and re-encoding it must reproduce that field byte for byte.
                want = ref[k][0].split(b"\x00")[0].decode("ascii").rstrip(" ")
                if kit["name"] != want:
                    ok_name = False
                if encode_name(kit["name"]) != ref[k][0]:
                    ok_name = False
                    print(f"        kit {k}: {ref[k][0]!r} is not expressible "
                          f"as a name (encodes to "
                          f"{encode_name(kit['name'])!r})")
                for s, slot in enumerate(kit["slots"]):
                    sid, lvl, pan, grp, res = ref[k][1][s]
                    if (slot["sample_id"], slot["level"], slot["pan"],
                            slot["group"]) != (sid, lvl, pan, grp):
                        ok_slot = False
                    if slot["reserved"] != res.hex():
                        ok_slot = False
            if base == "R24SYS_X0X.BIN":
                # The X0X names are 'TR-606' + 10 spaces = 16 bytes, no NUL.
                # A name-only field cannot express that (max 15 + NUL), so the
                # 16th byte is normalised to NUL. Everything else is untouched.
                check(f"{base}: TR-xxx names are 16-byte fields",
                      ref[7][0] == b"TR-606          "
                      and b"\x00" not in ref[7][0])
                check(f"{base}: known name-normalisation is the only "
                      f"difference", doc["kits"][7]["name"] == "TR-606")
            else:
                check(f"{base}: every kit name matches the image", ok_name)
            check(f"{base}: every slot field + reserved padding matches", ok_slot)

            samples = doc["samples"]
            ok_tbl = len(samples) == NUM_SAMPLES
            for i, row in enumerate(samples):
                rel, length = struct.unpack_from("<II", data, SAMPLE_TABLE + 8 * i)
                if (row["rel_start"], row["length"]) != (rel, length):
                    ok_tbl = False
                if row["offset"] != BANK_BASE + rel:
                    ok_tbl = False
            check(f"{base}: all {NUM_SAMPLES} sample-table rows match", ok_tbl)
            check(f"{base}: image size recorded", doc["image_size"] == len(data))
            check(f"{base}: notes block explains the fields",
                  any("HOW TO EDIT" in n for n in doc["notes"]))
            exports[base] = {"doc": doc, "dir": outdir}

        # ── 2. exported matrix vs the documented factory matrix ────────────
        print("\nagreement with docs/08_drum_kits.md §4")
        try:
            docm = matrix_from_doc()
            toolm = {kit["name"]: [s["sample_id"] for s in kit["slots"]]
                     for kit in exports["R24SYS.BIN"]["doc"]["kits"]}
            check("doc matrix has all 10 kits", len(docm) == NUM_KITS)
            same = all(len(docm.get(n, [])) == SLOTS and
                       docm.get(n) == toolm.get(n) for n in docm)
            check("exported matrix == documented matrix", same)
        except (StopIteration, ValueError, IndexError) as exc:
            check(f"doc matrix parsed ({exc})", False)

        # ── 3. the headline claim: byte-identical round-trip ───────────────
        print("\nround-trip")
        for image in IMAGES:
            base = os.path.basename(image)
            data = read(image)
            spec = os.path.join(exports[base]["dir"], "kits.json")
            out = os.path.join(tmp, f"{base}.repack.bin")
            r = run([image, spec, "-o", out])
            check(f"{base}: import exits 0", r.returncode == 0)
            got = read(out) if os.path.isfile(out) else b""
            if base == "R24SYS.BIN":
                check(f"{base}: repack is byte-identical", got == data)
                check(f"{base}: untouched export reports 0 changes",
                      "changes : 0" in (r.stdout or ""))
            else:
                # X0X: identical except the 3 TR-xxx name fields (16-byte, no
                # NUL, not expressible as a name) and the checksum that follows
                # from them. Assert exactly that, so a real regression in any
                # other byte still fails.
                names = {KIT_TABLE + k * KIT_SIZE for k in (7, 8, 9)}
                allowed = set(range(CHECKSUM, CHECKSUM + 4))
                for off in names:
                    allowed |= set(range(off, off + 16))
                outside = [i for i in range(len(data))
                           if got[i] != data[i] and i not in allowed]
                check(f"{base}: only the TR-xxx name bytes + checksum differ",
                      got and not outside)
                check(f"{base}: the 3 name changes are the only slot-free edits",
                      "changes : 3" in (r.stdout or ""))
                for k in (7, 8, 9):
                    off = KIT_TABLE + k * KIT_SIZE
                    check(f"{base}: kit {k} name normalised to 15 chars + NUL",
                          got[off:off + 16] == data[off:off + 15] + b"\x00")
            if got and got != data and base == "R24SYS.BIN":
                first = next(i for i in range(len(data)) if got[i] != data[i])
                print(f"        first difference at 0x{first:06X}")

        # ── 4. a re-seal is confined to the checksum (and X0X name padding) ─
        print("\nre-seal confinement")
        for image in IMAGES:
            base = os.path.basename(image)
            out = os.path.join(tmp, f"{base}.resea1.bin")
            run([image, os.path.join(exports[base]["dir"], "kits.json"), "-o", out])
            a, b = read(image), read(out)
            allowed = set(range(CHECKSUM, CHECKSUM + 4))
            if base == "R24SYS_X0X.BIN":
                # The documented normalisation: the 16-byte TR-xxx names.
                for k in (7, 8, 9):
                    off = KIT_TABLE + k * KIT_SIZE
                    allowed |= set(range(off, off + 16))
            diff = [i for i in range(min(len(a), len(b))) if a[i] != b[i]]
            outside = [i for i in diff if i not in allowed]
            check(f"{base}: size unchanged", len(a) == len(b))
            check(f"{base}: no byte outside the checksum/name fields changed",
                  not outside)
            if outside:
                print(f"        unexpected diffs: {[hex(i) for i in outside[:8]]}")

        # ── 5. a real edit is surgical ────────────────────────────────────
        print("\nsurgical edit")
        image = IMAGES[0]
        base = os.path.basename(image)
        data = read(image)
        doc = json.loads(json.dumps(exports[base]["doc"]))
        rock = next(k for k in doc["kits"] if k["name"] == "ROCK")
        rock["slots"][0].update({"sample_id": 12, "level": 15, "pan": 20})
        rock["slots"][1].update({"pan": 100, "group": 3})
        spec = os.path.join(tmp, "edited.json")
        with open(spec, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        out = os.path.join(tmp, "edited.bin")
        r = run([image, spec, "-o", out])
        check("edited import exits 0", r.returncode == 0)
        if os.path.isfile(out):
            got = read(out)
            rock = KIT_TABLE + 3 * KIT_SIZE
            s0, s1 = rock + 16, rock + 24
            check("edited kit is byte-identical elsewhere",
                  all(got[i] == data[i] for i in range(len(data))
                      if not (CHECKSUM <= i < CHECKSUM + 4
                              or s0 <= i < s0 + 8 or s1 <= i < s1 + 8)))
            sid, lvl, pan, grp = struct.unpack_from("<HBBB", got, s0)
            check("slot 0 edit applied (id 12, level 15, pan 20)",
                  (sid, lvl, pan) == (12, 15, 20))
            check("slot 0 reserved padding preserved", got[s0 + 5:s0 + 8] == b"\0\0\0")
            sid, lvl, pan, grp = struct.unpack_from("<HBBB", got, s1)
            check("slot 1 edit applied (pan 100, group 3)",
                  (pan, grp) == (100, 3))
            check("slot 1 reserved padding preserved", got[s1 + 5:s1 + 8] == b"\0\0\0")
            check("reported 'changes : 2'", "changes : 2" in r.stdout)
            check("output verifies", "verified True" in r.stdout)

        # ── 6. rejects are refusals, not guesses ──────────────────────────
        print("\nrejections")

        def expect_error(label: str, spec_path: str) -> None:
            out = os.path.join(tmp, f"rej_{abs(hash(label))}.bin")
            if os.path.exists(out):
                os.remove(out)
            r = run([image, spec_path, "-o", out])
            check(f"{label}: non-zero exit", r.returncode != 0)
            check(f"{label}: no output written", not os.path.exists(out))
            # A rejection must be a message, not a traceback.
            check(f"{label}: clean error message",
                  (r.stderr or "").startswith("error:")
                  and "Traceback" not in (r.stderr or ""))

        def variant(label: str, mutate) -> None:
            """Apply `mutate` to a fresh copy of the export and expect refusal."""
            doc = json.loads(json.dumps(exports[base]["doc"]))
            mutate(doc)
            p = os.path.join(tmp, f"bad_{abs(hash(label))}.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
            expect_error(label, p)

        def set_field(field: str, value):
            """Set a slot field to an arbitrary (possibly wrong-typed) value."""
            def mut(doc):
                doc["kits"][0]["slots"][0][field] = value
            return mut

        variant("sample id 99", set_field("sample_id", 99))
        variant("sample id -1", set_field("sample_id", -1))
        variant("level 99", set_field("level", 99))
        variant("pan 200", set_field("pan", 200))
        variant("non-numeric field", set_field("level", "loud"))
        variant("boolean sample id", set_field("sample_id", True))

        def dup_slot(doc):
            doc["kits"][0]["slots"].append(dict(doc["kits"][0]["slots"][0]))
        variant("duplicate slot (17 slots)", dup_slot)

        def drop_kit(doc):
            doc["kits"].pop()
        variant("missing kit 9", drop_kit)

        # JSON-level refusals
        def json_variant(label: str, mutate) -> None:
            doc = json.loads(json.dumps(exports[base]["doc"]))
            mutate(doc)
            p = os.path.join(tmp, f"bad_{abs(hash(label))}.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            expect_error(label, p)

        def move_length(doc):
            doc["samples"][3]["length"] += 2
        json_variant("changed sample length", move_length)

        def move_rel(doc):
            doc["samples"][0]["rel_start"] = 4
        json_variant("moved sample start", move_rel)

        def non_ascii(doc):
            doc["kits"][0]["name"] = "BÁSICO"
        json_variant("non-ascii name", non_ascii)

        def too_long(doc):
            doc["kits"][0]["name"] = "X" * 16
        json_variant("16-char name (max is 15)", too_long)

        def way_too_long(doc):
            doc["kits"][0]["name"] = "X" * 40
        json_variant("40-char name", way_too_long)

        def empty_name(doc):
            doc["kits"][0]["name"] = ""
        json_variant("empty name", empty_name)

        def missing_name(doc):
            doc["kits"][0].pop("name")
        json_variant("missing name", missing_name)

        def non_string_name(doc):
            doc["kits"][0]["name"] = 123
        json_variant("non-string name", non_string_name)

        def trailing_space(doc):
            doc["kits"][0]["name"] = "BASIC "
        json_variant("name with trailing space", trailing_space)

        def control_char(doc):
            doc["kits"][0]["name"] = "BAS\tIC"
        json_variant("name with control character", control_char)

        def nul_char(doc):
            doc["kits"][0]["name"] = "BAS\u0000IC"
        json_variant("name containing NUL", nul_char)

        def duplicate_name(doc):
            doc["kits"][1]["name"] = doc["kits"][0]["name"]
        json_variant("duplicate kit name", duplicate_name)

        def duplicate_name_case(doc):
            doc["kits"][1]["name"] = doc["kits"][0]["name"].lower()
        json_variant("duplicate kit name (different case)", duplicate_name_case)

        def leftover_name_hex(doc):
            # The field is gone; a file still carrying it must say so clearly
            # rather than silently ignoring it.
            doc["kits"][0]["name_hex"] = "4241534943" + "2020202020" + "2000"
        json_variant("leftover name_hex rejected", leftover_name_hex)

        def bad_reserved(doc):
            doc["kits"][0]["slots"][0]["reserved"] = "zzzzzz"
        json_variant("non-hex reserved", bad_reserved)

        def wrong_reserved_len(doc):
            doc["kits"][0]["slots"][0]["reserved"] = "0000"
        json_variant("2-byte reserved", wrong_reserved_len)

        def too_few_slots(doc):
            doc["kits"][0]["slots"].pop()
        json_variant("15 slots", too_few_slots)

        def wrong_format(doc):
            doc["format"] = "something-else"
        json_variant("wrong format tag", wrong_format)

        def bad_version(doc):
            doc["format_version"] = 99
        json_variant("unsupported format_version", bad_version)

        def dup_index(doc):
            doc["kits"][1]["index"] = doc["kits"][0]["index"]
        json_variant("duplicate kit index", dup_index)

        def no_index(doc):
            doc["kits"][0].pop("index")
        json_variant("missing kit index", no_index)

        def partial_samples(doc):
            doc["samples"] = doc["samples"][:10]
        json_variant("trimmed sample table (10 rows)", partial_samples)

        print("\ncross-image guard")
        # An export from one image applied to the other is the mistake that
        # silently rewrote all 160 slots before this guard existed.
        x0x = IMAGES[1]
        x0x_name = os.path.basename(x0x)
        other = os.path.join(exports[x0x_name]["dir"], "kits.json")
        out = os.path.join(tmp, "cross.bin")
        r = run([image, other, "-o", out])
        check("cross-image import refused", r.returncode != 0)
        check("cross-image: no output written", not os.path.exists(out))
        check("cross-image: message names the mismatch",
              "different image" in (r.stderr or ""))
        r = run([image, other, "-o", out, "--force"])
        check("cross-image import proceeds with --force", r.returncode == 0)
        check("cross-image: --force warns", "different image" in (r.stderr or ""))
        check("cross-image: --force output verifies",
              "verified True" in (r.stdout or ""))

        # An export with its identity stripped must warn, not apply silently.
        doc = json.loads(json.dumps(exports[x0x_name]["doc"]))
        doc.pop("image_sha256")
        doc.pop("image_size")
        p = os.path.join(tmp, "no_identity.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        r = run([image, p, "-o", os.path.join(tmp, "no_id.bin")])
        check("export without image hash still warns",
              "records no image hash" in (r.stderr or ""))

        # A different image size is refused even if the hash is absent.
        doc = json.loads(json.dumps(exports[x0x_name]["doc"]))
        doc.pop("image_sha256")
        doc["image_size"] = 12345
        p = os.path.join(tmp, "wrong_size.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        expect_error("wrong image_size", p)

        # ── 7. renaming, and the name field's exact bytes ─────────────────
        print("\nrenaming")

        def do_rename(new_name: str, label: str):
            doc = json.loads(json.dumps(exports[base]["doc"]))
            doc["kits"][0]["name"] = new_name
            p = os.path.join(tmp, f"rename_{abs(hash(label))}.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            out = os.path.join(tmp, f"rename_{abs(hash(label))}.bin")
            r = run([image, p, "-o", out])
            got = read(out) if os.path.isfile(out) else b""
            return r, got

        # NB: a name already used by another kit is refused (tested below),
        # so these are all new names.
        for new_name in ("RENAMED", "MY-KIT", "TR-606", "A", "ABCDEFGHIJKLMNO"):
            r, got = do_rename(new_name, new_name)
            check(f"rename to {new_name!r}: imports", r.returncode == 0)
            if got:
                check(f"rename to {new_name!r}: field is 15 chars + NUL",
                      got[KIT_TABLE:KIT_TABLE + 16] == encode_name(new_name))
                check(f"rename to {new_name!r}: only that kit + checksum move",
                      all(got[i] == data[i] for i in range(len(data))
                          if not (CHECKSUM <= i < CHECKSUM + 4
                                  or KIT_TABLE <= i < KIT_TABLE + 16)))
                # A rename must not disturb any slot.
                kt_end = KIT_TABLE + KIT_SIZE * NUM_KITS
                check(f"rename to {new_name!r}: slot table untouched",
                      got[KIT_TABLE + 16:kt_end] == data[KIT_TABLE + 16:kt_end])

        # A pre-existing duplicate name must still round-trip: the device
        # accepts such an image, so refusing it would strand the user. Only
        # *introducing* a duplicate is an error.
        dup_img = os.path.join(tmp, "dupnames.bin")
        d = bytearray(data)
        d[KIT_TABLE + KIT_SIZE:KIT_TABLE + KIT_SIZE + 16] = \
            d[KIT_TABLE:KIT_TABLE + 16]          # kits 0 and 1 both "BASIC"
        img = r24image.R24Image.from_parts(bytes(d[:0x40]), bytes(d[0x40:]))
        img.refresh_checksum()
        img.write(dup_img)
        dup_bytes = read(dup_img)          # as written, checksum included
        exp = os.path.join(tmp, "dup_export")
        r = run([dup_img, "--export", "-o", exp])
        check("pre-existing duplicate name: export exits 0", r.returncode == 0)
        out = os.path.join(tmp, "dup_round.bin")
        r = run([dup_img, os.path.join(exp, "kits.json"), "-o", out])
        check("pre-existing duplicate name: import exits 0", r.returncode == 0)
        check("pre-existing duplicate name: warns",
              "already" in (r.stderr or ""))
        check("pre-existing duplicate name: byte-identical round-trip",
              os.path.isfile(out) and read(out) == dup_bytes)
        # But renaming *another* kit onto an existing name is refused.
        doc = json.loads(json.dumps(exports[base]["doc"]))
        doc["kits"][1]["name"] = doc["kits"][0]["name"]
        p = os.path.join(tmp, "introduce_dup.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        expect_error("introduced duplicate name", p)

        # Reading back what was written is the real test of a single field:
        # rename -> import -> export -> the name comes back unchanged, and
        # re-importing that export is a no-op.
        r, renamed = do_rename("ROUNDTRIP", "roundtrip")
        check("rename to 'ROUNDTRIP': imports", r.returncode == 0)
        if renamed:
            rt_img = os.path.join(tmp, "rt.bin")
            with open(rt_img, "wb") as fh:
                fh.write(renamed)
            exp = os.path.join(tmp, "rt_export")
            run([rt_img, "--export", "-o", exp])
            doc3 = json.loads(read(os.path.join(exp, "kits.json")).decode())
            check("renamed kit reads back with the same name",
                  doc3["kits"][0]["name"] == "ROUNDTRIP")
            out = os.path.join(tmp, "rt2.bin")
            r2 = run([rt_img, os.path.join(exp, "kits.json"), "-o", out])
            check("re-import of the renamed image is byte-identical",
                  os.path.isfile(out) and read(out) == renamed)
            check("re-import of the renamed image reports 0 changes",
                  "changes : 0" in (r2.stdout or ""))

    print(f"\n{passed}/{passed + failed} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
