/*
 * kit_assign.c — "KIT ASSIGN" in the FILE menu: put the selected WAV on a pad
 * of the CUSTOM kit, from the R24 itself. Needs custom_kit.c in the same build.
 *
 * The FILE menu (FILE SELECT -> ENTER: INFO / COPY / DELETE / RENAME / DIVIDE)
 * is opened by 0x800409F6, which builds a 48-byte menu header on the stack:
 * item count `movs r0,#5` (0x800409FC) and the item pointer array 0x800EA174
 * (literal at 0x80040BB4, used only there), then calls 0x8003A776. The items
 * are 80-byte records (label, enabled, ..., callback at +0x10), like TAKE
 * SELECT (docs/10_loop_import.md §1). docs/13_kit_assign.md has the details.
 *
 * So the patch: a 6-pointer array here (the 5 originals + ours), the literal
 * repointed at it, 5 -> 6.
 *
 * Choosing KIT ASSIGN opens a screen drawn like FILE COPY's (the FILE menu is
 * a graphic screen; text-mode fw_lcd_text() is invisible there):
 *     KIT ASSIGN
 *     FILE      KICK.WAV
 *     PAD 04    OP HAT *        (dial / cursor keys; * = pad already custom)
 *     ENTER:OK EXIT:BACK
 * ENTER copies and reloads, EXIT leaves.
 * The file is copied to A:\ZOOM_R24\PROJECT\<project>\KIT\<same name> (the
 * KIT folder is created if needed), the pad is recorded in <project>\KIT.TXT
 * and the CUSTOM kit is reloaded at once.
 */
#include "r24.h"
#include "custom_kit.h"
#include "kit_assign.h"

/* ── the menu ──────────────────────────────────────────────────────────── */
struct menu_item {                  /* 80 bytes, as at 0x800E9F94 (INFO) */
    const char *label;              /* +0x00 */
    uint32_t enabled;               /* +0x04 1 */
    uint32_t w2, w3;
    const void *callback;           /* +0x10 Thumb */
    uint32_t w5, w6, w7, w8, w9, w10, w11;
    const void *str[4];             /* +0x30 pointers to an empty string */
    uint32_t w16, w17, w18, w19;
};

void ka_menu_cb(void);

/* the five original items and their pointer array, checked byte for byte */
R24_CHECK(0x800EA174, "949f0e80e49f0e8034a00e8084a00e80d4a00e80");
R24_CHECK(0x800E9F94, "ec9b1e80010000000000000000000000c3de0380"  /* INFO */
                      "0000000001000000" "58be1e80");
R24_CHECK(0x800E9FC4, "819c1e80");                 /* empty string */

static const char ka_label[] = "KIT ASSIGN";
struct menu_item ka_item = {                        /* a copy of INFO's record */
    ka_label, 1, 0, 0, (const void *)ka_menu_cb, 0, 1, 0x801EBE58, 0, 0, 1, 0,
    { (const void *)0x801E9C81, (const void *)0x801E9C81,
      (const void *)0x801E9C81, (const void *)0x801E9C81 }, 0, 0, 0, 0
};

const void *ka_items[6] = {
    (const void *)0x800E9F94, (const void *)0x800E9FE4, (const void *)0x800EA034,
    (const void *)0x800EA084, (const void *)0x800EA0D4, &ka_item,
};

/* 0x800409F6: movs r0,#5 (item count) and the pointer-array literal */
R24_BYTES(0x800409FC, "0520", "0620");
R24_POINTER(0x80040BB4, 0x800EA174, ka_items);

/* ── the dialog ────────────────────────────────────────────────────────── */
/* short (<= 6 chars) so the file name gets the room */
const char *const ka_pad_name[CUST_SLOTS] = {
    "KICK", "SNARE", "CL HH", "OP HH", "CRASH", "RIDE", "TOM1", "TOM2",
    "TOM3", "CLAP", "TAMB", "COWB", "CLAVE", "H CONG", "L CONG", "PERC",
};

int ka_last = 0;                    /* pad shown first next time */
static char src[256], dst[160], dir[160], t1[24], t2[24];

#define X_VALUE 0x3E                /* value column, as FILE COPY */
#define ROW(n)  (0x0C + 9 * (n))

static int is_wav(const char *name)
{
    uint32_t n = r24_strlen(name);
    return n > 4 && r24_streq_nocase(name + n - 4, ".WAV");
}

static void copy_n(char *out, const char *s, int n)
{
    int i = 0;
    while (i < n && s[i]) { out[i] = s[i]; i++; }
    out[i] = 0;
}

void ka_draw(const char *file, int slot)
{
    const char *p = ka_pad_name[slot];
    int n = 0;
    fw_gfx_lock();
    fw_gfx_clear(0);
    fw_gfx_title("KIT ASSIGN", 0x20);
    fw_gfx_text("FILE", 1, ROW(0));
    copy_n(t1, file, 11);
    fw_gfx_text(t1, X_VALUE, ROW(0));
    t2[n++] = 'P'; t2[n++] = 'A'; t2[n++] = 'D'; t2[n++] = ' ';
    t2[n++] = (char)('0' + (slot + 1) / 10);
    t2[n++] = (char)('0' + (slot + 1) % 10);
    t2[n] = 0;
    fw_gfx_text(t2, 1, ROW(1));
    n = 0;
    while (*p && n < 9) t1[n++] = *p++;
    if (cust_slot_result(slot) > 0) { t1[n++] = ' '; t1[n++] = '*'; }
    t1[n] = 0;
    fw_gfx_text(t1, X_VALUE, ROW(1));
    fw_gfx_text("ENTER:OK EXIT:BACK", 1, ROW(3));
    fw_gfx_unlock();
}

/* copy AUDIO\<name> to KIT\<name> (original name kept), record the pad in
 * KIT.TXT, reload; returns the message to show */
const char *ka_assign(const char *name, int slot)
{
    int err, res;
    if (r24_strlen(name) >= 64 ||
        r24_strlen(fw_cur_project_name) + r24_strlen(name) + 32 > sizeof(src))
        return "Name Too Long";
    dir[0] = 0;                     /* A:\ZOOM_R24\PROJECT\<project>\KIT */
    r24_strcat(dir, "A:\\ZOOM_R24\\PROJECT\\");
    r24_strcat(dir, fw_cur_project_name);
    r24_strcat(dir, "\\KIT");
    cust_file_path(dst, fw_cur_project_name, name);
    src[0] = 0;                     /* A:\ZOOM_R24\PROJECT\<project>\AUDIO\<name> */
    r24_strcat(src, "A:\\ZOOM_R24\\PROJECT\\");
    r24_strcat(src, fw_cur_project_name);
    r24_strcat(src, "\\AUDIO\\");
    r24_strcat(src, name);

    fw_ui_progress("Copying...");
    err = fw_make_dir(dir);
    if (!err) err = fw_file_copy(src, dst);
    fw_ui_progress_end(0);
    if (err) return err == 0x1C ? "Card Full" : "File Access Error";
    if (cust_set_pad(slot, name)) return "KIT.TXT Error";
    cust_reload();
    res = cust_slot_result(slot);
    if (res > 0) return "Complete!";
    if (res == -4) return "Kit Memory Full";
    return "Bad WAV Format";
}

/* the menu item's callback; structure of FILE COPY (0x8003E07C) */
void ka_menu_cb(void)
{
    const char *name = fw_selected_file;
    const void *saved;
    const char *msg = 0;
    int slot = ka_last;

    if (fw_busy_guard(9)) return;                   /* as FILE COPY / DELETE */
    if (fw_project_protect) { fw_ui_message("Project Protect"); return; }
    if (fw_cur_project < 0) { fw_ui_message("No Project"); return; }
    if (!name[0]) return;
    if (!is_wav(name)) { fw_ui_message("Not a WAV file"); return; }
    if (r24_strlen(fw_cur_project_name) + r24_strlen(name) + 32 > 256) return;

    saved = fw_screen;
    fw_set_screen(R24_SCREEN_MENU);
    fw_ui_enter();
    fw_edit_reset();
    for (;;) {
        int ev;
        ka_draw(name, slot);
        ev = fw_wait_event(-1);
        if (ev == R24_EV_EXIT) break;
        if (ev == R24_EV_ENTER) {
            ka_last = slot;
            msg = ka_assign(name, slot);
            break;
        }
        if (ev == R24_EV_DIAL) slot += fw_dial_delta();
        else if (R24_ARROW(ev) == R24_EV_UP || R24_ARROW(ev) == R24_EV_LEFT) slot--;
        else if (R24_ARROW(ev) == R24_EV_DOWN || R24_ARROW(ev) == R24_EV_RIGHT) slot++;
        if (slot < 0) slot = 0;
        if (slot >= CUST_SLOTS) slot = CUST_SLOTS - 1;
    }
    if (fw_edit_active()) fw_edit_reset();
    fw_ui_leave();
    fw_set_screen(saved);
    if (msg) fw_ui_message(msg);    /* like "Project Protect": after the screen is back */
}
