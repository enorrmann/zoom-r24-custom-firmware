// R24MenuTrace.java — trace how the sampler's TAKE menu resolves its entries.
//
// Question this answers (the blocker for adding a second loop entry):
//   * the LOOP[USB] record at file 0x43427 holds the label bytes at +0x09;
//   * NO word in the image equals &"LOOP[USB]" (0x800433A3);
//   * yet the entry works and navigates on hardware.
// So something derives the label address at run time. This script finds the
// code that reaches the menu block and decodes the pointer algebra around it.
//
// Everything here works straight off the program bytes, so no auto-analysis is
// required (-noanalysis is fine).
//
//@category R24

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;

import java.util.*;

public class R24MenuTrace extends GhidraScript {

    // The image is loaded with -loader-baseAddr 0x207ffc0, so the payload at
    // VA 0x02080000 sits at file offset 0x40.  Everything below is expressed in
    // FILE OFFSETS and converted with va():  VA = file + 0x0207FFC0.
    static final long VA_DELTA = 0x0207FFC0L;    // file offset -> VA
    static final long LOAD     = 0x02080000L;    // VA of file offset 0x40
    static final long FILE0    = 0x40L;          // first payload byte

    private Memory mem;

    /** file offset -> Ghidra address */
    private Address va(long fileOff) { return toAddr(fileOff + VA_DELTA); }

    private long u32(long fileOff) throws Exception {
        return getInt(va(fileOff)) & 0xFFFFFFFFL;
    }

    private int u16(long fileOff) throws Exception {
        return getShort(va(fileOff)) & 0xFFFF;
    }

    private String cstr(long fileOff) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 40; i++) {
            int b = getByte(va(fileOff + i)) & 0xFF;
            if (b == 0) break;
            sb.append(b >= 32 && b < 127 ? (char) b : '.');
        }
        return sb.toString();
    }

    /** all offsets (step 2) whose little-endian u32 == v */
    private List<Long> findWord(long v, boolean aligned) throws Exception {
        List<Long> out = new ArrayList<>();
        long end = currentProgram.getMaxAddress().getOffset() - VA_DELTA;
        int step = aligned ? 4 : 1;
        for (long o = FILE0; o < end - 4; o += step) {
            if (u32(o) == v) out.add(o);
        }
        return out;
    }

    private void dump(long lo, long hi) throws Exception {
        for (long o = lo; o < hi; o += 16) {
            StringBuilder hex = new StringBuilder();
            StringBuilder asc = new StringBuilder();
            for (int i = 0; i < 16; i++) {
                int b = getByte(va(o + i)) & 0xFF;
                hex.append(String.format("%02x ", b));
                asc.append(b >= 32 && b < 127 ? (char) b : '.');
            }
            println(String.format("R24M:   %06x  %s |%s|", o, hex, asc));
        }
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        long rec = args.length > 0 ? Long.decode(args[0]) : 0x43427L;   // menu record

        println("R24M: === menu record at file 0x" + Long.toHexString(rec) + " ===");
        dump(rec, rec + 0x20);
        long labelVa = rec + 9 + VA_DELTA;
        println("R24M: implied label VA = 0x" + Long.toHexString(labelVa)
                + "  text=\"" + cstr(rec + 9) + "\"");

        println("R24M: --- exact-word references to the label address ---");
        for (long o : findWord(labelVa, true)) println("R24M:   0x" + Long.toHexString(o));
        for (long o : findWord(labelVa, false)) println("R24M:   (unaligned) 0x" + Long.toHexString(o));

        println("R24M: --- exact-word references to the RECORD address ---");
        long recVa = rec + VA_DELTA;
        for (long o : findWord(recVa, true)) println("R24M:   0x" + Long.toHexString(o));

        println("R24M: --- loader base / table-base candidates ---");
        // the four records visible in the image: 0x43404, 0x4340F, 0x4341A?, 0x43425
        long[] recs = {0x43404L, 0x4340FL, 0x4341AL, 0x43425L};
        for (long r : recs) {
            println(String.format("R24M:   rec 0x%06x label@+9=%s", r, cstr(r + 9)));
        }

        println("R24M: --- u32 in the image equal to a small table base ---");
        for (long cand : new long[]{0x43404L, 0x43404L + VA_DELTA, 0x43427L, 0x43427L + VA_DELTA,
                                    0x43425L, 0x43425L + VA_DELTA}) {
            List<Long> hits = findWord(cand, true);
            println(String.format("R24M:   0x%08x -> %d refs %s", cand, hits.size(),
                    hits.size() > 8 ? "(first 8)" : ""));
            for (int i = 0; i < Math.min(hits.size(), 8); i++) {
                println(String.format("R24M:      at file 0x%06x", hits.get(i)));
            }
        }

        println("R24M: --- the function that indexes the RAM list context 0x81149808 ---");
        for (long o : findWord(0x81149808L, true)) {
            println(String.format("R24M:   pool word at file 0x%06x", o));
        }

        println("R24M: DONE");
    }
}
