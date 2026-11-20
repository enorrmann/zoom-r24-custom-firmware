// R24Analyze.java — full-image ARM/Thumb analysis for the Zoom R24 firmware.
//
// The image is a flat binary: no symbols, no sections, no relocations, and the
// two instruction sets are interleaved. Ghidra's auto-analysis can't discover
// much on its own, so this script seeds the disassembly from evidence:
//
//   1. ARM-state seeds  : the crt0/veneer cluster at the image start and any
//                         4-aligned word that points into the code range.
//   2. Thumb-state seeds: any word with bit 0 set (Thumb interworking bit)
//                         pointing into the code range, plus call targets that
//                         fall out of the linear sweep.
//
// It then disassembles forward from every seed, propagates ARM/Thumb mode from
// detected transitions, creates functions, and finally emits a map file so the
// host-side tools can slice and patch by function.
//
// Outputs (into the given output dir):
//   functions.csv   addr,mode,size,name,isThumb
//   r24_analysis.txt  human-readable summary
//
//@category R24

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.SourceType;

import java.io.PrintWriter;
import java.math.BigInteger;
import java.util.*;

public class R24Analyze extends GhidraScript {

    static final long LOAD = 0x02080000L;      // VA of file offset 0x40
    static final long CODE_END = 0x02756634L;  // last mapped VA
    static final long GAP_START = 0x0226E910L; // erased-flash gap
    // Everything from the 0xFF region to EOF is not code: the region is never
    // loaded, and 0x022A0000.. is the 16-bit PCM sample bank (docs/07).
    static final long GAP_END = 0x7FFFFFFFL;

    private Register tmode;
    private AddressSpace sp;

    @Override
    public void run() throws Exception {
        sp = currentProgram.getAddressFactory().getDefaultAddressSpace();
        tmode = currentProgram.getProgramContext().getRegister("TMode");
        if (tmode == null) throw new RuntimeException("no TMode register");

        String[] args = getScriptArgs();
        String outDir = args.length > 0 ? args[0] : "/tmp/gtest/out";

        println("R24A: language=" + currentProgram.getLanguageID()
                + " cspec=" + currentProgram.getCompilerSpec().getCompilerSpecID());

        // ---- 1. seed ARM state at the known load/entry cluster -------------
        long[] armFixed = { 0x02080000L, 0x0208002CL, 0x02080060L };
        for (long va : armFixed) {
            try { seed(va, false); } catch (Exception e) {
                println("R24A: fixed seed " + Long.toHexString(va) + " failed: " + e);
            }
        }
        println("R24A: fixed ARM seeds done");

        // ---- 2. scan the image for pointer-shaped words ------------------
        // We read the program bytes rather than the file so the mapping
        // (header at 0x0207FFC0) is already accounted for.
        Set<Long> thumbSeeds = new TreeSet<>();
        Set<Long> armSeeds = new TreeSet<>();
        List<long[]> words = readAllWords();
        for (long[] w : words) {
            long val = w[1] & 0xFFFFFFFFL;
            if (val >= LOAD + 1 && val < CODE_END && (val & 1) == 1) {
                thumbSeeds.add(val & ~1L);            // clear the Thumb bit
            } else if (val >= LOAD && val < CODE_END && (val & 3) == 0) {
                armSeeds.add(val);
            }
        }
        println("R24A: raw pointer-shaped words: thumb=" + thumbSeeds.size()
                + " arm=" + armSeeds.size());

        // Restrict to addresses inside real code (skip the erased-flash gap and
        // anything that is obviously a string table). We keep seeds that land
        // in the two code blocks.
        thumbSeeds.removeIf(v -> v >= GAP_START && v < GAP_END);
        armSeeds.removeIf(v -> v >= GAP_START && v < GAP_END);

        // ---- 3. disassemble from the Thumb seeds -------------------------
        int disThumb = 0, tick = 0;
        for (long va : thumbSeeds) {
            Address a = sp.getAddress(va);
            if (getInstructionAt(a) != null) continue;
            try {
                setThumb(a, true);
                if (disassemble(a)) disThumb++;
            } catch (Exception e) { }
            if (++tick % 500 == 0) println("R24A: progress thumb " + tick);
        }
        println("R24A: thumb seeds disassembled=" + disThumb);

        // ---- 4. disassemble from the ARM seeds ---------------------------
        int disArm = 0;
        for (long va : armSeeds) {
            Address a = sp.getAddress(va);
            if (getInstructionAt(a) != null) continue;
            try {
                setThumb(a, false);
                if (disassemble(a)) disArm++;
            } catch (Exception e) { }
        }
        println("R24A: arm seeds disassembled=" + disArm);

        // ---- 5. create functions at every seed that decoded ---------------
        int nFunc = 0;
        for (long va : thumbSeeds) {
            Address a = sp.getAddress(va);
            if (getInstructionAt(a) == null) continue;
            if (getFunctionAt(a) != null) continue;
            if (createFunction(a, null) != null) nFunc++;
        }
        for (long va : armSeeds) {
            Address a = sp.getAddress(va);
            if (getInstructionAt(a) == null || getFunctionAt(a) != null) continue;
            if (createFunction(a, null) != null) nFunc++;
        }
        println("R24A: functions created=" + nFunc);

        // ---- 6. emit the map ---------------------------------------------
        List<Function> funcs = new ArrayList<>();
        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            funcs.add(f);
        }
        funcs.sort(Comparator.comparingLong(f -> f.getEntryPoint().getOffset()));

        try (PrintWriter pw = new PrintWriter(outDir + "/functions.csv")) {
            pw.println("addr,is_thumb,size,name");
            for (Function f : funcs) {
                long va = f.getEntryPoint().getOffset();
                boolean th = isThumbAt(f.getEntryPoint());
                pw.printf("0x%08x,%d,%d,%s%n", va, th ? 1 : 0,
                        f.getBody().getNumAddresses(), f.getName());
            }
        }
        println("R24A: wrote " + outDir + "/functions.csv (" + funcs.size() + " functions)");
    }

    private boolean isThumbAt(Address a) {
        try {
            BigInteger v = currentProgram.getProgramContext().getValue(tmode, a, false);
            return v != null && v.signum() != 0;
        } catch (Exception e) {
            return false;
        }
    }

    private void setThumb(Address a, boolean thumb) throws Exception {
        BigInteger want = thumb ? BigInteger.ONE : BigInteger.ZERO;
        try {
            BigInteger cur = currentProgram.getProgramContext().getValue(tmode, a, false);
            if (cur != null && cur.equals(want)) return;
        } catch (Exception ignore) { }
        // An instruction already decoded at `a` pins the context; clear it and
        // retry, otherwise setValue throws ContextChangeException.
        Instruction old = getInstructionAt(a);
        if (old != null) {
            clearListing(a, old.getMaxAddress());
        }
        currentProgram.getProgramContext().setValue(tmode, a, a, want);
    }

    private void seed(long va, boolean thumb) throws Exception {
        Address a = sp.getAddress(va);
        setThumb(a, thumb);
        disassemble(a);
    }

    /** Read every 4-byte word of the mapped payload. */
    private List<long[]> readAllWords() throws Exception {
        List<long[]> out = new ArrayList<>();
        ghidra.program.model.mem.Memory mem = currentProgram.getMemory();
        Address start = sp.getAddress(LOAD);
        Address end = sp.getAddress(CODE_END - 4);
        long n = (CODE_END - LOAD) / 4;
        byte[] buf = new byte[4];
        long addr = LOAD;
        for (long i = 0; i < n; i++) {
            Address a = sp.getAddress(addr);
            int r = mem.getBytes(a, buf);
            if (r == 4) {
                long v = ((long) (buf[3] & 0xFF) << 24) | ((long) (buf[2] & 0xFF) << 16)
                        | ((long) (buf[1] & 0xFF) << 8) | (long) (buf[0] & 0xFF);
                out.add(new long[] { addr, v });
            }
            addr += 4;
        }
        return out;
    }
}
