# 09 — Sample positions inside the binary

> **Generated file — do not edit by hand.**
> Regenerate with `python3 tools/gen_sample_map.py R24SYS.BIN -o docs/09_sample_map.md`

Every entry of the Master Sample Table, with its exact byte position in the image, its length, and the preset kits that use it. These are the coordinates the firmware plays from and that `tools/patch_samples.py` patches (in place) or rewrites (`--repack`).

## Geometry

```
image            R24SYS.BIN   (7169652 bytes)
sample table     factory: file 0x000E20EC .. 0x000E2334   (73 entries x 8 bytes = <II rel_start,length>)
table literal    0x800E1B38   (file 0x2E244 and 0x2E624)
bank base        file 0x00220040   (VA 0x022A0000, copied to RAM 0x87540000)
bank size        4941364 bytes (0x4B6634), ends at EOF
audio area       0x0 .. 0x4B6634 (56.02 s at 44100 Hz)
audio used       0x0 .. 0x4B6634; 73 distinct ranges for 73 entries
format           16-bit signed LE, mono, no header, even lengths
```

A sample's bytes live at:

```
start = 0x00220040 + rel_start
end   = start + length

# what the firmware does at voice start (VA 0x020AE016):
#   ram_start = 0x87540000 + rel_start
#   ram_end   = ram_start + length - 2
```

## Positions

| id | rel_start | length (B) | file start | file end | ms @44.1k | used by |
|---:|---:|---:|---:|---:|---:|---|
| 0 | `0x000000` | 73664 | `0x00220040` | `0x00232000` | 835.2 | LIVE |
| 1 | `0x011FC0` | 206172 | `0x00232000` | `0x0026455C` | 2337.6 | STUDIO, LIVE, POP, JAZZ, ACOUSTIC |
| 2 | `0x04451C` | 72016 | `0x0026455C` | `0x00275EAC` | 816.5 | ROCK |
| 3 | `0x055E6C` | 38616 | `0x00275EAC` | `0x0027F584` | 437.8 | FUNK |
| 4 | `0x05F544` | 40576 | `0x0027F584` | `0x00289404` | 460.0 | BASIC |
| 5 | `0x0693C4` | 181312 | `0x00289404` | `0x002B5844` | 2055.7 | BASIC, ROCK, FUNK |
| 6 | `0x095804` | 41456 | `0x002B5844` | `0x002BFA34` | 470.0 | BASIC |
| 7 | `0x09F9F4` | 44664 | `0x002BFA34` | `0x002CA8AC` | 506.4 | POP |
| 8 | `0x0AA86C` | 26208 | `0x002CA8AC` | `0x002D0F0C` | 297.1 | BASIC, ROCK |
| 9 | `0x0B0ECC` | 31232 | `0x002D0F0C` | `0x002D890C` | 354.1 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 10 | `0x0B88CC` | 70376 | `0x002D890C` | `0x002E9BF4` | 797.9 | BASIC, ROCK |
| 11 | `0x0C9BB4` | 59936 | `0x002E9BF4` | `0x002F8614` | 679.5 | POP, ACOUSTIC |
| 12 | `0x0D85D4` | 44056 | `0x002F8614` | `0x0030322C` | 499.5 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 13 | `0x0E31EC` | 59994 | `0x0030322C` | `0x00311C86` | 680.2 | LIVE |
| 14 | `0x0F1C46` | 42272 | `0x00311C86` | `0x0031C1A6` | 479.3 | ROCK |
| 15 | `0x0FC166` | 50856 | `0x0031C1A6` | `0x0032884E` | 576.6 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 16 | `0x10880E` | 92736 | `0x0032884E` | `0x0033F28E` | 1051.4 | POP, ACOUSTIC |
| 17 | `0x11F24E` | 42096 | `0x0033F28E` | `0x003496FE` | 477.3 | ACOUSTIC |
| 18 | `0x1296BE` | 153312 | `0x003496FE` | `0x0036EDDE` | 1738.2 | POP, ACOUSTIC |
| 19 | `0x14ED9E` | 57632 | `0x0036EDDE` | `0x0037CEFE` | 653.4 | STUDIO |
| 20 | `0x15CEBE` | 38548 | `0x0037CEFE` | `0x00386592` | 437.1 | POP |
| 21 | `0x166552` | 32576 | `0x00386592` | `0x0038E4D2` | 369.3 | STUDIO |
| 22 | `0x16E492` | 90272 | `0x0038E4D2` | `0x003A4572` | 1023.5 | ROCK |
| 23 | `0x184532` | 6072 | `0x003A4572` | `0x003A5D2A` | 68.8 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 24 | `0x185CEA` | 39072 | `0x003A5D2A` | `0x003AF5CA` | 443.0 | JAZZ |
| 25 | `0x18F58A` | 37276 | `0x003AF5CA` | `0x003B8766` | 422.6 | ROCK |
| 26 | `0x198726` | 14162 | `0x003B8766` | `0x003BBEB8` | 160.6 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 27 | `0x19BE78` | 30912 | `0x003BBEB8` | `0x003C3778` | 350.5 | JAZZ |
| 28 | `0x1A3738` | 43376 | `0x003C3778` | `0x003CE0E8` | 491.8 | LIVE |
| 29 | `0x1AE0A8` | 55306 | `0x003CE0E8` | `0x003DB8F2` | 627.1 | LIVE |
| 30 | `0x1BB8B2` | 69066 | `0x003DB8F2` | `0x003EC6BC` | 783.1 | LIVE |
| 31 | `0x1CC67C` | 97536 | `0x003EC6BC` | `0x004043BC` | 1105.9 | FUNK |
| 32 | `0x1E437C` | 99328 | `0x004043BC` | `0x0041C7BC` | 1126.2 | FUNK |
| 33 | `0x1FC77C` | 94672 | `0x0041C7BC` | `0x0043398C` | 1073.4 | FUNK |
| 34 | `0x21394C` | 22952 | `0x0043398C` | `0x00439334` | 260.2 | LIVE, JAZZ, ACOUSTIC |
| 35 | `0x2192F4` | 24992 | `0x00439334` | `0x0043F4D4` | 283.4 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 36 | `0x21F494` | 92284 | `0x0043F4D4` | `0x00455D50` | 1046.3 | ROCK |
| 37 | `0x235D10` | 52574 | `0x00455D50` | `0x00462AAE` | 596.1 | FUNK |
| 38 | `0x242A6E` | 5344 | `0x00462AAE` | `0x00463F8E` | 60.6 | POP, FUNK |
| 39 | `0x243F4E` | 123712 | `0x00463F8E` | `0x004822CE` | 1402.6 | POP, FUNK |
| 40 | `0x26228E` | 125248 | `0x004822CE` | `0x004A0C0E` | 1420.0 | JAZZ |
| 41 | `0x280BCE` | 156768 | `0x004A0C0E` | `0x004C706E` | 1777.4 | JAZZ |
| 42 | `0x2A702E` | 260384 | `0x004C706E` | `0x0050698E` | 2952.2 | JAZZ |
| 43 | `0x2E694E` | 30326 | `0x0050698E` | `0x0050E004` | 343.8 | ACOUSTIC |
| 44 | `0x2EDFC4` | 52592 | `0x0050E004` | `0x0051AD74` | 596.3 | STUDIO |
| 45 | `0x2FAD34` | 62992 | `0x0051AD74` | `0x0052A384` | 714.2 | BASIC, STUDIO, LIVE, ROCK, POP, FUNK, JAZZ, ACOUSTIC, TECHNO, URBAN |
| 46 | `0x30A344` | 124884 | `0x0052A384` | `0x00548B58` | 1415.9 | LIVE, JAZZ, ACOUSTIC |
| 47 | `0x328B18` | 12494 | `0x00548B58` | `0x0054BC26` | 141.7 | STUDIO |
| 48 | `0x32BBE6` | 69214 | `0x0054BC26` | `0x0055CA84` | 784.7 | STUDIO |
| 49 | `0x33CA44` | 60400 | `0x0055CA84` | `0x0056B674` | 684.8 | STUDIO |
| 50 | `0x34B634` | 68256 | `0x0056B674` | `0x0057C114` | 773.9 | STUDIO |
| 51 | `0x35C0D4` | 22408 | `0x0057C114` | `0x0058189C` | 254.1 | URBAN |
| 52 | `0x36185C` | 31952 | `0x0058189C` | `0x0058956C` | 362.3 | URBAN |
| 53 | `0x36952C` | 31344 | `0x0058956C` | `0x00590FDC` | 355.4 | TECHNO |
| 54 | `0x370F9C` | 43482 | `0x00590FDC` | `0x0059B9B6` | 493.0 | TECHNO |
| 55 | `0x37B976` | 243712 | `0x0059B9B6` | `0x005D71B6` | 2763.2 | BASIC, ROCK, FUNK |
| 56 | `0x3B7176` | 12544 | `0x005D71B6` | `0x005DA2B6` | 142.2 | BASIC |
| 57 | `0x3BA276` | 179960 | `0x005DA2B6` | `0x006061AE` | 2040.4 | STUDIO, LIVE, POP, JAZZ, ACOUSTIC |
| 58 | `0x3E616E` | 23276 | `0x006061AE` | `0x0060BC9A` | 263.9 | URBAN |
| 59 | `0x3EBC5A` | 58972 | `0x0060BC9A` | `0x0061A2F6` | 668.6 | TECHNO, URBAN |
| 60 | `0x3FA2B6` | 65536 | `0x0061A2F6` | `0x0062A2F6` | 743.0 | URBAN |
| 61 | `0x40A2B6` | 65536 | `0x0062A2F6` | `0x0063A2F6` | 743.0 | URBAN |
| 62 | `0x41A2B6` | 24978 | `0x0063A2F6` | `0x00640488` | 283.2 | URBAN |
| 63 | `0x420448` | 26404 | `0x00640488` | `0x00646BAC` | 299.4 | URBAN |
| 64 | `0x426B6C` | 5658 | `0x00646BAC` | `0x006481C6` | 64.1 | TECHNO |
| 65 | `0x428186` | 65536 | `0x006481C6` | `0x006581C6` | 743.0 | TECHNO |
| 66 | `0x438186` | 52256 | `0x006581C6` | `0x00664DE6` | 592.5 | TECHNO |
| 67 | `0x444DA6` | 17388 | `0x00664DE6` | `0x006691D2` | 197.1 | TECHNO |
| 68 | `0x449192` | 26496 | `0x006691D2` | `0x0066F952` | 300.4 | TECHNO |
| 69 | `0x44F912` | 35104 | `0x0066F952` | `0x00678272` | 398.0 | BASIC |
| 70 | `0x458232` | 51024 | `0x00678272` | `0x006849C2` | 578.5 | BASIC |
| 71 | `0x464982` | 114840 | `0x006849C2` | `0x006A0A5A` | 1302.0 | URBAN |
| 72 | `0x480A1A` | 220186 | `0x006A0A5A` | `0x006D6674` | 2496.4 | TECHNO |

## Notes

* Ids are **table entries**, not names. A kit slot stores an id; the firmware looks the id up in this table at playback time.
* **The table is data, not frozen.** The code reads it through two literal words only and never addresses a sample by a fixed address, so positions, lengths and the entry count can all change (`patch_samples.py --repack`, `r24bank.py relocate`). The only fixed things are the bank base and the file size.
* Entries may share audio (a relocated per-kit table starts that way); an in-place patch refuses an entry whose bytes are shared.
* The bank is 44.1 kHz mono 16-bit; the `ms` column uses that rate (the firmware's voice-duration code divides by 44100 as well).
