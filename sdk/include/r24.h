/*
 * r24.h — writing firmware patches for the Zoom R24 in C.
 *
 * A patch is a .c file. It declares what it changes in the firmware with the
 * macros below; tools/r24sdk.py compiles every patch of a build into one
 * section in the PCM bank tail (RAM 0x8754xxxx), checks the original bytes at
 * every site and applies the changes. See sdk/README.md.
 *
 *   R24_REPLACE(addr, "orig hex", func)
 *       Make the firmware function at RAM `addr` jump to `func` (same
 *       arguments, same return value). The first 8 or 10 bytes become a long
 *       jump; "orig hex" must be at least that long and match the image.
 *       The original cannot be called any more — re-implement what you keep.
 *       The jump goes through r3: only the first THREE arguments arrive.
 *
 *   R24_POINTER(addr, old, object)
 *       Store &object in the 32-bit data word at RAM `addr`, which must hold
 *       `old` (a callback in a menu record, a table pointer, ...). For a
 *       Thumb function pass the function: the linker sets bit 0.
 *
 *   R24_BYTES(addr, "old hex", "new hex")
 *       Replace raw bytes (same length) — an immediate, a branch, a string.
 *
 *   R24_CHECK(addr, "hex")
 *       Refuse to build unless the image holds these bytes at `addr` — for
 *       data your code relies on without changing it.
 *
 *   R24_KIT("NAME")
 *       Add a drum kit called NAME to KIT SELECT (16 sample-table entries of
 *       its own, initially a copy of kit 1). Find it at run time with
 *       r24_find_kit("NAME").
 *
 * Addresses are RAM addresses (0x80xxxxxx), as in the docs and disassembly.
 */
#ifndef R24_H
#define R24_H

#include "r24fw.h"

enum { R24_K_REPLACE = 1, R24_K_POINTER = 2, R24_K_BYTES = 3, R24_K_KIT = 4 };

struct r24_hook {
    uint32_t kind;
    uint32_t addr;          /* RAM address of the site */
    const void *target;     /* function / object (REPLACE, POINTER) */
    uint32_t old;           /* POINTER: expected old word */
    char a[96];             /* REPLACE/BYTES: expected hex; KIT: name */
    char b[96];             /* BYTES: new hex */
};

#define R24_CAT_(a, b) a##b
#define R24_CAT(a, b) R24_CAT_(a, b)
#define R24_HOOK_(...) \
    __attribute__((section(".r24hooks"), used, aligned(4))) \
    static const struct r24_hook R24_CAT(r24_hook_, __COUNTER__) = { __VA_ARGS__ }

#define R24_REPLACE(addr, orig, func) \
    R24_HOOK_(R24_K_REPLACE, (addr), (const void *)(func), 0, orig, "")
#define R24_POINTER(addr, oldv, obj) \
    R24_HOOK_(R24_K_POINTER, (addr), (const void *)(obj), (oldv), "", "")
#define R24_BYTES(addr, oldhex, newhex) \
    R24_HOOK_(R24_K_BYTES, (addr), 0, 0, oldhex, newhex)
#define R24_CHECK(addr, hex) \
    R24_HOOK_(R24_K_BYTES, (addr), 0, 0, hex, hex)
#define R24_KIT(name) \
    R24_HOOK_(R24_K_KIT, 0, 0, 0, name, "")

/* ── helpers (sdk/runtime/runtime.c) ──────────────────────────────────── */
struct r24_sample *r24_sample_table(void);
struct r24_kit *r24_kit(int k);
int r24_kit_count(void);
int r24_find_kit(const char *name);              /* -1 if absent */
void *r24_bank_ptr(uint32_t rel);                /* bank-relative -> RAM */
uint32_t r24_bank_rel(const void *p);            /* RAM -> bank-relative */
uint32_t r24_strlen(const char *s);
char *r24_strcat(char *d, const char *s);        /* d must be large enough */
int r24_streq_nocase(const char *a, const char *b);

/* gcc may emit these; the runtime provides them */
void *memcpy(void *d, const void *s, size_t n);
void *memset(void *d, int c, size_t n);
void *memmove(void *d, const void *s, size_t n);

#endif
