/*
 * pattern_track.c — a rhythm pattern on ONE track (as on the R8), not a pair.
 *
 * Stock v1.13 keeps one pattern per track PAIR: project word 0x804C4600 is a
 * 12-bit mask (bit k = tracks 2k/2k+1), 0x804C4604 an int[12] of pattern
 * numbers, and every user divides the track by two (`asrs rX,rT,#1`). Assigning
 * marks both tracks, forces STEREO LINK and the second track is dead: the
 * drums never pass through the tracks' DSP channels — the drum engine applies
 * the level/pan/sends/mute of the pair's FIRST track to its own voices
 * (0x8002E5F4.. setters: `if (track & 1) return; slot[track/2] = v`), and
 * mixes in stereo on its own bus. docs/16_pattern_track.md.
 *
 * Here:
 *   - the project mask becomes per TRACK (24 bits) and pattern[] an int[24]
 *     at 0x804C4604..0x804C4663. That range of the project block (saved in
 *     PRJDATA.ZDT) is unused by v1.13: no code reads +0x38..+0xC7 of
 *     0x804C45FC. Every `track>>1` used to index them becomes `track`.
 *   - the drum engine still has 12 slots: slot k serves the pair 2k/2k+1, and
 *     plays the pattern of whichever track of the pair has one (its "owner").
 *     Only one pattern per pair (assigning one removes the partner's).
 *   - the other track of the pair is a normal audio track; the pair is
 *     unlinked when a pattern is assigned and STEREO LINK shows '-' while
 *     either track has a pattern.
 *   - old projects (pair format) are converted the first time the project's
 *     patterns are sent to the engine (bit k -> bit 2k), marked by
 *     PT_MAGIC in the unused word 0x804C4664.
 */
#include "r24.h"

#define PS      ((volatile uint32_t *)0x804C45FC)   /* sampler block of the project */
#define PMASK   (PS[1])                              /* bit t: track t has a pattern */
#define PAT     (&PS[2])                             /* int[24] pattern number per track */
#define PMAGIC  (PS[26])                             /* 0x804C4664, unused in v1.13 */
#define PT_MAGIC 0x31545450u                         /* "PTT1" */
#define ENGMASK (*(volatile uint32_t *)0x801EBECC)   /* mask the engine/mixer read */
#define REC(t)  ((volatile uint32_t *)(0x804C46C4 + (t) * 0xD8))   /* track records */
#define PRJ     ((volatile uint32_t *)0x804C1E18)
#define LINKMASK (*(volatile uint32_t *)(0x804C2478 + 0x30))       /* bit k: pair k linked */

/* firmware (Thumb, RAM addresses) */
#define FW(addr, ret, ...) ((ret (*)(__VA_ARGS__))((addr) | 1))
#define fw_post(id, msg)        FW(0x800534B8, void, int, uint32_t)(id, msg)
#define fw_pat_info(p, buf)     FW(0x8004AB28, int, int, void *)(p, buf)
#define fw_set_rec(m)           FW(0x800204A8, void, uint32_t)(m)
#define fw_set_play(m)          FW(0x800204AE, void, uint32_t)(m)
#define fw_rec_ui1(a, b, c, d)  FW(0x80019E6A, void, uint32_t, uint32_t, uint32_t, uint32_t)(a, b, c, d)
#define fw_rec_ui2(a, b, c, d)  FW(0x80019EBA, void, uint32_t, uint32_t, uint32_t, uint32_t)(a, b, c, d)
#define fw_rec_done(z)          FW(0x8001F170, void, int)(z)
#define fw_link(t, on)          FW(0x80004A90, void, int, int)(t, on)
#define fw_trk_len(t, n)        FW(0x8002D01A, void, int, uint32_t)(t, n)
#define fw_trk_f8(t, v)         FW(0x8002D0F0, void, int, int)(t, v)
#define fw_trk_stereo(t, v)     FW(0x8002D0FC, void, int, int)(t, v)
#define fw_trk_stereo2(t, v)    FW(0x80032140, void, int, int)(t, v)
#define fw_trk_stop(t)          FW(0x8002066C, void, int)(t)
#define fw_trk_e0(t, v)         FW(0x80032148, void, int, int)(t, v)

/* ---- owner of a slot ----------------------------------------------------- */
static int pt_owner(int k)
{
    uint32_t m = PMASK;
    if (PMAGIC != PT_MAGIC)                 /* not converted yet: stock rule */
        return 2 * k;
    if (!(m >> (2 * k) & 1) && (m >> (2 * k + 1) & 1))
        return 2 * k + 1;
    return 2 * k;
}

static void pt_migrate(void)
{
    if (PMAGIC == PT_MAGIC)
        return;
    uint32_t old = PMASK, m = 0;
    for (int k = 11; k >= 0; k--) {         /* pair k -> track 2k */
        uint32_t p = PAT[k];
        PAT[2 * k + 1] = 0;
        PAT[2 * k] = p;
        if (old >> k & 1)
            m |= 1u << (2 * k);
    }
    PMASK = m;
    PMAGIC = PT_MAGIC;
    ENGMASK = m;
}

/* 0x800488B6: send slot's pattern to the drum engine. Callers now pass a
 * TRACK; the pattern argument is ignored and read from the owner. */
void pt_engine_set(int t, int pat_ignored);
R24_REPLACE(0x800488B6, "70b504000d002006c00b", pt_engine_set);
void pt_engine_set(int t, int pat_ignored)
{
    (void)pat_ignored;
    pt_migrate();
    if (t < 0 || t > 23)
        return;
    int k = t >> 1;
    int own = pt_owner(k);
    if (!(PMASK >> own & 1) && (PMASK >> t & 1))
        own = t;
    uint32_t msg = 0x15000000u | ((uint32_t)(k & 0xFF) << 9) | (PAT[own] & 0x1FF);
    fw_post(*(volatile int16_t *)0x801EB820, msg);
}

/* 0x8002E5F4.. : track mixer value -> drum slot, only from the owner track */
#define SETTER(name, addr, arr)                                          \
    void name(int t, int v);                                             \
    R24_REPLACE(addr, "10b5c207d20f06d1", name);                         \
    void name(int t, int v)                                              \
    {                                                                    \
        if (t >= 0 && t < 24 && t == pt_owner(t >> 1))                   \
            ((volatile int *)(arr))[t >> 1] = v;                         \
    }
SETTER(pt_set_level, 0x8002E5F4, 0x805382C4)
SETTER(pt_set_pan,   0x8002E60C, 0x80538294)
SETTER(pt_set_send1, 0x8002E624, 0x805382F4)
SETTER(pt_set_send2, 0x8002E63C, 0x80538324)
SETTER(pt_set_on,    0x8002E654, 0x80538354)

/* 0x8004B6A6: assign pattern `pat` to track t (TAKE SELECT > PATTERN,
 * RHYTHM > ASSIGN, and every refresh after a pattern edit). Stock code did
 * all of this for both tracks of the pair and linked them. */
void pt_assign(int t, int pat);
R24_REPLACE(0x8004B6A6, "f0b585b004000d006946", pt_assign);
void pt_assign(int t, int pat)
{
    uint8_t info[0x14] __attribute__((aligned(4)));
    if (t < 0 || t > 23)
        return;
    pt_migrate();
    if (fw_pat_info(pat, info) != 0) {      /* as stock: default 4 x 2 */
        info[9] = 2;
        info[8] = 4;
    }
    uint32_t len = (uint32_t)info[9] * info[8] * 0x30;
    uint32_t bit = 1u << t;
    int k = t >> 1, p = t ^ 1;

    if (PRJ[8] & bit) {                     /* armed for recording -> play */
        PRJ[8] &= ~bit;
        PRJ[9] |= bit;
        fw_set_rec(PRJ[8]);
        fw_set_play(PRJ[9]);
        fw_rec_ui1(PRJ[9], PRJ[8], *(volatile uint32_t *)(0x804C67D8 + 0x30),
                   *(volatile uint32_t *)(0x804C44C0 + 8));
        fw_rec_ui2(PRJ[9], PRJ[8], *(volatile uint32_t *)(0x804C67D8 + 0x30),
                   *(volatile uint32_t *)(0x804C44C0 + 0xC));
        fw_rec_done(0);
    }
    if (PMASK & (1u << p)) {                /* one pattern per pair */
        PMASK &= ~(1u << p);
        REC(p)[4] = 0;                      /* +0x10: no data */
        REC(p)[1] = 0;
        fw_trk_len(p, 0);
        fw_trk_f8(p, 0);
    }
    REC(t)[4] = 1;                          /* +0x10 */
    REC(t)[1] = len;                        /* +0x04 */
    PMASK |= bit;
    PAT[t] = (uint32_t)pat;
    pt_engine_set(t, pat);
    if (LINKMASK >> k & 1)
        fw_link(2 * k, 0);                  /* the partner is a mono track */
    ENGMASK = PMASK;
    fw_trk_len(t, len);
    fw_trk_f8(t, 1);
    fw_trk_stereo(t, 0);
    fw_trk_stereo2(t, 0);
    *(volatile uint8_t *)(0x804C24B0 + t * 0x100) = 0;   /* file name */
    fw_trk_stop(t);
    fw_trk_e0(t, 0);
    /* the slot takes this track's mixer settings */
    pt_set_level(t, *(volatile int *)(0x804C1E48 + 4 * t));
    pt_set_pan(t, *(volatile int *)(0x804C1EA8 + 4 * t));
    pt_set_send1(t, *(volatile int *)(0x804C1F08 + 4 * t));
    pt_set_send2(t, *(volatile int *)(0x804C1F68 + 4 * t));
    pt_set_on(t, (int)(*(volatile uint32_t *)0x804C1E3C >> t & 1));
}

/* ---- in-place instruction changes (generated, each checked) ------------- */
/* -- per-track pattern mask / pattern[track] (was track>>1) -- */
R24_BYTES(0x8000429C, "4b10", "0b00");  /* asrs r3, r1, #1 -> movs r3, r1 */
R24_BYTES(0x80007876, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x80007886, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x80007896, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x800078AA, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x800078C2, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x800078D6, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x800078EC, "4010", "0000");  /* asrs r0, r0, #1 -> movs r0, r0 */
R24_BYTES(0x800078FA, "4010", "0000");  /* asrs r0, r0, #1 -> movs r0, r0 */
R24_BYTES(0x80007902, "5110", "1100");  /* asrs r1, r2, #1 -> movs r1, r2 */
R24_BYTES(0x8000790E, "5010", "1000");  /* asrs r0, r2, #1 -> movs r0, r2 */
R24_BYTES(0x8000791C, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x80007926, "5010", "1000");  /* asrs r0, r2, #1 -> movs r0, r2 */
R24_BYTES(0x8000792E, "5210", "1200");  /* asrs r2, r2, #1 -> movs r2, r2 */
R24_BYTES(0x80007948, "5010", "1000");  /* asrs r0, r2, #1 -> movs r0, r2 */
R24_BYTES(0x800132FC, "4210", "0200");  /* asrs r2, r0, #1 -> movs r2, r0 */
R24_BYTES(0x80013834, "4210", "0200");  /* asrs r2, r0, #1 -> movs r2, r0 */
R24_BYTES(0x80017144, "6210", "2200");  /* asrs r2, r4, #1 -> movs r2, r4 */
R24_BYTES(0x80018742, "6210", "2200");  /* asrs r2, r4, #1 -> movs r2, r4 */
R24_BYTES(0x80018802, "6210", "2200");  /* asrs r2, r4, #1 -> movs r2, r4 */
R24_BYTES(0x8001AF02, "6a10", "2a00");  /* asrs r2, r5, #1 -> movs r2, r5 */
R24_BYTES(0x8004050A, "4a10", "0a00");  /* asrs r2, r1, #1 -> movs r2, r1 */
R24_BYTES(0x80040ED6, "4010", "0000");  /* asrs r0, r0, #1 -> movs r0, r0 */
R24_BYTES(0x80040EEC, "4910", "0900");  /* asrs r1, r1, #1 -> movs r1, r1 */
R24_BYTES(0x80041F6E, "4a10", "0a00");  /* asrs r2, r1, #1 -> movs r2, r1 */
R24_BYTES(0x80042326, "4a10", "0a00");  /* asrs r2, r1, #1 -> movs r2, r1 */
R24_BYTES(0x80042A3A, "4a10", "0a00");  /* asrs r2, r1, #1 -> movs r2, r1 */
R24_BYTES(0x80042BB8, "4410", "0400");  /* asrs r4, r0, #1 -> movs r4, r0 */
R24_BYTES(0x80046C08, "4210", "0200");  /* asrs r2, r0, #1 -> movs r2, r0 */
R24_BYTES(0x80046C1C, "4010", "0000");  /* asrs r0, r0, #1 -> movs r0, r0 */
R24_BYTES(0x800417A6, "4210", "0200");  /* asrs r2, r0, #1 -> movs r2, r0 */
R24_BYTES(0x80047144, "4210", "0200");  /* asrs r2, r0, #1 -> movs r2, r0 */
R24_BYTES(0x800050BC, "4a10", "2200");  /* asrs r2, r1, #1 -> movs r2, r4 (file-missing path) */
R24_BYTES(0x800050E0, "0861", "c046");  /* str r0, [r1, #0x10] -> nop: do not mark the partner track */
/* -- mixer: engine mask is per track now -- */
R24_BYTES(0x8002985C, "6910", "2900");  /* asrs r1, r5, #1 -> movs r1, r5 */
R24_BYTES(0x8002A2C6, "6110", "2100");  /* asrs r1, r4, #1 -> movs r1, r4 */
R24_BYTES(0x8002A57C, "6110", "2100");  /* asrs r1, r4, #1 -> movs r1, r4 */
R24_BYTES(0x8002A888, "6110", "2100");  /* asrs r1, r4, #1 -> movs r1, r4 */
R24_BYTES(0x8002A8BE, "6110", "2100");  /* asrs r1, r4, #1 -> movs r1, r4 */
R24_BYTES(0x8002A9D4, "6110", "2100");  /* asrs r1, r4, #1 -> movs r1, r4 */
R24_BYTES(0x8002B48E, "4110", "0100");  /* asrs r1, r0, #1 -> movs r1, r0 */
/* -- engine loops over the 12 slots: slot k = tracks 2k|2k+1 -- */
R24_BYTES(0x8004830E, "0122", "0322");  /* movs r2, #1 -> movs r2, #3 */
R24_BYTES(0x80048322, "5200", "9200");  /* lsls r2, r2, #1 -> lsls r2, r2, #2 */
R24_BYTES(0x80048344, "0123", "0323");  /* movs r3, #1 -> movs r3, #3 */
R24_BYTES(0x8004835E, "5b00", "9b00");  /* lsls r3, r3, #1 -> lsls r3, r3, #2 */
R24_BYTES(0x80048350, "1d42", "1d40");  /* tst r5, r3 -> ands r5, r3 (owner bit of the pair) */
R24_BYTES(0x80048354, "2142", "2942");  /* tst r1, r4 -> tst r1, r5 (only the owner track) */
R24_BYTES(0x80048850, "0127", "0327");  /* movs r7, #1 -> movs r7, #3 */
R24_BYTES(0x8004886A, "7f00", "bf00");  /* lsls r7, r7, #1 -> lsls r7, r7, #2 */
/* -- record-arm exclusion: only the pattern track -- */
R24_BYTES(0x8001EFAA, "6800", "2800");  /* lsls r0, r5, #1 -> movs r0, r5 */
R24_BYTES(0x8001EFAC, "0321", "0121");  /* movs r1, #3 -> movs r1, #1 */
R24_BYTES(0x8001EFB6, "0c2d", "182d");  /* cmp r5, #0xc -> #0x18 */
/* -- mute (+0x34) pushes: all 24 tracks, not only even ones -- */
R24_BYTES(0x80019EAC, "8008", "4008");  /* lsrs r0, r0, #2 -> lsrs r0, r0, #1 */
R24_BYTES(0x80019EB0, "bf1c", "7f1c");  /* adds r7, r7, #2 -> adds r7, r7, #1 */
R24_BYTES(0x80019F24, "8008", "4008");  /* lsrs r0, r0, #2 -> lsrs r0, r0, #1 */
R24_BYTES(0x80019F28, "bf1c", "7f1c");  /* adds r7, r7, #2 -> adds r7, r7, #1 */
R24_BYTES(0x8001A1D6, "bf08", "7f08");  /* lsrs r7, r7, #2 -> lsrs r7, r7, #1 */
R24_BYTES(0x8001A1D8, "b61c", "761c");  /* adds r6, r6, #2 -> adds r6, r6, #1 */
/* -- project load + pattern refresh loops: 24 tracks, pass the track -- */
R24_BYTES(0x8000551C, "0c2d", "182d");  /* cmp r5, #0xc -> #0x18 */
R24_BYTES(0x8004BD8A, "6800", "2800");  /* lsls r0, r5, #1 -> movs r0, r5 */
R24_BYTES(0x8004BDA0, "0c2d", "182d");  /* cmp r5, #0xc -> #0x18 */
R24_BYTES(0x8004C6B2, "6000", "2000");  /* lsls r0, r4, #1 -> movs r0, r4 */
R24_BYTES(0x8004C6C8, "0c2c", "182c");  /* cmp r4, #0xc -> #0x18 */
R24_BYTES(0x8004C85C, "6000", "2000");  /* lsls r0, r4, #1 -> movs r0, r4 */
R24_BYTES(0x8004C872, "0c2c", "182c");  /* cmp r4, #0xc -> #0x18 */
R24_BYTES(0x8004CE92, "6000", "2000");  /* lsls r0, r4, #1 -> movs r0, r4 */
R24_BYTES(0x8004CEA8, "0c2c", "182c");  /* cmp r4, #0xc -> #0x18 */
R24_BYTES(0x8004E8B0, "6000", "2000");  /* lsls r0, r4, #1 -> movs r0, r4 */
R24_BYTES(0x8004E8C6, "0c2c", "182c");  /* cmp r4, #0xc -> #0x18 */
R24_BYTES(0x8004C7E4, "6000", "2000");  /* lsls r0, r4, #1 -> movs r0, r4 */
R24_BYTES(0x8004C7FA, "0c2c", "182c");  /* cmp r4, #0xc -> #0x18 */
R24_BYTES(0x8004CB86, "5000", "1000");  /* lsls r0, r2, #1 -> movs r0, r2 */
R24_BYTES(0x8004CBA4, "0c28", "1828");  /* cmp r0, #0xc -> #0x18 */
/* -- STEREO LINK shows '-' while EITHER track of the pair has a pattern -- */
R24_BYTES(0x8004185C, "0120", "d140");  /* movs r0, #1 -> lsrs r1, r2 */
R24_BYTES(0x8004185E, "9040", "d140");  /* lsls r0, r2 -> lsrs r1, r2  (mask >> 2k) */
R24_BYTES(0x80041860, "0142", "8907");  /* tst r1, r0 -> lsls r1, r1, #30 (Z = no pattern in the pair) */
/* -- RHYTHM > ASSIGN + track pad: the pressed track, not the pair -- */
R24_BYTES(0x8004F450, "4408", "0400");  /* lsrs r4, r0, #1 -> movs r4, r0 */
R24_BYTES(0x8004F452, "6400", "c046");  /* lsls r4, r4, #1 -> nop */
R24_BYTES(0x8004F6E0, "4e6f2e2530336415545241434b25642f25640000", "4e6f2e2530336415545241434b25640000000000");  /* "TRACK%d/%d" -> "TRACK%d" */
/* -- TAKE SELECT > PATTERN: no forced stereo pair (pattern_assign does it per track) -- */
R24_BYTES(0x80048110, "bcf7befc", "c046c046");  /* bl #0x80004a90 -> nop */
R24_BYTES(0x8004812E, "e4f7e5ff", "c046c046");  /* bl #0x8002d0fc -> nop */
R24_BYTES(0x8004813C, "eaf700f8", "c046c046");  /* bl #0x80032140 -> nop */
R24_BYTES(0x8004814C, "e4f7d6ff", "c046c046");  /* bl #0x8002d0fc -> nop */
R24_BYTES(0x8004815C, "e9f7f0ff", "c046c046");  /* bl #0x80032140 -> nop */
