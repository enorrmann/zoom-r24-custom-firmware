/*
 * mem_dump.c — PROJECT > DUMP 1 / DUMP 2: copy flash, DSP memory and ARM low
 * memory to files on the SD card (read only: nothing is ever written to the
 * flash or to the DSP). DUMP 1 writes A:\DUMP1, DUMP 2 A:\DUMP2 (below as
 * A:\DUMP): dump once after power-on, once after heavy use, then
 * `r24dump.py --diff DUMP1 DUMP2` shows which DDR stayed untouched.
 *
 *   A:\DUMP\INFO.TXT     CPU id, JTAG id, DSP boot address, the file list
 *   A:\DUMP\DSP_DDR.BIN  0x87B00000  5 MiB    DSP code/data in DDR (uncached)
 *   A:\DUMP\NOR.BIN      0x02000000  16 MiB   NOR flash: boot loader
 *                                              (0x02000000) + this firmware
 *                                              (0x02080000); a smaller chip
 *                                              shows as a repeat
 *   A:\DUMP\DSP_L2.BIN   0x11800000  64 KiB   DSP L2 RAM (vectors at 0)
 *   A:\DUMP\DSP_L1D.BIN  0x11F04000  48 KiB   DSP L1D RAM (the ARM<->DSP area)
 *   A:\DUMP\SYSMOD.BIN   0x01C40000  256 B    system module registers
 *   A:\DUMP\DSP_L1P.BIN  0x11E08000  32 KiB   DSP L1P RAM   (never read by the
 *   A:\DUMP\ARM_LOW.BIN  0x00000000  48 KiB   ARM RAM/ROM    firmware: last)
 *   A:\DUMP\DDR.BIN      0x80000000  128 MiB  the whole DDR — only when chosen
 *                                              on the confirm screen (dial)
 *
 * Every region is mapped by the firmware's own MMU table (TTB 0x800C0000,
 * docs/17_dsp_and_dump.md §3); the regions the firmware itself reads come
 * first, the two it never reads come last, so a bus error there cannot cost
 * the earlier files (each file is closed before the next is opened).
 * Memory is copied word by word (volatile, 32-bit) into a 64 KiB malloc'ed
 * buffer and written from there.
 */
#include "r24.h"

struct md_region { const char *name; uint32_t addr, size; };

static const struct md_region md_regions[] = {
    { "DSP_DDR", 0x87B00000u, 0x00500000u },
    { "NOR",     0x02000000u, 0x01000000u },
    { "DSP_L2",  0x11800000u, 0x00010000u },
    { "DSP_L1D", 0x11F04000u, 0x0000C000u },
    { "SYSMOD",  0x01C40000u, 0x00000100u },
    { "DSP_L1P", 0x11E08000u, 0x00008000u },
    { "ARM_LOW", 0x00000000u, 0x0000C000u },
    { "DDR",     0x80000000u, 0x08000000u },      /* optional: last */
};
#define MD_COUNT ((int)(sizeof(md_regions) / sizeof(md_regions[0])))
#define MD_CHUNK 0x10000u

static char md_path[48], md_msg[40], md_info[1024];
static const char *md_dir = "A:\\DUMP1";

/* ARM-state helper: CP15 c0 main ID (identifies the ARM core revision) */
__attribute__((target("arm"), noinline)) uint32_t md_cpu_id(void)
{
    uint32_t v;
    __asm__ volatile ("mrc p15, 0, %0, c0, c0, 0" : "=r"(v));
    return v;
}

static void md_copy(uint32_t *dst, uint32_t src, uint32_t n)
{
    const volatile uint32_t *s = (const volatile uint32_t *)src;
    uint32_t i;
    for (i = 0; i < n / 4; i++)
        dst[i] = s[i];
}

static void md_name(const char *name, const char *ext)
{
    md_path[0] = 0;
    r24_strcat(md_path, md_dir);
    r24_strcat(md_path, "\\");
    r24_strcat(md_path, name);
    r24_strcat(md_path, ext);
}

/* -> 0 ok, 1 card full, 2 cannot create */
static int md_write(const char *name, const void *buf, uint32_t n)
{
    fw_file *f;
    int r = 0;
    md_name(name, ".TXT");
    f = fw_fopen(md_path, "w");
    if (!f) return 2;
    if (fw_fwrite(buf, 1, n, f) != n) r = 1;
    fw_fclose(f);
    return r;
}

static int md_region(const struct md_region *g, uint32_t *buf)
{
    fw_file *f;
    uint32_t done = 0, n;
    int r = 0;
    md_name(g->name, ".BIN");
    f = fw_fopen(md_path, "w");
    if (!f) return 2;
    while (done < g->size && !r) {
        n = g->size - done;
        if (n > MD_CHUNK) n = MD_CHUNK;
        if ((done & 0xFFFFF) == 0) {                     /* every MiB */
            fw_sprintf(md_msg, "%s %d/%dM", g->name, (int)(done >> 20),
                       (int)((g->size + 0xFFFFF) >> 20));
            fw_ui_progress_end(0);
            fw_ui_progress(md_msg);
        }
        md_copy(buf, g->addr + done, n);
        if (fw_fwrite(buf, 1, n, f) != n) r = 1;
        done += n;
    }
    fw_fclose(f);
    return r;
}

/* the whole dump into folder `dir` (ddr != 0: also DDR.BIN); -> message for
 * fw_ui_message */
const char *md_dump(int ddr, const char *dir)
{
    uint32_t *buf;
    int i, r = 0, len, count = ddr ? MD_COUNT : MD_COUNT - 1;

    md_dir = dir;
    fw_make_dir(md_dir);
    len = fw_sprintf(md_info,
        "R24 memory dump (sdk/patches/mem_dump.c)\r\n"
        "CP15_ID     %08X\r\n"
        "JTAGID      %08X\r\n"
        "DSPBOOTADDR %08X\r\n"
        "file        address  bytes\r\n",
        (unsigned)md_cpu_id(),
        (unsigned)*(volatile uint32_t *)0x01C40028u,
        (unsigned)*(volatile uint32_t *)0x01C40008u);
    for (i = 0; i < count; i++)
        len += fw_sprintf(md_info + len, "%-11s %08X %08X\r\n", md_regions[i].name,
                          (unsigned)md_regions[i].addr, (unsigned)md_regions[i].size);
    if (md_write("INFO", md_info, (uint32_t)len)) return "File Access Error";

    buf = (uint32_t *)fw_malloc(MD_CHUNK);
    if (!buf) return "No Memory";
    fw_ui_progress("Dumping...");
    for (i = 0; i < count && !r; i++)
        r = md_region(&md_regions[i], buf);
    fw_ui_progress_end(0);
    fw_free(buf);
    fw_rescan_drive('A');
    if (r == 1) return "Card Full";
    if (r) return "File Access Error";
    return "Dump Done";
}
