#!/usr/bin/env python3
"""r24sdk.py — build C patches into an R24 firmware image.

    python3 tools/r24sdk.py build IMAGE sdk/patches/custom_kit.c [more.c ...] -o OUT
            [-D NAME=VALUE ...] [--no-trim]
    python3 tools/r24sdk.py check IMAGE      # what an image contains
    python3 tools/r24sdk.py syms  [IMAGE]    # verify sdk/firmware.sym against an image

How a build works (sdk/README.md has the full story):

 1. every patch plus sdk/runtime/runtime.c is compiled with arm-none-eabi-gcc
    (Thumb, ARMv5TE, -mlong-calls) and linked as ONE blob; the firmware's
    functions and variables are linker symbols generated from
    sdk/firmware.sym, each checked against the image first;
 2. the hook records the patches declare (R24_REPLACE / R24_POINTER /
    R24_BYTES / R24_KIT, sdk/include/r24.h) are read back from the blob;
 3. R24_KIT adds kits (r24bank.expand_kits) before anything is placed;
 4. the blob becomes extension section "R24SDK01" in the PCM bank tail
    (RAM 0x8754xxxx, copied there by the boot code). If the bank is full, the
    longest samples are capped (20 ms fade) to make room — unless --no-trim;
 5. every hook site is checked against its expected bytes, then patched;
 6. the checksum is recomputed; OUT.syms.json records where everything went.

The image size never changes, and nothing is patched unless every check
passes. An image takes one SDK build: start again from a clean image to change
the set of patches (LOOP[SD] from loop_sd.py can be added before or after).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r24bank
import r24image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.path.join(ROOT, "sdk")
SYMFILE = os.path.join(SDK, "firmware.sym")
RUNTIME = os.path.join(SDK, "runtime", "runtime.c")
TAG = b"R24SDK01"
RATE = 44100
FADE_MS = 20
DATA_RAM, DATA_FILE, DATA_END = 0x80000000, 0x5B4, 0x801EE35C

K_REPLACE, K_POINTER, K_BYTES, K_KIT = 1, 2, 3, 4
HOOK_FMT = "<IIII96s96s"
HOOK_SIZE = struct.calcsize(HOOK_FMT)          # 208
HEAD_FMT = "<8sIIII"                           # tag, version, hooks, hooks_end, end

CFLAGS = ["-mthumb", "-march=armv5te", "-mfloat-abi=soft", "-mlong-calls",
          "-Os", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
          "-Wno-unused-parameter", "-ffreestanding", "-fno-builtin",
          "-fno-tree-loop-distribute-patterns", "-fno-common",
          "-ffunction-sections", "-fdata-sections", "-nostdlib"]


class SdkError(Exception):
    pass


# ── firmware symbols ───────────────────────────────────────────────────────
def load_syms(path: str = SYMFILE) -> list[dict]:
    out = []
    for n, line in enumerate(open(path), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        p = line.split()
        if len(p) != 4 or p[2] not in ("thumb", "arm", "data"):
            raise SdkError(f"{path}:{n}: expected `name addr kind check`")
        out.append(dict(name=p[0], addr=int(p[1], 16), kind=p[2],
                        check=None if p[3] == "-" else bytes.fromhex(p[3])))
    return out


def ram_to_file(addr: int, data: bytes) -> int:
    if DATA_RAM <= addr < DATA_END:
        return addr - DATA_RAM + DATA_FILE
    if r24bank.RAM_BASE <= addr < r24bank.RAM_BASE + r24bank.bank_size(data):
        return addr - r24bank.RAM_BASE + r24bank.BANK_BASE
    raise SdkError(f"RAM 0x{addr:08X} is not part of the image")


def verify_syms(data: bytes, syms: list[dict], only: set[str] | None = None) -> list[str]:
    bad = []
    for s in syms:
        if s["check"] is None or (only is not None and s["name"] not in only):
            continue
        o = ram_to_file(s["addr"], data)
        if data[o:o + len(s["check"])] != s["check"]:
            bad.append(f"{s['name']} @ 0x{s['addr']:08X}: image has "
                       f"{data[o:o + len(s['check'])].hex()}, firmware.sym expects "
                       f"{s['check'].hex()}")
    return bad


# ── compiling ──────────────────────────────────────────────────────────────
def tool(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise SdkError(f"{name} not found (Debian: apt install gcc-arm-none-eabi)")
    return p


def linker_script(ram: int, syms: list[dict]) -> str:
    lines = [f"{s['name']} = 0x{s['addr'] | (1 if s['kind'] == 'thumb' else 0):08X};"
             for s in syms]
    return "\n".join(lines) + f"""
SECTIONS {{
  . = 0x{ram:08X};
  .text : {{
    KEEP(*(.r24head))
    *(.text*) *(.rodata*) *(.data*)
    . = ALIGN(4);
    __r24_hooks_start = .;
    KEEP(*(.r24hooks))
    __r24_hooks_end = .;
    *(.bss*) *(COMMON)
    . = ALIGN(4);
    __r24_end = .;
  }}
  /DISCARD/ : {{ *(.ARM.exidx*) *(.ARM.extab*) *(.comment) *(.ARM.attributes)
                *(.note*) *(.init_array*) *(.fini_array*) }}
}}
"""


class Build:
    """One compiled blob linked at `ram`."""

    def __init__(self, blob: bytes, ram: int, symbols: dict[str, int], undefined: set[str]):
        self.blob, self.ram, self.symbols, self.undefined = blob, ram, symbols, undefined
        tag, ver, hs, he, end = struct.unpack_from(HEAD_FMT, blob, 0)
        if tag != TAG or ver != 1:
            raise SdkError("internal: SDK header is not at the start of the blob")
        self.hooks = parse_hooks(blob, ram, hs, he)


def parse_hooks(blob: bytes, ram: int, hs: int, he: int) -> list[dict]:
    if (he - hs) % HOOK_SIZE:
        raise SdkError("internal: hook table size")
    out = []
    for o in range(hs - ram, he - ram, HOOK_SIZE):
        kind, addr, target, old, a, b = struct.unpack_from(HOOK_FMT, blob, o)
        out.append(dict(kind=kind, addr=addr, target=target, old=old,
                        a=a.split(b"\0")[0].decode(), b=b.split(b"\0")[0].decode()))
    return out


def compile_all(sources: list[str], ram: int, defines: list[str],
                syms: list[dict]) -> Build:
    gcc = tool("arm-none-eabi-gcc")
    with tempfile.TemporaryDirectory() as tmp:
        ld = os.path.join(tmp, "r24.ld")
        open(ld, "w").write(linker_script(ram, syms))
        elf, binf = os.path.join(tmp, "b.elf"), os.path.join(tmp, "b.bin")
        objs = []
        for i, src in enumerate([RUNTIME, *sources]):
            obj = os.path.join(tmp, f"{i}.o")
            r = subprocess.run([gcc, *CFLAGS, "-I", os.path.join(SDK, "include"),
                                *[f"-D{d}" for d in defines], "-c", src, "-o", obj],
                               capture_output=True, text=True)
            if r.returncode:
                raise SdkError(f"compiling {src}:\n{r.stderr}")
            objs.append(obj)
        undefined = set()
        for obj in objs:
            nm = subprocess.run([tool("arm-none-eabi-nm"), "-u", obj],
                                capture_output=True, text=True, check=True).stdout
            undefined |= {l.split()[-1] for l in nm.splitlines() if l.strip()}
        r = subprocess.run([gcc, *CFLAGS, "-T", ld, "-o", elf, *objs, "-lgcc"],
                           capture_output=True, text=True)
        if r.returncode:
            raise SdkError(f"linking:\n{r.stderr}")
        subprocess.run([tool("arm-none-eabi-objcopy"), "-O", "binary", "-j", ".text",
                        elf, binf], check=True)
        nm = subprocess.run([tool("arm-none-eabi-nm"), elf], capture_output=True,
                            text=True, check=True).stdout
        blob = open(binf, "rb").read()
    symbols = {}
    for line in nm.splitlines():
        p = line.split()
        if len(p) == 3:
            symbols[p[2]] = int(p[0], 16)
    if symbols.get("R24_HEAD") != ram:
        raise SdkError(f"internal: build not linked at 0x{ram:08X} (alignment?)")
    end = symbols["__r24_end"]
    blob = blob.ljust(end - ram, b"\0")
    blob += b"\0" * (-len(blob) % 32)          # same length at any 8-aligned address
    return Build(blob, ram, symbols, undefined)


# ── section layout ────────────────────────────────────────────────────────
# The section starts with a small prefix ("R24SDK01", u32 RAM address of the
# build) so that the build itself can be linked at a 32-byte aligned address
# (gcc aligns the output to its largest input alignment, e.g. a cache-line
# aligned buffer); the prefix length is constant, so the size is too.
PREFIX = 64


def link_address(ram: int) -> int:
    return (ram + 16 + 31) & ~31


def section_prefix(ram: int, link: int) -> bytes:
    pre = TAG + struct.pack("<I", link)
    return pre + b"\0" * (link - ram - len(pre)) if link - ram >= len(pre) else b""


# ── trampolines and hooks ─────────────────────────────────────────────────
def trampoline(addr: int, target: int) -> bytes:
    """Long jump placed at a Thumb function entry; clobbers only r3."""
    if not target & 1:
        raise SdkError(f"R24_REPLACE target 0x{target:08X} is not a Thumb function")
    if addr % 4 == 2:            # ldr r3,[pc,#4] ; bx r3 ; nop ; .word
        return bytes.fromhex("014b1847c046") + struct.pack("<I", target)
    if addr % 4 == 0:            # ldr r3,[pc,#0] ; bx r3 ; .word
        return bytes.fromhex("004b1847") + struct.pack("<I", target)
    raise SdkError(f"R24_REPLACE address 0x{addr:08X} is odd; give the even address")


def plan_hooks(data: bytes, b: Build) -> list[tuple[int, bytes, bytes, str]]:
    """[(file offset, old bytes, new bytes, description)] — all checked."""
    out = []
    lo, hi = b.ram, b.ram + len(b.blob)
    for h in b.hooks:
        k, addr = h["kind"], h["addr"]
        if k == K_KIT:
            continue
        o = ram_to_file(addr, data)
        if k == K_REPLACE:
            if not lo <= h["target"] - 1 < hi:
                raise SdkError(f"R24_REPLACE 0x{addr:08X}: target outside the build")
            new = trampoline(addr, h["target"])
            want = bytes.fromhex(h["a"])
            if len(want) < len(new):
                raise SdkError(f"R24_REPLACE 0x{addr:08X}: give at least {len(new)} "
                               f"original bytes (got {len(want)})")
            if data[o:o + len(want)] != want:
                raise SdkError(f"R24_REPLACE 0x{addr:08X}: image has "
                               f"{data[o:o + len(want)].hex()}, patch expects {want.hex()}")
            out.append((o, want[:len(new)], new,
                        f"replace 0x{addr:08X} -> 0x{h['target']:08X}"))
        elif k == K_POINTER:
            if addr % 4:
                raise SdkError(f"R24_POINTER 0x{addr:08X} is not word aligned")
            cur = struct.unpack_from("<I", data, o)[0]
            if cur != h["old"]:
                raise SdkError(f"R24_POINTER 0x{addr:08X}: image has 0x{cur:08X}, "
                               f"patch expects 0x{h['old']:08X}")
            out.append((o, struct.pack("<I", cur), struct.pack("<I", h["target"]),
                        f"pointer 0x{addr:08X}: 0x{cur:08X} -> 0x{h['target']:08X}"))
        elif k == K_BYTES:
            old, new = bytes.fromhex(h["a"]), bytes.fromhex(h["b"])
            if len(old) != len(new) or not old:
                raise SdkError(f"R24_BYTES 0x{addr:08X}: old and new differ in length")
            if data[o:o + len(old)] != old:
                raise SdkError(f"R24_BYTES 0x{addr:08X}: image has "
                               f"{data[o:o + len(old)].hex()}, patch expects {old.hex()}")
            out.append((o, old, new, f"check 0x{addr:08X}: {old.hex()}" if old == new
                        else f"bytes 0x{addr:08X}: {old.hex()} -> {new.hex()}"))
        else:
            raise SdkError(f"unknown hook kind {k}")
    spans = sorted((o, o + len(n)) for o, _x, n, _d in out)
    for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
        if b0 < a1:
            raise SdkError(f"two hooks overlap at file 0x{b0:X}")
    return out


# ── space ──────────────────────────────────────────────────────────────────
def capped(pcm: bytes, cap: int) -> bytes:
    if len(pcm) <= cap:
        return pcm
    n = cap // 2
    s = list(struct.unpack(f"<{n}h", pcm[:cap]))
    f = min(RATE * FADE_MS // 1000, n // 4)
    for i in range(f):
        k = n - f + i
        s[k] = int(s[k] * (f - 1 - i) / f)
    return struct.pack(f"<{n}h", *s)


def packed_size(pcms: list[bytes]) -> int:
    seen, total = set(), 0
    for p in pcms:
        h = hashlib.sha256(p).digest()
        if h not in seen:
            seen.add(h)
            total += len(p)
    return total


def free_audio(data: bytearray, target: int, trim: bool) -> list[str]:
    """Repack the bank so that its audio takes at most `target` bytes."""
    lay = r24bank.locate(data)
    tab = r24bank.read_table(data, lay)
    pcms = [r24bank.sample_bytes(data, tab, i) for i in range(lay.count)]
    have = packed_size(pcms)
    notes = []
    if have > target:
        if not trim:
            raise SdkError(f"the bank needs {have - target} more free bytes "
                           f"({(have - target) / 2 / RATE:.2f} s); drop --no-trim, "
                           f"shrink the build or repack with shorter samples")
        lo, hi = 2, max(len(p) for p in pcms)
        while lo < hi:                               # largest cap that fits
            mid = ((lo + hi + 1) // 2) & ~1
            if mid <= lo:
                break
            if packed_size([capped(p, mid) for p in pcms]) <= target:
                lo = mid
            else:
                hi = mid - 2
        if packed_size([capped(p, lo) for p in pcms]) > target:
            raise SdkError("cannot free enough space in the bank")
        cut = sorted({i for i, p in enumerate(pcms) if len(p) > lo})
        pcms = [capped(p, lo) for p in pcms]
        notes.append(f"trimmed {len(cut)} table entries to {lo / 2 / RATE * 1000:.0f} ms "
                     f"({FADE_MS} ms fade): {', '.join(map(str, cut))}")
    r24bank.pack_bank(data, pcms)
    notes.append(f"bank audio: {have} -> {packed_size(pcms)} bytes")
    return notes


# ── kits ───────────────────────────────────────────────────────────────────
def kit_names(data: bytes) -> list[str]:
    base, n = r24bank.locate_kits(data)
    return [data[base + k * r24bank.KIT_SIZE:base + k * r24bank.KIT_SIZE + 16]
            .split(b"\0")[0].decode("ascii", "replace").strip() for k in range(n)]


def add_kits(data: bytearray, names: list[str]) -> list[str]:
    have = [n.upper() for n in kit_names(data)]
    want = [n for n in dict.fromkeys(names) if n.upper() not in have]
    for n in want:
        if not 1 <= len(n) <= 15 or not n.isascii():
            raise SdkError(f"kit name {n!r} must be 1..15 ASCII characters")
    if not want:
        return []
    if r24bank.find_section(data, r24bank.KITS_TAG):
        raise SdkError(f"kits {want} are needed but the kit table was already moved "
                       f"(r24bank.py kits); rename one with r24kits.py instead")
    _b, n0 = r24bank.locate_kits(data)
    notes = r24bank.expand_kits(data, n0 + len(want), per_kit=True)
    base, _n = r24bank.locate_kits(data)
    for i, name in enumerate(want):
        o = base + (n0 + i) * r24bank.KIT_SIZE
        data[o:o + 16] = name.encode().ljust(15, b" ") + b"\0"
        notes.append(f"kit {n0 + i + 1} = {name}")
    return notes


# ── build ──────────────────────────────────────────────────────────────────
def build(data: bytearray, sources: list[str], defines: list[str] = (),
          trim: bool = True) -> tuple[Build, int, list[str]]:
    """Apply a build to `data` in place. Returns (build, section rel, notes)."""
    if r24bank.find_section(data, TAG):
        raise SdkError("this image already holds an SDK build; start from an image "
                       "without one (all patches go into a single build)")
    syms = load_syms()
    probe = compile_all(sources, 0x87000020, list(defines), syms)
    used = {s["name"] for s in syms} & probe.undefined
    bad = verify_syms(data, syms, used)
    if bad:
        raise SdkError("firmware.sym does not match this image (not v1.13?):\n  "
                       + "\n  ".join(bad))
    plan_hooks(data, probe)                         # every site checks out first
    notes = add_kits(data, [h["a"] for h in probe.hooks if h["kind"] == K_KIT])
    size = PREFIX + len(probe.blob)

    lay = r24bank.locate(data)
    margin = 64
    while True:
        trial = bytearray(data)
        target = lay.audio_limit - size - margin
        if target <= 0:
            raise SdkError(f"the build ({size} bytes) is larger than the bank")
        space = free_audio(trial, target, trim)
        got: dict = {}

        def place(ram):
            link = link_address(ram)
            b = compile_all(sources, link, list(defines), syms)
            if PREFIX + len(b.blob) != size:
                raise SdkError("internal: build size depends on its address")
            got["b"] = b
            pre = section_prefix(ram, link)
            return pre + b.blob + b"\0" * (PREFIX - len(pre))

        try:
            _l, rel = r24bank.add_section(trial, TAG, size, place)
            break
        except r24bank.BankError:
            margin += 4096
            if margin > 1 << 20:
                raise
    data[:] = trial
    b = got["b"]
    for o, old, new, desc in plan_hooks(data, b):
        data[o:o + len(new)] = new
        notes.append(desc)
    notes += space
    notes.insert(0, f"build: {size} bytes, code @ RAM 0x{b.ram:08X} (section R24SDK01), "
                    f"{len(b.hooks)} hook records")
    return b, rel, notes


def describe(data: bytes) -> dict | None:
    sec = r24bank.find_section(data, TAG)
    if not sec:
        return None
    rel, ln = sec
    o = r24bank.BANK_BASE + rel
    ram = struct.unpack_from("<I", data, o + 8)[0]
    blob = bytes(data[o + ram - (r24bank.RAM_BASE + rel):o + ln])
    tag, ver, hs, he, end = struct.unpack_from(HEAD_FMT, blob, 0)
    if tag != TAG:
        raise SdkError("SDK section without its header")
    hooks = parse_hooks(blob, ram, hs, he)
    state = []
    for h in hooks:
        if h["kind"] == K_REPLACE:
            o = ram_to_file(h["addr"], data)
            t = trampoline(h["addr"], h["target"])
            state.append(data[o:o + len(t)] == t)
        elif h["kind"] == K_POINTER:
            o = ram_to_file(h["addr"], data)
            state.append(struct.unpack_from("<I", data, o)[0] == h["target"])
        elif h["kind"] == K_BYTES:
            o = ram_to_file(h["addr"], data)
            new = bytes.fromhex(h["b"])
            state.append(data[o:o + len(new)] == new)
        else:
            state.append(h["a"].upper() in [n.upper() for n in kit_names(data)])
    return dict(rel=rel, len=ln, ram=ram, end=end, hooks=hooks, applied=state)


def seal(data: bytearray, out: str) -> str:
    img = r24bank.seal(data, out)
    return (f"wrote  : {out} ({img.size} bytes, checksum 0x{img.stored_checksum:08X}, "
            f"verifies {img.checksum_ok()})")


KIND_NAMES = {K_REPLACE: "REPLACE", K_POINTER: "POINTER", K_BYTES: "BYTES", K_KIT: "KIT"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build C patches into an R24 image.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("image")
    p.add_argument("sources", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("-D", dest="defines", action="append", default=[],
                   help="preprocessor define, e.g. -D CUSTOM_KIT_RESERVE_SEC=8")
    p.add_argument("--no-trim", action="store_true",
                   help="fail instead of shortening samples to make room")
    p = sub.add_parser("check")
    p.add_argument("image")
    p = sub.add_parser("syms")
    p.add_argument("image", nargs="?", default=os.path.join(ROOT, "R24SYS.BIN"))
    args = ap.parse_args(argv)
    try:
        data = bytearray(open(args.image, "rb").read())
        if args.cmd == "syms":
            syms = load_syms()
            bad = verify_syms(data, syms)
            print(f"{len(syms)} symbols, {sum(s['check'] is not None for s in syms)} "
                  f"checked against {args.image}: {'OK' if not bad else 'MISMATCH'}")
            for line in bad:
                print(f"  {line}")
            return 1 if bad else 0
        if args.cmd == "check":
            d = describe(data)
            if not d:
                print("no SDK build in this image")
                return 1
            print(f"SDK build: section {d['len']} bytes, code @ RAM 0x{d['ram']:08X} "
                  f"(used up to 0x{d['end']:08X})")
            for h, okk in zip(d["hooks"], d["applied"]):
                what = h["a"] if h["kind"] == K_KIT else f"0x{h['addr']:08X}"
                kind = ("CHECK" if h["kind"] == K_BYTES and h["a"] == h["b"]
                        else KIND_NAMES.get(h["kind"], "?"))
                print(f"  {kind:8s} {what:12s} "
                      f"{'applied' if okk else 'NOT APPLIED'}")
            return 0 if all(d["applied"]) else 1
        before = len(data)
        b, rel, notes = build(data, args.sources, args.defines, not args.no_trim)
        if len(data) != before:
            raise SdkError("internal: image size changed")
        print(f"source : {args.image}")
        for n in notes:
            print(f"  {n}")
        print(seal(data, args.output))
        with open(args.output + ".syms.json", "w") as f:
            json.dump({"section_ram": b.ram, "size": len(b.blob),
                       "sources": args.sources, "defines": args.defines,
                       "symbols": {k: v for k, v in sorted(b.symbols.items())
                                   if b.ram <= v < b.ram + len(b.blob)}}, f, indent=1)
        print(f"symbols: {args.output}.syms.json")
        return 0
    except (SdkError, r24bank.BankError, r24image.ImageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
