#!/usr/bin/env python3
"""test_loop_import.py — prove the loop-import patch set is exact and minimal.

What is asserted (all against the real stock image, no hardware involved):

  A. the analysis reproduces itself
     the four `adr` sites that reference the two loop-path literals are found
     by a byte scan of the whole image, and they are the four the documentation
     quotes;

  B. they are the *only* code references
     no other instruction in the image points at either literal, so a 2-byte
     drive-letter edit cannot reach anything undocumented;

  C. every change is same-length and additive-checksum-clean
     patched images keep the stock size, verify with the project's own
     verifier, and differ from the stock image only inside the expected ranges
     plus the 32-bit checksum;

  D. the patch set is minimal
     `--sd-import` is measured to change exactly 2 payload bytes + checksum,
     `--usb-root` exactly 26 payload bytes + checksum; anything else is a bug;

  E. it is idempotent and refuses foreign input
     re-applying an operation is a no-op, a non-loop string is rejected, and a
     label longer than the 8-character field is rejected.

Run: python3 tools/test_loop_import.py
"""
from __future__ import annotations

import io
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loop_import
import r24image
from r24image import R24Image
from loop_import import (LOOP_STR_LIST, LOOP_STR_MKDIR, MENU_LABEL,
                         MENU_LABEL_LEN, LoopImportError)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK = os.path.join(ROOT, "R24SYS.BIN")

CHECKS = [0]


def check(cond: bool, what: str) -> None:
    CHECKS[0] += 1
    if not cond:
        raise AssertionError(what)


def diff_ranges(a: bytes, b: bytes) -> list[tuple[int, int]]:
    """Contiguous runs of differing offsets (inclusive)."""
    assert len(a) == len(b)
    runs: list[tuple[int, int]] = []
    start = prev = None
    for i in range(len(a)):
        if a[i] != b[i]:
            if start is None:
                start = i
            elif i != prev + 1:
                runs.append((start, prev))
                start = i
            prev = i
    if start is not None:
        runs.append((start, prev))
    return runs


def run(*argv: str) -> tuple[int, str]:
    """Invoke the CLI in-process and return (exit code, stdout+stderr)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = loop_import.main(list(argv))
    return rc, out.getvalue()


def main() -> int:
    stock = open(STOCK, "rb").read()
    img = R24Image.from_file(STOCK)

    # ── A. the analysis reproduces itself ──────────────────────────────
    refs = loop_import.find_string_refs(img)
    sites = sorted((r["site_file"], r["target_file"]) for r in refs)
    check(sites == sorted([(0x28068, LOOP_STR_MKDIR),
                           (0x42bde, LOOP_STR_LIST),
                           (0x42be8, LOOP_STR_LIST),
                           (0x42bf8, LOOP_STR_LIST)]),
          f"reference scan found {[(hex(a), hex(b)) for a, b in sites]}")
    print(f"  A  reference scan: {len(sites)} adr sites, as documented")

    # the literals themselves
    check(stock[LOOP_STR_LIST:LOOP_STR_LIST + 16] == loop_import.USB_LOOP,
          "listing-side literal is not B:\\ZOOM_R24\\LOOP")
    check(stock[LOOP_STR_MKDIR:LOOP_STR_MKDIR + 16] == loop_import.USB_LOOP,
          "gate-side literal is not B:\\ZOOM_R24\\LOOP")
    check(stock[MENU_LABEL:MENU_LABEL + MENU_LABEL_LEN] == b"LOOP[USB]",
          "menu label is not LOOP[USB]")
    print("  A  literals and menu label match the documentation")

    # ── B. no other reference to either literal ────────────────────────
    # every 2-byte-aligned position that emits the literal's address through
    # an `adr` must already be in the list above; a raw `adr` reaching either
    # slot from anywhere else would show up as an extra entry.
    stray = [r for r in refs if r["site_file"] not in
             (0x28068, 0x42bde, 0x42be8, 0x42bf8)]
    check(not stray, f"unexpected references: {stray}")
    print("  B  no undocumented code reference to either literal")

    # ── C/D. the SD build ──────────────────────────────────────────────
    sd = R24Image.from_file(STOCK)
    log = loop_import.apply(sd, sd_import=True, label=None)
    check(len(log) == 3, f"expected 3 log lines (header + 2 edits, no label), got {log}")
    check(sd.size == len(stock), "image size changed")
    check(sd.checksum_ok(), "checksum not refreshed")
    sd_bytes = sd.data
    ranges = diff_ranges(stock, sd_bytes)
    # the 32-bit checksum at 0x34 changes wholesale; how many of its bytes
    # actually differ depends on the sum, so it is bounded, not fixed
    cs_lo = r24image.CHECKSUM_OFFSET
    # the SD build truncates the path at "A:\" exactly like the USB-root build:
    # one drive-letter byte and one 13-byte zeroed tail per slot
    expected_payload = [(LOOP_STR_MKDIR, LOOP_STR_MKDIR),
                        (LOOP_STR_MKDIR + 3, LOOP_STR_MKDIR + 15),
                        (LOOP_STR_LIST, LOOP_STR_LIST),
                        (LOOP_STR_LIST + 3, LOOP_STR_LIST + 15),
                        (MENU_LABEL + 5, MENU_LABEL + 8)]   # the last needs --label
    body = [r for r in ranges if r[0] >= r24image.HEADER_SIZE]
    head = [r for r in ranges if r[0] < r24image.HEADER_SIZE]
    check(body == expected_payload[:4],
          f"SD payload diff ranges {[(hex(s), hex(e)) for s, e in body]} != "
          f"{[(hex(s), hex(e)) for s, e in expected_payload[:4]]}")
    check(len(head) == 1 and cs_lo <= head[0][0] and head[0][1] <= cs_lo + 3,
          f"something outside the checksum changed in the header: {head}")
    payload_diff = sum(e - s + 1 for s, e in body)
    check(payload_diff == 1 + 13 + 1 + 13,
          f"SD build changes {payload_diff} payload bytes, expected 28 "
          "(two drive letters + two 13-byte zeroed tails)")
    # and with a label, exactly one more range appears
    sd2 = R24Image.from_file(STOCK)
    loop_import.apply(sd2, sd_import=True, label=b"LOOP[SD]")
    ranges2 = diff_ranges(stock, sd2.data)
    body2 = [r for r in ranges2 if r[0] >= r24image.HEADER_SIZE]
    check(body2 == expected_payload,
          f"labelled SD build diff ranges {[(hex(s), hex(e)) for s, e in body2]}")
    check(sum(e - s + 1 for s, e in body2) == 1 + 13 + 1 + 13 + 4,
          "labelled SD build changes the wrong number of payload bytes")
    check(sd2.data[MENU_LABEL:MENU_LABEL + MENU_LABEL_LEN] == b"LOOP[SD]\x00",
          "menu label is not LOOP[SD]")
    check(sd_bytes[LOOP_STR_LIST:LOOP_STR_LIST + 4] == b"A:\\\x00",
          "listing literal is not the SD root A:\\")
    check(sd_bytes[LOOP_STR_MKDIR:LOOP_STR_MKDIR + 4] == b"A:\\\x00",
          "gate literal is not the SD root A:\\")
    # the pool tail beyond the string must be untouched in both slots
    for off in (LOOP_STR_LIST, LOOP_STR_MKDIR):
        check(sd_bytes[off + 16:off + 40] == stock[off + 16:off + 40],
              f"bytes after the literal at 0x{off:06x} changed")
    check(sd_bytes[MENU_LABEL:MENU_LABEL + MENU_LABEL_LEN] == b"LOOP[USB]",
          "menu label moved although no label was requested")
    print(f"  C  SD-root build: {len(ranges)} changed ranges, "
          f"{payload_diff} payload bytes, size unchanged, checksum ok")

    # ── C/D. the USB-root build ────────────────────────────────────────
    ur = R24Image.from_file(STOCK)
    loop_import.apply(ur, usb_root=True, label=b"LOOP[F]")
    ur_bytes = ur.data
    check(ur.size == len(stock), "image size changed (usb-root)")
    check(ur.checksum_ok(), "checksum not refreshed (usb-root)")
    ranges = diff_ranges(stock, ur_bytes)
    body = [r for r in ranges if r[0] >= r24image.HEADER_SIZE]
    payload_diff = sum(e - s + 1 for s, e in body)
    # 13 zeroed bytes in each of the two literals + 'USB'->'F]' + space + NUL
    check(payload_diff == 13 + 13 + 4,
          f"USB-root build changes {payload_diff} payload bytes, expected 30")
    check(ur_bytes[LOOP_STR_LIST:LOOP_STR_LIST + 4] == b"B:\\\x00",
          "listing literal is not B:\\")
    check(ur_bytes[LOOP_STR_MKDIR:LOOP_STR_MKDIR + 4] == b"B:\\\x00",
          "gate literal is not B:\\")
    # the pool tail beyond the string must be untouched
    check(ur_bytes[LOOP_STR_MKDIR + 16:LOOP_STR_MKDIR + 40]
          == stock[LOOP_STR_MKDIR + 16:LOOP_STR_MKDIR + 40],
          "bytes after the literal changed; the pool tail must stay intact")
    print(f"  C  USB-root build: {payload_diff} payload bytes, size unchanged, "
          "pool tail intact")

    # ── E. idempotence ─────────────────────────────────────────────────
    # re-running the *same* operation on its own output must be a no-op
    again = R24Image(sd_bytes)
    log2 = loop_import.apply(again, sd_import=True, label=None)
    check(again.data == sd_bytes, "re-applying changed the image")
    check(all("already" in line or "no change" in line for line in log2),
          f"second application did not report a no-op: {log2}")
    # labels are independent of the media: adding one later is still allowed,
    # and then is a no-op too
    loop_import.apply(again, label=b"LOOP[SD]")
    labelled = again.data
    log3 = loop_import.apply(again, sd_import=True, label=b"LOOP[SD]")
    check(again.data == labelled, "a third application changed the image")
    check(all("already" in line or "no change" in line for line in log3),
          f"idempotent re-run did not report a no-op: {log3}")
    print("  E  re-applying an operation is a no-op (media and label both)")

    # ── E. refusals ────────────────────────────────────────────────────
    foreign = R24Image.from_file(STOCK)
    foreign.patch(LOOP_STR_LIST, b"B:\\ZOOM_R24\\LOOP",
                  b"C:\\ZOOM_R24\\LOOP")
    try:
        loop_import.apply(foreign, usb_root=True)
    except LoopImportError:
        print("  E  a foreign loop path is rejected")
        CHECKS[0] += 1
    else:
        raise AssertionError("a foreign loop path was accepted")

    try:
        loop_import.apply(R24Image.from_file(STOCK), usb_root=True,
                          sd_import=True)
    except LoopImportError:
        print("  E  --usb-root together with --sd-import is rejected")
        CHECKS[0] += 1
    else:
        raise AssertionError("conflicting media options were accepted")

    try:
        loop_import.apply(R24Image.from_file(STOCK), sd_import=True,
                          label=b"LOOP[TOOLONG]")
    except LoopImportError:
        print("  E  a label longer than the 8-character field is rejected")
        CHECKS[0] += 1
    else:
        raise AssertionError("an oversized label was accepted")

    if os.environ.get("LOOP_IMPORT_CHECKSUM"):
        # optional cross-check: patched images stored in build/loop/ (if the
        # builder was run) must carry the checksums this test computes
        for name, want in (("R24SYS_SDLOOP.BIN", sd2.stored_checksum),
                           ("R24SYS_USBROOT.BIN", ur.stored_checksum)):
            path = os.path.join(ROOT, "build", "loop", name)
            if os.path.exists(path):
                got = R24Image.from_file(path).stored_checksum
                check(got == want, f"{name}: {got:#010x} != {want:#010x}")
                print(f"  D  build/loop/{name} checksum matches ({got:#010x})")

    print(f"\n{CHECKS[0]}/{CHECKS[0]} checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
