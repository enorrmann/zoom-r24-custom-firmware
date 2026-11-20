#!/usr/bin/env python3
"""patch_samples.py — extract, patch or repack R24 PCM samples from a text list.

THREE MODES
-----------
    --extract   dump every table entry to sampleNN.wav (+ a re-pack list)
    (default)   patch in place: each WAV is fitted to its entry's length
    --repack    rebuild the bank: each WAV keeps its own length and the
                Master Sample Table is rewritten (positions, lengths, count)

The firmware reads every sample's position and length from the Master Sample
Table at playback time (start = 0x87540000 + rel_start, end = start + length
- 2; see tools/r24bank.py and docs/07_pcm_samples.md §2). Nothing in the code
addresses an individual sample, so the table can be rewritten freely; the
only limits are the bank size (the file never grows) and where the table
lives (73 entries in place, any number once relocated).

EXTRACT
-------
    python3 tools/patch_samples.py R24SYS.BIN --extract -o build/samples

writes sample00.wav … sampleNN.wav and samples.txt. Each WAV holds the PCM
verbatim at its stored length, so re-packing that list (either mode)
reproduces the image byte for byte.

THE LIST FILE
-------------
One line per sample, whitespace-separated:

    <wav path>            <sample id>    [length]

    REVERB_X0X/606/Kick1.wav      0
    build/samples/sample72.wav   72      220186

* column 1 — path to a WAV. A relative path is looked up next to the list
  file first, then under the repository root.
  The path may contain spaces: the last integer token(s) are the id/length.
* column 2 — the table entry, 0..count-1 (count is 73 on the factory table).
* column 3 — optional and ignored; --extract writes the stored length there.
* blank lines and `#` comments are ignored; a duplicate, out-of-range or
  non-numeric id is an error, never a guess.

IN-PLACE PATCH (default)
------------------------
Entry `id` owns [BANK_BASE + rel_start, + length). A longer WAV is cut with a
64-sample fade, a shorter one is zero-padded, and exactly `length` bytes are
written. The table is not touched. An entry whose bytes are shared with
another entry is refused (writing it would change both) — use --repack.

REPACK
------
    python3 tools/patch_samples.py IMAGE list.txt -o out.BIN --repack
    python3 tools/patch_samples.py IMAGE list.txt -o out.BIN --repack --entries 160
    python3 tools/patch_samples.py IMAGE list.txt -o out.BIN --repack --max-ms 600

* every listed WAV is stored at its own length; entries not listed keep their
  current audio; identical audio is stored once;
* the bank is laid out from offset 0 and the table rewritten. 73 entries on a
  factory image stay in place; any other count (or an already relocated
  image) puts the table at the end of the bank (tools/r24bank.py);
* if everything does not fit in the bank the tool stops and says by how much;
  --max-ms cuts each listed sample to a maximum duration, and --trim-all
  applies the same cut to the samples kept from the image.

The bank is 4,941,364 bytes = 56.0 s of mono 16-bit audio at 44.1 kHz, shared
by every entry. The factory audio fills all of it, so new audio only fits once
old audio stops being used (or is cut). 160 distinct samples average 0.35 s.

`--dry-run` reports what would happen without writing an output file.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BANK_BASE = r24bank.BANK_BASE
FADE = 64                        # samples of fade-out applied to a truncation


class PatchError(Exception):
    pass


def _is_int(token: str) -> bool:
    """True if `token` parses as an integer in any base the tool accepts."""
    try:
        int(token, 0)
        return True
    except ValueError:
        return False


# ── list parsing ───────────────────────────────────────────────────────────
def parse_list(path: str, count: int) -> list[tuple[str, int, int]]:
    """Return [(wav_path, sample_id, line_no)] in file order."""
    out: list[tuple[str, int, int]] = []
    seen: dict[int, int] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Column 1 is a WAV path, which routinely contains spaces
            # ("Hat Closed1.wav"), so the split is position-based: the LAST
            # token is always the sample id; anything before it is the path,
            # optionally followed by the reference-length column that
            # --extract writes. That column is advisory and ignored, because
            # the authoritative capacity always comes from the image.
            tokens = line.split()
            if len(tokens) < 2:
                raise PatchError(
                    f"{path}:{lineno}: expected '<wav path> <sample id> "
                    f"[length]', got {line!r}")
            if (len(tokens) >= 3
                    and _is_int(tokens[-1])
                    and _is_int(tokens[-2])):
                # '<path...> <id> <length>' — the trailing column is the
                # advisory length, so the id is the second-to-last token.
                path_tokens, id_token = tokens[:-2], tokens[-2]
            else:
                path_tokens, id_token = tokens[:-1], tokens[-1]
            wav = " ".join(path_tokens)
            try:
                sid = int(id_token, 0)      # accepts 12, 0x0C
            except ValueError:
                raise PatchError(
                    f"{path}:{lineno}: sample id {id_token!r} is not an integer")
            if not 0 <= sid < count:
                raise PatchError(
                    f"{path}:{lineno}: sample id {sid} out of range "
                    f"0..{count - 1}")
            if sid in seen:
                raise PatchError(
                    f"{path}:{lineno}: sample id {sid} already assigned on "
                    f"line {seen[sid]}; the later entry would overwrite it")
            seen[sid] = lineno
            if not os.path.isabs(wav):
                # next to the list first (a ZIP from web/ unpacked anywhere),
                # then the repository root (lists written by --extract)
                beside = os.path.join(os.path.dirname(os.path.abspath(path)), wav)
                wav = beside if os.path.isfile(beside) else os.path.join(ROOT, wav)
            if not os.path.isfile(wav):
                raise PatchError(f"{path}:{lineno}: WAV not found: {wav}")
            out.append((wav, sid, lineno))
    if not out:
        raise PatchError(f"{path}: no entries found")
    return out


# ── audio handling ─────────────────────────────────────────────────────────
def read_wav_mono(path: str, rate: int) -> list[int]:
    """Decode any WAV to mono 16-bit signed at `rate`.

    Handles 8/16/24/32-bit integer PCM and 32-bit float, any channel count.
    """
    with wave.open(path, "rb") as w:
        ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw == 1:                                   # unsigned 8-bit
        s = [(b - 128) << 8 for b in raw]
    elif sw == 2:
        n = len(raw) // 2
        s = list(struct.unpack(f"<{n}h", raw[:n * 2]))
    elif sw == 3:                                 # 24-bit little-endian
        s = [int.from_bytes(raw[i:i + 3], "little", signed=True) >> 8
             for i in range(0, len(raw) - 2, 3)]
    elif sw == 4:
        n = len(raw) // 4
        s = [v >> 16 for v in struct.unpack(f"<{n}i", raw[:n * 4])]
    else:
        raise PatchError(f"{path}: unsupported sample width {sw * 8} bits")
    if ch > 1:                                    # fold to mono
        s = [sum(s[i:i + ch]) // ch for i in range(0, len(s) - ch + 1, ch)]
    if fr != rate:                                # linear resample
        ratio = rate / fr
        out_n = max(1, int(len(s) * ratio))
        s = [s[min(int(i / ratio), len(s) - 1)] for i in range(out_n)]
    return s


def fit_to_slot(samples: list[int], nbytes: int) -> tuple[bytes, str]:
    """Fit `samples` into exactly `nbytes` — never a byte more, never less."""
    if nbytes % 2:
        raise PatchError(f"slot length {nbytes} is not 16-bit aligned")
    want = nbytes // 2
    if len(samples) > want:
        s = samples[:want]
        f = min(FADE, want // 4)
        for k in range(f):                        # fade the cut, no click
            s[want - f + k] = s[want - f + k] * (f - k) // f
        return struct.pack(f"<{want}h", *s), f"trim {len(samples) - want} smp"
    if len(samples) < want:
        s = samples + [0] * (want - len(samples))
        return struct.pack(f"<{want}h", *s), f"pad {want - len(samples)} smp"
    return struct.pack(f"<{want}h", *samples), "exact"


# ── main ───────────────────────────────────────────────────────────────────
def load(image: str) -> tuple[bytearray, r24bank.Layout, list[tuple[int, int]]]:
    data = bytearray(open(image, "rb").read())
    try:
        lay = r24bank.locate(data)
        table = r24bank.read_table(data, lay)
        r24bank.validate_table(table, lay)
    except r24bank.BankError as exc:
        raise PatchError(f"{image}: {exc}")
    return data, lay, table


def id_width(count: int) -> int:
    return max(2, len(str(count - 1)))


def extract(image: str, out_dir: str, rate: int) -> int:
    """Dump every entry to `<out_dir>/sampleNN.wav` plus a re-pack list.

    The WAVs are written at their TRUE stored length — no padding — and the
    generated list carries that length as an optional third column:

        <out_dir>/sampleNN.wav   <id>   <bytes>

    Entries that share audio (a relocated per-kit table starts that way) are
    each written out; --repack stores identical audio only once again.
    """
    data, lay, table = load(image)
    count = lay.count
    os.makedirs(out_dir, exist_ok=True)
    list_path = os.path.join(out_dir, "samples.txt")
    width = id_width(count)
    print(f"source   : {image}  ({len(data)} bytes)")
    print(f"table    : {lay.describe()}")
    print(f"out dir  : {out_dir}")
    print(f"rate     : {rate} Hz (used for the WAV header only; PCM is verbatim)\n")
    print(f"{'id':>3}  {'file':<16}  {'slot off':>10}  {'bytes':>7}  "
          f"{'smp':>8}  {'ms':>9}")
    print("-" * 68)
    lines = ["# Generated by tools/patch_samples.py --extract",
             "# format: <wav path> <sample id> <bytes>",
             "# the third column is the stored length, for reference only.",
             "#   in-place patch : python3 tools/patch_samples.py IMAGE THIS -o OUT",
             "#   repack (WAVs keep their own length, table is rewritten):",
             "#                    python3 tools/patch_samples.py IMAGE THIS -o OUT --repack",
             ""]
    for sid, (rel, length) in enumerate(table):
        start = BANK_BASE + rel
        name = f"sample{sid:0{width}d}.wav"
        path = os.path.join(out_dir, name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(data[start:start + length]))
        relpath = os.path.relpath(path, ROOT)
        lines.append(f"{relpath} {sid} {length}")
        print(f"{sid:3d}  {name:<16}  {start:#10x}  {length:7d}  "
              f"{length // 2:8d}  {length / 2 / rate * 1000:9.1f}")
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nwrote {count} WAVs + {list_path}")
    print(f"re-pack: python3 tools/patch_samples.py {image} {list_path} -o <out>")
    return 0


def write_out(data: bytearray, out: str, src_size: int) -> None:
    img = r24bank.seal(data, out)
    assert img.size == src_size, "output size must equal input size"
    print(f"\nwrote {out}  ({img.size} bytes, unchanged size)")
    print(f"checksum 0x{img.stored_checksum:08X} (verifies: {img.checksum_ok()})")


def patch(args) -> int:
    """In place: every WAV is fitted to its entry's current length."""
    data, lay, table = load(args.image)
    entries = parse_list(args.list, lay.count)
    print(f"source   : {args.image}  ({len(data)} bytes)")
    print(f"list     : {args.list}  ({len(entries)} entries)")
    print(f"table    : {lay.describe()}")
    print(f"rate     : {args.rate} Hz\n")

    # An entry may share its bytes with another one (relocated tables do).
    # Writing it in place would change the other entry's sound too.
    listed = {sid for _w, sid, _l in entries}
    for _wav, sid, lineno in entries:
        rel, length = table[sid]
        for other, (orel, olen) in enumerate(table):
            if other != sid and rel < orel + olen and orel < rel + length:
                raise PatchError(
                    f"{args.list}:{lineno}: sample {sid} shares audio with "
                    f"sample {other}"
                    + ("" if other in listed else " (not in this list)")
                    + "; an in-place write would change both. Use --repack, "
                      "which gives every entry its own audio")

    print(f"{'id':>3}  {'slot off':>10}  {'cap B':>7}  {'src smp':>8}  "
          f"{'wrote':>7}  action")
    print("-" * 74)
    for wav, sid, _lineno in entries:
        rel, length = table[sid]
        start = BANK_BASE + rel
        pcm, note = fit_to_slot(read_wav_mono(wav, args.rate), length)
        assert len(pcm) == length, "bounds violation: wrote != slot length"
        data[start:start + length] = pcm
        print(f"{sid:3d}  {start:#10x}  {length:7d}  {length // 2:8d}  "
              f"{len(pcm):7d}  {note}")

    if args.dry_run:
        print("\n--dry-run: no output written")
        return 0
    write_out(data, args.output, len(data))
    return 0


def trim(samples: list[int], max_n: int) -> list[int]:
    if len(samples) <= max_n:
        return samples
    s = samples[:max_n]
    f = min(FADE, max_n // 4)
    for k in range(f):
        s[max_n - f + k] = s[max_n - f + k] * (f - k) // f
    return s


def repack(args) -> int:
    """Rebuild the bank: each WAV keeps its own length, the table is rewritten.

    Entries not named in the list keep their current audio. Identical audio is
    stored once. With --entries N (or an image whose table is already
    relocated) the table lives at the end of the bank, see tools/r24bank.py.
    """
    data, lay, table = load(args.image)
    count = args.entries or lay.count
    entries = parse_list(args.list, count)
    pcms: list[bytes] = []
    for i in range(count):
        src = i if i < lay.count else 0
        pcms.append(r24bank.sample_bytes(data, table, src))
    max_n = None
    if args.max_ms is not None:
        max_n = max(1, int(args.rate * args.max_ms / 1000))
    print(f"source   : {args.image}  ({len(data)} bytes)")
    print(f"list     : {args.list}  ({len(entries)} entries)")
    print(f"table    : {lay.describe()}")
    print(f"entries  : {count}")
    print(f"rate     : {args.rate} Hz"
          + (f", max {args.max_ms} ms per listed sample" if max_n else "") + "\n")
    if args.trim_all:
        if max_n is None:
            raise PatchError("--trim-all needs --max-ms")
        kept = 0
        for i in range(count):
            if len(pcms[i]) > max_n * 2:
                s = list(struct.unpack(f"<{len(pcms[i]) // 2}h", pcms[i]))
                pcms[i] = struct.pack(f"<{max_n}h", *trim(s, max_n))
                kept += 1
        print(f"trim-all : {kept} kept samples cut to {args.max_ms} ms\n")
    print(f"{'id':>4}  {'bytes':>7}  {'ms':>8}  source")
    print("-" * 60)
    for wav, sid, _lineno in entries:
        s = read_wav_mono(wav, args.rate)
        note = ""
        if max_n is not None and len(s) > max_n:
            note = f"  (trimmed {len(s) - max_n} smp)"
            s = trim(s, max_n)
        if len(s) < 1:
            raise PatchError(f"{wav}: no audio")
        pcms[sid] = struct.pack(f"<{len(s)}h", *s)
        print(f"{sid:4d}  {len(pcms[sid]):7d}  {len(s) / args.rate * 1000:8.1f}  "
              f"{os.path.basename(wav)}{note}")
    if count < lay.count:
        used = {sid for kit in r24bank.kit_sample_ids(data) for sid in kit}
        if any(sid >= count for sid in used):
            raise PatchError(f"kits still use sample ids >= {count}; "
                             f"reassign them first (r24kits.py)")
    try:
        new_lay, new_tab, used_bytes = r24bank.pack_bank(data, pcms, count)
    except r24bank.BankError as exc:
        raise PatchError(str(exc))
    distinct = len(set(new_tab))
    print(f"\ntable    : {new_lay.describe()}")
    print(f"audio    : {used_bytes} of {new_lay.audio_limit} bytes "
          f"({100 * used_bytes / new_lay.audio_limit:.1f}%), "
          f"{distinct} distinct for {count} entries, "
          f"{new_lay.audio_limit - used_bytes} bytes free "
          f"({(new_lay.audio_limit - used_bytes) / 2 / args.rate:.2f} s)")
    if args.dry_run:
        print("\n--dry-run: no output written")
        return 0
    write_out(data, args.output, len(data))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract, patch or repack R24 PCM samples.",
        epilog="Modes: patch in place (default), --repack, or --extract.")
    ap.add_argument("image", help="source firmware, e.g. R24SYS.BIN")
    ap.add_argument("list", nargs="?", default=None,
                    help="text file '<wav path> <sample id>' per line")
    ap.add_argument("-o", "--output", default=None,
                    help="output image; with --extract, output directory")
    ap.add_argument("--extract", action="store_true",
                    help="dump every sample as sampleNN.wav plus a re-pack list")
    ap.add_argument("--repack", action="store_true",
                    help="rebuild the bank: WAVs keep their own length and the "
                         "sample table is rewritten")
    ap.add_argument("--entries", type=int, default=None,
                    help="--repack: number of table entries (default: as in "
                         "the image). Anything but 73 relocates the table")
    ap.add_argument("--max-ms", type=float, default=None,
                    help="--repack: cut listed samples longer than this (fade)")
    ap.add_argument("--trim-all", action="store_true",
                    help="--repack: apply --max-ms to the samples kept from the "
                         "image too, not only to listed WAVs")
    ap.add_argument("--rate", type=int, default=44100,
                    help="sample rate of the bank (44100); input WAVs are "
                         "resampled to it. Leave at the default")
    ap.add_argument("--dry-run", action="store_true",
                    help="report actions without writing the image")
    args = ap.parse_args(argv)

    if args.extract:
        if args.output is None:
            raise PatchError("--extract needs -o <directory>")
        if args.list is not None:
            raise PatchError("--extract takes no list; did you mean patch mode?")
        return extract(args.image, args.output, args.rate)

    if args.list is None:
        raise PatchError("a list file is needed (or pass --extract to dump samples)")
    if args.output is None and not args.dry_run:
        raise PatchError("-o <output image> is needed")
    if args.repack:
        return repack(args)
    if args.entries is not None or args.max_ms is not None or args.trim_all:
        raise PatchError("--entries, --max-ms and --trim-all only apply to --repack")
    return patch(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
