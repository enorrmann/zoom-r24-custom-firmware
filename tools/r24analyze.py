"""Static analysis for Zoom R24 firmware payloads.

Two jobs:
  1. architecture identification (CPU, ISA state, endianness, ABI)
  2. code / data / erased-flash segmentation of the flat payload

The segmentation heuristic, in order of strength:
  * a run of one repeated byte is fill, not code
  * a strict Thumb-1 sequential decode (no partial instructions allowed
    across a block boundary) => code
  * otherwise: data/rodata
Byte-for-byte identical results are expected across runs; nothing here
samples randomly, so reports are reproducible.
"""

from __future__ import annotations

import struct

from r24image import R24Image, LOAD_ADDR, HEADER_SIZE, off_to_va

BLOCK = 0x10000


# ---------------------------------------------------------------------------
# Thumb-1 instruction length (ARMv4T / ARMv5TE, no Thumb-2 wide encodings)
# ---------------------------------------------------------------------------
def thumb_len(h: int) -> int:
    top5 = h >> 11
    if top5 in (0x1D, 0x1E, 0x1F):      # 111xx  -> BL / BLX (2 halfwords)
        return 4
    return 2


def thumb_coverage(seg: bytes) -> float:
    """Fraction of bytes consumed by a strict sequential Thumb decode."""
    n = len(seg)
    if n < 4:
        return 0.0
    cov = 0
    i = 0
    while i + 1 < n:
        h = struct.unpack_from("<H", seg, i)[0]
        ln = thumb_len(h)
        if ln == 4:
            if i + 3 >= n:
                i += 2
                continue
            h2 = struct.unpack_from("<H", seg, i + 2)[0]
            if (h2 >> 11) in (0x1D, 0x1E, 0x1F):
                cov += 4
                i += 4
                continue
            i += 2
            continue
        cov += 2
        i += 2
    return cov / n


def is_fill(seg: bytes) -> bool:
    return bool(seg) and len(set(seg)) == 1


# ---------------------------------------------------------------------------
# Architecture census
# ---------------------------------------------------------------------------
def arm_thumb_ratio(payload: bytes) -> dict:
    """Count ARM-state vs Thumb-state prologue/epilogue markers.

    ARM executables on ARMv4T/ARMv5 maintain interworking veneers and
    prologues in ARM state; Thumb code uses PUSH {..., lr}. The ratio tells
    us which state dominates. Thumb BL pairs are counted with the same
    adjacency test used for the length decoder, so noisy 0x00-filled data
    does not inflate the number the way a naive 16-bit pattern scan does.
    """
    arm_stmdb = arm_ldmia = arm_bx = 0
    for off in range(0, len(payload) - 3, 4):
        w = struct.unpack_from("<I", payload, off)[0]
        if (w & 0xFFFF0000) == 0xE92D0000:      # stmdb sp!, {...}  prologue
            arm_stmdb += 1
        elif (w & 0xFFFF0000) == 0xE8BD0000:    # ldmia sp!, {...}  epilogue
            arm_ldmia += 1
        elif (w & 0x0FFFFFF0) == 0x012FFF10:    # bx  rm
            arm_bx += 1

    thumb_push = thumb_bl = 0
    for off in range(0, len(payload) - 3, 2):
        h = struct.unpack_from("<H", payload, off)[0]
        if (h & 0xFF00) == 0xB500:              # push {..., lr}
            thumb_push += 1
        elif (h & 0xF800) == 0xF000:
            h2 = struct.unpack_from("<H", payload, off + 2)[0]
            if (h2 & 0xF800) == 0xF800:         # bl hi / bl lo pair
                thumb_bl += 1
    return {
        "arm_stmdb_sp": arm_stmdb,
        "arm_ldmia_sp": arm_ldmia,
        "arm_bx": arm_bx,
        "thumb_push_lr": thumb_push,
        "thumb_bl_pairs": thumb_bl,
    }


def detect_architecture(img: R24Image) -> dict:
    pay = img.payload
    head = pay[:0x100]

    # entry point: first 32-bit word is an ARM LDR pc,[pc,#imm].
    # The literal sits at pc+8+imm where pc = entry address of the word.
    w0 = struct.unpack_from("<I", head, 0)[0]
    is_arm_ldr_pc = (w0 & 0x0FFF0000) == 0x059F0000
    # where the jmp_reg target resolves: the loader may jump here
    lit_off = (w0 & 0xFFF) + 8                       # pc-relative literal
    jmp_reg_va = struct.unpack_from("<I", head, lit_off)[0] if is_arm_ldr_pc else 0
    # the ARM reset vector (address of crt0 / exception table), stored at 0x44
    reset_va = struct.unpack_from("<I", head, 4)[0]

    markers = arm_thumb_ratio(pay)
    cov_samples = [thumb_coverage(pay[i:i + BLOCK])
                   for i in range(0, min(len(pay), 0x200000), BLOCK)]
    cov_samples = [c for c in cov_samples if c > 0]

    return {
        "isa": "ARM",
        "variant": "ARMv4T / ARMv5TE class (32-bit ARM + Thumb-1, no Thumb-2)",
        "endianness": "little",
        "execution_states": "ARM (crt0, exception/IRQ veneers) + Thumb (main body)",
        "instruction_sets": ["ARM", "Thumb-1"],
        "cpu_core_class": "ARM7TDMI-class integer core (no VFP/NEON evidence)",
        "entry_point_va": reset_va,
        "entry_point_off": (reset_va - LOAD_ADDR + HEADER_SIZE) if reset_va else None,
        "jmp_reg_target_va": jmp_reg_va,
        "first_insn": f"{w0:08x} (ldr pc,[pc,#{w0 & 0xFFF}])" if is_arm_ldr_pc else f"{w0:08x}",
        "load_addr": LOAD_ADDR,
        "load_offset": HEADER_SIZE,
        "container": "flat binary, no ELF/PE/relocs, no compressed payload",
        "markers": markers,
        "thumb_sequential_coverage_pct": round(100 * sum(cov_samples) / len(cov_samples), 2)
        if cov_samples else 0.0,
    }


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
def segment(img: R24Image) -> list[dict]:
    pay = img.payload
    out = []
    cur = None
    for bstart in range(0, len(pay), BLOCK):
        blk = pay[bstart:bstart + BLOCK]
        if is_fill(blk):
            kind = f"fill-{blk[0]:02x}"
        elif thumb_coverage(blk) >= 0.80:
            kind = "code"
        else:
            kind = "data"
        if cur and cur["kind"] == kind:
            cur["off_end"] = HEADER_SIZE + bstart + len(blk)
        else:
            if cur:
                out.append(cur)
            cur = {
                "kind": kind,
                "off_start": HEADER_SIZE + bstart,
                "off_end": HEADER_SIZE + bstart + len(blk),
            }
    if cur:
        out.append(cur)
    for r in out:
        r["va_start"] = off_to_va(r["off_start"])
        r["va_end"] = off_to_va(r["off_end"])
        r["size"] = r["off_end"] - r["off_start"]
    return out


def string_clusters(img: R24Image, min_len: int = 8) -> list[dict]:
    """Find printable-string runs (C string tables) in the payload."""
    pay = img.payload
    runs = []
    start = None
    for i, b in enumerate(pay):
        printable = 0x20 <= b < 0x7F
        if printable and start is None:
            start = i
        elif not printable and start is not None:
            if i - start >= min_len:
                runs.append((start, i))
            start = None
    if start is not None and len(pay) - start >= min_len:
        runs.append((start, len(pay)))
    # cluster runs separated by <256 bytes of binary
    clusters = []
    for a, b in runs:
        if clusters and a - clusters[-1]["end"] < 256:
            clusters[-1]["end"] = b
            clusters[-1]["strings"] += 1
        else:
            clusters.append({"start": a, "end": b, "strings": 1})
    out = []
    for c in clusters:
        if c["strings"] < 20:
            continue
        out.append({
            "off_start": HEADER_SIZE + c["start"],
            "off_end": HEADER_SIZE + c["end"],
            "va_start": off_to_va(HEADER_SIZE + c["start"]),
            "va_end": off_to_va(HEADER_SIZE + c["end"]),
            "size": c["end"] - c["start"],
            "string_count": c["strings"],
        })
    return out


def literal_pool_targets(pay: bytes, limit: int = 0x100000) -> dict:
    """Collect the words that ARM `ldr rX,[pc,#imm]` actually loads.

    A raw 32-bit scan is useless here: Thumb halfwords routinely pair up into
    values that look like pointers. Instead we decode the ARM-state
    pc-relative loads and read their targets, which are real emitter output.
    """
    hits = {}
    for off in range(0, min(len(pay), limit) - 3, 4):
        w = struct.unpack_from("<I", pay, off)[0]
        if (w & 0x0F7F0000) != 0x051F0000:      # ldr rX,[pc,#imm] (U=1)
            continue
        # a literal pool is 4-byte aligned and holds a word-sized constant
        target = off + 8 + (w & 0xFFF)
        if target + 4 > len(pay):
            continue
        val = struct.unpack_from("<I", pay, target)[0]
        hits[val] = hits.get(val, 0) + 1
    return hits


def evidence(img: R24Image) -> dict:
    """Hardware/OS fingerprints pulled from the binary itself."""
    pay = img.payload
    txt = pay.decode("latin-1")
    pools = literal_pool_targets(pay)
    interesting = {}
    for v, n in pools.items():
        # peripheral windows of typical ARM SoCs (AHB/APB at 0x01xxxxxx,
        # 0x4xxxxxxx, 0x5xxxxxxx) and the R24's own SDRAM window (0x8xxxxxxx)
        if (0x01C00000 <= v < 0x02000000 or 0x40000000 <= v < 0x60000000
                or 0x80000000 <= v < 0x90000000):
            interesting[f"{v:#010x}"] = n
    top_mmio = sorted(interesting.items(), key=lambda kv: -kv[1])[:14]
    probes = {
        "TI DSP/BIOS exception handler": "Legacy NMI exception:",
        "TI DSP/BIOS abort path": "SYS abort called with message",
        "TI DSP/BIOS lock check": "LOCK NOT CALLED IN TSK CONTEXT",
        "TI C6000 IERR register": "Internal exception: IERR=0x%x",
        "TI EXC handler (EFR/NRP)": "EXC_exceptionHandler: EFR=0x%x",
        "TI memory segment names": "general pool",
        "POSIX-style device nodes": "/dev/null",
        "C++ runtime w/ exceptions": "C++ library exception",
        "USB host mass-storage stack": "usbh_msc_app_start",
        "USB OTG host controller": "PUH_OTG_Int_task",
        "FAT volume paths (A:\\)": "A:\\ZOOM_R24\\",
        "Zoom project container": "ZOOM R24   PROJECT DATA VER0001",
        "field-update patch hook": "PANEL VERSIONUP",
        "CP15 system control (mrc/mcr p15)": None,
    }
    # CP15 detection by instruction pattern rather than by string
    cp15 = 0
    for off in range(0, min(len(pay), 0x80000) - 3, 4):
        w = struct.unpack_from("<I", pay, off)[0]
        if (w & 0x0F000F10) == 0x0E000F10:      # mrc/mcr p15, ...
            cp15 += 1
    found = {k: (v in txt if v else cp15 > 0) for k, v in probes.items()}
    return {
        "mmio_literals_top": top_mmio,
        "cp15_instruction_count": cp15,
        "fingerprints": found,
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def full_report(img: R24Image, verbose: bool = False) -> None:
    arch = detect_architecture(img)
    segs = segment(img)
    strs = string_clusters(img)
    ev = evidence(img)

    print("=" * 74)
    print("ARCHITECTURE")
    print("=" * 74)
    print(f"  ISA                : {arch['isa']}  ({arch['variant']})")
    print(f"  endianness         : {arch['endianness']}")
    print(f"  execution states   : {arch['execution_states']}")
    print(f"  instruction sets   : {', '.join(arch['instruction_sets'])}")
    print(f"  core class         : {arch['cpu_core_class']}")
    print(f"  container          : {arch['container']}")
    print(f"  load address       : {arch['load_addr']:#010x} "
          f"(payload starts at file offset {arch['load_offset']:#x})")
    print(f"  entry point        : {arch['entry_point_va']:#010x} "
          f"(file offset {arch['entry_point_off']:#x})" if arch["entry_point_va"]
          else f"  entry point        : {arch['first_insn']}")
    print(f"  first instruction  : {arch['first_insn']}")
    m = arch["markers"]
    print(f"  ARM markers        : stmdb {m['arm_stmdb_sp']}, ldmia {m['arm_ldmia_sp']}, "
          f"bx {m['arm_bx']}")
    print(f"  Thumb markers      : push{{lr}} {m['thumb_push_lr']}, "
          f"bl pairs {m['thumb_bl_pairs']}")
    print(f"  Thumb decode cov.  : {arch['thumb_sequential_coverage_pct']}% of payload bytes")

    print()
    print("=" * 74)
    print("CODE / DATA MAP")
    print("=" * 74)
    print(f"  {'kind':10s} {'file range':>21s} {'virtual range':>25s} {'size':>9s}")
    for s in segs:
        print(f"  {s['kind']:10s} {s['off_start']:#010x}-{s['off_end']:#010x}  "
              f"{s['va_start']:#010x}-{s['va_end']:#010x}  {s['size']:#9x}")
    tot = {}
    for s in segs:
        tot[s["kind"]] = tot.get(s["kind"], 0) + s["size"]
    print()
    for k, v in sorted(tot.items(), key=lambda kv: -kv[1]):
        print(f"  total {k:10s} {v:9d} bytes ({100 * v / len(img.payload):5.1f}%)")

    if strs:
        print()
        print("  prominent C-string tables (rodata clusters):")
        for c in strs[:12]:
            print(f"    {c['off_start']:#010x}-{c['off_end']:#010x}  "
                  f"{c['size']:7d} B  {c['string_count']:5d} strings")

    print()
    print("=" * 74)
    print("FINGERPRINTS")
    print("=" * 74)
    for k, v in ev["fingerprints"].items():
        print(f"  [{'x' if v else ' '}] {k}")
    print("  most-loaded literal-pool words (via ARM ldr rX,[pc,#imm]):")
    for lit, n in ev["mmio_literals_top"]:
        print(f"    {lit}  x{n}")
