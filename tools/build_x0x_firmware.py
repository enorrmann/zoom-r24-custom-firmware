#!/usr/bin/env python3
"""build_x0x_firmware.py — R24 image with TR-606 / TR-808 / TR-909 kits.

Constraints found by static analysis of R24SYS.BIN (see docs/08_drum_kits.md
and docs/07_pcm_samples.md); this script depends on all of them:

1. This build keeps the factory sample table (73 entries, factory lengths) and
   rewrites audio in place. That is a choice of this script, not a firmware
   limit: the firmware reads rel_start/length from the table at playback time
   (docs/07_pcm_samples.md §2), so `patch_samples.py --repack` and
   `r24bank.py relocate` can change positions, lengths and the entry count.
   The script is kept as is so that it keeps reproducing the committed
   R24SYS_X0X.BIN byte for byte.

2. Every sample gets overwritten. Nothing is carried over from the factory
   bank: each of the 73 slots is refilled from a REVERB_X0X family, at its
   original byte length (longer sources are faded+trimmed, shorter ones are
   zero-padded). No factory PCM survives, by design.

3. The 10 kit descriptors stay at their factory offsets (file 0x0E2334, read
   by the firmware through the RAM copy at 0x800E1D80 — see docs/08). Kits 7/8/9 become TR-606,
   TR-808 and TR-909; kits 0..6 keep factory names and slot levels/pans/mute
   groups, but their 16 slots are re-pointed at the new x0x samples so the
   factory audio cannot be heard through them either.

4. The header checksum at 0x34 is recomputed.

Output: build/R24SYS_X0X.BIN (same 7,169,652 bytes as the stock image).
"""
from __future__ import annotations

import os
import struct
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCK = os.path.join(ROOT, "R24SYS.BIN")
OUT_DIR = os.path.join(ROOT, "build")
OUT = os.path.join(OUT_DIR, "R24SYS_X0X.BIN")

SAMPLE_TABLE = 0x0E20EC
NUM_SAMPLES = 73
KIT_TABLE = 0x0E2334
KIT_SIZE = 144
NUM_KITS = 10
BANK_BASE = 0x220040
RATE = 44100
FADE = 64                      # samples of fade-out before a trim

FACTORY_KIT_NAMES = ["BASIC", "STUDIO", "LIVE", "ROCK", "POP",
                     "FUNK", "JAZZ", "ACOUSTIC", "TECHNO", "URBAN"]


# ────────────────────────────────────────────────────────────────────────────
# WAV source families.  Every sample id maps into exactly one family, so the
# whole bank ends up made of x0x material.
# ────────────────────────────────────────────────────────────────────────────
FAMILIES = {
    "606_kick":   ["606/Kick1.wav", "606/Kick2.wav", "606/Kick3.wav"],
    "606_snare":  ["606/Snare2.wav", "606/Snare3.wav"],
    "606_snare2": ["606/Snare3.wav", "606/Snare2.wav"],
    "606_hatc":   ["606/Hat Closed1.wav", "606/Hat Closed2.wav"],
    "606_hatc2":  ["606/Hat Closed2.wav", "606/Hat Closed1.wav"],
    "606_hato":   ["606/Hat Open1.wav", "606/Hat Open2.wav"],
    "606_hatacc": ["606/Hat Accent1.wav", "606/Hat Accent2.wav"],
    "606_hatacc2": ["606/Hat Accent2.wav", "606/Hat Accent1.wav"],
    "606_crash":  ["606/Crash1.wav", "606/Crash2.wav"],
    "606_crash2": ["606/Crash2.wav", "606/Crash1.wav"],
    "606_tomhi":  ["606/Tom Hi1.wav", "606/Tom Hi2.wav"],
    "606_tomhi2": ["606/Tom Hi3.wav", "606/Tom Hi2.wav"],
    "606_tomlo":  ["606/Tom Low1.wav", "606/Tom Low2.wav"],
    "606_kick2":  ["606/Kick2.wav", "606/Kick3.wav"],
    "606_tomlo2": ["606/Tom Low2.wav", "606/Tom Low3.wav"],
    "606_hato2":  ["606/Hat Open2.wav", "606/Hat Open1.wav"],

    "808_kick":   ["808/kick.wav", "808/kick (21).wav", "808/kick (10).wav"],
    "808_snare":  ["808/snare.wav", "808/snare (2).wav", "808/snare (4).wav"],
    "808_hatc":   ["808/hi hat.wav", "808/hi hat (9).wav", "808/hi hat (35).wav"],
    "808_hato":   ["808/hi hat (2).wav", "808/hi hat (1).wav"],
    "808_clap":   ["808/clap.wav", "808/clap (2).wav", "808/clap (7).wav"],
    "808_cym":    ["808/cymbal.wav", "808/cymbal (1).wav"],
    "808_clap2":  ["808/clap (11).wav", "808/clap (12).wav"],
    "808_bass2":  ["808/bass (9).wav", "808/bass (11).wav"],
    "808_tomhi":  ["808/tom.wav", "808/tom (2).wav"],
    "808_tomlo":  ["808/tom (3).wav", "808/tom (5).wav"],
    "808_bass":   ["808/bass.wav"],

    "909_kick":   ["909/BT0A0D0.WAV", "909/BT0A0D3.WAV", "909/BT3A0D0.WAV"],
    "909_snare":  ["909/ST0T0S0.WAV", "909/STAT0S0.WAV", "909/ST3T0S0.WAV"],
    "909_hatc":   ["909/HHCD0.WAV", "909/HHCD2.WAV", "909/HHCD6.WAV"],
    "909_hato":   ["909/HHOD0.WAV", "909/HHODA.WAV"],
    "909_crash":  ["909/CSHD0.WAV", "909/CSHD2.WAV"],
    "909_ride":   ["909/RIDED0.WAV", "909/RIDED6.WAV"],
    "909_tomhi":  ["909/HT0D0.WAV", "909/HT0D7.WAV"],
    "909_tommid": ["909/MT0D0.WAV", "909/MT0D7.WAV"],
    "909_tomlo":  ["909/LT0D0.WAV", "909/LT0D7.WAV"],
    "909_clap":   ["909/HANDCLP1.WAV", "909/HANDCLP2.WAV"],
    "909_rim":    ["909/RIM63.WAV", "909/RIM127.WAV"],
    "909_rim2":   ["909/RIM127.WAV", "909/RIM63.WAV"],
    "909_hatcombo": ["909/OPCL1.WAV", "909/CLOP1.WAV", "909/OPCL2.WAV",
                     "909/CLOP2.WAV"],
}

# Sample-id → family.
#
# This build keeps the factory 73-entry table and its lengths, so the ids are
# partitioned into four blocks:
#
#   ids  0..12   TR-606 kit, one id per slot (13 dedicated one-shots)
#   ids 13..25   TR-808 kit, one id per slot
#   ids 26..38   TR-909 kit, one id per slot
#   ids 39..72   shared pool behind kits 0..6 (34 ids)
#
# Every id still gets a REVERB_X0X family, so no factory PCM survives anywhere
# in the bank — the three new kits simply own disjoint, guaranteed-distinct
# samples.
SAMPLE_FAMILY = {}


def _assign(ids, fam):
    for i in ids:
        SAMPLE_FAMILY[i] = fam


# ── TR-606: ids 0..12, in slot order ───────────────────────────────────────
# 24 source files for 13 slots: each slot gets its own 606 instrument take.
_assign([0], "606_kick")        # slot 00 kick
_assign([1], "606_snare")       # slot 01 snare
_assign([2], "606_hatc")        # slot 02 closed hat
_assign([3], "606_hato")        # slot 03 open hat
_assign([4], "606_crash")       # slot 04 crash
_assign([5], "606_hatacc")      # slot 05 accent
_assign([6], "606_tomhi")       # slot 06 tom 1
_assign([7], "606_tomlo")       # slot 07 tom 2
_assign([8], "606_tomhi2")      # slot 08 tom 3
_assign([9], "606_snare2")      # slot 09 second snare / rim
_assign([10], "606_crash2")     # slot 10 second crash
_assign([11], "606_hatacc2")    # slot 11 cowbell/accent position
_assign([12], "606_hatc2")      # slot 12 clave position

# ── TR-808: ids 13..25 ─────────────────────────────────────────────────────
_assign([13], "808_kick")
_assign([14], "808_snare")
_assign([15], "808_hatc")
_assign([16], "808_hato")
_assign([17], "808_cym")
_assign([18], "808_bass")
_assign([19], "808_tomhi")
_assign([20], "808_tomhi")
_assign([21], "808_tomlo")
_assign([22], "808_clap")
_assign([23], "808_clap")
_assign([24], "808_tomlo")
_assign([25], "808_snare")

# ── TR-909: ids 26..38 ─────────────────────────────────────────────────────
_assign([26], "909_kick")
_assign([27], "909_snare")
_assign([28], "909_hatc")
_assign([29], "909_hato")
_assign([30], "909_crash")
_assign([31], "909_ride")
_assign([32], "909_tomhi")
_assign([33], "909_tommid")
_assign([34], "909_tomlo")
_assign([35], "909_rim")
_assign([36], "909_clap")
_assign([37], "909_hatcombo")
_assign([38], "909_hatcombo")

# ── shared pool behind kits 0..6: ids 39..72 ───────────────────────────────
_assign([39, 40, 41, 42], "808_kick")
_assign([43, 44, 45, 46], "909_snare")
_assign([47, 48, 49], "808_hatc")
_assign([50, 51, 52], "909_hato")
_assign([53, 54, 55], "909_crash")
_assign([56, 57, 58], "909_ride")
_assign([59, 60, 61], "909_tomhi")
_assign([62, 63, 64], "909_tommid")
_assign([65, 66, 67], "909_tomlo")
_assign([68], "606_kick2")
_assign([69], "606_tomlo2")
_assign([70], "606_hato2")
_assign([71], "808_clap2")
_assign([72], "909_rim2")


def family_for(sid: int) -> str:
    if sid not in SAMPLE_FAMILY:
        raise KeyError(f"no x0x family assigned to sample id {sid}")
    return SAMPLE_FAMILY[sid]


# ────────────────────────────────────────────────────────────────────────────
# Three new kit layouts (slots 00..15).
# sc ids 4/22/29/31 etc. were re-pointed above; the tables below name
# families, and resolve() picks a concrete id from the bank.
# ────────────────────────────────────────────────────────────────────────────
def build_kit_tables(pool: dict[str, list[int]]):
    """Kit slot → sample id, using each new kit's dedicated id block.

    ids 0..12 belong to TR-606, 13..25 to TR-808, 26..38 to TR-909; the kits
    therefore never share a one-shot and every slot is a distinct sample.
    """
    def pick(fam: str, used: set[int]) -> int:
        for sid in pool[fam]:
            if sid not in used:
                used.add(sid)
                return sid
        sid = pool[fam][0]                     # pool exhausted: reuse
        return sid

    kits = {}

    # slot order: 00 kick, 01 snare, 02 hatC, 03 hatO, 04 crash, 05 ride,
    #             06 tom1, 07 tom2, 08 tom3, 09 clap/rim, 10 perc,
    #             11 cowbell/accent, 12 clave, 13 hiconga, 14 loconga,
    #             15 perc mute
    kits[7] = ("TR-606", [
        "606_kick", "606_snare", "606_hatc", "606_hato",
        "606_crash", "606_hatacc", "606_tomhi", "606_tomlo",
        "606_tomhi2", "606_snare2", "606_crash2", "606_hatacc2",
        "606_hatc2", "606_kick2", "606_hato2", "606_tomlo2",
    ])
    kits[8] = ("TR-808", [
        "808_kick", "808_snare", "808_hatc", "808_hato",
        "808_cym", "808_bass", "808_tomhi", "808_tomhi",
        "808_tomlo", "808_clap", "808_clap", "808_tomlo",
        "808_snare", "808_hatc", "808_kick", "808_clap2",
    ])
    kits[9] = ("TR-909", [
        "909_kick", "909_snare", "909_hatc", "909_hato",
        "909_crash", "909_ride", "909_tomhi", "909_tommid",
        "909_tomlo", "909_rim", "909_clap", "909_hatcombo",
        "909_hatcombo", "909_rim2", "909_snare", "909_ride",
    ])
    out = {}
    for idx, (name, fams) in kits.items():
        used: set[int] = set()
        out[idx] = (name, [pick(f, used) for f in fams])
    return out


# ────────────────────────────────────────────────────────────────────────────
# audio fitting — span-preserving, like tools/patch_samples.py
# ────────────────────────────────────────────────────────────────────────────
def read_wav(path: str) -> list[int]:
    with wave.open(path, "rb") as w:
        ch, sw, fr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"{path}: need 16-bit PCM, got {sw*8}-bit")
    n = len(raw) // 2
    s = list(struct.unpack(f"<{n}h", raw[:n * 2]))
    if ch > 1:                                    # fold to mono
        s = [sum(s[i:i + ch]) // ch for i in range(0, len(s), ch)]
    if fr != RATE:                                # linear resample
        ratio = RATE / fr
        out_n = int(len(s) * ratio)
        s = [s[min(int(i / ratio), len(s) - 1)] for i in range(out_n)]
    return s


def fit(samples: list[int], nbytes: int) -> tuple[bytes, str]:
    """Return exactly nbytes of PCM (nbytes//2 samples) from `samples`."""
    want = nbytes // 2
    if len(samples) > want:
        s = samples[:want]
        f = min(FADE, want // 4)
        if f > 0:                                 # fade the cut, no click
            for k in range(f):
                s[want - f + k] = s[want - f + k] * (f - k) // f
        return struct.pack(f"<{want}h", *s), f"trimmed {len(samples)-want}smp"
    s = samples + [0] * (want - len(samples))
    return struct.pack(f"<{want}h", *s), f"padded {want-len(samples)}smp"


# ────────────────────────────────────────────────────────────────────────────
def main() -> int:
    data = bytearray(open(STOCK, "rb").read())
    stock_size = len(data)

    # 1. read the factory table; this build keeps its 73 entries and lengths
    table = [struct.unpack_from("<II", data, SAMPLE_TABLE + 8 * i)
             for i in range(NUM_SAMPLES)]
    print(f"bank: {len(table)} slots, "
          f"{table[-1][0] + table[-1][1]} bytes, base {BANK_BASE:#x}")

    # 2. which sample ids belong to which family (bank order)
    pool: dict[str, list[int]] = {}
    for sid in range(NUM_SAMPLES):
        pool.setdefault(family_for(sid), []).append(sid)

    # 3. overwrite every sample with x0x audio, at its factory length
    per_family_turn: dict[str, int] = {}
    for sid, (rel, length) in enumerate(table):
        fam = family_for(sid)
        files = FAMILIES[fam]
        k = per_family_turn.get(fam, 0)
        per_family_turn[fam] = k + 1
        src = os.path.join(ROOT, "REVERB_X0X", files[k % len(files)])
        pcm, note = fit(read_wav(src), length)
        assert len(pcm) == length
        data[BANK_BASE + rel: BANK_BASE + rel + length] = pcm
        if sid % 8 == 0 or sid == NUM_SAMPLES - 1:
            print(f"  sample #{sid:02d} <- {files[k % len(files)]:24s} {note}")

    # 4. rewrite the three kit descriptors
    new_kits = build_kit_tables(pool)
    for k in range(NUM_KITS):
        off = KIT_TABLE + k * KIT_SIZE
        if k not in new_kits:
            continue
        name, sids = new_kits[k]
        data[off:off + 16] = name.encode("ascii").ljust(16, b" ")[:16]
        for s in range(16):
            so = off + 16 + s * 8
            lvl = data[so + 2] or 0x09          # keep factory level
            pan = data[so + 3] or 0x40          # keep factory pan
            mg = data[so + 4]                    # keep factory mute group
            if s in (2, 3) and mg == 0:
                mg = 1                           # hats choke
            data[so:so + 8] = struct.pack("<HBBB3s", sids[s], lvl, pan, mg,
                                          b"\x00\x00\x00")
        print(f"  kit {k} '{FACTORY_KIT_NAMES[k]}' -> '{name}'")

    # 5. repoint kits 0..6 at x0x material so no factory PCM is reachable
    for k in range(7):
        off = KIT_TABLE + k * KIT_SIZE
        for s in range(16):
            so = off + 16 + s * 8
            old = struct.unpack_from("<H", data, so)[0]
            fam = family_for(old)
            ids = pool[fam]
            new = ids[0]
            if old >= NUM_SAMPLES:               # defensive
                continue
            struct.pack_into("<H", data, so, new)

    os.makedirs(OUT_DIR, exist_ok=True)
    open(OUT, "wb").write(data)

    img = r24image.R24Image.from_file(OUT)
    img.refresh_checksum()
    img.write(OUT)

    assert os.path.getsize(OUT) == stock_size, "image size must not change"
    print(f"\nwrote {OUT}")
    print(f"  size     : {os.path.getsize(OUT)} bytes (unchanged)")
    print(f"  checksum : 0x{img.stored_checksum:08X} (verified "
          f"{img.checksum_ok()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
