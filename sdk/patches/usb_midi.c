/*
 * usb_midi.c — SYSTEM > MIDI SYNC (Off/Out/In): the R24 becomes a USB host
 * for a class-compliant USB-MIDI interface (e.g. a USB -> DIN5 cable, CH345
 * 1a86:752d). Out: it sends MIDI clock (24 PPQN) and Start/Continue/Stop,
 * locked to the recorder transport, on cable 0 (the first DIN OUT). In: it
 * follows Start/Continue/Stop arriving on the DIN IN (PLAY/STOP, recording
 * if REC is armed).
 * docs/22_usb_midi.md.
 *
 * Pieces (all RAM addresses, see the doc for the evidence):
 *
 *   setting    um_on (Off / Out / In), a variable of this patch: Off at every power-on (the
 *              user's choice, like SYNC REC), never saved. Changing it does
 *              not mark the system settings dirty, so no NOR write.
 *   menu       SYSTEM header 0x800D4AE4 (count 8, item array at +0x20):
 *              11 items: MIDI SYNC (Off/Out/In), MIDI OFFSET (0..50 ms: the
 *              clock leaves that much early, to cancel the receiver's
 *              latency) and MIDI CALIBRATE (measures it on a mono take of
 *              the synth recorded from 00:00: the silence before the first
 *              hit; sets MIDI OFFSET) last. Each is a list item modelled on
 *              BATTERY (0x800D4A24), with a change callback that does
 *              nothing (BATTERY's 0x80019B56 would mark the settings dirty).
 *   host mode  0x8005B066(2, 2) = USB port -> host, "Cstm" app: the mode
 *              SYNC REC Master switches to at run time with the project
 *              open. NOT the mass-storage host (1, 2): its app task unmounts
 *              and remounts the SD card (0x80000156 + 0x80000050 at
 *              0x800826C6) under the open project — the first device build
 *              did that, and every STOP afterwards ("Please Wait", files
 *              reopened) took seconds until power-off (2026-11-20).
 *              Our class driver is registered on top of it
 *              (0x800818E0); the host stack offers it every interface whose
 *              bInterfaceClass is 1 (audio); attach() keeps MIDIStreaming
 *              (subclass 3) ones.
 *   I/O        a task of our own: open the bulk OUT pipe (0x80081BAC) and
 *              send USB-MIDI event packets with the synchronous transfer
 *              0x800819D6. The host API blocks on the USBD task, so it is
 *              never called from attach/detach (they run in that task).
 *   timing     transport 0x81220A90: +4 bit 0 = playing, +8 = fs,
 *              +0x10 = position in MILLISECONDS (0x80059744: start +
 *              timer * 1000 / (64 * fs), the timer counting 64 * fs per
 *              second; the first device build took it for samples and its
 *              clock ran ~44x slow) (refreshed every OS tick by the
 *              firmware's cyclic handler 0x800597DA). Tempo = 0x801EB744
 *              (BPM x 10, what the bar/beat counter uses, 0x8002D4EC).
 *              clocks = ms * tempo * 24 / 600000, accumulated, so
 *              a tempo change mid-song bends the clock instead of jumping.
 *   start      at 0 -> Start (FA); elsewhere -> Song Position (F2, rounded
 *              up to the next 16th) + Continue (FB), clocks then resume from
 *              that 16th. A jump of the position while playing (locate)
 *              -> Stop and re-start from the new position. Stop -> FC.
 *   boot       detour at 0x80008288 (main init, after the bank copy that
 *              puts this code in RAM, and after the settings load):
 *              creates the task, which waits 3 s and then follows the
 *              setting.
 *
 * MIDI SYNC = In: the task reads the bulk IN pipe instead and turns Start /
 * Continue / Stop into the R24's PLAY / STOP keys (see "MIDI SYNC = In").
 *
 * SYNC REC (Master/Slave) uses the USB port itself: while it is not Off the
 * task leaves the port alone.
 */
#include "r24.h"

#define FW(addr, ret, ...) ((ret (*)(__VA_ARGS__))((addr) | 1))

/* firmware */
#define fw_usb_mode(kind, dir)   FW(0x8005B066, int, int, int)(kind, dir)
#define fw_usb_off()             FW(0x8005AFC6, int, void)()
#define fw_usbd_register(drv)    FW(0x800818E0, int, const void *)(drv)
#define fw_open_pipe(ifc, ep, dir, type, a, b, out) \
    FW(0x80081BAC, int, void *, int, int, int, int, int, void **)(ifc, ep, dir, type, a, b, out)
#define fw_xfer(pipe, buf, len, res, tmo, fl) \
    FW(0x800819D6, int, void *, void *, uint32_t, uint32_t *, uint32_t, uint32_t)(pipe, buf, len, res, tmo, fl)
#define fw_delay(n)              FW(0x800547B8, int, int)(n)
#define fw_task_create(desc)     FW(0x800545F8, int, void *)(desc)
#define fw_task_start(id)        FW(0x80054678, int, int)(id)

#define SETTINGS       ((volatile uint32_t *)0x804C6DD4)
#define SYNC_REC       SETTINGS[0x3C / 4]       /* 0 Off, 1 Master, 2 Slave */
#define PORT           ((volatile uint8_t *)0x801EC650)  /* [1] dir (0 off, 1 dev, 2 host), [2] kind */
#define USBD_RUNNING   (*(volatile uint32_t *)(0x801ED690 + 0x14))
#define XPORT          ((volatile uint32_t *)0x81220A90)
#define XPORT_PLAYING  (XPORT[1] & 1)
#define XPORT_POS      ((int32_t)XPORT[4])
#define UM_KIND        2                         /* host kind: Cstm (never 1 = MSC) */
#define TEMPO10        (*(volatile int32_t *)0x801EB744)

/* ── SYSTEM menu item ──────────────────────────────────────────────────── */
struct menu_item {                  /* 80 bytes, as SYSTEM > BATTERY (0x800D4A24) */
    const char *label;
    uint32_t type, count;
    const void *values;
    const void *callback;
    uint32_t w5, w6;
    volatile uint32_t *var;
    uint32_t w8, w9, max;
    const void *changed;
    const void *str[4];
    uint32_t w16, w17, w18, w19;
};

R24_CHECK(0x800D4AC4, "30480d8080480d80d0480d8020490d80cc490d80"
                      "244a0d8070490d80744a0d80");           /* the 8 items */
R24_CHECK(0x800D4A24, "f99c1e8000000000020000001c4a0d80000000000000000001000000"
                      "0c6e4c80000000000000000001000000579b0180");  /* BATTERY */

static const char *const um_values[3] = { "Off", "Out", "In" };

/* 0 Off, 1 Out (the R24 is the clock master), 2 In (the R24 follows
 * Start/Continue/Stop from the cable's DIN IN). Off at power-on, not saved. */
volatile uint32_t um_on;
#define UM_OUT 1
#define UM_IN  2
volatile uint32_t um_offset;        /* 0..50 ms: clocks leave this early; 0 at power-on */
/* Timing: the hardware timer, waiting for each clock's exact moment (was
 * the option MIDI TIMING = EXACT). The other option, following the whole-ms
 * position the firmware publishes, measured worse on the device
 * (2026-11-20, STE-001/002.WAV, L synth / R the R24's pattern: L-R 18.7 vs
 * 16.5 ms, std 0.94 vs 0.33 ms) and was removed. */

#define MS(n) #n " ms"
#define UM_OFFSET_MAX 50
static const char *const um_off_values[UM_OFFSET_MAX + 1] = {
    MS(0), MS(1), MS(2), MS(3), MS(4), MS(5), MS(6), MS(7), MS(8), MS(9),
    MS(10), MS(11), MS(12), MS(13), MS(14), MS(15), MS(16), MS(17), MS(18),
    MS(19), MS(20), MS(21), MS(22), MS(23), MS(24), MS(25), MS(26), MS(27),
    MS(28), MS(29), MS(30), MS(31), MS(32), MS(33), MS(34), MS(35), MS(36),
    MS(37), MS(38), MS(39), MS(40), MS(41), MS(42), MS(43), MS(44), MS(45),
    MS(46), MS(47), MS(48), MS(49), MS(50)
};

__attribute__((used)) void um_changed(void)
{
}

struct menu_item um_item = {
    "MIDI SYNC", 0, 3, um_values, 0, 0, 1, &um_on, 0, 0, 2,
    (const void *)um_changed,
    { (const void *)0x801E9C81, (const void *)0x801E9C81,
      (const void *)0x801E9C81, (const void *)0x801E9C81 }, 0, 0, 0, 0
};

struct menu_item um_off_item = {
    "MIDI OFFSET", 0, UM_OFFSET_MAX + 1, um_off_values, 0, 0, 1, &um_offset, 0, 0, UM_OFFSET_MAX,
    (const void *)um_changed,
    { (const void *)0x801E9C81, (const void *)0x801E9C81,
      (const void *)0x801E9C81, (const void *)0x801E9C81 }, 0, 0, 0, 0
};

void um_calibrate(void);
struct menu_item um_cal_item = {                    /* action, as VERSION (0x800D4920) */
    "MIDI CALIBRATE", 1, 0, 0, (const void *)um_calibrate, 0, 1, 0, 0, 0, 0, 0,
    { (const void *)0x801E9C81, (const void *)0x801E9C81,
      (const void *)0x801E9C81, (const void *)0x801E9C81 }, 0, 0, 0, 0
};

const void *um_items[11] = {
    (const void *)0x800D4830, (const void *)0x800D4880, (const void *)0x800D48D0,
    (const void *)0x800D4920, (const void *)0x800D49CC, (const void *)0x800D4A24,
    (const void *)0x800D4970, (const void *)0x800D4A74,      /* LIGHT .. PHANTOM */
    &um_item, &um_off_item, &um_cal_item,
};
R24_BYTES(0x800D4AE4, "08000000", "0b000000");           /* item count 11 */
R24_POINTER(0x800D4B04, 0x800D4AC4, um_items);           /* item array */

/* ── class driver ──────────────────────────────────────────────────────── */
struct usbh_driver {                /* 16 bytes, as the MSC driver 0x801ED5F8 */
    uint16_t vid, pid;
    uint8_t dev_class, if_class, match, pad;
    int (*attach)(void *ifc);
    int (*detach)(void *ifc);
};
#define MATCH_IF_CLASS 0x08

/* interface object (0x8009360A): +0x10 class, +0x11 subclass, +0x12 protocol */
#define IFC_SUBCLASS(i) (((volatile uint8_t *)(i))[0x11])

volatile void *um_iface;            /* claimed MIDIStreaming interface */
volatile void *um_pipe;             /* its bulk OUT pipe */
volatile void *um_pipe_in;          /* its bulk IN pipe (MIDI SYNC = In) */
volatile uint32_t um_attaches, um_detaches;

__attribute__((used)) int um_attach(void *ifc)
{
    if (!ifc || IFC_SUBCLASS(ifc) != 3 || um_iface)
        return -1;                  /* AudioControl etc.: not ours */
    um_pipe = 0;
    um_pipe_in = 0;
    um_iface = ifc;
    um_attaches++;
    return 0;                       /* claimed */
}

__attribute__((used)) int um_detach(void *ifc)
{
    if (ifc == um_iface) {
        um_iface = 0;
        um_pipe = 0;                /* freed by the stack with the device */
        um_pipe_in = 0;
        um_detaches++;
    }
    return 0;
}

static const struct usbh_driver um_driver = {
    0, 0, 0, 1 /* audio */, MATCH_IF_CLASS, 0, um_attach, um_detach
};

/* ── output ────────────────────────────────────────────────────────────── */
static uint8_t um_buf[32] __attribute__((aligned(32)));    /* 8 packets */
static int um_n;
volatile uint32_t um_sent, um_errors, um_last_status;
volatile uint32_t um_xfer_ticks, um_xfer_max_ticks;    /* one transfer, timer ticks */
#define fw_timer() (((uint32_t (*)(void))0x80000C80)())

__attribute__((noinline, used)) void um_flush(void)
{
    uint32_t res[2] = { 0, 0 };
    void *pipe = (void *)um_pipe;
    int rc;

    if (!um_n)
        return;
    if (pipe && um_iface) {
        uint32_t t0;
        fw_dcache_clean(um_buf, sizeof um_buf);
        t0 = fw_timer();
        rc = fw_xfer(pipe, um_buf, (uint32_t)um_n, res, 20, 0);
        um_xfer_ticks = fw_timer() - t0;
        if (um_xfer_ticks > um_xfer_max_ticks)
            um_xfer_max_ticks = um_xfer_ticks;
        um_last_status = rc ? (uint32_t)rc : res[0];
        if (um_last_status) {
            if (++um_errors % 64 == 0)
                um_pipe = 0;        /* many failures: reopen */
        } else {
            um_sent += (uint32_t)um_n / 4;
        }
    }
    um_n = 0;
}

static void um_put(uint8_t cin, uint8_t b0, uint8_t b1, uint8_t b2)
{
    if (um_n == sizeof um_buf)
        um_flush();
    um_buf[um_n++] = cin;           /* cable 0 */
    um_buf[um_n++] = b0;
    um_buf[um_n++] = b1;
    um_buf[um_n++] = b2;
}

#define RT(b) um_put(0x0F, (b), 0, 0)          /* single-byte real time */

/* ── clock ─────────────────────────────────────────────────────────────── */
static int um_playing;
static int64_t um_acc;              /* tempo*24 units; a clock is due at >= 0 */

#define UM_UNIT 600000                          /* ms * tempo*10 * 24 per clock */
#define UM_ZERO_MS 10                           /* a play from here or earlier = from 00:00 */

/* The transport's start record (0x800599EE at PLAY, 0x800595F0 at a locate):
 * [0] position (ms) where it (re)started, [1] hardware timer at that moment,
 * [2] last timer value seen, [3] timer wraps since the start (both kept by
 * 0x80059744). The live position (+0x10) is already 1-2 ms on when the task
 * first sees the play bit, so it must not decide Start vs Continue (the first
 * device build sent Continue for a play from 00:00 that way). */
#define XSTART ((volatile uint32_t *)0x81220A80)

/* Time: the transport's own clock, the hardware timer read by 0x80000C80
 * (ARM; [[0x801EA9B8] + 0x10]), counting K = 64 * fs per second (0x80059744:
 * position ms = start + ticks * 1000 / (64 * fs)) — 0.35 us at 44.1 kHz,
 * locked to the audio. The position the firmware publishes (+0x10) is whole
 * ms, refreshed once per OS tick; reading the timer directly removes that
 * ~1 ms of age and rounding. Accumulator unit: one timer tick adds
 * tempo*24*1000, one clock is 600000 * K (= UM_UNIT ms scaled by K). */

/* ticks since the start record, read-only replica of 0x80059744 */
static int64_t um_elapsed(void)
{
    uint32_t w, last, now, w2;
    do {
        w = XSTART[3];
        last = XSTART[2];
        now = fw_timer();
        w2 = XSTART[3];
    } while (w != w2);
    if (last > now)                             /* wrapped since the firmware looked */
        w++;
    return (int64_t)(((uint64_t)w << 32) | now) - (int64_t)XSTART[1];
}

static uint32_t um_spos, um_stamp;
static int32_t um_off_applied;
static int64_t um_last_e;                       /* ticks since the start, last step */
static int64_t um_k;                            /* timer ticks per second */
static int32_t um_tempo;
volatile uint32_t um_late_us, um_late_max_us;   /* send time - exact time of a clock */

static void um_start(int32_t spos, int32_t tempo, int64_t k)
{
    int64_t unit = UM_UNIT;                     /* one clock, ms domain */
    int64_t phase = (int64_t)spos * tempo * 24;
    int64_t s16 = (phase + 6 * unit - 1) / (6 * unit);   /* next 16th */

    if (spos < UM_ZERO_MS) {
        RT(0xFA);                               /* Start: clock 0 at 00:00 */
        um_acc = phase * k;                     /* >= 0: first F8 now */
    } else {
        if (s16 > 0x3FFF)
            s16 = 0x3FFF;
        um_put(0x03, 0xF2, (uint8_t)(s16 & 0x7F), (uint8_t)((s16 >> 7) & 0x7F));
        RT(0xFB);                               /* Continue */
        um_acc = (phase - s16 * 6 * unit) * k;  /* <= 0: first F8 on that 16th */
    }
    um_off_applied = (int32_t)um_offset;        /* clocks leave this many ms early */
    um_acc += (int64_t)um_off_applied * tempo * 24 * k;
    um_last_e = 0;                              /* the start record's moment */
    um_spos = XSTART[0];
    um_stamp = XSTART[1];
    um_late_max_us = 0;
    um_playing = 1;
}

__attribute__((noinline, used)) void um_clock_step(void)
{
    int32_t tempo = TEMPO10, fs = (int32_t)XPORT[2], off;
    int64_t k, unit, e, d, inc;
    int n = 0;

    if (!XPORT_PLAYING || tempo <= 0 || fs <= 0) {
        if (um_playing) {
            RT(0xFC);                           /* Stop */
            um_playing = 0;
        }
        return;
    }
    k = (int64_t)64 * fs;
    if (!um_playing) {
        um_start((int32_t)XSTART[0], tempo, k);
    } else if (XSTART[0] != um_spos || XSTART[1] != um_stamp) {
        RT(0xFC);                               /* restarted / located (FF, REW...) */
        um_start((int32_t)XSTART[0], tempo, k);
    }
    um_k = k;
    um_tempo = tempo;

    e = um_elapsed();
    d = e - um_last_e;
    if (d < 0 || d > k)                         /* nonsense: resync time, keep phase */
        d = 0;
    um_last_e = e;
    unit = (int64_t)UM_UNIT * k;
    inc = (int64_t)tempo * 24 * 1000;           /* per timer tick */
    off = (int32_t)um_offset;                   /* changed while playing */
    if (off != um_off_applied) {
        um_acc += (int64_t)(off - um_off_applied) * tempo * 24 * k;
        um_off_applied = off;
    }
    um_acc += d * inc;
    while (um_acc >= 0 && n < 16) {
        um_late_us = (uint32_t)(um_acc / inc * 1000000 / k);
        if (um_late_us > um_late_max_us)
            um_late_max_us = um_late_us;
        RT(0xF8);
        um_acc -= unit;
        n++;
    }
    if (um_acc >= 0)                            /* stalled: drop, keep phase */
        um_acc -= ((um_acc / unit) + 1) * unit;
}

/* timer ticks until the next clock is due; -1 if none is pending */
__attribute__((noinline, used)) int32_t um_next_due(void)
{
    int64_t inc, t;

    if (!um_playing || um_k <= 0 || um_tempo <= 0 || um_acc >= 0)
        return -1;
    inc = (int64_t)um_tempo * 24 * 1000;
    t = (-um_acc + inc - 1) / inc - (um_elapsed() - um_last_e);
    return t < 0 ? 0 : t > 0x7FFFFFFF ? 0x7FFFFFFF : (int32_t)t;
}

/* ── MIDI CALIBRATE ────────────────────────────────────────────────────────
 * The user records the synth on a MONO track, from 00:00, with USB MIDI on.
 * The first clock leaves with the Start (an offset cannot move it earlier),
 * so the silence before the synth's first hit in the take IS the whole
 * latency: clock, USB, cable, synth, the R24's input. The new MIDI OFFSET is
 * that silence (not "current + rest"). Later hits, if any, give the jitter:
 * their spread around the 16th-note grid of the project tempo.
 * Onsets: 1 ms envelope (|x| summed over fs/1000 samples), threshold =
 * max/4, an onset = a rising crossing at least 60 ms after the previous one,
 * moved back to where the envelope was still below threshold/10 (at most
 * 10 ms) — as the analysis of STE-001.WAV (2026-11-20). */
#define CAL_MAXHITS 512
#define CAL_HIST 512                    /* envelope history: 11.6 ms at 44.1k */

static int32_t cal_ring[96], cal_hist[CAL_HIST], cal_on[CAL_MAXHITS];
static int32_t cal_sum, cal_max, cal_thr, cal_prev, cal_last_on;
static int cal_n;
static uint8_t cal_buf[3 * 1024];
static char cal_cur[264], cal_start[264], cal_full[264];
volatile int32_t um_cal_lag;            /* 1/100 ms: start of the take -> first hit */
volatile int32_t um_cal_jitter;         /* 1/100 ms, peak to peak of the later hits; -1 = none */
volatile int32_t um_cal_hits;

static uint32_t cal_le(const uint8_t *b, int n)
{
    uint32_t v = 0;
    while (n--) v = (v << 8) | b[n];
    return v;
}

/* open the WAV and stop at its data; -> file or 0 */
static fw_file *cal_open(const char *path, int *nch, int *bps, int *fs, uint32_t *len)
{
    fw_file *f = fw_fopen(path, "r");
    uint8_t h[12];
    int got_fmt = 0;

    if (!f)
        return 0;
    if (fw_fread(h, 1, 12, f) != 12 || h[0] != 'R' || h[8] != 'W')
        goto bad;
    for (;;) {
        uint32_t sz;
        if (fw_fread(h, 1, 8, f) != 8)
            goto bad;
        sz = cal_le(h + 4, 4);
        if (h[0] == 'f' && h[1] == 'm' && h[2] == 't' && sz >= 16 && sz <= sizeof cal_buf) {
            if (fw_fread(cal_buf, 1, sz, f) != sz)
                goto bad;
            if (cal_le(cal_buf, 2) != 1)            /* PCM only */
                goto bad;
            *nch = (int)cal_le(cal_buf + 2, 2);
            *fs = (int)cal_le(cal_buf + 4, 4);
            *bps = (int)cal_le(cal_buf + 14, 2) / 8;
            got_fmt = 1;
            if (sz & 1)
                fw_fread(cal_buf, 1, 1, f);
            continue;
        }
        if (h[0] == 'd' && h[1] == 'a' && h[2] == 't' && h[3] == 'a') {
            if (!got_fmt)
                goto bad;
            *len = sz;
            return f;
        }
        sz += sz & 1;                               /* skip the chunk (LIST: ~384 KiB) */
        while (sz) {
            uint32_t k = sz > sizeof cal_buf ? sizeof cal_buf : sz;
            if (fw_fread(cal_buf, 1, k, f) != k)
                goto bad;
            sz -= k;
        }
    }
bad:
    fw_fclose(f);
    return 0;
}

/* one pass: 0 = envelope maximum, 1 = onsets. -> fs, -1 file, -2 not mono */
static int cal_pass(const char *path, int pass)
{
    int nch = 0, bps = 0, fs = 0, w;
    uint32_t len, t = 0;
    fw_file *f = cal_open(path, &nch, &bps, &fs, &len);

    if (!f)
        return -1;
    if (nch != 1 || (bps != 2 && bps != 3) || fs < 8000 || fs > 96000) {
        fw_fclose(f);
        return -2;
    }
    w = fs / 1000;
    memset(cal_ring, 0, sizeof cal_ring);
    memset(cal_hist, 0, sizeof cal_hist);
    cal_sum = 0;
    cal_prev = 0;
    if (pass == 0) {
        cal_max = 0;
    } else {
        cal_thr = cal_max / 4;
        cal_n = 0;
        cal_last_on = -0x40000000;
    }
    while (len >= (uint32_t)bps) {
        uint32_t want = sizeof cal_buf / bps * bps, got, i;
        if (want > len)
            want = len / bps * bps;
        got = fw_fread(cal_buf, 1, want, f);
        if (got < (uint32_t)bps)
            break;
        len -= got;
        for (i = 0; i + bps <= got; i += bps, t++) {
            const uint8_t *b = cal_buf + i;
            int32_t x = bps == 2 ? (int16_t)(b[0] | b[1] << 8)
                                 : (int16_t)(b[1] | b[2] << 8);   /* top 16 bits */
            int32_t v = x < 0 ? -x : x, env;
            int slot = (int)(t % (uint32_t)w);
            cal_sum += v - cal_ring[slot];
            cal_ring[slot] = v;
            env = cal_sum;
            if (pass == 0) {
                if (env > cal_max)
                    cal_max = env;
                continue;
            }
            cal_hist[t % CAL_HIST] = env;
            if (env > cal_thr && cal_prev <= cal_thr &&
                (int32_t)t - cal_last_on > fs * 60 / 1000 && cal_n < CAL_MAXHITS) {
                int32_t j = 0, lim = fs / 100;          /* 10 ms back */
                if (lim > CAL_HIST - 1)
                    lim = CAL_HIST - 1;
                while (j < lim && j < (int32_t)t &&
                       cal_hist[(t - (uint32_t)j) % CAL_HIST] > cal_thr / 10)
                    j++;
                cal_on[cal_n++] = (int32_t)t - j;
                cal_last_on = (int32_t)t;
            }
            cal_prev = env;
        }
    }
    fw_fclose(f);
    return fs;
}

/* -> 0 ok (um_cal_lag, um_cal_jitter, um_cal_hits), -1 file, -2 not a mono
 *    16/24-bit WAV, -3 no hit. tempo10 = project tempo (BPM x 10) */
__attribute__((noinline, used)) int um_cal_analyze(const char *path, int tempo10)
{
    int fs, i;

    um_cal_hits = 0;
    um_cal_jitter = -1;
    fs = cal_pass(path, 0);
    if (fs < 0)
        return fs;
    if (cal_max <= 0)
        return -3;
    fs = cal_pass(path, 1);
    if (fs < 0)
        return fs;
    if (cal_n < 1)
        return -3;
    um_cal_lag = (int32_t)((int64_t)cal_on[0] * 100000 / fs);
    um_cal_hits = cal_n;
    if (cal_n >= 4 && tempo10 > 0) {
        /* later hits vs the 16th grid (150 * fs / tempo10 samples), in units of
         * 1/tempo10 sample so it stays integer; centred on the first residual */
        int64_t g = (int64_t)150 * fs, r0 = 0, lo = 0, hi = 0;
        for (i = 1; i < cal_n; i++) {
            int64_t r = ((int64_t)cal_on[i] * tempo10) % g;
            if (i == 1)
                r0 = r;
            r -= r0;                                /* relative to the 2nd hit */
            if (r > g / 2) r -= g;
            if (r < -g / 2) r += g;
            if (r < lo) lo = r;
            if (r > hi) hi = r;
        }
        um_cal_jitter = (int32_t)((hi - lo) * 100000 / tempo10 / fs);
    }
    return 0;
}

static void cal_num(char *o, int32_t v)             /* small non-negative int */
{
    char t[12];
    int n = 0, i = 0;
    do { t[n++] = (char)('0' + v % 10); v /= 10; } while (v && n < 11);
    while (n) o[i++] = t[--n];
    o[i] = 0;
}

static void cal_ms(char *o, int32_t cms)            /* 1/100 ms -> "4.97 ms" */
{
    char t[12];
    o[0] = 0;
    if (cms < 0) { r24_strcat(o, "-"); cms = -cms; }
    cal_num(t, cms / 100);
    r24_strcat(o, t);
    r24_strcat(o, ".");
    t[0] = (char)('0' + cms / 10 % 10);
    t[1] = (char)('0' + cms % 10);
    t[2] = 0;
    r24_strcat(o, t);
    r24_strcat(o, " ms");
}

/* the new offset (ms, 0..UM_OFFSET_MAX): the measured silence, rounded */
__attribute__((noinline, used)) int um_cal_offset(int32_t lag_cms)
{
    int32_t v = lag_cms < 0 ? 0 : (lag_cms + 50) / 100;
    return v > UM_OFFSET_MAX ? UM_OFFSET_MAX : (int)v;
}

#define CAL_X 0x3E
#define CAL_ROW(n) (0x0C + 9 * (n))

static int cal_screen(int32_t lag, int32_t jit, int cur, int nw)
{
    static char a[24], b[24];
    const void *saved = fw_screen;
    int yes = 0;

    fw_set_screen(R24_SCREEN_MENU);
    fw_ui_enter();
    fw_edit_reset();
    for (;;) {
        int ev;
        fw_gfx_lock();
        fw_gfx_clear(0);
        fw_gfx_title("MIDI CALIBRATE", 0x20);
        fw_gfx_text("FIRST HIT", 1, CAL_ROW(0));
        cal_ms(a, lag);
        fw_gfx_text(a, CAL_X, CAL_ROW(0));
        fw_gfx_text("JITTER", 1, CAL_ROW(1));
        if (jit < 0)
            fw_gfx_text("-", CAL_X, CAL_ROW(1));
        else {
            cal_ms(a, jit);
            fw_gfx_text(a, CAL_X, CAL_ROW(1));
        }
        fw_gfx_text("OFFSET", 1, CAL_ROW(2));
        cal_num(a, cur);
        r24_strcat(a, " > ");
        cal_num(b, nw);
        r24_strcat(a, b);
        r24_strcat(a, " ms");
        fw_gfx_text(a, CAL_X, CAL_ROW(2));
        fw_gfx_text("ENTER:SET EXIT:BACK", 1, CAL_ROW(3));
        fw_gfx_unlock();
        ev = fw_wait_event(-1);
        if (ev == R24_EV_EXIT) break;
        if (ev == R24_EV_ENTER) { yes = 1; break; }
    }
    if (fw_edit_active()) fw_edit_reset();
    fw_ui_leave();
    fw_set_screen(saved);
    return yes;
}

void um_calibrate(void)
{
    static char msg[24];
    int idx, rc, nw;

    if (fw_busy_guard(9))                           /* as the FILE operations */
        return;
    if (fw_cur_project < 0) { fw_ui_message("No Project"); return; }
    cal_start[0] = 0;
    r24_strcat(cal_start, "A:\\ZOOM_R24\\PROJECT\\");
    r24_strcat(cal_start, fw_cur_project_name);
    r24_strcat(cal_start, "\\AUDIO");
    memcpy(cal_cur, cal_start, sizeof cal_cur);
    cal_full[0] = 0;
    idx = fw_browse(cal_cur, cal_full, cal_start);
    if (idx == -2) { fw_ui_message("No File"); return; }
    if (idx < 0 || !cal_full[0]) return;

    fw_ui_progress("Calibrating...");
    rc = um_cal_analyze(cal_full, TEMPO10);
    fw_ui_progress_end(0);
    if (rc == -1) { fw_ui_message("File Access Error"); return; }
    if (rc == -2) { fw_ui_message("Need Mono WAV"); return; }
    if (rc) { fw_ui_message("No Sound"); return; }

    nw = um_cal_offset(um_cal_lag);
    if (!cal_screen(um_cal_lag, um_cal_jitter, (int)um_offset, nw))
        return;
    um_offset = (uint32_t)nw;
    msg[0] = 0;
    r24_strcat(msg, "MIDI OFFSET ");
    cal_num(msg + r24_strlen(msg), nw);
    r24_strcat(msg, " ms");
    fw_ui_message(msg);
}

/* ── transport start hook ─────────────────────────────────────────────────
 * 0x800599EE starts the transport (DSP start for the tracks, start record,
 * then +4 bit 0). A detour at its end (0x80059A3C) runs um_on_start in the
 * transport task (priority 0x34, above ours): Start + clock 0 leave at the
 * moment the transport starts, not up to a tick later when the task polls.
 * The clock state is shared with the task: um_busy, taken with IRQs off, says
 * who is in it; the side that finds it taken skips (the task) or leaves it
 * to the task (the hook, which sets um_kick). */
#define fw_irq_off()     (((uint32_t (*)(void))0x800BE75C)())     /* ARM: -> old CPSR */
#define fw_irq_restore(c) (((void (*)(uint32_t))0x800BE770)(c))
static volatile uint32_t um_busy;
volatile uint32_t um_kick, um_hook_starts;

static int um_take(void)
{
    uint32_t c = fw_irq_off();
    int got = !um_busy;
    if (got)
        um_busy = 1;
    fw_irq_restore(c);
    return got;
}

__attribute__((used)) void um_on_start(void)
{
    if (um_on != UM_OUT || SYNC_REC != 0 || !um_pipe || !um_iface)
        return;
    if (!um_take()) {
        um_kick = 1;                            /* the task will do it at once */
        return;
    }
    um_hook_starts++;
    um_clock_step();
    um_flush();
    um_busy = 0;
}

/* 0x80059A3C: orrs r0,r1 ; ldr r1,=0x81220A90 ; str r0,[r1,#4] ; movs r0,#0
 * (then pop {r4-r6,pc} at 0x80059A44) */
__attribute__((naked)) void um_det_start(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "orrs r0, r1\n"
        "ldr  r1, 1f\n"
        "str  r0, [r1, #4]\n"
        "ldr  r3, 2f\n"
        "blx  r3\n"
        "movs r0, #0\n"
        "ldr  r3, 3f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word 0x81220A90\n"
        "2: .word um_on_start\n"
        "3: .word 0x80059A45\n"
        ".syntax divided\n");
}
R24_REPLACE(0x80059A3C, "0843bf4948600020", um_det_start);

/* ── MIDI SYNC = In: follow Start / Continue / Stop ───────────────────────
 * The task reads the bulk IN pipe (the cable's DIN IN) and turns the
 * transport messages into the R24's own key presses, posted to the UI event
 * queue (0x80003C00 -> data queue [0x801EA9E0], the one fw_wait_event reads):
 * the main screen's handler (0x800093EA) does exactly what the keys do, so
 * PLAY in REC standby starts recording (REC 0x10034 -> 0x800203C2, PLAY
 * 0x10033 -> 0x8002042E, STOP 0x10032 -> 0x8001FA00).
 *   Start  FA  locate to MIDI OFFSET ms (normally 0), PLAY. If the R24 is
 *              playing, STOP first.
 *   Cont.  FB  PLAY from where it is, or from the last Song Position (F2,
 *              16ths at the project tempo) received while stopped.
 *   Stop   FC  STOP (only if playing).
 *   Clock  F8  the project tempo follows it (see "tempo from the incoming
 *              clock").
 * Locate = 0x8001F8C0(ms, 0), as the key handler's return to zero
 * (0x8000957E); it takes the locate mutex 0x801EB5B2 itself.
 * MIDI OFFSET in this mode: the R24 starts some ms after the Start arrives
 * (USB, the UI task, REC's own 16 ms pre-delay), so what the sequencer
 * played at its Start is never recorded and the take lands that much early.
 * Starting at OFFSET ms instead of 00:00 compensates: OFFSET = that latency
 * puts sequencer time t at R24 position t. */
#define fw_ui_post(ev)        FW(0x80003C00, int, int)(ev)
#define fw_locate(ms, f)      FW(0x8001F8C0, int, int, int)(ms, f)
#define EV_STOP 0x00010032
#define EV_PLAY 0x00010033

/* ── tempo from the incoming clock ────────────────────────────────────────
 * The project tempo (BPM x 10) is 0x804C44C0 + 0x20; the TEMPO edit of the
 * main screen (0x800093A8) stores it there and calls 0x80004FEA(t):
 * 0x8002B738 clamps to 400..2500 and sets the rhythm engine's 0x801EB744,
 * then 0x800205E8 sets the counter 0x801EB5A4 = 100. Nothing task-bound.
 * Measure: the hardware timer (64 * fs per second) at each F8, a window of
 * UM_TW clocks (4 beats). USB delivers within ~1 ms, so 4 beats (2 s at 120)
 * give about +-0.05 BPM; the measure is kept in BPM x 100 and a new tempo
 * is applied only when it differs from the project's by more than
 * UM_THYST (0.07 BPM) for UM_HOLD_STOP clocks (stopped) / UM_HOLD_PLAY
 * (playing: a tempo change mid-song can move the drum pattern's bar/beat).
 * Whole 0.1 BPM is all the R24 keeps: 120.04 on the sequencer stays 120.0
 * here and drifts ~0.7 ms per bar; the phase is not locked, only the tempo. */
#define PRJ_TEMPO     (*(volatile int32_t *)(0x804C44C0 + 0x20))
#define fw_set_tempo(t)       FW(0x80004FEA, int, int)(t)
#define UM_TW         96                        /* clocks in the window = 4 beats */
#define UM_THYST      7                         /* BPM x 100 */
#define UM_HOLD_STOP  24
#define UM_HOLD_PLAY  48

static uint32_t um_ck[UM_TW + 1];               /* timer at the last UM_TW + 1 clocks */
static int um_ckn, um_ckw;
static int32_t um_cand, um_hold;
volatile int32_t um_in_bpm100;                  /* measured, BPM x 100 (0 = not yet) */
volatile uint32_t um_in_clocks, um_in_tempo_sets;

static uint32_t um_k_now(void)
{
    uint32_t fs = XPORT[2];
    return 64 * (fs ? fs : 44100);
}

/* one F8 at timer value t */
__attribute__((noinline, used)) void um_in_clock(uint32_t t)
{
    uint32_t k = um_k_now(), span;
    int32_t m, cur, d, hold;

    um_in_clocks++;
    if (um_ckn && t - um_ck[(um_ckw + UM_TW) % (UM_TW + 1)] > k / 2)
        um_ckn = 0;                             /* > 0.5 s without clock: start over */
    um_ck[um_ckw] = t;
    um_ckw = (um_ckw + 1) % (UM_TW + 1);
    if (um_ckn < UM_TW + 1)
        um_ckn++;
    if (um_ckn < UM_TW + 1) {
        um_in_bpm100 = 0;
        um_hold = 0;
        return;
    }
    span = t - um_ck[um_ckw];                   /* the oldest: UM_TW clocks ago */
    if (!span)
        return;
    /* UM_TW clocks = UM_TW/24 beats: BPM = 60 * k * (UM_TW / 24) / span */
    m = (int32_t)(((uint64_t)6000 * k * (UM_TW / 24) + span / 2) / span);
    um_in_bpm100 = m;
    if (m < 4000 || m > 25000)
        return;                                 /* outside 40.0..250.0 */
    cur = PRJ_TEMPO;
    d = m - cur * 10;
    if (d <= UM_THYST && d >= -UM_THYST) {
        um_hold = 0;
        return;
    }
    m = (m + 5) / 10;                           /* BPM x 10 */
    if (m != um_cand) {
        um_cand = m;
        um_hold = 0;
    }
    hold = XPORT_PLAYING ? UM_HOLD_PLAY : UM_HOLD_STOP;
    if (++um_hold < hold)
        return;
    um_hold = 0;
    PRJ_TEMPO = m;
    fw_set_tempo(m);
    um_in_tempo_sets++;
}

static uint8_t um_inbuf[64] __attribute__((aligned(32)));
static int32_t um_spp = -1;                     /* Song Position (16ths) to continue from */
volatile uint32_t um_in_pkts, um_in_starts, um_in_conts, um_in_stops, um_in_errors;
volatile uint32_t um_in_last_status;

static void um_wait_stopped(void)
{
    int i;
    for (i = 0; i < 100 && XPORT_PLAYING; i++)  /* at most 1 s */
        fw_delay(10);
}

__attribute__((noinline, used)) void um_in_byte(uint8_t b, uint8_t d1, uint8_t d2)
{
    int32_t tempo = TEMPO10;

    switch (b) {
    case 0xFA:                                  /* Start */
        um_in_starts++;
        um_spp = -1;
        if (XPORT_PLAYING) {
            fw_ui_post(EV_STOP);
            um_wait_stopped();
        }
        fw_locate((int)um_offset, 0);
        fw_ui_post(EV_PLAY);
        break;
    case 0xFB:                                  /* Continue */
        um_in_conts++;
        if (XPORT_PLAYING)
            break;
        if (um_spp >= 0 && tempo > 0)           /* 16th = 150000 / tempo10 ms */
            fw_locate((int)((int64_t)um_spp * 150000 / tempo) + (int)um_offset, 0);
        um_spp = -1;
        fw_ui_post(EV_PLAY);
        break;
    case 0xFC:                                  /* Stop */
        um_in_stops++;
        if (XPORT_PLAYING)
            fw_ui_post(EV_STOP);
        break;
    case 0xF2:                                  /* Song Position */
        if (!XPORT_PLAYING)
            um_spp = (int32_t)(d1 & 0x7F) | (int32_t)(d2 & 0x7F) << 7;
        break;
    }
}

/* USB-MIDI event packets: [cable<<4 | CIN, b0, b1, b2]; t = timer at arrival */
__attribute__((noinline, used)) void um_in_parse(const uint8_t *p, int n, uint32_t t)
{
    for (; n >= 4; p += 4, n -= 4) {
        uint8_t cin = p[0] & 0x0F;
        if (!p[0] && !p[1])
            continue;                           /* padding */
        um_in_pkts++;
        if (cin == 0x0F && p[1] == 0xF8)
            um_in_clock(t);
        else if (cin == 0x0F || cin == 0x05 || cin == 0x03 || cin == 0x02)
            um_in_byte(p[1], p[2], p[3]);
    }
}

/* one read; blocks up to 100 ms (the host stack's transfer timeout) */
static void um_in_read(void)
{
    uint32_t res[2] = { 0, 0 };
    uint32_t t0 = fw_timer();
    int rc;

    fw_dcache_flush(um_inbuf, sizeof um_inbuf);
    rc = fw_xfer((void *)um_pipe_in, um_inbuf, sizeof um_inbuf, res, 100, 0x2000);
    um_in_last_status = rc ? (uint32_t)rc : res[0];
    if (!um_in_last_status && res[1]) {
        fw_dcache_inv(um_inbuf, sizeof um_inbuf);
        um_in_parse(um_inbuf, res[1] > sizeof um_inbuf ? (int)sizeof um_inbuf : (int)res[1],
                    fw_timer());
        return;
    }
    if (um_in_last_status)
        um_in_errors++;
    /* failed or empty and back at once (no timeout wait): never busy-loop,
     * the task would starve everything below priority 0x39. The timer runs
     * at 64 * fs (fs from the transport, 44.1 kHz until it is known). */
    {
        uint32_t k = um_k_now();
        if (fw_timer() - t0 < k / 200)          /* < 5 ms */
            fw_delay(um_in_last_status ? 20 : 1);
    }
}

/* ── the task ──────────────────────────────────────────────────────────── */
volatile uint32_t um_state;         /* for dumps: 0 off, 1 wait port, 2 host, 3 pipe */
volatile int32_t um_mode_rc = 1, um_reg_rc = 1, um_pipe_rc = 1;
static int um_own, um_registered, um_off_ticks;

static void um_reset_link(void)
{
    um_registered = 0;
    um_iface = 0;
    um_pipe = 0;
    um_pipe_in = 0;
}

/* The task wakes once per OS tick; a clock due before the next wake is
 * waited for here, on the timer, and sent on time (the firmware's own
 * position would only show it up to a tick later). At most 1.5 ms of busy
 * wait per clock (one every 20.8 ms at 120 BPM: ~7 % of the CPU at worst). */
volatile uint32_t um_wake_ticks;     /* a wake period (running estimate), timer ticks */
volatile uint32_t um_spins;

__attribute__((noinline, used)) void um_note_wake(uint32_t t)
{
    if (um_k <= 0 || t > (uint32_t)um_k)        /* unknown fs or a long stall */
        return;
    if (!um_wake_ticks || t > um_wake_ticks)
        um_wake_ticks = t;
    else
        um_wake_ticks -= (um_wake_ticks - t) / 16;
}

__attribute__((noinline, used)) void um_wait_and_send(void)
{
    int32_t due;
    uint32_t margin, limit, t0;
    int64_t target;

    if (!um_pipe || um_k <= 0)
        return;
    due = um_next_due();
    if (due < 0)
        return;
    margin = (uint32_t)(um_k / 4000);           /* 0.25 ms */
    limit = (um_wake_ticks ? um_wake_ticks : (uint32_t)(um_k / 1000)) + margin;
    if (limit > (uint32_t)(um_k * 3 / 2000))    /* never more than 1.5 ms of busy wait */
        limit = (uint32_t)(um_k * 3 / 2000);
    if ((uint32_t)due > limit)
        return;                                 /* the next wake is early enough */
    target = um_elapsed() + due;
    t0 = fw_timer();
    while (um_elapsed() < target) {
        if (!XPORT_PLAYING || XSTART[1] != um_stamp ||
            fw_timer() - t0 > limit + (uint32_t)(um_k / 1000))
            return;                             /* stopped / relocated / too long */
    }
    um_spins++;
    um_clock_step();
    um_flush();
}

__attribute__((noinline, used)) void um_task(void)
{
    uint32_t t0;

    fw_delay(3000);
    for (;;) {
        int want = um_on != 0 && SYNC_REC == 0;

        if (!want) {
            if (um_playing) {
                um_n = 0;
                um_playing = 0;
            }
            /* give the port back, unless SYNC REC now uses the same mode */
            if (um_own && SYNC_REC == 0 && PORT[1] == 2 && PORT[2] == UM_KIND)
                fw_usb_off();
            um_own = 0;
            um_reset_link();
            um_state = 0;
            fw_delay(50);
            continue;
        }

        if (PORT[1] == 0) {                     /* port off: take it after 1 s */
            um_own = 0;
            um_reset_link();
            um_state = 1;
            if (++um_off_ticks >= 10) {
                um_off_ticks = 0;
                um_mode_rc = fw_usb_mode(UM_KIND, 2);
                um_own = um_mode_rc == 0;
            }
            fw_delay(100);
            continue;
        }
        um_off_ticks = 0;
        if (!(PORT[1] == 2 && PORT[2] == UM_KIND) || !USBD_RUNNING) {
            um_reset_link();                    /* someone else's mode, or starting */
            um_state = 1;
            fw_delay(20);
            continue;
        }
        if (!um_registered) {
            um_reg_rc = fw_usbd_register(&um_driver);
            um_registered = 1;
        }
        um_state = 2;

        if (um_on == UM_IN) {
            if (um_playing) {                   /* switched from Out */
                um_n = 0;
                um_playing = 0;
            }
            if (um_iface && !um_pipe_in) {
                void *p = 0;
                um_pipe_rc = fw_open_pipe((void *)um_iface, 0, 0x80 /* IN */, 2 /* bulk */, 0, 0, &p);
                if (um_pipe_rc == 0 && p)
                    um_pipe_in = p;
                else
                    fw_delay(200);
            }
            if (um_pipe_in) {
                um_state = 3;
                um_in_read();                   /* blocks until data or 100 ms */
            } else {
                fw_delay(10);
            }
            continue;
        }

        if (um_iface && !um_pipe) {
            void *p = 0;
            um_pipe_rc = fw_open_pipe((void *)um_iface, 0, 0x00 /* OUT */, 2 /* bulk */, 0, 0, &p);
            if (um_pipe_rc == 0 && p)
                um_pipe = p;
            else
                fw_delay(200);
        }
        if (um_pipe)
            um_state = 3;

        if (um_take()) {
            um_kick = 0;
            um_clock_step();
            um_flush();
            um_wait_and_send();
            um_busy = 0;
        }
        if (um_kick)                            /* the start hook left it to us */
            continue;
        t0 = fw_timer();
        fw_delay(1);
        um_note_wake(fw_timer() - t0);
    }
}

/* ── boot ──────────────────────────────────────────────────────────────── */
struct task_desc {                  /* as the MSC app's (0x80082B08) */
    uint16_t a; uint16_t pad0;
    uint32_t b;
    void (*entry)(void);
    uint16_t prio; uint16_t pad1;
    uint32_t stack_size;
    void *stack;
};

static uint64_t um_stack[4096 / 8];
volatile int32_t um_task_id = -1;

__attribute__((used)) void um_boot(void)
{
    struct task_desc d;
    int id;

    memset(&d, 0, sizeof d);
    d.entry = um_task;
    d.prio = 0x39;                  /* just after the rhythm engine (0x38) */
    d.stack_size = sizeof um_stack;
    d.stack = um_stack;
    id = (int16_t)fw_task_create(&d);
    um_task_id = id;
    if (id >= 0)
        fw_task_start(id);
}

/* main init, AFTER the bank copy 0x8002D888 (bl at 0x8000827E: this code
 * lives in the bank region 0x87540000+, which is empty before it — a detour
 * earlier in the boot jumps into nothing; the first build did, 2026-11-20):
 *   0x80008288  bl 0x8006C868 (boot the DSP) ; movs r5,#0 ; b 0x800082A2  */
__attribute__((naked)) void um_det_boot(void)
{
    __asm__ volatile(
        ".syntax unified\n"
        "ldr  r3, 1f\n"
        "blx  r3\n"
        "ldr  r3, 2f\n"
        "blx  r3\n"
        "movs r5, #0\n"
        "ldr  r3, 3f\n"
        "bx   r3\n"
        ".align 2\n"
        "1: .word 0x8006C869\n"
        "2: .word um_boot\n"
        "3: .word 0x800082A3\n"
        ".syntax divided\n");
}
R24_REPLACE(0x80008288, "64f0eefa002508e0", um_det_boot);
