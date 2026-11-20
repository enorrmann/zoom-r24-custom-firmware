/*
 * fast_browse.c — LOOP[SD] / LOOP[USB] folder browser without the long wait
 * (docs/10_loop_import.md §9).
 *
 * The browser (fw_browse, 0x800423C4) lists a folder through
 * fw_browse_select (0x80041DF2) with flags 4/5. Flag 4 makes fw_list_files
 * call fw_list_info for EVERY entry: open the file as a WAV, read its header,
 * stat it (another directory lookup), close. With N files that is N header
 * reads plus ~2N directory searches — seconds in a big folder. The list
 * screen only shows names; the record (channels, rate, length) is read for
 * one entry only: the one chosen (fw_browse) or previewed (event 0x10033 in
 * fw_browse_event).
 *
 * This patch re-implements fw_browse_select. For the browser's flags (4/5)
 * it lists without flag 4, fills each record with just the name and the
 * folder mark (+0x18 = 2, used by the row icon), and calls fw_list_info for
 * the entry that is previewed or chosen. Any other caller (flags 0x1D/0x2D,
 * 0x80041E8E) runs exactly as the original.
 */
#include "r24.h"

#define FB_REC      28
#define FB_ENTER    0x10024
#define FB_PREVIEW  0x10033

static int fb_lazy;

static void fb_fill(int i)
{
    struct fw_dirlist *l = &fw_browse_list;
    if (fb_lazy && i >= 0 && i < l->count)
        fw_list_info(i, l);
}

/* the menu's event callback: make the previewed entry's record real first */
static int fb_event(int ev)
{
    if (ev == FB_PREVIEW)
        fb_fill(fw_browse_sel);
    return fw_browse_event(ev);
}

/* The R24_REPLACE trampoline uses r3, so the 4th argument (noback) is lost.
 * Each of the three callers passes a constant, pinned here: fw_browse
 * passes flags 4/5 with noback 1, 0x80041E8E passes 0x1D/0x2D with 0. */
R24_CHECK(0x8004241E, "0527");      /* fw_browse: movs r7,#5 (flags, deep) */
R24_CHECK(0x80042422, "0427");      /*            movs r7,#4 (flags)       */
R24_CHECK(0x80042430, "0123");      /*            movs r3,#1 (noback)      */
R24_CHECK(0x80041ED0, "1d22");      /* 0x80041E8E: movs r2,#0x1d */
R24_CHECK(0x80041ED6, "0023");      /*             movs r3,#0    */
R24_CHECK(0x80041EE0, "2d22");      /*             movs r2,#0x2d */
R24_CHECK(0x80041EE6, "0023");      /*             movs r3,#0    */

int fb_select(uint32_t head_cb, const char *dir, int flags);
R24_REPLACE(0x80041DF2, "f8b505000e0017001c00", fb_select);

/* Same as the original (0x80041DF2..0x80041E8C), plus the lazy records. */
int fb_select(uint32_t head_cb, const char *dir, int flags)
{
    struct fw_dirlist *l = &fw_browse_list;
    uint32_t *m = fw_browse_menu;
    int i, r, noback;

    fb_lazy = (flags & ~1) == 4;            /* fw_browse: 4, or 5 when deep */
    noback = fb_lazy;                       /* see the checks above */
    fw_browse_sel = 0;
    l->dir = dir;
    fw_list_files(l, fb_lazy ? flags & ~4 : flags, fw_track_sel);
    if (fb_lazy) {
        /* files are entries [0, files), folders [files, count) */
        for (i = 0; i < l->count; i++) {
            uint8_t *e = (uint8_t *)l->entries + i * FB_REC;
            memset(e, 0, FB_REC);
            *(const char **)e = fw_list_names[i];
            e[0x18] = i >= l->files ? 2 : 0;
        }
    }
    m[0] = head_cb;
    if (noback) {
        if (l->count == 0)
            return -2;
        m[1] = l->count;
    } else {
        m[1] = l->count + 1;
    }
    m[2] = 0;
    m[3] = noback ? (uint32_t)fw_browse_row : (uint32_t)fw_browse_row_back;
    m[4] = (uint32_t)&fw_browse_sel;
    m[5] = m[6] = m[7] = 0;
    m[8] = FB_ENTER;
    m[9] = m[10] = m[11] = m[12] = m[13] = 0;
    fw_ui_progress_end(0);
    r = fw_menu_run(m, fb_lazy ? fb_event : fw_browse_event, fw_browse_draw);
    if (r != FB_ENTER)
        return -1;
    r = fw_browse_sel;
    fb_fill(r);                             /* the chosen entry, before fw_browse reads it */
    return r;
}
