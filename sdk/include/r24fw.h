/*
 * r24fw.h — the Zoom R24 v1.13 firmware, as seen from C.
 *
 * Every name here is bound to an absolute RAM address by sdk/firmware.sym
 * (the build turns that file into linker symbols and checks each one against
 * the image). Patches are compiled with -mlong-calls, so a call becomes
 * `ldr rX,=addr ; blx rX` and works from anywhere, in Thumb or ARM.
 *
 * Evidence for each entry is in firmware.sym; [INFERRED] ones are marked.
 * To add a function: find it (docs/06_assembly_workflow.md), add a line to
 * firmware.sym and a prototype here.
 */
#ifndef R24FW_H
#define R24FW_H

#include <stdint.h>
#include <stddef.h>

typedef struct fw_file fw_file;            /* opaque FILE */

/* ── files (paths are "A:\\..." = SD card, "B:\\..." = USB stick) ─────── */
fw_file *fw_fopen(const char *path, const char *mode);   /* "r" / "w", binary */
uint32_t fw_fread(void *buf, uint32_t size, uint32_t n, fw_file *f);
uint32_t fw_fwrite(const void *buf, uint32_t size, uint32_t n, fw_file *f);
int      fw_fclose(fw_file *f);
int      fw_feof(fw_file *f);
int      fw_remove(const char *path);
int      fw_mkdir(const char *path);       /* 0 ok; fw_errno() == 0x11: exists */
int      fw_make_dir(const char *path);    /* 0 if created or already there */
int      fw_errno(void);
int      fw_file_copy(const char *src, const char *dst);
void     fw_rescan_drive(int letter);      /* 'A' after adding files */

/* ── memory and strings ────────────────────────────────────────────────── */
void    *fw_malloc(uint32_t n);
void     fw_free(void *p);
int      fw_get_mpl(int pool, uint32_t size, void **blk); /* RTOS pool; blocks if no room */
int      fw_sprintf(char *buf, const char *fmt, ...);
char    *fw_strcpy(char *d, const char *s);
int      fw_strcmp(const char *a, const char *b);
int      fw_strncmp(const char *a, const char *b, uint32_t n);
void    *fw_memcpy(void *d, const void *s, uint32_t n);
void     fw_memset_aeabi(void *d, uint32_t n, int c);  /* note the order */
void     fw_memclr(void *d, uint32_t n);

/* ── caches (ARM926: write-back D-cache; the DSP reads RAM directly) ──── */
void     fw_dcache_clean(void *a, uint32_t n);   /* before the DSP reads a buffer */
void     fw_dcache_inv(void *a, uint32_t n);     /* before the ARM reads DSP output */
void     fw_dcache_flush(void *a, uint32_t n);   /* clean + invalidate */
void     fw_dcache_sync(void *a, uint32_t n);    /* after writing code: D clean, I inv */

/* ── graphic screens (FILE menu and its dialogs: 128-px wide, 6-px font) ── */
/* Pattern of FILE COPY (0x8003E07C): save fw_screen, fw_set_screen(R24_SCREEN_MENU),
 * fw_ui_enter(), fw_edit_reset(); loop { lock, clear, title, text..., unlock,
 * wait_event }; exit: if (fw_edit_active()) fw_edit_reset(); fw_ui_leave();
 * fw_set_screen(saved). Messages: fw_ui_message(). */
void     fw_gfx_lock(void);
void     fw_gfx_unlock(void);
void     fw_gfx_clear(int mode);
void     fw_gfx_title(const char *text, int arg);    /* COPY passes 0x20 */
void     fw_gfx_text(const char *text, int x, int y);
void     fw_set_screen(const void *handler);
extern const void *volatile fw_screen;
void     fw_ui_enter(void);
void     fw_ui_leave(void);
void     fw_edit_reset(void);
int      fw_edit_active(void);
void     fw_ui_message(const char *text);
void     fw_ui_progress(const char *text);
void     fw_ui_progress_end(int zero);
#define R24_SCREEN_MENU ((const void *)0x8003BC41)

/* ── system / UI (text-mode screens; the FILE menu is graphic, see above) ─ */
void     fw_sleep_ms(int ms);                    /* [INFERRED] */
/* The LCD has two text lines. Modal dialogs are a loop on fw_wait_event(). */
void     fw_lcd_text(const char *text, int line, int zero);  /* line 0 or 1 */
void     fw_lcd_title(const char *text);         /* line 0 */
void     fw_message(const char *text);           /* line 1 */
int      fw_wait_event(int ms);                  /* -1 = forever; 0 on timeout */
int      fw_dial_delta(void);                    /* steps turned since last call */
int      fw_confirm(const char *line0, const char *line1);   /* 1 ENTER, 0 EXIT */
void     fw_wait_ms(int ms);                     /* a key press ends it early */
void     fw_busy(int on);                        /* [INFERRED] */
#define R24_EV_EXIT   0x00010019
#define R24_EV_ENTER  0x00010024
#define R24_EV_DIAL   0x10000000                 /* then read fw_dial_delta() */
/* cursor keys: 0x1002x = press, 0x2002x = held (auto-repeat), as the menu
 * engine treats them (0x8003B740). UP/DOWN move between rows (FILE COPY),
 * RIGHT/LEFT = +1/-1 (date editor 0x8003995E). [STRONG] */
#define R24_EV_UP     0x00010020
#define R24_EV_DOWN   0x00010021
#define R24_EV_RIGHT  0x00010022
#define R24_EV_LEFT   0x00010023
#define R24_ARROW(ev) ((((ev) >> 16) == 1 || ((ev) >> 16) == 2) && \
                       ((ev) & 0xFFFF) >= 0x20 && ((ev) & 0xFFFF) <= 0x23 \
                       ? (int)(0x10000 | ((ev) & 0xFFFF)) : 0)   /* -> R24_EV_UP.. */

/* ── FILE SELECT (the FILE menu: INFO / COPY / DELETE / RENAME / DIVIDE) ─ */
extern char fw_selected_file[];          /* name of the chosen file in
                                            A:\ZOOM_R24\PROJECT\<prj>\AUDIO */
extern volatile int fw_project_protect;  /* nonzero: project protected */
int      fw_busy_guard(int op);          /* nonzero: not now (FILE ops use 9) */

/* ── the file list of the other file browser at 0x80011B9C ───────────── */
struct fw_list_entry {                   /* 28 bytes */
    const char *name;
    uint8_t pad[3];
    uint8_t channels;                    /* +7: 1 mono, 2 stereo */
    uint8_t rate_type;                   /* +8: 2 = 44.1 kHz, 3 = 48 kHz */
    uint8_t rest[19];
};
struct fw_list {
    const char *dir;                     /* +0 folder being listed */
    struct fw_list_entry *entries;       /* +4 */
    int count;                           /* +8 */
};
const char *fw_list_name(int i, struct fw_list *list);
const char *fw_list_dir(struct fw_list *list);
extern struct fw_list fw_file_list;
extern volatile int fw_file_menu_sel;    /* selected entry */

/* ── projects ──────────────────────────────────────────────────────────── */
const char *fw_project_name(int n);              /* "PRJ001"…, 0 if n < 0 */
int      fw_set_current_project(int n);          /* hooked by custom_kit.c! */
int      fw_save_sys(void);                      /* writes SYS\ZOOM.ZDT */
int      fw_open_project(int n);
extern volatile int fw_cur_project;
extern char fw_cur_project_name[];

/* ── drum machine ──────────────────────────────────────────────────────── */
struct r24_sample { uint32_t rel_start, length; };   /* bank-relative, bytes */
struct r24_slot {
    uint16_t sample_id;     /* index in the sample table */
    uint8_t  level;         /* 7..15 */
    uint8_t  pan;           /* 0..127, 64 = centre */
    uint8_t  group;         /* mute group, 0 = none */
    uint8_t  reserved[3];   /* [1] = voice-steal priority 0..2; [0], [2] unused (docs/08 §3.2.1) */
};
struct r24_kit { char name[16]; struct r24_slot slot[16]; };

void     fw_set_kit(int k);
int      fw_get_kit(void);
struct r24_kit *fw_kit_ptr(int k);
extern volatile int fw_kit_index;
/* the code reads both tables through these literal words; tools may move
 * the tables, so always go through them (r24.h has helpers) */
extern struct r24_sample *const fw_lit_sample_table;
extern struct r24_kit *const fw_lit_kit_table;
extern const uint32_t fw_kit_count;

#define R24_PCM_BANK   0x87540000u     /* RAM copy of the PCM bank */
#define R24_RATE       44100u          /* bank sample rate, mono 16-bit */

/* ── folder listing (the lister FILE SELECT uses) ─────────────────────── */
struct fw_dirlist {                      /* 0x1C bytes, as 0x800406C0 builds it */
    const char *dir;                     /* +0x00 folder to list */
    void *entries;                       /* +0x04 set by the lister */
    int count;                           /* +0x08 entries listed */
    int files;                           /* +0x0C */
    int w4, w5;
    int dirs_low;                        /* +0x18 */
};
int      fw_list_files(struct fw_dirlist *l, int flags, int zero);
extern char fw_list_names[][260];
extern uint8_t fw_project_settings[];    /* +0x38: nonzero = 48 kHz project */
void     fw_kit_select_confirm(void);
/* the LOOP[USB]/[SD] file browser (folders + WAVs, sub-folders work). cur:
 * start folder, updated while browsing; full: path of the chosen file. */
int      fw_browse(char *cur, char *full, const char *start);
/* inside fw_browse: list + menu (docs/10_loop_import.md §9). Record of entry i
 * (28 bytes at fw_browse_list.entries + 28*i): +0 name, +7 channels, +8 rate
 * type, +0x14 length, +0x18 2 = folder. */
int      fw_browse_select(uint32_t head_cb, const char *dir, int flags, int noback);
extern struct fw_dirlist fw_browse_list;
extern volatile int fw_browse_sel;
extern uint32_t fw_browse_menu[14];
int      fw_browse_event(int ev);
void     fw_browse_row(int i);
void     fw_browse_row_back(int i);
void     fw_browse_draw(void);
int      fw_menu_run(uint32_t *rec, int (*event)(int), void (*cb2)(void));
void     fw_list_info(int i, struct fw_dirlist *l);    /* opens the file: slow */

/* ── USB stick access, exactly as LOOP[USB] (0x800425BA) does it ───────── */
int      fw_sync_rec_guard(void);
void     fw_audio_mode(int mode);
void     fw_usb_wait_box(void);
void     fw_save_project_now(void);
extern volatile int fw_project_open_flag;
void     fw_close_audio_files(void);
int      fw_usb_host(int a, int b);              /* (1, 2) */
int      fw_usb_wait(void);
void     fw_usb_off(void);
void     fw_reload_tracks_a(void);
void     fw_reload_tracks_b(void);
extern volatile int fw_track_sel;
extern uint8_t fw_rec_settings[];                /* +0x34 */

/* ── files of the project and their takes ─────────────────────────────── */
extern int fw_file_take[];                       /* per list entry; -1 = unused */
void     fw_audio_sync(void);

/* ARM -> DSP mailbox (docs/19_drum_pitch.md): drum voice ch starts at
 * 0x11F07970 + ch * 0x40, word 0 = playback step (Q29). */
void     fw_dsp_write(volatile uint32_t *dst, const uint32_t *src, int nwords);
extern volatile int fw_audio_rate;               /* 44100 / 48000 */

/* ── RTOS, drum-engine queue, PATTERN EDIT (docs/21_step_pitch.md) ─────── */
void     fw_mutex_lock(int id);
void     fw_mutex_unlock(int id);
int      fw_flag_set(int id, uint32_t bits);
int      fw_mbox_send(int id, const void *msg, int len);
void     fw_seq_kick(void);
void     fw_drum_voice_on(int ch, int note, int vel, const uint8_t pad[8]);
int      fw_seq_nearly_full(void);
void     fw_seq_full_msg(void);
void     fw_seq_cells_changed(void);
void     fw_lcd_begin(void);
void     fw_lcd_end(void);
void     fw_pe_rec_draw(void);
void     fw_softkey_label(int idx, const char *text, int inverted);
void     fw_pe_key_pre(void);
extern volatile int fw_pe_redraw;
extern volatile int fw_pe_rec;
extern volatile int fw_pe_alldel;
extern volatile int fw_pe_row;
extern const uint8_t fw_pad_notes[16];
extern volatile uint32_t fw_seq_cells[63];
extern volatile uint8_t fw_seq_running;
extern volatile uint8_t fw_seq_full_flag;

/* ── DSP boot and NOR flash: REFERENCE ONLY (docs/17_dsp_and_dump.md) ─────
 * Declared so the names are documented and checked; a patch must never call
 * the flash ones: erasing 0x02000000..0x0207FFFF destroys the boot loader. */
void     fw_dsp_boot(void);                      /* boot-time only */
void     fw_dsp_load(void);                      /* boot-time only */
struct fw_dsp_section { const void *src; void *dst; uint32_t len; };
extern const struct fw_dsp_section fw_dsp_sections[14];
int      fw_flash_update(int kind, const char *path, int skip);  /* DANGEROUS */
int      fw_nor_erase_sector(uint32_t addr);     /* DANGEROUS */
int      fw_nor_program(void);                   /* DANGEROUS, args not decoded */

#endif

