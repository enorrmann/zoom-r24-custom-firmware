/*
 * runtime.c — linked into every SDK build, first in the section.
 *
 * The section header (struct r24_head) lets tools/r24sdk.py find the build
 * and its hook records in an image without any other metadata.
 */
#include "r24.h"

struct r24_head {
    char tag[8];                    /* "R24SDK01" */
    uint32_t version;               /* SDK format, 1 */
    const struct r24_hook *hooks;   /* hook records ... */
    const struct r24_hook *hooks_end;
    const void *end;                /* end of the section's used bytes */
    uint32_t reserved[2];
};

extern const struct r24_hook __r24_hooks_start[], __r24_hooks_end[];
extern const char __r24_end[];

__attribute__((section(".r24head"), used))
const struct r24_head R24_HEAD = {
    { 'R', '2', '4', 'S', 'D', 'K', '0', '1' }, 1,
    __r24_hooks_start, __r24_hooks_end, __r24_end, { 0, 0 }
};

void *memcpy(void *d, const void *s, size_t n)
{
    uint8_t *a = d;
    const uint8_t *b = s;
    while (n--) *a++ = *b++;
    return d;
}

void *memmove(void *d, const void *s, size_t n)
{
    uint8_t *a = d;
    const uint8_t *b = s;
    if (a < b) {
        while (n--) *a++ = *b++;
    } else {
        a += n;
        b += n;
        while (n--) *--a = *--b;
    }
    return d;
}

void *memset(void *d, int c, size_t n)
{
    uint8_t *a = d;
    while (n--) *a++ = (uint8_t)c;
    return d;
}

struct r24_sample *r24_sample_table(void) { return fw_lit_sample_table; }

struct r24_kit *r24_kit(int k) { return &fw_lit_kit_table[k]; }

int r24_kit_count(void) { return (int)fw_kit_count; }

void *r24_bank_ptr(uint32_t rel) { return (void *)(R24_PCM_BANK + rel); }

uint32_t r24_bank_rel(const void *p) { return (uint32_t)p - R24_PCM_BANK; }

uint32_t r24_strlen(const char *s)
{
    uint32_t n = 0;
    while (s[n]) n++;
    return n;
}

char *r24_strcat(char *d, const char *s)
{
    char *p = d + r24_strlen(d);
    while ((*p++ = *s++)) {}
    return d;
}

static char up(char c) { return (c >= 'a' && c <= 'z') ? (char)(c - 32) : c; }

/* kit names are stored space-padded ("CUSTOM         ") */
int r24_streq_nocase(const char *a, const char *b)
{
    while (*a && *b && up(*a) == up(*b)) a++, b++;
    while (*a == ' ') a++;
    while (*b == ' ') b++;
    return *a == 0 && *b == 0;
}

int r24_find_kit(const char *name)
{
    int k, n = r24_kit_count();
    for (k = 0; k < n; k++) {
        char tmp[17];
        memcpy(tmp, r24_kit(k)->name, 16);
        tmp[16] = 0;
        if (r24_streq_nocase(tmp, name)) return k;
    }
    return -1;
}
