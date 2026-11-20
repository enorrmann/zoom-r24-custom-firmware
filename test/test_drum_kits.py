#!/usr/bin/env python3
"""Verification of all drum kit architecture claims from docs/08_drum_kits.md.

Every assertion here corresponds to a specific claim in the documentation.
If any check fails, the corresponding claim in the doc is wrong.
"""
import struct
import sys
import os

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "R24SYS.BIN")

with open(BIN, "rb") as f:
    data = f.read()

passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"  ok    {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}")
        failed += 1

# ═══════════════════════════════════════════════════════════════════════
print("file basics")
check("file size is 7,169,652 bytes", len(data) == 7169652)

# ═══════════════════════════════════════════════════════════════════════
print("\nmaster sample table geometry (offset 0x000E20EC)")

TABLE_OFFSET = 0x0E20EC
TABLE_END    = 0x0E2334
SAMPLE_BASE  = 0x220040
NUM_SAMPLES  = 73

check("table occupies 584 bytes (73 × 8)", TABLE_END - TABLE_OFFSET == 73 * 8)

samples = []
for i in range(NUM_SAMPLES):
    pos = TABLE_OFFSET + i * 8
    rel, length = struct.unpack("<2I", data[pos:pos+8])
    samples.append((rel, length))

# contiguity: entry[n].rel + entry[n].length == entry[n+1].rel
contiguous = all(
    samples[i][0] + samples[i][1] == samples[i+1][0]
    for i in range(NUM_SAMPLES - 1)
)
check("all 73 samples are perfectly contiguous", contiguous)

# first sample starts at 0
check("first sample rel_start == 0", samples[0][0] == 0)

# total length fills to end of file
total_pcm = samples[-1][0] + samples[-1][1]
check(f"total PCM = 0x{total_pcm:x} fills to EOF", SAMPLE_BASE + total_pcm == len(data))

# last sample
check("entry 72 rel_start == 0x480a1a", samples[72][0] == 0x480A1A)
check("entry 72 length    == 0x035c1a", samples[72][1] == 0x035C1A)

# all samples have nonzero length
check("no zero-length samples", all(s[1] > 0 for s in samples))

# all lengths are even (16-bit samples)
check("all lengths are 16-bit aligned (even)", all(s[1] % 2 == 0 for s in samples))

# ═══════════════════════════════════════════════════════════════════════
print("\ndrum kit table geometry (offset 0x000E2334)")

KIT_TABLE_OFFSET = 0x0E2334
KIT_TABLE_END    = 0x0E28D4
KIT_SIZE         = 0x90  # 144 bytes
NUM_KITS         = 10

check("kit table = 1,440 bytes (10 × 144)", KIT_TABLE_END - KIT_TABLE_OFFSET == NUM_KITS * KIT_SIZE)

EXPECTED_NAMES = ["BASIC", "STUDIO", "LIVE", "ROCK", "POP", "FUNK", "JAZZ", "ACOUSTIC", "TECHNO", "URBAN"]
EXPECTED_OFFSETS = [0x0E2334, 0x0E23C4, 0x0E2454, 0x0E24E4, 0x0E2574, 0x0E2604, 0x0E2694, 0x0E2724, 0x0E27B4, 0x0E2844]

for idx in range(NUM_KITS):
    off = KIT_TABLE_OFFSET + idx * KIT_SIZE
    name_raw = data[off:off+16]
    name = name_raw.split(b'\x00')[0].decode('ascii', errors='replace').strip()
    check(f"kit {idx} offset == {hex(EXPECTED_OFFSETS[idx])}", off == EXPECTED_OFFSETS[idx])
    check(f"kit {idx} name == '{EXPECTED_NAMES[idx]}'", name == EXPECTED_NAMES[idx])

# ═══════════════════════════════════════════════════════════════════════
print("\nkit slot structure (8 bytes per slot, 16 slots per kit)")

# Documented sample_id assignments (slot 0 = Kick across all 10 kits)
KICK_IDS  = [56, 21, 13, 25, 20,  3, 24, 43, 65, 60]
SNARE_IDS = [70, 19,  0, 14,  7, 37, 27, 17, 68, 62]

for idx in range(NUM_KITS):
    off = KIT_TABLE_OFFSET + idx * KIT_SIZE + 16  # skip name
    kick_id,  = struct.unpack("<H", data[off:off+2])
    snare_id, = struct.unpack("<H", data[off+8:off+10])
    check(f"{EXPECTED_NAMES[idx]} kick  sample_id == {KICK_IDS[idx]}", kick_id == KICK_IDS[idx])
    check(f"{EXPECTED_NAMES[idx]} snare sample_id == {SNARE_IDS[idx]}", snare_id == SNARE_IDS[idx])

# all sample_ids in kits are 0..72
all_ids_valid = True
for k in range(NUM_KITS):
    for s in range(16):
        off = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + s * 8
        sid, = struct.unpack("<H", data[off:off+2])
        if sid > 72:
            all_ids_valid = False
check("all sample_ids in all kits are in range 0..72", all_ids_valid)

# all 73 samples are used by at least one kit
used = set()
for k in range(NUM_KITS):
    for s in range(16):
        off = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + s * 8
        sid, = struct.unpack("<H", data[off:off+2])
        used.add(sid)
check("all 73 samples are referenced by preset kits (zero unused)", used == set(range(73)))

# ═══════════════════════════════════════════════════════════════════════
print("\nslot byte 3 (pan) values")

# Documented: kick and snare always have pan = 0x40 (64, center)
for k in range(NUM_KITS):
    off_kick  = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 0 * 8 + 3
    off_snare = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 1 * 8 + 3
    check(f"{EXPECTED_NAMES[k]} kick  pan == 0x40 (center)", data[off_kick] == 0x40)
    check(f"{EXPECTED_NAMES[k]} snare pan == 0x40 (center)", data[off_snare] == 0x40)

# ═══════════════════════════════════════════════════════════════════════
print("\nslot byte 4 (mute_group) values")

# Documented: slots 2 and 3 (hi-hats) use mute_group 1 for kits 0..8, and 0 for URBAN
for k in range(NUM_KITS):
    off2 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 2 * 8 + 4
    off3 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 3 * 8 + 4
    expected_mg = 0 if EXPECTED_NAMES[k] == "URBAN" else 1
    check(f"{EXPECTED_NAMES[k]} closed HH mute_group == {expected_mg}", data[off2] == expected_mg)
    check(f"{EXPECTED_NAMES[k]} open   HH mute_group == {expected_mg}", data[off3] == expected_mg)

# Documented: slots 11 and 12 (cowbell/clave) use mute_group 2 across all 10 kits
for k in range(NUM_KITS):
    off11 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 11 * 8 + 4
    off12 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 12 * 8 + 4
    check(f"{EXPECTED_NAMES[k]} slot 11 (cowbell) mute_group == 2", data[off11] == 2)
    check(f"{EXPECTED_NAMES[k]} slot 12 (clave)   mute_group == 2", data[off12] == 2)

# Documented: slots 13 and 14 (congas) use mute_group 3 across all 10 kits
for k in range(NUM_KITS):
    off13 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 13 * 8 + 4
    off14 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 14 * 8 + 4
    check(f"{EXPECTED_NAMES[k]} slot 13 (hi conga)  mute_group == 3", data[off13] == 3)
    check(f"{EXPECTED_NAMES[k]} slot 14 (low conga) mute_group == 3", data[off14] == 3)

# Documented: slot 15 uses mute_group 3 in FUNK, 0 in other kits
for k in range(NUM_KITS):
    off15 = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + 15 * 8 + 4
    expected_15 = 3 if EXPECTED_NAMES[k] == "FUNK" else 0
    check(f"{EXPECTED_NAMES[k]} slot 15 mute_group == {expected_15}", data[off15] == expected_15)

# ═══════════════════════════════════════════════════════════════════════
print("\nauxiliary percussion uniformity (slots 09-15)")

# Documented: slots 9..15 have the same sample_id across all 10 kits
AUX_EXPECTED = {9: 35, 10: 45, 11: 26, 12: 23, 13: 15, 14: 9, 15: 12}
for slot, expected_id in AUX_EXPECTED.items():
    for k in range(NUM_KITS):
        off = KIT_TABLE_OFFSET + k * KIT_SIZE + 16 + slot * 8
        sid, = struct.unpack("<H", data[off:off+2])
        if sid != expected_id:
            check(f"slot {slot} uniform across kits (expected #{expected_id})", False)
            break
    else:
        check(f"slot {slot:02d} uniform across all 10 kits (sample #{expected_id})", True)

# ═══════════════════════════════════════════════════════════════════════
print("\nhi-hat sharing patterns")

def get_sample(kit_idx, slot_idx):
    off = KIT_TABLE_OFFSET + kit_idx * KIT_SIZE + 16 + slot_idx * 8
    return struct.unpack("<H", data[off:off+2])[0]

# Documented sharing:
# BASIC/ROCK share hats #08/#10
check("BASIC & ROCK share closed HH #08", get_sample(0,2) == 8 and get_sample(3,2) == 8)
check("BASIC & ROCK share open   HH #10", get_sample(0,3) == 10 and get_sample(3,3) == 10)

# POP/FUNK share #38/#39
check("POP & FUNK share closed HH #38", get_sample(4,2) == 38 and get_sample(5,2) == 38)
check("POP & FUNK share open   HH #39", get_sample(4,3) == 39 and get_sample(5,3) == 39)

# LIVE/JAZZ/ACOUSTIC share #34/#46
check("LIVE/JAZZ/ACOUSTIC share closed HH #34",
      get_sample(2,2) == 34 and get_sample(6,2) == 34 and get_sample(7,2) == 34)
check("LIVE/JAZZ/ACOUSTIC share open   HH #46",
      get_sample(2,3) == 46 and get_sample(6,3) == 46 and get_sample(7,3) == 46)

# STUDIO has dedicated hats #47/#50
check("STUDIO has dedicated closed HH #47", get_sample(1,2) == 47)
check("STUDIO has dedicated open   HH #50", get_sample(1,3) == 50)

# ═══════════════════════════════════════════════════════════════════════
print("\nfirmware code: kit indexer (VA 0x020CBC0E)")

# Documented: movs r0, #0x90; muls r0, r1, r0
# At file offset 0x4bc4e (VA 0x020CBC0E)
off = 0x4BC4E
insn1 = struct.unpack("<H", data[off:off+2])[0]     # movs r1, r0 (0x0001)
insn2 = struct.unpack("<H", data[off+2:off+4])[0]   # movs r0, #0x90 (0x2090)
insn3 = struct.unpack("<H", data[off+4:off+6])[0]   # muls r0, r1, r0 (0x4348)
check("VA 0x020CBC0E: movs r1, r0 (opcode 0x0001)", insn1 == 0x0001)
check("VA 0x020CBC10: movs r0, #0x90 (opcode 0x2090)", insn2 == 0x2090)
check("VA 0x020CBC12: muls r0, r1, r0 (opcode 0x4348)", insn3 == 0x4348)

# ═══════════════════════════════════════════════════════════════════════
print("\nfirmware code: bounds check (VA 0x020AF24A)")

# Documented: cmp r0, #9 at file offset 0x2F28A
off = 0x2F28A
insn = struct.unpack("<H", data[off:off+2])[0]
check("VA 0x020AF24A: cmp r0, #9 (opcode 0x2809)", insn == 0x2809)

# ═══════════════════════════════════════════════════════════════════════
print("\nno padding between kit table end and velocity curves")

# Documented: immediately after URBAN at 0xE28D4, velocity data begins (no zero padding)
first_bytes_after = data[0xE28D4:0xE28D8]
check("no zero padding after kit table (velocity data starts)", first_bytes_after != b'\x00\x00\x00\x00')

# ═══════════════════════════════════════════════════════════════════════
print("\nerased flash gap (potential relocation space)")

gap_start = 0x1EE950
gap_end   = 0x220040
gap = data[gap_start:gap_end]
check(f"erased flash gap {hex(gap_start)}..{hex(gap_end)} is all 0xFF",
      all(b == 0xFF for b in gap))
check(f"gap size is 202,480 bytes", gap_end - gap_start == 202480)

# ═══════════════════════════════════════════════════════════════════════
print("\nsample table rel_start values are NOT duplicated outside the table")

# The sample offsets only appear once (in the table itself), not hardcoded in code
TABLE_BYTES = data[TABLE_OFFSET:TABLE_END]
code_region = data[0x40:TABLE_OFFSET] + data[TABLE_END:SAMPLE_BASE]
duped_offsets = 0
for i in range(1, NUM_SAMPLES):  # skip entry 0 whose rel_start=0
    rel = struct.pack("<I", samples[i][0])
    if rel in code_region:
        duped_offsets += 1
check("sample rel_start values NOT found outside the table (fully table-driven)", duped_offsets == 0)

# ═══════════════════════════════════════════════════════════════════════
print("\nPCM audio plausibility (peak, smoothness)")

import math
for i in [0, 36, 72]:
    rel, length = samples[i]
    file_start = SAMPLE_BASE + rel
    pcm = data[file_start:file_start+length]
    shorts = struct.unpack(f"<{len(pcm)//2}h", pcm)
    peak = max(abs(s) for s in shorts)
    rms = math.sqrt(sum(s*s for s in shorts) / len(shorts))
    check(f"sample #{i}: peak={peak} (near 32768), rms={rms:.0f} (>1000)", peak >= 30000 and rms > 1000)

# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"{passed + failed} checks: {passed} passed, {failed} failed")
if failed:
    print("SOME CHECKS FAILED — documentation claims may be incorrect")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED — every documented claim verified")
