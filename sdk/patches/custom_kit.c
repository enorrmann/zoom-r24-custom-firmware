/*
 * custom_kit.c — the CUSTOM drum kit: samples loaded from the project folder.
 *
 * Whenever a project becomes current (power-on, PROJECT SELECT, NEW, ...)
 * the 16 slots of kit CUSTOM are loaded from
 *     A:\ZOOM_R24\PROJECT\<project>\KIT\SAMPLE01.WAV .. SAMPLE16.WAV
 * converted to mono 16-bit 44.1 kHz into `reserve` and, with -D CUSTOM_KIT_DDR=1,
 * free DDR (pool 3 + a fixed 3 MiB, regions_setup below). A missing or
 * unreadable file leaves the slot at its default (the kit's sound in the
 * image, a copy of BASIC). Details: docs/12_custom_kit.md.
 *
 * Build:  python3 tools/custom_kit.py install IMAGE -o OUT [--reserve-sec 6]
 *   or    python3 tools/r24sdk.py build IMAGE sdk/patches/custom_kit.c [other.c] -o OUT
 */
#include "r24.h"
#include "custom_kit.h"

/* reserve for the kit's audio: -D CUSTOM_KIT_RESERVE_SEC=n (whole seconds)
 * or -D CUSTOM_KIT_RESERVE_BYTES=n; default 6 s of mono 44.1 kHz */
#ifndef CUSTOM_KIT_RESERVE_BYTES
#ifndef CUSTOM_KIT_RESERVE_SEC
#define CUSTOM_KIT_RESERVE_SEC 6
#endif
#define CUSTOM_KIT_RESERVE_BYTES (CUSTOM_KIT_RESERVE_SEC * R24_RATE * 2)
#endif
#define RESERVE_BYTES ((uint32_t)(CUSTOM_KIT_RESERVE_BYTES) & ~31u)

/* -D CUSTOM_KIT_DDR=1: the audio also goes to DDR no one uses
 * (docs/18_dump_analysis.md §5.1, plan docs/20_kit_memory_plan.md):
 *   - RTOS pool 3 (1 MiB @0x815E1018, no caller ever passes id 3), taken with
 *     the kernel's own get_mpl so the RTOS knows it is in use;
 *   - 3 MiB @0x819126A8 (only reference: a start-up getter whose result is
 *     discarded) — unless -D CUSTOM_KIT_DDR_FIXED=0.
 *   - 19.9 MiB after the kernel block (0x81C128B0..0x82FFFFFF) and 5.25 MiB
 *     below the bank (0x87000000..0x8753FFFF): no reference in the code, and
 *     unchanged on the device between a power-on DDR dump and one after
 *     8-track recording (DUMP1/DUMP2, 2026-11-20) — unless
 *     -D CUSTOM_KIT_DDR_BIG=0.
 * With it, CUSTOM_KIT_RESERVE_BYTES may be 0: no bank room, nothing trimmed. */
#ifndef CUSTOM_KIT_DDR
#define CUSTOM_KIT_DDR 0
#endif
#ifndef CUSTOM_KIT_DDR_FIXED
#define CUSTOM_KIT_DDR_FIXED CUSTOM_KIT_DDR
#endif
#ifndef CUSTOM_KIT_DDR_BIG
#define CUSTOM_KIT_DDR_BIG CUSTOM_KIT_DDR
#endif
#define POOL3_ID    3
#define POOL3_ASK   0xF0000u        /* < 1 MiB: get_mpl would wait forever if it did not fit */
#define FIXED_BASE  0x819126C0u     /* 0x819126A8 rounded up to a cache line */
#define FIXED_END   0x81C12680u     /* 0x81C126A8 = kernel control block, keep clear */
#define BIG1_BASE   0x81C128C0u     /* after that block (ends 0x81C128B0) */
#define BIG1_END    0x83000000u     /* uncached window 0x83000000.. is in use */
#define BIG2_BASE   0x87000000u
#define BIG2_END    0x87540000u     /* the PCM bank */
#define NREG        5
#define SLOTS       16
#define OUT_RATE    R24_RATE
#define FADE_FRAMES 220u            /* 5 ms fade where a sample is cut short */
#define READ_CHUNK  0x10000u

int cust_hook(int n);

/* the kit, and the function that runs on every project change */
R24_KIT("CUSTOM");
/* set_current_project(n): push {r4-r6,lr}; [cur]=n; strcpy(name, project_name(n));
 * save_sys(); pop — docs/12_custom_kit.md §2.2 */
R24_REPLACE(0x8001D6A6, "70b50400e34804602000fff7affd0500", cust_hook);

/* errors reported in cust_status.result[] */
enum { E_NOFILE = 0, E_RIFF = -1, E_FMT = -2, E_NODATA = -3, E_SPACE = -4,
       E_EMPTY = -5, E_NOKIT = -6 };

/* diagnostics, readable in a RAM dump / the emulator (symbol cust_status) */
struct cust_status {
    uint32_t loads;                 /* times the hook ran */
    uint32_t used;                  /* reserve bytes in use */
    int16_t  result[SLOTS];         /* >0 loaded (frames/1024 + 1), 0 default, <0 error */
    int32_t  kit;                   /* CUSTOM kit index, -1 if absent */
    int32_t  pool_err;              /* get_mpl result for pool 3 (1 = not tried) */
    uint32_t nreg;                  /* memory regions in use, then per region: */
    uint32_t reg_base[NREG], reg_size[NREG], reg_used[NREG];
};
struct cust_status cust_status = { 0, 0, { 0 }, -1, 1, 0, { 0 }, { 0 }, { 0 } };

#if (CUSTOM_KIT_RESERVE_BYTES) >= 32
static uint8_t reserve[RESERVE_BYTES] __attribute__((aligned(32)));
#define HAVE_RESERVE 1
#else
#define HAVE_RESERVE 0
#endif

/* where the kit's audio may go; filled from the start of each load */
struct ck_region { uint8_t *base; uint32_t size, used; };
static struct ck_region reg[NREG];
static int nreg;
static struct r24_sample defaults[SLOTS];
static uint16_t ids[SLOTS];
static int ready;                   /* defaults captured */
static char path[192];

/* the kit's configuration, PROJECT\<prj>\KIT.TXT:
 *     # comment
 *     01=KICK.WAV          pad 1 plays KIT\KICK.WAV
 * A pad without a line plays KIT\SAMPLEnn.WAV if present (older kits). */
#define FILE_MAX 64
static char pad_file[SLOTS][FILE_MAX];   /* file in KIT\ of each pad, "" = none */
static char cfgbuf[2048];

/* per-pad settings, KIT.TXT lines "S01=level,pan,group[,pitch[,poly]]":
 * level 0..15, pan 0..126 (64 = centre), mute group 0..7, then the optional
 * values (written only up to the last one that is not 0; missing = 0):
 * pitch -24..+19 ("+5" / "-5"), poly 0..1. Older builds read the first three.
 * The order is CUST_LEVEL.. CUST_POLY (custom_kit.h). */
#define NSET CUST_NSET
#define NREQ 3                           /* values every S line must have */
const int8_t cust_set_min[CUST_NSET] = { 0, 0, 0, CUST_PITCH_MIN, 0 };
const int8_t cust_set_max[CUST_NSET] = { CUST_LEVEL_MAX, CUST_PAN_MAX, CUST_GROUP_MAX,
                                         CUST_PITCH_MAX, CUST_POLY_MAX };
static uint8_t set_has[SLOTS];
static int8_t set_val[SLOTS][NSET];
static int8_t def_val[SLOTS][NSET];      /* the kit's own values (image); poly 0 */

static uint32_t rd16(const uint8_t *p) { return p[0] | (uint32_t)p[1] << 8; }
static uint32_t rd32(const uint8_t *p) { return rd16(p) | rd16(p + 2) << 16; }

static int tag_is(const uint8_t *p, const char *t)
{
    return p[0] == (uint8_t)t[0] && p[1] == (uint8_t)t[1] &&
           p[2] == (uint8_t)t[2] && p[3] == (uint8_t)t[3];
}

/* one sample of channel data at p -> signed 16-bit */
static int32_t sample16(const uint8_t *p, uint32_t bits, int is_float)
{
    if (is_float) {                         /* IEEE-754 single, no FPU */
        uint32_t v = rd32(p);
        int32_t e = (int32_t)((v >> 23) & 0xFF) - 127;
        uint32_t m = (v & 0x7FFFFF) | 0x800000;
        int32_t r;
        /* x = m * 2^(e-23); x * 32768 = m * 2^(e-8) = m >> (8 - e) */
        if (e < -16) return 0;              /* |x| < 2^-16: below 1 LSB */
        if (e >= 0) r = 32767;              /* |x| >= 1.0: clip */
        else r = (int32_t)(m >> (uint32_t)(8 - e));
        if (r > 32767) r = 32767;
        return (v & 0x80000000u) ? -r : r;
    }
    switch (bits) {
    case 16: return (int16_t)rd16(p);
    case 24: return (int16_t)rd16(p + 1);
    case 32: return (int16_t)rd16(p + 2);
    default: return 0;
    }
}

/*
 * Convert the WAV file image in buf[0..n) to mono 16-bit OUT_RATE PCM at
 * buf[0..), in place. `cap` is how many bytes may be written at buf.
 * Returns the PCM length in bytes (even, >= 2) or an error (< 0).
 */
int32_t cust_wav_to_pcm(uint8_t *buf, uint32_t n, uint32_t cap)
{
    uint32_t off = 12, fmt = 0, data = 0, dlen = 0, truncated = 0;
    uint32_t tagfmt, ch, rate, align, bits, frames, out, i, c;
    int is_float;
    int16_t *o = (int16_t *)buf;

    if (n < 12 || !tag_is(buf, "RIFF") || !tag_is(buf + 8, "WAVE"))
        return E_RIFF;
    while (off + 8 <= n) {
        uint32_t len = rd32(buf + off + 4);
        if (tag_is(buf + off, "fmt ") && len >= 16 && off + 8 + 16 <= n)
            fmt = off + 8;
        else if (tag_is(buf + off, "data")) {
            data = off + 8;
            dlen = len;
            if (data + dlen > n || data + dlen < data) {
                dlen = n - data;            /* file cut short (or did not fit) */
                truncated = 1;
            }
            if (fmt) break;
        }
        if (len > n - off - 8) break;
        off += 8 + len + (len & 1);
    }
    if (!fmt) return E_FMT;
    if (!data) return E_NODATA;

    tagfmt = rd16(buf + fmt);
    ch = rd16(buf + fmt + 2);
    rate = rd32(buf + fmt + 4);
    align = rd16(buf + fmt + 12);
    bits = rd16(buf + fmt + 14);
    if (tagfmt == 0xFFFE && rd32(buf + fmt - 4) >= 40 && fmt + 26 <= n)
        tagfmt = rd16(buf + fmt + 24);      /* WAVE_FORMAT_EXTENSIBLE subformat */
    is_float = (tagfmt == 3);
    if (!(tagfmt == 1 || (is_float && bits == 32)))
        return E_FMT;
    if (!(bits == 16 || bits == 24 || bits == 32) || ch < 1 || ch > 8 ||
        align != ch * (bits / 8) || rate < 4000 || rate > 192000)
        return E_FMT;

    frames = dlen / align;
    if (frames < 1) return E_EMPTY;

    /* 1. fold to mono 16-bit at buf[0..): write 2*i < read data+i*align */
    for (i = 0; i < frames; i++) {
        const uint8_t *p = buf + data + i * align;
        int32_t s = 0;
        for (c = 0; c < ch; c++)
            s += sample16(p + c * (bits / 8), bits, is_float);
        if (ch > 1) s /= (int32_t)ch;
        o[i] = (int16_t)s;
    }
    if (frames * 2 > cap) { frames = cap / 2; truncated = 1; }

    /* 2. resample (linear, 16.16 fixed point), in place */
    out = frames;
    if (rate != OUT_RATE) {
        uint64_t want = (uint64_t)frames * OUT_RATE / rate;
        uint32_t step = (uint32_t)(((uint64_t)rate << 16) / OUT_RATE);
        if (want < 1) want = 1;
        if (want > cap / 2) { want = cap / 2; truncated = 1; }
        out = (uint32_t)want;
        if (rate > OUT_RATE) {              /* shrinking: forward is safe */
            uint64_t pos = 0;
            for (i = 0; i < out; i++, pos += step) {
                uint32_t k = (uint32_t)(pos >> 16), f = (uint32_t)pos & 0xFFFF;
                int32_t a = o[k], b = (k + 1 < frames) ? o[k + 1] : a;
                o[i] = (int16_t)(a + (((b - a) * (int32_t)f) >> 16));
            }
        } else {                            /* growing: backward is safe */
            for (i = out; i-- > 0;) {
                uint64_t pos = (uint64_t)i * step;
                uint32_t k = (uint32_t)(pos >> 16), f = (uint32_t)pos & 0xFFFF;
                int32_t a, b;
                if (k >= frames) k = frames - 1;
                a = o[k];
                b = (k + 1 < frames) ? o[k + 1] : a;
                o[i] = (int16_t)(a + (((b - a) * (int32_t)f) >> 16));
            }
        }
    }

    /* 3. fade a cut so it does not click */
    if (truncated && out > 4) {
        uint32_t f = out / 4 < FADE_FRAMES ? out / 4 : FADE_FRAMES;
        for (i = 0; i < f; i++) {
            uint32_t k = out - f + i;
            o[k] = (int16_t)((int32_t)o[k] * (int32_t)(f - 1 - i) / (int32_t)f);
        }
    }
    if (out < 1) return E_EMPTY;
    if (out == 1) o[1] = 0, out = 2;        /* playback needs length >= 2 bytes */
    return (int32_t)(out * 2);
}

static uint32_t read_all(fw_file *f, uint8_t *dst, uint32_t cap)
{
    uint32_t got = 0;
    while (got < cap) {
        uint32_t want = cap - got < READ_CHUNK ? cap - got : READ_CHUNK;
        uint32_t r = fw_fread(dst + got, 1, want, f);
        if (r > want) break;                /* error indication */
        got += r;
        if (r < want) break;                /* end of file */
    }
    return got;
}

/* "A:\ZOOM_R24\PROJECT\<prj>" + tail */
static void prj_path(char *out, const char *prj, const char *tail)
{
    out[0] = 0;
    r24_strcat(out, "A:\\ZOOM_R24\\PROJECT\\");
    if (r24_strlen(prj) < 64) r24_strcat(out, prj);
    r24_strcat(out, tail);
}

/* "A:\ZOOM_R24\PROJECT\<prj>\KIT\<file>" */
void cust_file_path(char *out, const char *prj, const char *file)
{
    prj_path(out, prj, "\\KIT\\");
    if (r24_strlen(file) < FILE_MAX) r24_strcat(out, file);
}

/* "A:\ZOOM_R24\PROJECT\<prj>\KIT\SAMPLEnn.WAV" */
void cust_make_path(char *out, const char *prj, int slot)
{
    char num[3] = { (char)('0' + (slot + 1) / 10), (char)('0' + (slot + 1) % 10), 0 };
    prj_path(out, prj, "\\KIT\\SAMPLE");
    r24_strcat(out, num);
    r24_strcat(out, ".WAV");
}

static void copy_name(char *d, const char *s, uint32_t n)
{
    uint32_t i = 0;
    while (i + 1 < n && s[i]) { d[i] = s[i]; i++; }
    d[i] = 0;
}

/* a plain file name: no folders, no drive */
static int plain_name(const char *s, uint32_t n)
{
    uint32_t i;
    if (!n || n >= FILE_MAX) return 0;
    for (i = 0; i < n; i++)
        if (s[i] == '\\' || s[i] == '/' || s[i] == ':' || (uint8_t)s[i] < 0x20) return 0;
    return 1;
}

/* parse KIT.TXT in cfgbuf[0..n) into pad_file[] */
void cust_parse_config(uint32_t n)
{
    uint32_t i = 0;
    int s;
    for (s = 0; s < SLOTS; s++) pad_file[s][0] = 0, set_has[s] = 0;
    while (i < n) {
        uint32_t a, b, e = i;
        int slot = 0, digits = 0, is_set = 0;
        while (e < n && cfgbuf[e] != '\n' && cfgbuf[e] != '\r') e++;
        a = i;
        while (a < e && (cfgbuf[a] == ' ' || cfgbuf[a] == '\t')) a++;
        if (a < e && (cfgbuf[a] == 'S' || cfgbuf[a] == 's')) is_set = 1, a++;
        while (a < e && cfgbuf[a] >= '0' && cfgbuf[a] <= '9' && digits < 3)
            slot = slot * 10 + (cfgbuf[a++] - '0'), digits++;
        while (a < e && (cfgbuf[a] == ' ' || cfgbuf[a] == '\t')) a++;
        if (is_set && digits && a < e && cfgbuf[a] == '=' && slot >= 1 && slot <= SLOTS) {
            int v[NSET], k, okv = 1;
            a++;
            for (k = 0; k < NSET && okv; k++) {
                int d = 0, neg = 0;
                v[k] = 0;
                if (k) {                        /* separator; optional values may be absent */
                    while (a < e && (cfgbuf[a] == ' ' || cfgbuf[a] == '\t')) a++;
                    if (a < e && cfgbuf[a] == ',') a++;
                    else { if (k < NREQ) okv = 0; break; }
                }
                while (a < e && (cfgbuf[a] == ' ' || cfgbuf[a] == '\t')) a++;
                if (k >= NREQ && a < e && (cfgbuf[a] == '-' || cfgbuf[a] == '+'))
                    neg = cfgbuf[a++] == '-';
                while (a < e && cfgbuf[a] >= '0' && cfgbuf[a] <= '9' && d < 4)
                    v[k] = v[k] * 10 + (cfgbuf[a++] - '0'), d++;
                if (neg) v[k] = -v[k];
                if (!d || v[k] < cust_set_min[k] || v[k] > cust_set_max[k]) okv = 0;
            }
            for (; k < NSET; k++) v[k] = 0;
            if (okv) {
                for (k = 0; k < NSET; k++) set_val[slot - 1][k] = (int8_t)v[k];
                set_has[slot - 1] = 1;
            }
        } else if (!is_set && digits && a < e && cfgbuf[a] == '=' && slot >= 1 && slot <= SLOTS) {
            a++;
            while (a < e && (cfgbuf[a] == ' ' || cfgbuf[a] == '\t')) a++;
            b = e;
            while (b > a && (cfgbuf[b - 1] == ' ' || cfgbuf[b - 1] == '\t')) b--;
            if (plain_name(cfgbuf + a, b - a)) {
                memcpy(pad_file[slot - 1], cfgbuf + a, b - a);
                pad_file[slot - 1][b - a] = 0;
            }
        }
        i = e + 1;
    }
}

static void read_config(const char *prj)
{
    fw_file *f;
    uint32_t n = 0;
    prj_path(path, prj, "\\KIT.TXT");
    f = fw_fopen(path, "r");
    if (f) {
        n = fw_fread(cfgbuf, 1, sizeof(cfgbuf) - 1, f);
        if (n > sizeof(cfgbuf) - 1) n = 0;
        fw_fclose(f);
    }
    cust_parse_config(n);
}

/* write pad_file[] to KIT.TXT; 0 ok */
static int write_config(const char *prj)
{
    static const char head[] = "# ZOOM R24 CUSTOM kit: pad=file in KIT\\ (01 = KICK ... 16 = PERC)\r\n";
    uint32_t n = 0;
    int s, ok;
    fw_file *f;
    memcpy(cfgbuf, head, sizeof(head) - 1);
    n = sizeof(head) - 1;
    for (s = 0; s < SLOTS; s++) {
        uint32_t l = r24_strlen(pad_file[s]);
        if (!l) continue;
        cfgbuf[n++] = (char)('0' + (s + 1) / 10);
        cfgbuf[n++] = (char)('0' + (s + 1) % 10);
        cfgbuf[n++] = '=';
        memcpy(cfgbuf + n, pad_file[s], l);
        n += l;
        cfgbuf[n++] = '\r';
        cfgbuf[n++] = '\n';
    }
    for (s = 0; s < SLOTS; s++) {
        int k, last;
        if (!set_has[s]) continue;
        cfgbuf[n++] = 'S';
        cfgbuf[n++] = (char)('0' + (s + 1) / 10);
        cfgbuf[n++] = (char)('0' + (s + 1) % 10);
        cfgbuf[n++] = '=';
        last = NREQ - 1;
        for (k = NREQ; k < NSET; k++)
            if (set_val[s][k]) last = k;
        for (k = 0; k <= last; k++) {
            int v = set_val[s][k];
            if (k) cfgbuf[n++] = ',';
            if (v < 0) cfgbuf[n++] = '-', v = -v;
            else if (k == CUST_PITCH && v) cfgbuf[n++] = '+';
            if (v >= 100) cfgbuf[n++] = (char)('0' + v / 100);
            if (v >= 10) cfgbuf[n++] = (char)('0' + v / 10 % 10);
            cfgbuf[n++] = (char)('0' + v % 10);
        }
        cfgbuf[n++] = '\r';
        cfgbuf[n++] = '\n';
    }
    prj_path(path, prj, "\\KIT.TXT");
    f = fw_fopen(path, "w");
    if (!f) return -1;
    ok = fw_fwrite(cfgbuf, 1, n, f) == n;
    fw_fclose(f);
    return ok ? 0 : -1;
}

static int same_name(const char *a, const char *b)
{
    return r24_strlen(a) == r24_strlen(b) && r24_streq_nocase(a, b);
}

const char *cust_pad_file(int slot)
{
    return (slot >= 0 && slot < SLOTS) ? pad_file[slot] : "";
}

/* pad `slot` now plays KIT\<file> ("" = none): update KIT.TXT and delete the
 * previous file of the pad if no other pad uses it. 0 ok, -1 KIT.TXT error. */
int cust_set_pad(int slot, const char *file)
{
    char old[FILE_MAX];
    int s, used = 0;
    if (slot < 0 || slot >= SLOTS || fw_cur_project < 0) return -1;
    if (file[0] && !plain_name(file, r24_strlen(file))) return -1;
    copy_name(old, pad_file[slot], FILE_MAX);
    copy_name(pad_file[slot], file, FILE_MAX);
    if (write_config(fw_cur_project_name)) return -1;
    if (old[0] && !same_name(old, file)) {
        for (s = 0; s < SLOTS; s++)
            if (same_name(pad_file[s], old)) used = 1;
        if (!used) {
            cust_file_path(path, fw_cur_project_name, old);
            fw_remove(path);
        }
    }
    return 0;
}

/* first run: the table is still as the image built it */
static int capture(void)
{
    struct r24_sample *tab = r24_sample_table();
    int k = r24_find_kit("CUSTOM"), s;
    cust_status.kit = k;
    if (k < 0) return 0;
    for (s = 0; s < SLOTS; s++) {
        struct r24_slot *p = &r24_kit(k)->slot[s];
        ids[s] = p->sample_id;
        defaults[s] = tab[ids[s]];
        def_val[s][CUST_LEVEL] = (int8_t)(p->level > CUST_LEVEL_MAX ? CUST_LEVEL_MAX : p->level);
        def_val[s][CUST_PAN] = (int8_t)(p->pan > CUST_PAN_MAX ? CUST_PAN_MAX : p->pan);
        def_val[s][CUST_GROUP] = (int8_t)(p->group & 7);
        {
            int pi = (int8_t)p->reserved[0];
            if (pi < CUST_PITCH_MIN) pi = CUST_PITCH_MIN;
            if (pi > CUST_PITCH_MAX) pi = CUST_PITCH_MAX;
            def_val[s][CUST_PITCH] = (int8_t)pi;
        }
        def_val[s][CUST_POLY] = 0;          /* CUSTOM default: each hit cuts the last */
    }
    return 1;
}

/* write the pad's settings into the CUSTOM kit (the engine reads the kit at
 * every note, so this is heard at once) */
static void apply_settings(int s)
{
    int k = cust_status.kit;
    const int8_t *v = set_has[s] ? set_val[s] : def_val[s];
    struct r24_slot *p;
    if (k < 0 || s < 0 || s >= SLOTS) return;
    p = &r24_kit(k)->slot[s];
    p->level = (uint8_t)v[CUST_LEVEL];
    p->pan = (uint8_t)v[CUST_PAN];
    /* byte 4: bits 0-2 mute group, bit 4 MONO (= not poly: a new hit of the
     * pad cuts its sounding voice, engine 0x8002DCE0) */
    p->group = (uint8_t)((p->group & ~0x17u) | (uint8_t)v[CUST_GROUP] |
                         (v[CUST_POLY] ? 0 : 0x10));
    p->reserved[0] = (uint8_t)v[CUST_PITCH];          /* byte 5: drum_pitch.c */
}

void cust_get_settings(int slot, int v[CUST_NSET])
{
    const int8_t *src;
    int k;
    if (slot < 0 || slot >= SLOTS) slot = 0;
    src = set_has[slot] ? set_val[slot] : def_val[slot];
    for (k = 0; k < NSET; k++) v[k] = src[k];
}

void cust_set_settings(int slot, const int v[CUST_NSET])
{
    int k, same = 1;
    if (slot < 0 || slot >= SLOTS || ready <= 0) return;
    for (k = 0; k < NSET; k++) {
        int x = v[k];
        if (x < cust_set_min[k]) x = cust_set_min[k];
        if (x > cust_set_max[k]) x = cust_set_max[k];
        set_val[slot][k] = (int8_t)x;
        if (x != def_val[slot][k]) same = 0;
    }
    set_has[slot] = !same;
    apply_settings(slot);
}

int cust_save_settings(void)
{
    if (fw_cur_project < 0) return -1;
    return write_config(fw_cur_project_name);
}

/* the regions, once: the reserve, then (CUSTOM_KIT_DDR) the fixed 3 MiB, the
 * two big zones and pool 3. Pool 3 is retried at every load until get_mpl succeeds. */
static void regions_setup(void)
{
    if (!nreg) {
#if HAVE_RESERVE
        reg[nreg].base = reserve;
        reg[nreg].size = sizeof(reserve);
        nreg++;
#endif
#if CUSTOM_KIT_DDR_FIXED
        reg[nreg].base = (uint8_t *)FIXED_BASE;
        reg[nreg].size = FIXED_END - FIXED_BASE;
        nreg++;
#endif
#if CUSTOM_KIT_DDR_BIG
        reg[nreg].base = (uint8_t *)BIG1_BASE;
        reg[nreg].size = BIG1_END - BIG1_BASE;
        nreg++;
        reg[nreg].base = (uint8_t *)BIG2_BASE;
        reg[nreg].size = BIG2_END - BIG2_BASE;
        nreg++;
#endif
    }
#if CUSTOM_KIT_DDR
    if (cust_status.pool_err != 0 && nreg < NREG) {
        void *p = 0;
        int32_t r = fw_get_mpl(POOL3_ID, POOL3_ASK, &p);
        cust_status.pool_err = r;
        if (r == 0 && p) {
            uint32_t a = ((uint32_t)p + 31u) & ~31u;
            reg[nreg].base = (uint8_t *)a;
            reg[nreg].size = (POOL3_ASK - (a - (uint32_t)p)) & ~31u;
            nreg++;
        } else if (r == 0) {
            cust_status.pool_err = -1;
        }
    }
#endif
}

static uint32_t reg_free(int i) { return reg[i].size - reg[i].used; }

void cust_load(const char *name)
{
    struct r24_sample *tab = r24_sample_table();
    uint32_t total = 0;
    int s, i;

    if (!ready) ready = capture() ? 1 : -1;
    if (ready < 0) {
        for (s = 0; s < SLOTS; s++) cust_status.result[s] = E_NOKIT;
        return;
    }
    regions_setup();
    for (i = 0; i < nreg; i++) reg[i].used = 0;
    if (name && name[0]) read_config(name);
    else cust_parse_config(0);
    for (s = 0; s < SLOTS; s++) {
        struct r24_sample e = defaults[s];
        int16_t res = E_NOFILE;
        int big = -1;                           /* region with the most room */
        for (i = 0; i < nreg; i++)
            if (big < 0 || reg_free(i) > reg_free(big)) big = i;
        if (name && name[0] && big >= 0 && reg_free(big) >= 64) {
            fw_file *f;
            if (pad_file[s][0]) {
                cust_file_path(path, name, pad_file[s]);
            } else {                            /* older kits: KIT\SAMPLEnn.WAV */
                cust_make_path(path, name, s);
            }
            f = fw_fopen(path, "r");
            if (f && !pad_file[s][0]) {
                copy_name(pad_file[s], path + r24_strlen(path) - 12, FILE_MAX);
            }
            if (f) {
                /* read + convert in the largest hole, then move the result to
                 * the smallest hole that holds it (the big ones stay free) */
                uint8_t *w = reg[big].base + reg[big].used;
                uint32_t cap = reg_free(big);
                uint32_t n = read_all(f, w, cap);
                int32_t got;
                fw_fclose(f);
                got = cust_wav_to_pcm(w, n, cap);
                if (got >= 2) {
                    uint32_t need = ((uint32_t)got + 31) & ~31u;
                    int fit = big;
                    for (i = 0; i < nreg; i++)
                        if (reg_free(i) >= need && reg_free(i) < reg_free(fit)) fit = i;
                    if (fit != big) {
                        uint8_t *d = reg[fit].base + reg[fit].used;
                        memcpy(d, w, (uint32_t)got);
                        w = d;
                    }
                    if (need > reg_free(fit)) need = reg_free(fit);
                    reg[fit].used += need;
                    total += need;
                    e.rel_start = r24_bank_rel(w);
                    e.length = (uint32_t)got;
                    res = (int16_t)(1 + ((uint32_t)got >> 11));
                } else {
                    res = (int16_t)got;
                }
            }
        } else if (name && name[0]) {
            res = E_SPACE;
        }
        tab[ids[s]] = e;
        cust_status.result[s] = res;
        apply_settings(s);
    }
    cust_status.used = total;
    cust_status.nreg = (uint32_t)nreg;
    for (i = 0; i < nreg; i++) {
        cust_status.reg_base[i] = (uint32_t)reg[i].base;
        cust_status.reg_size[i] = reg[i].size;
        cust_status.reg_used[i] = reg[i].used;
        if (reg[i].used)
            fw_dcache_clean(reg[i].base, reg[i].used);
    }
}

/* Replaces set_current_project(n). */
int cust_hook(int n)
{
    const char *name;
    int r;
    fw_cur_project = n;
    name = fw_project_name(n);
    fw_strcpy(fw_cur_project_name, name);
    r = fw_save_sys();
    cust_status.loads++;
    cust_load(n >= 0 ? name : 0);
    return r;
}

void cust_reload(void)
{
    cust_load(fw_cur_project >= 0 ? fw_cur_project_name : 0);
}

int cust_slot_result(int slot)
{
    return (slot >= 0 && slot < SLOTS) ? cust_status.result[slot] : E_NOFILE;
}
