# 14 — KIT SELECT > CUSTOM: the 16-pad editor

> Added 2026-11-19. SDK patch `sdk/patches/kit_editor.c` (needs `custom_kit.c`
> and `kit_assign.c`). Proven in the emulator (`tools/test_kit_editor.py`,
> 71 checks); **verified on a device** (2026-11-19, `build/R24SYS.BIN` sha256 `a4f1c0a2…`).

## Use

KIT SELECT → choose **CUSTOM** → ENTER opens:

```
CUSTOM KIT
>SETTINGS                ENTER        volume / pan / mute group (below)
 KICK   SNARE            UP/DOWN      pad (the list scrolls, 5 rows shown)
 SNARE  -                LEFT/RIGHT   file of that pad, among the project's
 CL HH  KICK?                         files (the list FILE SELECT shows); dial too
 OP HH  SAMPLE04         ENTER        assign the chosen file (? = not yet)
 CRASH  HAT2!            EXIT         leave (unassigned choices are dropped)
Complete!
```

Pad labels are short (KICK SNARE CL HH OP HH CRASH RIDE TOM1 TOM2 TOM3 CLAP
TAMB COWB CLAVE H CONG L CONG PERC) so the file name gets 13 characters.
Value column (names without `.WAV`): `NAME?` chosen, not assigned yet ·
`NAME` the pad's file (from `KIT.TXT`, so it survives reloading the project) ·
`NAME!` listed in `KIT.TXT` but it did not load (missing / bad WAV) ·
`-` default sound. Assigning = FILE > KIT ASSIGN: the file is copied to
`KIT\<same name>`, recorded in `KIT.TXT`, and the kit reloads at once. Choosing another kit in KIT
SELECT behaves as before.

## SETTINGS (2026-11-19)

The first row, **SETTINGS**, opens:

```
KIT SETTINGS
 PAD    VOL  PAN  MUTE        UP/DOWN     pad (scrolls, 4 pads shown)
>KICK  >14   C    OFF         LEFT/RIGHT  column (VOL / PAN / MUTE)
 SNARE  13   C    OFF         dial        change the value - heard at once
 CL HH   7   R35  1           ENTER       save to KIT.TXT ("Saved!")
 OP HH   7   R35  1           EXIT        back; saves if something changed
```

VOL 0..15, PAN L64..C..R62, MUTE OFF or 1..7 (pads of the same group cut each
other, like CL HH / OP HH). RIGHT after MUTE opens **page 2: PITCH** −24..+19
semitones and **POLY** 0/1 (0, the default: a new hit of the pad cuts the
previous one) — `19_drum_pitch.md`; LEFT comes back. The values are written straight into the CUSTOM
kit (the engine reads it at every note) and stored as `Snn=level,pan,group`
lines of `KIT.TXT` (`Snn=level,pan,group[,pitch[,poly]]`) (`12_custom_kit.md` §1.1), only for pads that differ from
the kit's own values. Protected project: "Project Protect", nothing written.
Code: `kit_editor.c` (`ks_settings`), `custom_kit.c` (`cust_get/set/save_settings`).
Emulator: `tools/test_kit_editor.py` (113 checks). **Not yet tried on a device.**

## How

| site | change |
|---|---|
| `0x800FD3BC` (KIT SELECT record `0x800FD398` + 0x24) | ENTER callback `0x8004B687` → `ke_confirm`: calls the original (settings.kit = choice, `set_kit`), then the editor if the kit is CUSTOM |

The record is opened by `0x8003C29A` (graphic menu engine, like FILE); the
editor draws with the FILE COPY primitives (`13_kit_assign.md` §3).

File list: the firmware's lister `0x8000205C(&list, 0x01, 0)` — `list.dir`
= `A:\ZOOM_R24\PROJECT\<prj>\AUDIO`; flag `0x01` = WAVs only, no folders.
Names come back in `0x8046A080 + i × 260`, the count in `list + 8`.
Until 2026-11-19 it used FILE SELECT's flags (`0x800406C0`: `0x13` at
44.1 kHz, `0x23` at 48 kHz). Their `0x10`/`0x20` bits are a sample-rate filter
that opens every WAV and reads its header (280 files = 280 opens: a visible
wait on entering the editor); `0x02` marks the files used by takes, unused
here. The custom kit loader resamples 4–192 kHz, so the filter only hid usable
files; a file it cannot read gives "Bad WAV Format" on ENTER. Real lister in
the emulator, 280 WAVs: `0x01` → 0 opens, `0x13` → 280.

List position: a pad with no pending choice steps from the last file chosen
on any pad (`ke_pos`), so files can be assigned one after another (KICK07 on
KICK, then RIGHT on SNARE shows KICK08). Kept by name (`ke_last`) across
visits and found again when the list changes; reaching "none" resets it.

Keys (menu engine `0x8003B740`): `0x10020` UP, `0x10021` DOWN, `0x10022`
RIGHT, `0x10023` LEFT; `0x2002x` = the same key held. ENTER `0x10024`, EXIT
`0x10019`, dial `0x10000000`.

## Device test

Verified on a device on 2026-11-19 (`build/R24SYS.BIN`, sha256 `a4f1c0a2…`): screen, keys, copy, reload and return to the menu all as described.

## Pending

* **Long file names** (> 8.3) copied into `KIT\`: whether the R24 creates
  them correctly is not verified (FILE COPY/RENAME limit names to 8
  characters). If not, shorten the destination name and record it in
  `KIT.TXT`. Left open on purpose (2026-11-19).

