/*
 * project_menu.c — four PROJECT menu items (after FILE):
 *
 *   IMPORT SD     copy every WAV of a card folder into the project's pool
 *   IMPORT USB    the same from a USB stick
 *   CLEAR UNUSED  delete the files of AUDIO that no track uses
 *   DUMP 1        only with -D PM_DUMP (build_firmware.py --dump): flash + DSP
 *   DUMP 2        (+ the whole DDR, ON by default; dial = off) -> A:\DUMP1 /
 *                 A:\DUMP2, so two states can be compared without reflashing
 *                 (r24dump.py --diff). Needs mem_dump.c in the build
 *
 * IMPORT: the LOOP[SD]/[USB] browser opens at the root (A: or B:); go into
 * the folder and choose ANY WAV in it ->
 *       IMPORT SD
 *       FOLDER    DRUMS
 *       FILES     12 WAV
 *       ENTER:OK EXIT:BACK
 * ENTER copies them to PROJECT\<prj>\AUDIO ("Importing 3/12"), then
 * "Imported 9 Skip 3". Only the WAVs FILE would show (FILE SELECT's lister
 * flags for this project, 0x13 at 44.1 kHz / 0x23 at 48 kHz); a name already
 * in AUDIO is skipped. IMPORT USB wraps it in LOOP[USB]'s own sequence
 * (host mode, wait for the stick, save/close, then USB off and reload).
 *
 * CLEAR UNUSED: lists AUDIO (all WAVs, flag 0x2 = file -> take map, the '*'
 * of FILE) and warns:
 *       CLEAR UNUSED
 *       DELETE    7 WAV
 *       NOT USED BY TRACKS
 *       CANNOT BE UNDONE!
 *       ENTER:OK EXIT:BACK
 * ENTER deletes exactly those, as FILE DELETE deletes an unused file.
 *
 * The PROJECT menu header is data (0x800D44DC: count 10, item pointer array
 * at +0x20 = 0x800D44B4, opened by 0x80019610 -> 0x8003A776): a 13-pointer
 * array with the four items after FILE, count 10 -> 14.
 * docs/15_project_menu.md.
 */
#include "r24.h"

struct menu_item {                  /* 80 bytes, as PROJECT > FILE (0x800D43C4) */
    const char *label;
    uint32_t enabled, w2, w3;
    const void *callback;
    uint32_t w5, w6, w7, w8, w9, w10, w11;
    const void *str[4];
    uint32_t w16, w17, w18, w19;
};

void pm_import_sd(void);
void pm_import_usb(void);
void pm_clear_unused(void);
void pm_mem_dump1(void);
void pm_mem_dump2(void);
const char *md_dump(int ddr, const char *dir);      /* mem_dump.c */
static int pm_dump_ddr;                             /* DUMP n: + DDR.BIN */
static const char *pm_dump_dir = "A:\\DUMP1";

R24_CHECK(0x800D44B4, "94410d80e4410d8034420d8084420d80d4420d80"
                      "24430d8074430d80c4430d8014440d8064440d80");
R24_CHECK(0x800D43C4, "a59b1e80010000000000000000000000390a0480"
                      "000000000100000000000000");       /* FILE, the model */

#define ITEM(label, cb) { label, 1, 0, 0, (const void *)cb, 0, 1, 0, 0, 0, 0, 0, \
    { (const void *)0x801E9C81, (const void *)0x801E9C81, \
      (const void *)0x801E9C81, (const void *)0x801E9C81 }, 0, 0, 0, 0 }

#ifdef PM_DUMP
#define PM_NEW 5                                    /* + DUMP 1, DUMP 2 */
#else
#define PM_NEW 3
#endif

struct menu_item pm_item[PM_NEW] = {
    ITEM("IMPORT SD", pm_import_sd),
    ITEM("IMPORT USB", pm_import_usb),
    ITEM("CLEAR UNUSED", pm_clear_unused),
#ifdef PM_DUMP
    ITEM("DUMP 1", pm_mem_dump1),
    ITEM("DUMP 2", pm_mem_dump2),
#endif
};

const void *pm_items[10 + PM_NEW] = {
    (const void *)0x800D4194, (const void *)0x800D41E4, (const void *)0x800D4234,
    (const void *)0x800D4284, (const void *)0x800D42D4, (const void *)0x800D4324,
    (const void *)0x800D4374, (const void *)0x800D43C4,                 /* ... FILE */
    &pm_item[0], &pm_item[1], &pm_item[2],
#ifdef PM_DUMP
    &pm_item[3], &pm_item[4],
#endif
    (const void *)0x800D4414, (const void *)0x800D4464,                 /* REC, SEQ PLAY */
};

#ifdef PM_DUMP
R24_BYTES(0x800D44DC, "0a000000", "0f000000");          /* item count 15 */
#else
R24_BYTES(0x800D44DC, "0a000000", "0d000000");          /* item count 13 */
#endif
R24_POINTER(0x800D44FC, 0x800D44B4, pm_items);          /* item array */

#define X_VALUE 0x3E
#define ROW(n)  (0x0C + 9 * (n))

static char if_cur[264], if_full[264], if_dir[264], if_src[320], if_dst[320];   /* browser writes <= 0x104 */
static char if_t[32];

static void num(char *out, int v)   /* small non-negative int -> text */
{
    char t[8];
    int n = 0, i = 0;
    do { t[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < 7);
    while (n) out[i++] = t[--n];
    out[i] = 0;
}

static void draw_confirm(const char *title, const char *folder, int count)
{
    fw_gfx_lock();
    fw_gfx_clear(0);
    fw_gfx_title(title, 0x20);
    num(if_t, count);
    r24_strcat(if_t, " WAV");
    if (title[0] == 'D') {                          /* DUMP n */
        fw_gfx_text("WRITE TO", 1, ROW(0));
        fw_gfx_text(pm_dump_dir, X_VALUE, ROW(0));
        fw_gfx_text("FLASH+DSP 21MB", 1, ROW(1));
        fw_gfx_text("+DDR 128MB", 1, ROW(2));
        fw_gfx_text(pm_dump_ddr ? "ON" : "OFF", X_VALUE, ROW(2));
    } else if (title[0] == 'C') {                   /* CLEAR UNUSED: the warning */
        fw_gfx_text("DELETE", 1, ROW(0));
        fw_gfx_text(if_t, X_VALUE, ROW(0));
        fw_gfx_text("NOT USED BY TRACKS", 1, ROW(1));
        fw_gfx_text("CANNOT BE UNDONE!", 1, ROW(2));
    } else {
        fw_gfx_text("FOLDER", 1, ROW(0));
        fw_gfx_text(folder, X_VALUE, ROW(0));
        fw_gfx_text("FILES", 1, ROW(1));
        fw_gfx_text(if_t, X_VALUE, ROW(1));
    }
    fw_gfx_text("ENTER:OK EXIT:BACK", 1, ROW(3));
    fw_gfx_unlock();
}

/* list `dir` with the lister; FILE SELECT's flags for this project are
 * 0x13 (44.1 kHz) / 0x23 (48 kHz); -> number of files */
int if_list(const char *dir, int flags)
{
    struct fw_dirlist l;
    memset(&l, 0, sizeof(l));
    l.dir = dir;
    fw_list_files(&l, flags, 0);
    return l.count > 0 ? l.count : 0;
}

static int pool_flags(void) { return fw_project_settings[0x38] ? 0x23 : 0x13; }

/* copy the listed files; -> "Imported N Skip M" or an error */
static const char *if_copy(int count)
{
    static char msg[40];
    int i, done = 0, skip = 0, err = 0;
    for (i = 0; i < count && !err; i++) {
        const char *name = fw_list_names[i];
        fw_file *f;
        if (r24_strlen(if_dir) + r24_strlen(name) + 2 > sizeof(if_src) ||
            r24_strlen(fw_cur_project_name) + r24_strlen(name) + 32 > sizeof(if_dst)) {
            skip++;
            continue;
        }
        if_src[0] = 0;
        r24_strcat(if_src, if_dir);
        r24_strcat(if_src, "\\");
        r24_strcat(if_src, name);
        if_dst[0] = 0;
        r24_strcat(if_dst, "A:\\ZOOM_R24\\PROJECT\\");
        r24_strcat(if_dst, fw_cur_project_name);
        r24_strcat(if_dst, "\\AUDIO\\");
        r24_strcat(if_dst, name);
        f = fw_fopen(if_dst, "r");
        if (f) { fw_fclose(f); skip++; continue; }       /* already in the pool */
        msg[0] = 0;
        r24_strcat(msg, "Importing ");
        num(if_t, i + 1);
        r24_strcat(msg, if_t);
        r24_strcat(msg, "/");
        num(if_t, count);
        r24_strcat(msg, if_t);
        fw_ui_progress(msg);
        err = fw_file_copy(if_src, if_dst);
        fw_ui_progress_end(0);
        if (!err) done++;
    }
    if (err == 0x1C) return "Card Full";
    if (err) return "File Access Error";
    msg[0] = 0;
    r24_strcat(msg, "Imported ");
    num(if_t, done);
    r24_strcat(msg, if_t);
    if (skip) {
        r24_strcat(msg, " Skip ");
        num(if_t, skip);
        r24_strcat(msg, if_t);
    }
    return msg;
}

/* FILE COPY's screen pattern around a confirm screen; -> 1 if ENTER */
static int confirm(const char *title, const char *a, int count)
{
    const void *saved = fw_screen;
    int yes = 0;
    fw_set_screen(R24_SCREEN_MENU);
    fw_ui_enter();
    fw_edit_reset();
    for (;;) {
        int ev;
        draw_confirm(title, a, count);
        ev = fw_wait_event(-1);
        if (ev == R24_EV_EXIT) break;
        if (ev == R24_EV_DIAL && fw_dial_delta() && title[0] == 'D')
            pm_dump_ddr = !pm_dump_ddr;             /* DUMP n: dial = DDR on/off */
        if (ev == R24_EV_ENTER) { yes = 1; break; }
    }
    if (fw_edit_active()) fw_edit_reset();
    fw_ui_leave();
    fw_set_screen(saved);
    return yes;
}

/* browse from `root` ("A:" / "B:"), confirm, copy. -> message or 0 */
static const char *import_from(const char *root, const char *title)
{
    const char *folder;
    int idx, count, i, last = -1;

    if_cur[0] = root[0]; if_cur[1] = ':'; if_cur[2] = 0;
    if_full[0] = 0;
    idx = fw_browse(if_cur, if_full, root);
    if (idx == -2) return "No File";
    if (idx < 0 || !if_full[0]) return 0;

    for (i = 0; if_full[i] && i < (int)sizeof(if_dir) - 1; i++) {
        if_dir[i] = if_full[i];
        if (if_full[i] == '\\') last = i;
    }
    if (last <= 0) return 0;
    if_dir[last] = 0;                               /* folder of the chosen file */
    folder = if_dir + last;
    while (folder > if_dir && folder[-1] != '\\') folder--;

    count = if_list(if_dir, pool_flags());
    if (!count) return "No File";
    if (!confirm(title, folder, count)) return 0;
    return if_copy(count);
}

static int can_change_project(void)
{
    if (fw_cur_project < 0) { fw_ui_message("No Project"); return 0; }
    if (fw_project_protect) { fw_ui_message("Project Protect"); return 0; }
    return 1;
}

void pm_import_sd(void)
{
    const char *msg;
    if (fw_busy_guard(9)) return;                   /* as FILE COPY */
    if (!can_change_project()) return;
    msg = import_from("A:", "IMPORT SD");
    if (msg) fw_ui_message(msg);
}

/* LOOP[USB]'s sequence (0x800425BA .. 0x80042AD4) around the import */
void pm_import_usb(void)
{
    const char *msg = 0;
    int sel;
    if (fw_busy_guard(0xFFFF)) return;              /* as LOOP[USB] */
    if (fw_sync_rec_guard()) return;
    if (!can_change_project()) return;
    fw_audio_mode(0);
    fw_usb_wait_box();
    if (fw_project_open_flag) fw_save_project_now();
    fw_project_open_flag = 0;
    fw_close_audio_files();
    if (fw_usb_host(1, 2)) {                        /* as LOOP[USB]: no cleanup */
        fw_ui_progress_end(0);
        fw_ui_message("USB Setup Error");
        return;
    }
    if (fw_usb_wait() >= 0) {                       /* < 0: no stick */
        fw_ui_progress_end(0);
        msg = import_from("B:", "IMPORT USB");
    }
    fw_ui_progress("Please Wait...");               /* LOOP[USB] cleanup */
    fw_rescan_drive('A');
    fw_usb_off();
    sel = fw_track_sel;
    fw_reload_tracks_a();
    fw_reload_tracks_b();
    fw_audio_mode(fw_rec_settings[0x34]);
    fw_project_open_flag = 1;
    fw_track_sel = sel;
    fw_ui_progress_end(0);
    if (msg) fw_ui_message(msg);
}

/* ── CLEAR UNUSED ───────────────────────────────────────────────────────── */
void pm_clear_unused(void)
{
    static char msg[32];
    int count, unused = 0, done = 0, fail = 0, i;

    if (fw_busy_guard(9)) return;                   /* as FILE DELETE */
    if (!can_change_project()) return;
    if_dir[0] = 0;
    r24_strcat(if_dir, "A:\\ZOOM_R24\\PROJECT\\");
    r24_strcat(if_dir, fw_cur_project_name);
    r24_strcat(if_dir, "\\AUDIO");
    count = if_list(if_dir, 0x03);                  /* all WAVs + take map */
    for (i = 0; i < count; i++)
        if (fw_file_take[i] == -1) unused++;
    if (!unused) { fw_ui_message("No Unused File"); return; }
    if (!confirm("CLEAR UNUSED", "", unused)) return;

    fw_ui_progress("Deleting...");
    fw_audio_sync();                                /* as FILE DELETE */
    for (i = 0; i < count; i++) {
        const char *name = fw_list_names[i];
        if (fw_file_take[i] != -1) continue;        /* used by a track: keep */
        if (r24_strlen(if_dir) + r24_strlen(name) + 2 > sizeof(if_src)) { fail++; continue; }
        if_src[0] = 0;
        r24_strcat(if_src, if_dir);
        r24_strcat(if_src, "\\");
        r24_strcat(if_src, name);
        if (fw_remove(if_src)) fail++;
        else done++;
    }
    fw_selected_file[0] = 0;                        /* as FILE DELETE */
    fw_ui_progress_end(0);
    msg[0] = 0;
    r24_strcat(msg, "Deleted ");
    num(if_t, done);
    r24_strcat(msg, if_t);
    if (fail) {
        r24_strcat(msg, " Error ");
        num(if_t, fail);
        r24_strcat(msg, if_t);
    }
    fw_ui_message(msg);
}

/* ── DUMP 1 / DUMP 2 ────────────────────────────────────────────────────── */
#ifdef PM_DUMP
static void pm_mem_dump(const char *title, const char *dir)
{
    if (fw_busy_guard(9)) return;                   /* as the FILE operations */
    pm_dump_ddr = 1;                                /* DDR on unless turned off */
    pm_dump_dir = dir;
    if (!confirm(title, "", 0)) return;
    fw_ui_message(md_dump(pm_dump_ddr, dir));
}

void pm_mem_dump1(void) { pm_mem_dump("DUMP 1", "A:\\DUMP1"); }
void pm_mem_dump2(void) { pm_mem_dump("DUMP 2", "A:\\DUMP2"); }
#endif
