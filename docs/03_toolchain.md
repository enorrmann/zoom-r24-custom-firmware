# 03 — Toolchain: reproduce and build a custom installable

## 1. What "reproduce" means here

Two distinct goals, and it is worth being precise about which is met:

| Goal | Status | How it is proved |
|---|---|---|
| Rebuild an **identical** file from its parts | **Met** | `test_roundtrip.py`: unpack → pack gives the same sha256 |
| Rebuild the **original source code** into firmware | **Not attempted** | No source, no symbol file, no linkable objects exist |

The first is what a maintainer needs: the container format is modelled exactly,
so any edit can be re-sealed into a file the device accepts. The second would
require reversing ~7 MB of Thumb code into compilable C and is out of scope for
this package (see `05_limitations.md`).

### Toolchain choice

The firmware is ARMv4T/ARMv5TE, little-endian, ARM+Thumb-1, bare-metal,
statically linked, position-dependent. The matching free toolchain is:

```
arm-none-eabi-gcc / binutils  (GNU Arm Embedded Toolchain)
```

Already present on this machine (`arm-none-eabi-objdump` 2.44) and used by the
`analyze` path. The **builder** deliberately does *not* use gcc: there is
nothing to feed it — no objects, no linker script, no symbols. Rebuilding is a
container operation, not a compile, and pretending otherwise would be the
dishonest version of this tool.

---

## 2. Command reference

All commands run from the repository root. `tools/r24tool` needs only Python 3
(standard library); nothing else is required for pack/verify/patch/build.

### `info` — header summary

```console
$ ./tools/r24tool info R24SYS.BIN
file        : R24SYS.BIN
size        : 7169652 bytes (6.838 MiB)
magic       : ZOOM/D211
version     : 1.13 (header) / 1.13 (UI string)
payload     : 7169588 bytes, linked at VA 0x02080000
checksum    : stored 0x3197cc47 / computed 0x3197cc47 (OK)
sha256      : 71c7e3c5715aef27a6e2eebf8eafa090ae607ce5ddccb5ea08ca1eaccf81dc93
```

### `verify` — structural validation (exit 1 on failure)

`verify`, and every tool that writes an image (`R24Image.write`), also refuse
an image the boot loader would flash into the wrong place: payload over
`0x720000` bytes (file over **7,471,168**: it would reach the boot flag, and
over `0x780000` wrap onto the boot loader), header kind byte `0x10` != 0, or
byte `0x30` = 0 (no checksum check). `docs/18_dump_analysis.md` §3.3;
`tools/test_flash_safety.py`.

```console
$ ./tools/r24tool verify R24SYS.BIN
verification passed
  size          : 7169652
  payload_size  : 7169588
  version       : 1.13
  magic         : ZOOM/D211
  checksum      : 0x3197cc47
  checksum_ok   : True
  sha256        : 71c7e3c5...
```

Use this in scripts: non-zero exit means *do not flash this file*.

### `analyze` — architecture and code/data map

```console
$ ./tools/r24tool analyze R24SYS.BIN
```

Prints ISA, endianness, execution states, entry point, ARM/Thumb marker
census, the segmentation map, rodata clusters and the fingerprint list.
Deterministic: same input, same output.

### `unpack` / `pack` — the reproduction path

```console
$ ./tools/r24tool unpack R24SYS.BIN -o work/
$ ./tools/r24tool pack work/ -o rebuilt.bin
```

`unpack` writes `header.bin`, `payload.bin`, `manifest.json`.
`pack` **recomputes the checksum** from the payload (disable with
`--no-checksum` for forensics) and verifies before writing.

Edit `payload.bin` with any hex editor, repack, and the result is valid:

```console
$ ./tools/r24tool pack work/ -o rebuilt.bin
packed work/ -> rebuilt.bin
  size     7169652 bytes
  version  1.13 (header) / 1.13 (UI)
  checksum 0x3197cc51
  sha256   ...
```

### `patch` — one-shot surgical edits

```console
# bump both version copies
$ ./tools/r24tool patch R24SYS.BIN -o out.bin --set-version 1.99

# guarded byte patch: refuses if the old bytes are not what you expect
$ ./tools/r24tool patch R24SYS.BIN -o out.bin \
    --patch 0x5d1c:4d4f4e4f2d253033642e574156:54414b452d253033642e574156
```

---

## 3. Building a custom installable

Customisation is **declarative data**, not code. A build is a JSON spec:

```json
{
  "output_name": "R24SYS.BIN",
  "version": "1.99",
  "ui_version": "1.99",
  "patches": [
    {
      "offset": "0x5d1c",
      "expected": "4d4f4e4f2d253033642e574156",
      "replacement": "54414b452d253033642e574156"
    }
  ]
}
```

### Spec fields

| Field | Required | Meaning |
|---|---|---|
| `output_name` | no | output filename, default `R24SYS.BIN` (**must** be this name for the updater) |
| `version` | no | header version field at `0x20` |
| `ui_version` | no | the string the LCD shows, at `0x1EBBFC` |
| `patches[].offset` | one of | hex/dec file offset |
| `patches[].find` | one of | hex pattern; located in the payload |
| `patches[].occurrence` | no | required when `find` matches more than once (0-based) |
| `patches[].expected` | **yes** | old bytes as hex — a **precondition**, not a hint |
| `patches[].replacement` | **yes** | new bytes, must be the **same length** |

### Build

```console
$ ./tools/r24tool build specs/examples/full-demo.json -o build/
spec         : specs/examples/full-demo.json
source       : R24SYS.BIN
version header: '1.13' -> '1.99'
version UI    : '1.13' -> '1.99'
patch 0      : 0x005d1c 4d4f4e4f... -> 54414b45...
patch 1      : 0x005d0c 5354452d... -> 5354455f...
output       : build/R24SYS.BIN
  size       : 7169652 bytes
  version    : 1.99 (header) / 1.99 (UI)
  checksum   : 0x3197cc73 (verified)
  sha256     : bca7831f0e95c8d7384b2819b08b2c89252e1bce373e98d35ba9ac0230c3f14b
```

`build/build.json` records the source hash, output hash and every change, so a
custom image is always traceable to the spec and the firmware it came from.

### Safety properties the builder enforces

1. **`expected` is checked.** A spec written against v1.13 fails loudly on any
   other revision instead of corrupting it.
2. **Length is preserved.** No insertion, no shifting, no broken literal pools.
3. **Ambiguity is refused.** A `find` matching twice without `occurrence`
   aborts rather than guessing.
4. **Checksum is recomputed** and the result re-verified before the file is
   written.
5. **Post-build verify.** A build that does not verify never reaches disk as a
   success.

---

## 4. Included examples

| Spec | What it does | Risk |
|---|---|---|
| `specs/examples/version-bump.json` | sets both version fields to `1.99` | none (cosmetic) |
| `specs/examples/full-demo.json` | version + rewrites two take templates | low (filename cosmetics) |
| `specs/examples/rename-wav-templates.json` | renames `STE-`/`MONO-`/`MASTR` templates | low |

Verified output of the demo:

```console
$ ./tools/r24tool build specs/examples/full-demo.json -o build/ --source R24SYS.BIN
$ python3 -c "print(open('build/R24SYS.BIN','rb').read()[0x5d0c:0x5d3c])"
b'STE_%03d.WAV\x00\x00\x00\x00TAKE-%03d.WAV\x00\x00\x00MASTR%03d.WAV\x00\x00\x00'
```

Only 10 bytes differ from stock (2 version bytes × 2 fields, 2 checksum bytes,
4 filename bytes) — provable with:

```console
$ cmp -l R24SYS.BIN build/R24SYS.BIN
```

---

## 5. Installing

The toolchain intentionally **does not automate flashing**. Do it yourself with
the vendor procedure (from `R24_Firmware_Update_Guide_E.pdf`):

1. Copy the built image to the **root** of an SD card, named exactly
   `R24SYS.BIN`.
2. Insert the card into the R24.
3. Power on while holding the documented button.
4. Select `OK` and confirm.
5. **Do not power off or remove the card during the update.**
6. Power off when complete.

Keep the stock `R24SYS.BIN` — it is your rollback. Recovery from a failed
update is to repeat the procedure from step 1 with the stock file.

---

## 6. Regression testing

```console
$ ./tools/test_roundtrip.py
```

Checks, in order:

- header magic, version parsing, UI version parsing
- checksum equals the payload byte sum
- payload size = file size − 64
- exact erased-flash gap boundaries and full `0xFF` fill
- `unpack`/`pack` produces a **byte-identical** file (sha256 equality)
- a corrupted payload with a stale header checksum is **rejected**
- a custom build verifies, changes version, preserves size, and confines its
  diff to the declared patches + checksum

Current status: **18/18 pass**.

---

## 7. Extending the toolchain

Because `r24image.py` is the only module that knows the format, new
capabilities are small additions:

```python
import sys; sys.path.insert(0, "tools")
from r24image import R24Image

img = R24Image.from_file("R24SYS.BIN")

# enumerate the derived segmentation
for r in img.regions():
    print(r["kind"], hex(r["va_start"]), hex(r["va_end"]))

# guarded edit
img.patch(0x5D2C, b"MASTR%03d.WAV", b"MAST_%03d.WAV")
img.refresh_checksum()
img.verify()
img.write("out.bin")
```

Rules for anything you add:

- never change the image length;
- never move code;
- always guard with `expected`;
- always `refresh_checksum()` then `verify()`.
