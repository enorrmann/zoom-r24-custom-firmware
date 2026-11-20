/*
 * selftest.c — exercises every SDK mechanism; used by tools/test_r24sdk.py.
 * NOT meant to be flashed (it relabels LOOP[USB] for the test).
 */
#include "r24.h"

R24_KIT("SELFTEST");

/* kit_ptr(k) = 0x800E1D80 + k*0x90 (12 bytes, addr % 4 == 2): replace it with
 * a C version that goes through the (moved) kit table literal */
int st_calls;
struct r24_kit *st_kit_ptr(int k);
R24_REPLACE(0x8004B69A, "010090204843874a8018", st_kit_ptr);
struct r24_kit *st_kit_ptr(int k)
{
    st_calls++;
    return r24_kit(k);
}

/* the KIT SELECT menu record's item getter (word at 0x800FD3A4) */
R24_POINTER(0x800FD3A4, 0x8004B69B, st_kit_ptr);

/* raw bytes: the TAKE SELECT label "LOOP[USB]" (RAM 0x80042E7C) */
R24_BYTES(0x80042E7C, "4c4f4f505b5553425d", "53454c465445535421");

static char buf[64];
static uint32_t words[4] = { 1, 2, 3, 4 };

/* returns a bit mask of passed checks; tools/test_r24sdk.py wants 0x3FF */
int st_probe(void)
{
    int ok = 0;
    uint32_t copy[4] = { 0 };
    if (fw_strcmp("abc", "abc") == 0 && fw_strcmp("abc", "abd") != 0) ok |= 1;   /* ARM */
    fw_memcpy(copy, words, sizeof(copy));                                        /* ARM */
    if (copy[0] == 1 && copy[3] == 4) ok |= 2;
    fw_strcpy(buf, "R24");                                                       /* Thumb */
    if (buf[0] == 'R' && buf[2] == '4' && buf[3] == 0) ok |= 4;
    if (r24_find_kit("selftest") == r24_kit_count() - 1) ok |= 8;
    if (r24_find_kit("basic") == 0 && r24_find_kit("nope") == -1) ok |= 16;
    if (r24_sample_table() == fw_lit_sample_table) ok |= 32;
    if (r24_kit(0)->slot[0].level >= 7) ok |= 64;
    fw_memclr(copy, sizeof(copy));                                               /* ARM */
    if (copy[0] == 0 && copy[3] == 0) ok |= 128;
    if (fw_strncmp("ZOOM R24", "ZOOM R16", 6) == 0) ok |= 256;
    r24_strcat(buf, "-SDK");
    if (r24_strlen(buf) == 7) ok |= 512;
    return ok;
}
