/*
 * kit_editor.c — KIT SELECT > CUSTOM opens an editor of the 16 pads.
 * Needs custom_kit.c and kit_assign.c in the same build.
 *
 *     CUSTOM KIT
 *     >SETTINGS                    ENTER: volume / pan / mute group per pad
 *      KICK   KICK01               UP/DOWN   choose the pad (list scrolls)
 *      SNARE  SNARE                LEFT/RIGHT or dial: choose a file among the
 *      CL HH  -                              project's files (FILE SELECT's list)
 *      OP HH  HAT2?                ? = chosen, not assigned yet; ! = did not load
 *      CRASH  CRASH                ENTER     assign the chosen file to the pad
 *   (names shown without ".WAV")
 *     Complete!                    EXIT      leave
 *
 * SETTINGS (first row) opens
 *     KIT SETTINGS
 *      PAD    VOL  PAN  MUTE       UP/DOWN   pad;  LEFT/RIGHT  column
 *     >KICK  [14]  C    OFF        dial      change the value (heard at once)
 *      SNARE  13   C    OFF        ENTER     save to KIT.TXT ("Saved!")
 *      CL HH   7   R35  1          EXIT      back (saves if anything changed)
 * VOL 0..15, PAN L64..C..R62 (kit byte 0..126, 64 = C), MUTE OFF/1..7 (pads of
 * one group cut each other, as CL HH / OP HH). RIGHT after MUTE goes to a
 * second page of parameters (LEFT from its first column comes back):
 *      PAD    PITCH POLY
 *     >KICK  [0]    0              PITCH -24..+19 semitones (drum_pitch.c)
 *                                  POLY 0 = a new hit cuts the pad's last one
 *                                  (default), 1 = hits overlap
 * Stored as "S01=14,64,0[,+5[,1]]" lines.
 *
 * Hook: the KIT SELECT menu record (0x800FD398) has its ENTER/confirm
 * callback at +0x24 (0x8004B687: settings.kit = choice; set_kit). It now
 * points at ke_confirm, which calls the original and then, if the chosen
 * kit is CUSTOM, runs the editor — a graphic screen drawn like FILE COPY's.
 *
 * The file list is made by the firmware's own lister (0x8000205C) with flag
 * 0x01 only: every WAV of the project's AUDIO folder, sorted, names only.
 * FILE SELECT's flags (0x13/0x23) add a sample-rate filter that opens every
 * WAV to read its header — seconds with hundreds of files — and the custom
 * kit loader resamples any rate anyway (a file it cannot read gives "Bad WAV
 * Format" on ENTER). The list keeps its position: stepping on a pad with no
 * pending choice continues from the last file chosen (remembered by name,
 * across visits), so samples can be assigned one after another. Assigning
 * copies the file to
 * KIT\<same name>, records it in <project>\KIT.TXT and reloads (ka_assign,
 * as FILE > KIT ASSIGN); the names shown come from KIT.TXT, so they survive
 * reloading the project.
 */
#include "r24.h"
#include "custom_kit.h"
#include "kit_assign.h"

void ke_confirm(void);
R24_POINTER(0x800FD3BC, 0x8004B687, ke_confirm);   /* KIT SELECT record +0x24 */

#define ROWS    5                   /* pad rows on screen */
#define X_LABEL 7
#define X_VALUE 0x2E                /* labels are <= 6 chars: 13 chars of name */
#define VALUE_MAX 13
#define ROW(n)  (0x0C + 9 * (n))

static char ke_dir[160], ke_t[24];
static int ke_choice[CUST_SLOTS];  /* index in fw_list_names, -1 = none */
static int ke_count;                /* files listed */
static int ke_pos = -1;             /* list position: last file chosen, -1 = none */
static char ke_last[260];           /* its name, to find it again after relisting */
static const char *ke_status;
int ke_row, ke_top;                 /* cursor row (0 = SETTINGS, 1.. = pad), first row shown */
#define KE_ROWS (CUST_SLOTS + 1)
#define PAD(r)  ((r) - 1)

/* append a file name without its ".WAV" (every listed file is a WAV) */
static void put(char *out, int *n, const char *s, int max)
{
    uint32_t l = r24_strlen(s);
    if (l > 4 && r24_streq_nocase(s + l - 4, ".WAV")) l -= 4;
    while (l-- && *n < max) out[(*n)++] = *s++;
}

/* value column of a pad: chosen file?, its file (KIT.TXT), file! if it did
 * not load, or "-" (default sound) */
static void value_of(int slot, char *out)
{
    int n = 0;
    const char *f = cust_pad_file(slot);
    if (ke_choice[slot] >= 0) {
        put(out, &n, fw_list_names[ke_choice[slot]], VALUE_MAX - 1);
        out[n++] = '?';
    } else if (f[0]) {
        put(out, &n, f, cust_slot_result(slot) > 0 ? VALUE_MAX : VALUE_MAX - 1);
        if (cust_slot_result(slot) <= 0) out[n++] = '!';
    } else {
        out[n++] = '-';
    }
    out[n] = 0;
}

void ke_draw(void)
{
    int i;
    fw_gfx_lock();
    fw_gfx_clear(0);
    fw_gfx_title("CUSTOM KIT", 0x20);
    for (i = 0; i < ROWS; i++) {
        int r = ke_top + i;
        if (r >= KE_ROWS) break;
        if (r == ke_row) fw_gfx_text(">", 1, ROW(i));
        if (r == 0) {
            fw_gfx_text("SETTINGS", X_LABEL, ROW(i));
            continue;
        }
        fw_gfx_text(ka_pad_name[PAD(r)], X_LABEL, ROW(i));
        value_of(PAD(r), ke_t);
        fw_gfx_text(ke_t, X_VALUE, ROW(i));
    }
    if (ke_status) fw_gfx_text(ke_status, 1, ROW(ROWS));
    fw_gfx_unlock();
}

/* list the project's files exactly as FILE SELECT does */
static void ke_list(void)
{
    struct fw_dirlist l;
    memset(&l, 0, sizeof(l));
    ke_dir[0] = 0;
    r24_strcat(ke_dir, "A:\\ZOOM_R24\\PROJECT\\");
    r24_strcat(ke_dir, fw_cur_project_name);
    r24_strcat(ke_dir, "\\AUDIO");
    l.dir = ke_dir;
    fw_list_files(&l, 0x01, 0);         /* WAVs, no folders, no header reads */
    ke_count = l.count > 0 ? l.count : 0;
    ke_pos = -1;
    if (ke_last[0]) {
        int i;
        for (i = 0; i < ke_count; i++)
            if (!fw_strcmp(fw_list_names[i], ke_last)) { ke_pos = i; break; }
    }
}

/* -1 = none, 0..count-1 = a file; wraps. A pad with no pending choice
 * starts from the list position (the last file chosen on any pad). */
static void ke_step(int d)
{
    int c;
    if (ke_row < 1) return;
    if (!ke_count) { ke_status = "No File"; return; }
    c = ke_choice[PAD(ke_row)];
    if (c < 0) c = ke_pos;
    c += d;
    while (c < -1) c += ke_count + 1;
    while (c >= ke_count) c -= ke_count + 1;
    ke_choice[PAD(ke_row)] = c;
    ke_pos = c;
    ke_last[0] = 0;
    if (c >= 0) fw_strcpy(ke_last, fw_list_names[c]);
    ke_status = 0;
}

/* ── SETTINGS: volume / pan / mute group (page 1), pitch (page 2) ──────── */
#define KS_X_VOL  0x2E
#define KS_X_PAN  0x46
#define KS_X_MUTE 0x62
#define KS_PADROWS (ROWS - 1)       /* row 0 is the column header */
#define KS_X_PITCH KS_X_VOL
#define KS_X_POLY 0x5A
#define KS_NCOL   CUST_NSET         /* VOL PAN MUTE | PITCH POLY */
#define KS_PAGE(c) ((c) >= CUST_PITCH)  /* page of column c */
static const int ks_x[KS_NCOL] = { KS_X_VOL, KS_X_PAN, KS_X_MUTE, KS_X_PITCH, KS_X_POLY };
static const char *const ks_head[KS_NCOL] = { "VOL", "PAN", "MUTE", "PITCH", "POLY" };
int ks_row, ks_top, ks_col;         /* pad, first pad shown, column 0..3 */
static const char *ks_status;
static char ks_t[8];

static const char *ks_value(int col, int v)
{
    int n = 0;
    if (col == CUST_PAN) {          /* pan: 64 = C */
        int d = v - 64;
        if (!d) { ks_t[n++] = 'C'; ks_t[n] = 0; return ks_t; }
        ks_t[n++] = d < 0 ? 'L' : 'R';
        if (d < 0) d = -d;
        v = d;
    } else if (col == CUST_GROUP && !v) {
        return "OFF";
    } else if (col == CUST_PITCH && v) {   /* pitch: +n / -n semitones */
        ks_t[n++] = v < 0 ? '-' : '+';
        if (v < 0) v = -v;
    }
    if (v >= 10) ks_t[n++] = (char)('0' + v / 10);
    ks_t[n++] = (char)('0' + v % 10);
    ks_t[n] = 0;
    return ks_t;
}

void ks_draw(void)
{
    int i, c, pg = KS_PAGE(ks_col);
    fw_gfx_lock();
    fw_gfx_clear(0);
    fw_gfx_title("KIT SETTINGS", 0x20);
    fw_gfx_text("PAD", X_LABEL, ROW(0));
    for (c = 0; c < KS_NCOL; c++)
        if (KS_PAGE(c) == pg) fw_gfx_text(ks_head[c], ks_x[c], ROW(0));
    for (i = 0; i < KS_PADROWS; i++) {
        int s = ks_top + i, v[KS_NCOL];
        if (s >= CUST_SLOTS) break;
        if (s == ks_row) fw_gfx_text(">", 1, ROW(i + 1));
        fw_gfx_text(ka_pad_name[s], X_LABEL, ROW(i + 1));
        cust_get_settings(s, v);
        for (c = 0; c < KS_NCOL; c++) {
            if (KS_PAGE(c) != pg) continue;
            if (s == ks_row && c == ks_col) fw_gfx_text(">", ks_x[c] - 6, ROW(i + 1));
            fw_gfx_text(ks_value(c, v[c]), ks_x[c], ROW(i + 1));
        }
    }
    if (ks_status) fw_gfx_text(ks_status, 1, ROW(ROWS));
    fw_gfx_unlock();
}

static void ks_change(int d)
{
    int v[KS_NCOL];
    cust_get_settings(ks_row, v);
    v[ks_col] += d;
    if (v[ks_col] < cust_set_min[ks_col]) v[ks_col] = cust_set_min[ks_col];
    if (v[ks_col] > cust_set_max[ks_col]) v[ks_col] = cust_set_max[ks_col];
    cust_set_settings(ks_row, v);
}

static int ks_save(void)
{
    if (fw_project_protect) { ks_status = "Project Protect"; return -1; }
    if (cust_save_settings()) { ks_status = "Write Error"; return -1; }
    ks_status = "Saved!";
    return 0;
}

/* runs inside the editor's screen (already entered) */
void ks_settings(void)
{
    int dirty = 0;
    ks_status = 0;
    if (ks_row < 0 || ks_row >= CUST_SLOTS) ks_row = 0;
    if (ks_col < 0 || ks_col >= KS_NCOL) ks_col = 0;
    for (;;) {
        int ev, a;
        if (ks_row < ks_top) ks_top = ks_row;
        if (ks_row >= ks_top + KS_PADROWS) ks_top = ks_row - KS_PADROWS + 1;
        ks_draw();
        ev = fw_wait_event(-1);
        a = R24_ARROW(ev);
        if (ev == R24_EV_EXIT) break;
        if (a == R24_EV_UP && ks_row > 0) ks_row--, ks_status = 0;
        else if (a == R24_EV_DOWN && ks_row < CUST_SLOTS - 1) ks_row++, ks_status = 0;
        else if (a == R24_EV_LEFT && ks_col > 0) ks_col--, ks_status = 0;
        else if (a == R24_EV_RIGHT && ks_col < KS_NCOL - 1) ks_col++, ks_status = 0;
        else if (ev == R24_EV_DIAL) {
            int d = fw_dial_delta();
            if (d) ks_change(d), dirty = 1, ks_status = 0;
        } else if (ev == R24_EV_ENTER) {
            if (!ks_save()) dirty = 0;
        }
    }
    if (dirty) ks_save();
}

void ke_editor(void)
{
    const void *saved = fw_screen;
    int i;

    for (i = 0; i < CUST_SLOTS; i++) ke_choice[i] = -1;
    ke_status = 0;
    if (ke_row < 0 || ke_row >= KE_ROWS) ke_row = 0;
    ke_list();

    fw_set_screen(R24_SCREEN_MENU);
    fw_ui_enter();
    fw_edit_reset();
    for (;;) {
        int ev, a;
        if (ke_row < ke_top) ke_top = ke_row;
        if (ke_row >= ke_top + ROWS) ke_top = ke_row - ROWS + 1;
        ke_draw();
        ev = fw_wait_event(-1);
        a = R24_ARROW(ev);
        if (ev == R24_EV_EXIT) break;
        if (a == R24_EV_UP && ke_row > 0) ke_row--, ke_status = 0;
        else if (a == R24_EV_DOWN && ke_row < KE_ROWS - 1) ke_row++, ke_status = 0;
        else if (a == R24_EV_RIGHT) ke_step(1);
        else if (a == R24_EV_LEFT) ke_step(-1);
        else if (ev == R24_EV_DIAL) ke_step(fw_dial_delta());
        else if (ev == R24_EV_ENTER && ke_row == 0) {
            ks_settings();
            ke_status = 0;
        }
        else if (ev == R24_EV_ENTER && ke_choice[PAD(ke_row)] >= 0) {
            if (fw_project_protect) ke_status = "Project Protect";
            else if (fw_busy_guard(9)) ke_status = 0;
            else {
                ke_status = ka_assign(fw_list_names[ke_choice[PAD(ke_row)]], PAD(ke_row));
                ke_choice[PAD(ke_row)] = -1;
            }
        }
    }
    if (fw_edit_active()) fw_edit_reset();
    fw_ui_leave();
    fw_set_screen(saved);
}

/* KIT SELECT ENTER: the original, then the editor if CUSTOM was chosen */
void ke_confirm(void)
{
    fw_kit_select_confirm();
    if (fw_cur_project >= 0 && fw_kit_index == r24_find_kit("CUSTOM"))
        ke_editor();
}
