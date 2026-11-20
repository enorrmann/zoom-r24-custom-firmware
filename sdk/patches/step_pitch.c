/*
 * step_pitch.c — per-step drum pitch in PATTERN EDIT (docs/21_step_pitch.md).
 *
 * In PATTERN EDIT REC the 4th soft key (event 0x10038, unused there; A-Z
 * outside REC) toggles what the dial does on a pad row:
 *   VEL    stock: none -> soft (79) -> loud (127) and back;
 *   PITCH  the semitone of that note at the edit position, -24..+19, added to
 *          the pad's own PITCH (drum_pitch.c). Turning it on an empty step
 *          puts a soft note there. The note is heard at once.
 * The soft key shows "  VEL  ", " PITCH " or "PIT+5  " (pitch of the note
 * under the cursor).
 *
 * Storage: a pattern event is one 32-bit word (sequencer task 0x80049114):
 *   bits 0-7 tick, 8-13 note-12 (63 = marker), 14 flag, 15-17 velocity/16,
 *   18-31 unused by the firmware: kept on commit (0x80048E22 rewrites only
 *   bits 0-7), copied raw into the step cells, saved raw to the card. The
 *   pitch goes in bits 18-23 (signed 6 bit). Old patterns have 0 there.
 *
 * Playback: the three places that turn an event into a drum note-on (song
 * playback 0x800485EA, audition on cursor move 0x80048F5E, REC playback
 * 0x8004A0DC) are detoured: they now put the note into the drum-engine queue
 * (0x80538192, 4 bytes per note) with the pitch in a parallel ring indexed
 * like the queue. Every other writer of the queue goes through the replaced
 * 0x8002E566 and writes pitch 0. The engine's voice trigger 0x8002E084 reads
 * the ring entry of the note it is starting and adds it to pad byte 5 in the
 * copy of the pad it hands to the voice (byte 5 -> voice+5 -> drum_pitch.c).
 *
 * Needs drum_pitch.c (the voice reads byte 5); does not change it.
 */
#include "r24.h"

/* drum-engine queue (writer 0x8002E566, reader 0x8002E3F0) */
#define SP_Q        ((volatile uint8_t *)0x80538192u)
#define SP_QR       (*(volatile uint8_t *)0x801EB868u)
#define SP_QW       (*(volatile uint8_t *)0x801EB869u)
#define SP_QFLAG    (*(volatile int16_t *)0x801EB876u)
#define SP_QLOCK    (*(volatile int16_t *)0x801EB878u)
#define SP_QBIT     (1u << 22)
/* sequencer task queue (0x8004AC2C) */
#define SP_SEQ_MBOX (*(volatile int16_t *)0x801EBF6Eu)
#define SP_SEQ_LOCK (*(volatile int16_t *)0x801EBF70u)

#define SP_EV_KEY4  0x10038
#define SP_MIN      (-24)                   /* drum_pitch.c limits */
#define SP_MAX      19
#define SP_SHIFT    18
#define SP_MASK     (0x3Fu << SP_SHIFT)
#define SP_HAS      0x100u                  /* message word: pitch present */

int sp_mode;                                /* 0 VEL, 1 PITCH */
static int8_t sp_ring[256];                 /* pitch of the queue entry at index i */
static char sp_shown[8];                    /* soft key 4 as last drawn */

static int sp_differ(const char *a, const char *b, int n)
{
    while (n--)
        if (*a++ != *b++)
            return 1;
    return 0;
}

static int sp_clamp(int p)
{
    return p < SP_MIN ? SP_MIN : p > SP_MAX ? SP_MAX : p;
}

int sp_word_pitch(uint32_t w)
{
    return (int32_t)(w << (26 - SP_SHIFT)) >> 26;
}

static int sp_rec_state(void)
{
    return fw_pe_rec && !fw_pe_alldel;
}

/* ── the drum-engine queue ───────────────────────────────────────────── */
int sp_put(int type, int ch, int note, int vel, int pitch)
{
    int r = 0;
    uint8_t w;

    fw_mutex_lock(SP_QLOCK);
    w = SP_QW;
    if ((uint8_t)(w + 4) != SP_QR) {
        sp_ring[w] = (int8_t)pitch;
        SP_Q[w] = (uint8_t)type;
        SP_Q[(uint8_t)(w + 1)] = (uint8_t)ch;
        SP_Q[(uint8_t)(w + 2)] = (uint8_t)note;
        SP_Q[(uint8_t)(w + 3)] = (uint8_t)vel;
        SP_QW = (uint8_t)(w + 4);
        r = fw_flag_set(SP_QFLAG, SP_QBIT);
    }
    fw_mutex_unlock(SP_QLOCK);
    return r;
}

/* the firmware's own puts (0x8002E566, 4 arguments): pitch 0 */
__attribute__((used)) int sp_put_fw(int type, int ch, int note, int vel)
{
    return sp_put(type, ch, note, vel, 0);
}

/* 0x8002E566 starts with a jump that saves r3 (the 4th argument) first:
 * push {r3}; ldr r3, [pc, #4]; bx r3; nop; nop; .word sp_put_stub */
__attribute__((naked)) void sp_put_stub(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "pop  {r3}\n"
        "push {r4, lr}\n"
        "ldr  r4, 1f\n"
        "blx  r4\n"
        "pop  {r4, pc}\n"
        ".align 2\n"
        "1: .word sp_put_fw\n"
        ".syntax divided\n");
}
R24_BYTES(0x8002E566, "f8b504000d0016001f00", "08b4014b1847c046c046");
R24_POINTER(0x8002E570, 0x1C804862, sp_put_stub);

/* 0x8002E084 (ch, note, vel): pad of `note` in the current kit -> voice.
 * Same as the original, plus the queue entry's pitch on pad byte 5. Only
 * caller: 0x8002E0D4, called by the queue reader after it advanced QR by 4. */
int sp_trigger(int ch, int note, int vel);
R24_REPLACE(0x8002E084, "feb505000c001600", sp_trigger);
int sp_trigger(int ch, int note, int vel)
{
    uint8_t pad[8];
    int p, step;

    for (p = 0; p < 16; p++)
        if (fw_pad_notes[p] == note)
            break;
    if (p == 16)
        return 1;
    memcpy(pad, &r24_kit(fw_kit_index)->slot[p], 8);
    step = sp_ring[(uint8_t)(SP_QR - 4)];
    if (step)
        pad[5] = (uint8_t)sp_clamp((int8_t)pad[5] + step);
    fw_drum_voice_on(ch, note, vel, pad);
    return 0;
}

/* ── events -> notes: the three detours ──────────────────────────────── */
__attribute__((used)) void sp_play(int ch, int idx, int vel3, const uint32_t *st)
{
    sp_put(1, ch & 0xFF, (idx + 12) & 0xFF, ((vel3 << 4) + 15) & 0xFF,
           sp_word_pitch(st[6]));
}

/* song playback: r4 = slot, r5 = note-12, r7 = vel/16, [sp] -> +0x18 word */
__attribute__((naked)) void sp_det_song(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "adds r0, r4, #2\n"
        "movs r1, r5\n"
        "movs r2, r7\n"
        "ldr  r3, 1f\n"
        "mov  ip, r3\n"
        "ldr  r3, [sp]\n"
        "blx  ip\n"
        "ldr  r3, 2f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word sp_play\n"
        "2: .word 0x80048607\n"
        ".syntax divided\n");
}
R24_REPLACE(0x800485EC, "0f3003061b0e2800", sp_det_song);

/* audition on cursor move: ch 1, same registers */
__attribute__((naked)) void sp_det_audition(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "movs r0, #1\n"
        "movs r1, r5\n"
        "movs r2, r7\n"
        "ldr  r3, 1f\n"
        "mov  ip, r3\n"
        "ldr  r3, [sp]\n"
        "blx  ip\n"
        "ldr  r3, 2f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word sp_play\n"
        "2: .word 0x80048F77\n"
        ".syntax divided\n");
}
R24_REPLACE(0x80048F60, "0f3003061b0e2800", sp_det_audition);

/* PATTERN EDIT playback: ch 1, struct in r4 */
__attribute__((naked)) void sp_det_edit(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "movs r0, #1\n"
        "movs r1, r5\n"
        "movs r2, r7\n"
        "ldr  r3, 1f\n"
        "mov  ip, r3\n"
        "movs r3, r4\n"
        "blx  ip\n"
        "ldr  r3, 2f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word sp_play\n"
        "2: .word 0x8004A0F5\n"
        ".syntax divided\n");
}
R24_REPLACE(0x8004A0E0, "03061b0e28000c30", sp_det_edit);

/* ── step cells: the sequencer task's "set note" message (type 8) ────── */
static int sp_send_cell(uint32_t word, int note, int vel)
{
    uint8_t m[8];
    int r;

    fw_mutex_lock(SP_SEQ_LOCK);
    m[0] = (uint8_t)word; m[1] = (uint8_t)(word >> 8);
    m[2] = (uint8_t)(word >> 16); m[3] = (uint8_t)(word >> 24);
    m[4] = (uint8_t)note; m[5] = (uint8_t)vel; m[6] = 0; m[7] = 8;
    r = fw_mbox_send(SP_SEQ_MBOX, m, 8);
    fw_seq_kick();
    fw_mutex_unlock(SP_SEQ_LOCK);
    return r;
}

/* 0x80049114 (word, note, vel): the type-8 handler. Stock builds the event
 * from (word & 1, note, vel); here bits 18-23 also come from the message
 * (word bit 8 set: pitch in bits 16-21) or are kept from the cell. */
void sp_cell_set(uint32_t word, int note, int vel);
R24_REPLACE(0x80049114, "f8b505000c001600", sp_cell_set);
void sp_cell_set(uint32_t word, int note, int vel)
{
    uint32_t w, keep;
    int i;

    if (!fw_seq_running)
        return;
    if (note == -1 && vel == 0) {           /* stock: clear every cell */
        for (i = 0; i < 63; i++)
            fw_seq_cells[i] = 0;
        fw_seq_cells_changed();
        return;
    }
    if (note < 12 || note >= 0x4B)
        return;
    i = note - 12;
    if (!vel) {
        fw_seq_cells[i] = 0;
        fw_seq_cells_changed();
        return;
    }
    keep = (word & SP_HAS) ? ((word >> 16) & 0x3Fu) << SP_SHIFT
                           : fw_seq_cells[i] & SP_MASK;
    w = (word & 1u) << 14 | (uint32_t)i << 8 | ((uint32_t)(vel >> 4) & 7u) << 15;
    if (!(w & (7u << 15)))
        w |= 1u << 15;
    w |= keep;
    if (fw_seq_nearly_full()) {
        fw_seq_full_flag = 1;
        fw_seq_full_msg();
        return;
    }
    fw_seq_cells[i] = w;
    fw_seq_cells_changed();
}

/* ── REC: the dial on a pad row in PITCH mode ────────────────────────── */
static int sp_row_note(void)
{
    int row = fw_pe_row, n;

    if (row < 1 || row > 16)
        return -1;
    n = fw_pad_notes[row - 1];
    return n >= 12 && n < 0x4B ? n : -1;
}

__attribute__((used)) void sp_pitch_dial(void)
{
    int d = fw_dial_delta(), note = sp_row_note(), vel, p;
    uint32_t w;

    if (!d || note < 0)
        return;
    w = fw_seq_cells[note - 12];
    vel = w ? (int)((w >> 15) & 7u) << 4 : 0x40;
    if (!vel)
        vel = 0x40;
    p = sp_clamp((w ? sp_word_pitch(w) : 0) + d);
    sp_send_cell(SP_HAS | ((uint32_t)p & 0x3Fu) << 16, note, vel);
    sp_put(1, 0, note, vel | 0x0F, p);      /* hear it (stock previews on ch 0) */
    fw_pe_redraw = 1;
}

/* 0x8004E68C: the dial with a pad row selected (r0 = row - 1, r1 = pad
 * note table, then 0x8004E694). PITCH: our handler, back to 0x8004E72A. */
__attribute__((naked)) void sp_det_dial(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "ldr  r3, 1f\n"
        "ldr  r3, [r3]\n"
        "cmp  r3, #0\n"
        "beq  5f\n"
        "ldr  r3, 2f\n"
        "blx  r3\n"
        "ldr  r3, 3f\n"
        "bx   r3\n"
        "5:\n"
        "ldr  r0, 6f\n"
        "ldr  r0, [r0]\n"
        "subs r0, #1\n"
        "ldr  r1, 7f\n"
        "ldr  r3, 4f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word sp_mode\n"
        "2: .word sp_pitch_dial\n"
        "3: .word 0x8004E72B\n"
        "4: .word 0x8004E695\n"
        "6: .word 0x801EC390\n"
        "7: .word 0x800E2D64\n"
        ".syntax divided\n");
}
R24_REPLACE(0x8004E68C, "d6480068401ed649", sp_det_dial);

/* ── REC: soft key 4 toggles VEL / PITCH ─────────────────────────────── */
__attribute__((used)) int sp_key(int ev)
{
    if (ev != SP_EV_KEY4 || !sp_rec_state())
        return 0;
    sp_mode = !sp_mode;
    fw_pe_redraw = 1;
    return 1;
}

/* 0x8004C06A (ev): PATTERN EDIT generic key handler, the REC loop's
 * default case. Our key first; else the original prologue (push {r4, lr};
 * movs r4, r0; bl 0x8004C026; ldr r0, =0x10005) and on at 0x8004C074. */
__attribute__((naked)) void sp_det_key(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "push {r0, lr}\n"
        "ldr  r3, 1f\n"
        "blx  r3\n"
        "movs r3, r0\n"
        "pop  {r0, r1}\n"
        "mov  lr, r1\n"
        "cmp  r3, #0\n"
        "beq  5f\n"
        "bx   lr\n"
        "5:\n"
        "push {r4, lr}\n"
        "movs r4, r0\n"
        "ldr  r3, 2f\n"
        "blx  r3\n"
        "ldr  r0, 3f\n"
        "ldr  r3, 4f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word sp_key\n"
        "2: .word fw_pe_key_pre\n"
        "3: .word 0x00010005\n"
        "4: .word 0x8004C075\n"
        ".syntax divided\n");
}
R24_REPLACE(0x8004C06A, "10b50400fff7daff3748", sp_det_key);

/* soft key 4 text, always 7 characters (the label is centred on its length) */
static void sp_label(char *s)
{
    int note, p, a;
    uint32_t w;

    memcpy(s, sp_mode ? " PITCH " : "  VEL  ", 8);
    if (!sp_mode || (note = sp_row_note()) < 0 || !(w = fw_seq_cells[note - 12]))
        return;
    p = sp_word_pitch(w);
    a = p < 0 ? -p : p;
    memcpy(s, "PIT    ", 8);
    s[3] = p > 0 ? '+' : p < 0 ? '-' : ' ';
    if (a >= 10) {
        s[4] = (char)('0' + a / 10);
        s[5] = (char)('0' + a % 10);
    } else {
        s[4] = (char)('0' + a);
    }
}

/* 0x8004E1B4: the REC loop's screen update (begin, draw, end). A changed
 * label forces a full redraw; after a full redraw, soft key 4 is ours. */
void sp_rec_screen(void);
R24_REPLACE(0x8004E1B4, "10b5e3f7f3fffff7", sp_rec_screen);
void sp_rec_screen(void)
{
    char s[8];
    int rec, full;

    fw_lcd_begin();
    rec = sp_rec_state();
    if (rec) {
        sp_label(s);
        if (sp_differ(s, sp_shown, 8)) {
            memcpy(sp_shown, s, 8);
            fw_pe_redraw = 1;
        }
    }
    full = fw_pe_redraw;
    fw_pe_rec_draw();
    if (rec && full)
        fw_softkey_label(3, sp_shown, 0);
    fw_lcd_end();
}
