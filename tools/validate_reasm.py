#!/usr/bin/env python3
"""Validate the reassembler against every function Ghidra identified.

This is the honesty check for the whole "humans can produce a binary" claim:
if we cannot rebuild a function byte-for-byte from its disassembly, a human
editing that assembly cannot produce correct machine code either.

Ghidra's function list contains OVERLAPS (a big function whose body range
contains separately-identified ARM functions). Assembling an overlapping range
in one mode decodes the inner ARM code as Thumb and produces nonsense, so the
validator honours the *innermost* function at each address: the range is cut
wherever a different function starts.

Usage: validate_reasm.py [functions.csv] [limit]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r24asm

REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, 'R24SYS.BIN')
BASE = 0x02080000
HEAD = 0x40
GAP = (0x0226E910, 0x022A0000)


def load(path):
    funcs = []
    with open(path) as fh:
        next(fh, None)
        for line in fh:
            p = line.strip().split(',')
            if len(p) >= 4:
                funcs.append({'addr': int(p[0], 16), 'thumb': p[1] == '1',
                              'size': int(p[2]), 'name': p[3]})
    funcs.sort(key=lambda f: f['addr'])
    return funcs


def segments(funcs):
    """Cut function ranges so no two overlap (innermost wins)."""
    starts = sorted(f['addr'] for f in funcs)
    for f in funcs:
        if f['size'] <= 4 or GAP[0] <= f['addr'] < GAP[1]:
            continue
        end = f['addr'] + f['size']
        # truncate at the next function start inside this body
        for s in starts:
            if f['addr'] < s < end:
                end = s
                break
        if end - f['addr'] > 4:
            yield {'addr': f['addr'], 'thumb': f['thumb'],
                   'size': end - f['addr'], 'name': f['name']}


def main():
    map_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/gtest/out/functions.csv'
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    data = open(SRC, 'rb').read()
    segs = list(segments(load(map_path)))
    if limit:
        segs = segs[:limit]

    print(f'validating {len(segs)} non-overlapping function ranges from {map_path}')
    ok = forced_total = readable_total = bytes_ok = total_bytes = 0
    fails = []
    t0 = time.time()
    for n, f in enumerate(segs, 1):
        va, size, thumb = f['addr'], f['size'], f['thumb']
        off = va - BASE + HEAD
        blob = data[off:off + size]
        total_bytes += len(blob)
        if len(blob) != size:
            fails.append((va, 'truncated'))
            continue
        try:
            got, st = r24asm.reassemble_function(blob, va, thumb)
            if got == blob:
                ok += 1
                bytes_ok += size
                forced_total += st['forced']
                readable_total += st['readable']
            else:
                fails.append((va, 'diff'))
        except Exception as exc:
            fails.append((va, str(exc)[:50]))
        if n % 250 == 0:
            print(f'  {n}/{len(segs)}  ok={ok}  fail={len(fails)}  {time.time()-t0:.0f}s')

    el = time.time() - t0
    print()
    print(f'functions identical : {ok}/{len(segs)} ({100*ok/max(1,len(segs)):.1f}%)')
    print(f'bytes reproduced    : {bytes_ok}/{total_bytes} '
          f'({100*bytes_ok/max(1,total_bytes):.1f}%)')
    print(f'instructions read   : {readable_total} (human-editable mnemonics)')
    print(f'instructions forced : {forced_total} (emitted verbatim as .inst)')
    print(f'elapsed             : {el:.0f}s')
    if fails:
        print(f'\nfailures ({len(fails)}):')
        for va, why in fails[:20]:
            print(f'  {va:#010x}  {why}')
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
