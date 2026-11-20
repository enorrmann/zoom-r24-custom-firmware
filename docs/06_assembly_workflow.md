# 06 — Writing firmware: assembly, decompilation and patching

This document covers the part of the toolchain that lets a **human write
machine code** for the R24: extracting real assembly, editing it, putting it
back, and reading Ghidra's decompiled C. It states plainly what works, what is
partial, and what is impossible.

---

## 1. What "generate an assembler" means here

The image is already machine code — the task is not to *create* an assembler
but to make the existing binary **round-trip through text a human can edit**:

```
firmware bytes  --disasm-->  editable .s  --assemble-->  firmware bytes
```

That round-trip is the deliverable, and it is **measured, not asserted**:

| Metric | Result |
|---|---|
| Functions rebuilt **byte-identically** | **5,564 / 5,733 (97.1 %)** |
| Code bytes reproduced exactly | **2,556,300 / 2,824,758 (90.5 %)** |
| Instructions exposed as readable mnemonics | **683,625** |
| Instructions pinned verbatim (`.inst`/`.short`) | 75,173 |

Reproduce with:

```bash
./tools/r24tool disasm R24SYS.BIN                  # list the function map (2,837 real functions)
python3 tools/validate_reasm.py tools/data/functions.csv
```

The 2.9 % that fail are listed in `docs/05_limitations.md` §7. They are
**not silently wrong** — the validator reports every one.

### Why an assembler alone was not enough

Three problems had to be solved before the round-trip was exact. All three are
the kind of thing that makes "just use objdump + as" fail:

1. **Mixed ARM/Thumb state per function.** A linear sweep in one state runs
   into the other and produces nonsense (`mrc2`, `vrhadd`). The mode of every
   function comes from Ghidra's map, not from guessing.
2. **The assembler may pick a different encoding.** `adds r2,r2,#4` is
   `0x1d12` in the original but GAS emits `0x3204` — same semantics, different
   bytes. Byte-exactness is non-negotiable here, so any instruction whose
   assembled bytes differ is re-emitted verbatim and the process repeats until
   it converges.
3. **Thumb `bl` cannot target an absolute symbol.** GAS emits an ARM
   interworking stub (`f7ff fffe`) for an undefined symbol. The only reliable
   fix is to place the target with a **linker script**, so `bl` is assembled
   and *linked*, not just assembled.

---

## 2. Reading the firmware

### List functions

```console
$ ./tools/r24tool disasm R24SYS.BIN | head -4
0x02080000  arm         4  FUN_02080000
0x02080008  thumb       4  FUN_02080008
0x0208002c  arm        68  FUN_0208002c
0x02080070  arm        68  FUN_02080070
```

### Disassemble one function

```console
$ ./tools/r24tool disasm R24SYS.BIN -a 0x0208002c
L0208002c:
  ldr r0, [pc, #0x38]
L02080030:
  mov lr, pc
L02080034:
  bx r0
```

Every instruction address becomes a label, so branches read as
`bne L02080874` and can be edited safely.

### Decompiled C

Ghidra's decompiler **does work** on this target (`ARM:LE:32:v4t` +
`ARM_v45.cspec`, AAPCS). Example — the real crt0:

```c
void FUN_0208002c(void)
{
  int iVar1; int iVar2; uint uVar3;

  FUN_02080490();
  (*pcRam02080074)(_FUN_02080070);
  (*(code *)0x800c4c34)();
  iVar1 = FUN_02080248;
  uVar3 = *(uint *)(FUN_02080248 + 0x100) & uRam02080250;
  *(uint *)(iVar1 + 0x100) = uVar3;
  ...
  iVar2 = 0x20;
  do { iVar2 = iVar2 + -1; } while (iVar2 != 0);   /* the delay loop */
}
```

This is genuinely readable: control flow is recovered, call targets are
resolved, the delay loop is obvious. Regenerate the full set with:

```bash
python3 tools/ghidra_run.sh decompile      # -> decomp/decomp/*.c  (~81 MB)
./tools/r24tool decompile decomp --grep 'do \{'
```

**Sample output is included** in `decomp/samples/`. The full 81 MB set is
generated on demand rather than committed.

### What the decompiled C is NOT

**It is not source code and it does not rebuild the firmware.** Specifically:

- Names are generated (`FUN_0208002c`, `uVar3`); no original identifiers
  survive — the binary is stripped.
- Types are inferred. `undefined4`, `unaff_r5` ("unaffected register") and
  `(*(code *)0x800c4c34)()` are decompiler artefacts, not the original code.
- The output **will not recompile to the same bytes**. Even the original
  compiler with a correct C file would need the exact original flags,
  libraries and link order — all unknown.
- Some functions decompile poorly (register-heavy Thumb code, computed
  jumps); `decomp_index.csv` records which.

Use it to **read and understand**, then edit **assembly** if you need to
change behaviour. That is why §3 exists.

---

## 3. Writing firmware — the working loop

This is the only sanctioned way to change code, and it has been verified
end-to-end (`tools/test_asm_edit.py`, 10/10 checks).

```bash
# 1. extract
./tools/r24tool disasm R24SYS.BIN -a 0x0208084e -o f.s

# 2. edit f.s  (any assembler editor)

# 3. put it back (splices at the same VA, refills the slack with nops,
#    recomputes the checksum, and re-verifies the container)
./tools/r24tool asm R24SYS.BIN f.s -a 0x0208084e -o build-asm/R24SYS.BIN

# 4. confirm
./tools/r24tool verify build-asm/R24SYS.BIN
```

### Rules the tool enforces for you

| Rule | Why | Enforced by |
|---|---|---|
| Code must **fit in the original size** | no relocation table; growing the image shifts every absolute address | `asm` errors with the byte counts |
| Splice at the **same VA** | literal pools hold absolute addresses | `asm` writes only at the entry VA |
| Leftover bytes are **nop-filled** | fall-through into the pad stays harmless | `r24patch.splice` |
| Checksum **recomputed** | it is a payload byte sum | `r24patch` + `refresh_checksum` |
| Result **re-verified** | a broken image must never reach disk as success | `verify()` before write |

### The real constraint

You can only make a function **the same size or smaller**. In practice that
means:

- changing constants (`movs r4,#1` instead of `#0`) — trivially safe;
- short-circuiting logic, removing a call, returning early;
- reimplementing a function more compactly.

You **cannot** add a feature that needs more code in place.
`r24patch.trampoline_patch()` used to place new code in the `0xFF` region; it is
now **disabled**, because that region is never loaded into RAM and the code
would not exist at runtime (`02_analysis.md` §3.2). Extra code belongs in the PCM
bank tail (RAM `0x87540000+`), reached through a long-call veneer
(`10_loop_import.md` §5).

**Execution address.** Code runs in RAM at `file − 0x5B4 + 0x80000000`.
`r24asm`/`r24patch` assemble at the load VA (`0x0208xxxx`), which is fine for
PC-relative branches but wrong for any absolute address you emit (a function
pointer, `ldr rX,=label`): write those as RAM addresses. Padding uses the Thumb-1
NOP `mov r8,r8` (`0x46C0`); `nop.n` (`0xBF00`) is Thumb-2 and undefined here.

---

## 4. Caveats that will bite you

1. **Verify your edit against the original bytes.** A silently-wrong
   instruction is the failure mode with no error message. Compare with
   `cmp -l R24SYS.BIN build-asm/R24SYS.BIN`; only intended offsets should move
   (plus the checksum).
2. **Interworking.** If you `bl` from Thumb to ARM code you must land on the
   address with bit 0 clear and use `bx`/`blx` semantics. Getting the state
   wrong usually means an immediate crash or, worse, garbage execution.
3. **Literal pools.** `ldr r0,[pc,#0xc8]` is position-dependent and survives
   only because the function does not move. Do not "clean up" those loads into
   `ldr r0,=value` — GAS may add a new literal pool and change the length.
4. **AAPCS calling convention.** r0–r3 are arguments, r0 the return value,
   r4–r11 callee-saved. Violating this corrupts the caller.
5. **No debugger.** There is no known way to single-step this target here. A
   bad patch is diagnosed by the device failing to boot.

---

## 5. Tooling reference

| File | Purpose |
|---|---|
| `tools/r24asm.py` | disassemble/assemble one function; the converging force loop |
| `tools/r24patch.py` | extract, `assemble()`, `splice()`, trampolines |
| `tools/validate_reasm.py` | measures the round-trip over the whole map |
| `tools/test_asm_edit.py` | end-to-end human-edit proof |
| `tools/R24Analyze.java` | Ghidra: seed ARM/Thumb, build the function map |
| `tools/R24Decompile.java` | Ghidra: export decompiled C per function |
| `tools/data/functions.csv` | the function map (8,941 entries; only the 2,837 below VA `0x0226E910` are code, the rest is PCM audio) |

Regenerating the function map (needs the bundled Ghidra):

```bash
export GHIDRA=/home/emilio/ghidras/ghidra_11.3.2_PUBLIC
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export XDG_CONFIG_HOME=/tmp/ghidra-cfg   # ~/.config may be read-only
mkdir -p /tmp/proj && $GHIDRA/support/analyzeHeadless /tmp/proj R24 \
  -import R24SYS.BIN -processor "ARM:LE:32:v4t" \
  -loader BinaryLoader -loader-baseAddr 0x207ffc0 -noanalysis \
  -scriptPath tools -postScript R24Analyze.java /tmp/out
```

Note the `-loader-baseAddr 0x207ffc0`: the file's first 64 bytes are the
container header, so the payload's `0x02080000` starts 0x40 into the file. Get
this wrong and every address is off by 64.

---

## 6. Honest summary

**Delivered:**

- full disassembly of the image — 2,837 real functions (plus 6,104 map entries that are PCM audio), ARM and Thumb, with real
  mnemonics and labels;
- byte-exact reassembly for **97.1 % of functions / 90.5 % of code bytes**,
  measured;
- a working human workflow: edit assembly → valid firmware image (verified);
- widespread decompiled C that is genuinely useful for reading the code.

**Not delivered, and why:**

- **A rebuildable source tree.** The binary is stripped, position-dependent
  and shipped without objects. No decompiler output recompiles to identical
  bytes without the original compiler, flags and libraries.
- **Free-form code growth.** There are no relocations, so a function cannot
  move or expand in place.
- **The DSP.** The C64x+ program is in this file (`17_dsp_and_dump.md`), but
  this workflow is ARM/Thumb only; DSP code needs TI's `cl6x`/`dis6x`.
