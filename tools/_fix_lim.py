import io

p = 'docs/05_limitations.md'
s = io.open(p, encoding='utf-8').read()

old1 = (
    "### 2.3 The `0xFF` gap **[INFERRED]**\n"
    "\n"
    "VA `0x0226E910`-`0x022A0000` is erased flash left inside the image. The most\n"
    "consistent explanation is two adjacent, separately-programmed regions where the\n"
    "tail of the first was never written. **What we cannot prove:** whether the\n"
    "updater or the loader writes this region, tolerates it, or skips it. It is\n"
    "observed as present in a shipped, working firmware, so writing data there is\n"
    "*plausible* - but it is not confirmed safe on hardware."
)
new1 = (
    "### 2.3 The `0xFF` gap **[PROVEN: allocated, not free]**\n"
    "\n"
    "VA `0x0226E910`-`0x022A0000` (202,480 bytes) is erased - every byte is `0xFF`.\n"
    "**It is nevertheless not free space.** A full sweep of the image finds **617\n"
    "four-byte-aligned pointers into that range**, 262 of them odd (thus Thumb\n"
    "function pointers):\n"
    "\n"
    "```\n"
    "import struct\n"
    "d = open('R24SYS.BIN','rb').read()\n"
    "al = [(o, struct.unpack_from('<I', d, o)[0]) for o in range(0, len(d)-4, 4)]\n"
    "al = [(o, v) for o, v in al if 0x0226E910 <= v < 0x022A0000]\n"
    "print(len(al), 'pointers,', sum(1 for _, v in al if v & 1), 'odd')\n"
    "# -> 617 pointers, 262 odd\n"
    "```\n"
    "\n"
    "Erased storage with live pointers aimed at it is the signature of an overlay or\n"
    "work area: the image leaves it blank because something fills it at runtime.\n"
    "Writing code or data there overwrites those targets. Earlier revisions of this\n"
    "file and of `02_analysis.md` section 3.2 called the region free on the strength\n"
    "of the `0xFF` bytes alone; the pointer count is the stronger evidence and\n"
    "supersedes that. See `docs/10_loop_import.md` section 6."
)

old2 = (
    "### 2.4 Writing into the `0xFF` gap is unverified\n"
    "\n"
    "`04_data_vs_code.md` recommends VA `0x0226BB40`-`0x0226C000` for added data\n"
    "because it is sparse and already contains structured descriptors. **This has not\n"
    "been tested on a device.** It is a recommendation based on static evidence, not\n"
    "a validated capability."
)
new2 = (
    "### 2.4 The only low-risk spot is a 1,216-byte data block\n"
    "\n"
    "VA `0x0226BB40`-`0x0226C000` (file `0x1EBB80`) is 1,216 bytes with 222 non-zero\n"
    "and **no** pointers into the `0xFF` range stored inside it - the sparse\n"
    "descriptor block `04_data_vs_code.md` points at. It is data space and far too\n"
    "small for a routine (~350 bytes of dense Thumb plus pools). **Untested on a\n"
    "device.**"
)

old3 = (
    "There are no relocations. A function cannot move and cannot expand past its\n"
    "original size; `r24patch.assemble()` refuses. The only escape is\n"
    "`trampoline_patch()`, which branches into the erased-flash gap\n"
    "(`0x0226E910`-`0x022A0000`) - **untested on hardware**, and writing into a\n"
    "region the loader may treat specially is exactly the kind of risk section 4 warns\n"
    "about."
)
new3 = (
    "There are no relocations. A function cannot move and cannot expand past its\n"
    "original size; `r24patch.assemble()` refuses. `trampoline_patch()` branches into\n"
    "the `0xFF` range (`0x0226E910`-`0x022A0000`) - but section 2.3 shows that range\n"
    "already has 617 pointers aimed at it, so **that escape hatch should be treated\n"
    "as unsafe** until the overlay's loader is understood. Anything that needs *more\n"
    "code* is blocked for now; everything shipped in this repository is same-length\n"
    "data patching, which needs no free space at all."
)

for i, (o, n) in enumerate([(old1, new1), (old2, new2), (old3, new3)], 1):
    if o not in s:
        raise SystemExit('block %d not found' % i)
    s = s.replace(o, n)

io.open(p, 'w', encoding='utf-8').write(s)
print('05_limitations.md updated (3 blocks)')
