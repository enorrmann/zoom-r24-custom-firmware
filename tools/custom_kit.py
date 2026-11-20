#!/usr/bin/env python3
"""custom_kit.py — the CUSTOM drum kit, samples loaded from the project folder.

The kit itself is an SDK patch, sdk/patches/custom_kit.c (docs/12_custom_kit.md);
this script is a shortcut for building it alone and for preparing the WAVs.

    install   python3 tools/custom_kit.py install IMAGE -o OUT [--reserve-sec 6] [--no-menu]
              = r24sdk.py build IMAGE sdk/patches/custom_kit.c kit_assign.c
              (kit_assign.c adds KIT ASSIGN to the FILE menu: pick a WAV on
              the R24, choose the pad). To combine with other patches use
              r24sdk.py directly:
                  r24sdk.py build IMAGE sdk/patches/custom_kit.c \
                            sdk/patches/kit_assign.c mine.c -o OUT -D CUSTOM_KIT_RESERVE_SEC=6
    check     python3 tools/custom_kit.py check IMAGE
    prepare   python3 tools/custom_kit.py prepare kick.wav snare.wav - hat.wav \\
                      -o /media/SD/ZOOM_R24/PROJECT/PRJ001/KIT
              converts any WAVs to mono 16-bit 44.1 kHz into KIT\\ (names kept)
              and writes the project's KIT.TXT ('-' skips a pad).
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24image
import r24bank
import r24sdk

PATCH = os.path.join(r24sdk.SDK, "patches", "custom_kit.c")
MENU = os.path.join(r24sdk.SDK, "patches", "kit_assign.c")     # FILE > KIT ASSIGN
EDITOR = os.path.join(r24sdk.SDK, "patches", "kit_editor.c")   # KIT SELECT > CUSTOM editor
RATE = 44100
SLOTS = 16


def reserve_define(reserve_sec: float, ddr: bool = False) -> str:
    """-D for the reserve in the bank tail. With `ddr` (CUSTOM_KIT_DDR: the
    audio also goes to free DDR) 0 is allowed: no bank room, nothing trimmed."""
    nbytes = int(reserve_sec * RATE) * 2
    if nbytes < 4096 and not (ddr and nbytes == 0):
        raise r24sdk.SdkError("--reserve-sec is too small")
    return f"CUSTOM_KIT_RESERVE_BYTES={nbytes}"


DDR_DEFINE = "CUSTOM_KIT_DDR=1"      # pool 3 + the free 3 MiB (docs/20_kit_memory_plan.md)


def install(data: bytearray, reserve_sec: float = 6.0, trim: bool = True,
            menu: bool = True):
    """Build custom_kit.c (+ kit_assign.c) into `data`. -> (build, rel, notes)."""
    return r24sdk.build(data, [PATCH] + ([MENU] if menu else []),
                        [reserve_define(reserve_sec)], trim)


def prepare(wavs: list[str], out: str, first: int = 1) -> list[str]:
    """Convert WAVs into a project's KIT folder, keeping their names, and write
    the project's KIT.TXT (pad -> file) next to that folder."""
    import patch_samples
    if first < 1 or first - 1 + len(wavs) > SLOTS:
        raise r24sdk.SdkError(f"slots run 1..{SLOTS}")
    out = os.path.normpath(out)
    if os.path.basename(out).upper() != "KIT":
        raise r24sdk.SdkError("-o must be the project's KIT folder (…/PROJECT/PRJxxx/KIT)")
    os.makedirs(out, exist_ok=True)
    cfg_path = os.path.join(os.path.dirname(out), "KIT.TXT")
    pads: dict[int, str] = {}
    if os.path.exists(cfg_path):                     # keep the other pads
        for line in open(cfg_path, encoding="latin-1"):
            k, sep, v = line.strip().partition("=")
            if sep and k.strip().isdigit() and 1 <= int(k) <= SLOTS and v.strip():
                pads[int(k)] = v.strip()
    notes, total = [], 0
    for i, src in enumerate(wavs):
        if src in ("-", ""):
            continue
        name = os.path.splitext(os.path.basename(src))[0][:59] + ".WAV"
        if any(c in name for c in "\\/:"):
            raise r24sdk.SdkError(f"{src}: unusable file name")
        s = [max(-32768, min(32767, v)) for v in patch_samples.read_wav_mono(src, RATE)]
        dst = os.path.join(out, name)
        with wave.open(dst, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(struct.pack(f"<{len(s)}h", *s))
        pads[first + i] = name
        total += (2 * len(s) + 31) & ~31
        notes.append(f"pad {first + i:2d}: {src} -> {dst} ({len(s) / RATE:.2f} s)")
    with open(cfg_path, "w", newline="") as f:
        f.write("# ZOOM R24 CUSTOM kit: pad=file in KIT\\ (01 = KICK ... 16 = PERC)\r\n")
        for k in sorted(pads):
            f.write(f"{k:02d}={pads[k]}\r\n")
    notes.append(f"{cfg_path}: {len(pads)} pads")
    notes.append(f"added: {total} bytes of reserve ({total / 2 / RATE:.2f} s)")
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CUSTOM kit loaded from the project folder.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("image")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--reserve-sec", type=float, default=6.0,
                   help="RAM for the kit's audio, seconds of mono 44.1 kHz (default 6)")
    p.add_argument("--no-trim", action="store_true",
                   help="fail instead of shortening factory samples to make room")
    p.add_argument("--no-menu", action="store_true",
                   help="leave out FILE > KIT ASSIGN (kit_assign.c)")
    p = sub.add_parser("check")
    p.add_argument("image")
    p = sub.add_parser("prepare")
    p.add_argument("wavs", nargs="+", help="in slot order; '-' skips a slot")
    p.add_argument("-o", "--output", required=True, help="the project's KIT folder")
    p.add_argument("--first", type=int, default=1, help="slot of the first WAV")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            return r24sdk.main(["check", args.image])
        if args.cmd == "install":
            return r24sdk.main(["build", args.image, PATCH]
                               + ([] if args.no_menu else [MENU]) + ["-o", args.output,
                                "-D", reserve_define(args.reserve_sec)]
                               + (["--no-trim"] if args.no_trim else []))
        for n in prepare(args.wavs, args.output, args.first):
            print(n)
        return 0
    except (r24sdk.SdkError, r24bank.BankError, r24image.ImageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
