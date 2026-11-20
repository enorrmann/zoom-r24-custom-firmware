# 13 — FILE > KIT ASSIGN: loading the CUSTOM kit from the R24 itself

> Added 2026-11-19. SDK patch `sdk/patches/kit_assign.c` (needs
> `custom_kit.c`, `12_custom_kit.md`). Proven in the emulator
> (`tools/test_kit_assign.py`, 191 checks); **verified on a device** (2026-11-19, `build/R24SYS.BIN` sha256 `a4f1c0a2…`).

## 1. Use

1. In FILE SELECT choose a WAV and press ENTER: the options are now
   INFO / COPY / DELETE / RENAME / DIVIDE / **KIT ASSIGN**.
2. KIT ASSIGN opens a screen like FILE COPY's:

   ```
   KIT ASSIGN
   FILE      KICK.WAV
   PAD 04    OP HAT *        * = this pad already holds a project sample
   ENTER:OK EXIT:BACK
   ```

   Turn the dial (or the cursor keys) to choose the pad. ENTER assigns,
   EXIT goes back.
3. The file is copied to `A:\ZOOM_R24\PROJECT\<project>\KIT\<same name>`
   (the `KIT` folder is created if needed), the pad is recorded in
   `<project>\KIT.TXT` (`12_custom_kit.md` §1.1) and the CUSTOM kit is
   reloaded at once: select kit CUSTOM and play the pad.

Messages: `Complete!`, `Card Full`, `File Access Error`, `Bad WAV Format`
(file kept on the card, pad keeps its default), `Kit Memory Full` (reserve used
up), `Not a WAV file`, `No Project`, `Project Protect`. Like COPY/DELETE it does
nothing while the recorder is busy (`busy_guard(9)`). Any WAV the loader
accepts works (`12_custom_kit.md` §1), so recordings of the project
(`AUDIO\TRK…`) can go straight onto a pad.

## 2. How the FILE menu is built **[PROVEN, static + emulator]**

> First version (same day) patched the wrong menu: the 4-item COPY/DELETE/
> RENAME/DIVIDE list at `0x801EB270`, opened by `0x80011B9C`, belongs to another
> file browser. The FILE menu seen on the device has INFO first; it is below.

`0x800409F6` opens it: it builds a 48-byte menu header on the stack and calls
`0x8003A776(header, 0, 0, 0)` — the same menu engine as TAKE SELECT
(`10_loop_import.md` §1):

```
+0x00  count        movs r0,#5  at 0x800409FC
+0x04  title        (argument)
+0x14  0x801EBE38   +0x18 0x801EBE3C   +0x1C 0x0001001A
+0x20  items        ldr r0,=0x800EA174  (literal at 0x80040BB4, used only here)
+0x24  0x8004C6F9   +0x28 0x8003BC41
```

`0x800EA174` holds 5 pointers to 80-byte item records (label, enabled, …,
callback at `+0x10`):

| record | label | callback |
|---|---|---|
| `0x800E9F94` | INFO   | `0x8003DEC3` |
| `0x800E9FE4` | COPY   | `0x8003E07D` |
| `0x800EA034` | DELETE | `0x8003E285` |
| `0x800EA084` | RENAME | `0x8003E3DB` |
| `0x800EA0D4` | DIVIDE | `0x8003EBA9` |

and is followed directly by other data, so it cannot grow in place. The
callbacks take no arguments: the chosen file's name is the global
`0x81138828`, in `A:\ZOOM_R24\PROJECT\<project>\AUDIO`; COPY/DELETE first
call `0x80005CA2(9)` (busy guard) and refuse when `0x804C1E18` (project
protect) is set.

The patch:

| site | change |
|---|---|
| `0x80040BB4` | `0x800EA174` → `ka_items`: the 5 original pointers + `ka_item` |
| `0x800409FC` | `movs r0,#5` → `#6` |
| `ka_item` | a copy of INFO's record with label `KIT ASSIGN` and our callback |
| `0x800EA174`, INFO record | checked only |

## 3. The dialog — a graphic screen **[fixed after the first device test]**

First device test (2026-11-19): the item appeared, but choosing it seemed to
hang the R24 until EXIT. Cause: the dialog drew with `lcd_text`
(`0x8001BC80`), which belongs to the text-mode screens; the FILE menu is a
**graphic** screen, so nothing was drawn while the dialog waited for keys —
EXIT ended its loop.

It now follows FILE COPY (`0x8003E07C`) call for call:

```
saved = [0x801EBA30]; set_screen(0x8003BC41); ui_enter() 0x8003A240; edit_reset() 0x8003C71E
loop:  gfx_lock() 0x800321A0; gfx_clear(0) 0x800338E2; gfx_title(t, 0x20) 0x8003716E
       gfx_text(s, x, y) 0x80037142  (rows y = 0x0C, 0x15, 0x1E, …; values at x = 0x3E)
       gfx_unlock() 0x80032176;  ev = wait_event(-1) 0x800033E0
       0x10024 ENTER, 0x10019 EXIT, 0x10020/0x10021 cursor, 0x10000000 dial (dial_delta 0x80003B7A)
ENTER: ui_progress("Copying...") 0x8003D9DA; copy 0x800016C0; ui_progress_end(0) 0x8003DA74
exit:  if edit_active() 0x8003C4E6: edit_reset(); ui_leave() 0x8003A24E; set_screen(saved)
then:  ui_message(text) 0x8003D32E  (as COPY's "Project Protect")
```

All of these are in `sdk/firmware.sym`; the test fails if a text-mode call
(`lcd_text`, `confirm`, `wait_ms`) is made from this dialog.

## 4. Device test

Verified on a device on 2026-11-19 (`build/R24SYS.BIN`, sha256 `a4f1c0a2…`): screen, keys, copy, reload and return to the menu all as described.
