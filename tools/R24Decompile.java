// R24Decompile.java — export Ghidra's C for every function in the R24 image.
//
// Ghidra's decompiler with ARM_v45.cspec (AAPCS) does work on this target, but
// the output is *reconstructed* C, not original source: names are generated,
// types are inferred, and there is no guarantee it recompiles to the same
// bytes. It is an aid for reading, not a build input.
//
// Outputs into the given dir:
//   decomp/<addr>_<name>.c    one file per function
//   decompiled_all.c          everything concatenated
//   strings.csv               string literals recovered from the image
//
//@category R24

import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.lang.Register;

import java.io.PrintWriter;
import java.util.*;

public class R24Decompile extends GhidraScript {

    static final long GAP_START = 0x0226E910L;
    // 0xFF region + PCM sample bank: not code (docs/07, docs/10 §0)
    static final long GAP_END = 0x7FFFFFFFL;

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String outDir = args.length > 0 ? args[0] : "/tmp/gtest/out";
        int limit = args.length > 1 ? Integer.parseInt(args[1]) : 0;

        new java.io.File(outDir + "/decomp").mkdirs();

        final Register tmode = currentProgram.getProgramContext().getRegister("TMode");
        DecompInterface dec = new DecompInterface();
        DecompileOptions opts = new DecompileOptions();
        dec.setOptions(opts);
        dec.openProgram(currentProgram);

        List<Function> funcs = new ArrayList<>();
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        for (Function f : it) {
            long va = f.getEntryPoint().getOffset();
            if (va >= GAP_START && va < GAP_END) continue;
            funcs.add(f);
        }
        if (limit > 0 && funcs.size() > limit) funcs = funcs.subList(0, limit);

        println("R24D: decompiling " + funcs.size() + " functions");

        int ok = 0, fail = 0;
        StringBuilder all = new StringBuilder();
        all.append("// Zoom R24 v1.13 -- Ghidra decompilation (read-only aid)\n");
        all.append("// ARM:LE:32:v4t / ARM_v45.cspec (AAPCS)\n");
        all.append("// NOTE: reconstructed C. Not original source, not buildable.\n\n");

        try (PrintWriter idx = new PrintWriter(outDir + "/decomp_index.csv")) {
            idx.println("addr,name,is_thumb,size,decompiled,file");
            int n = 0;
            for (Function f : funcs) {
                n++;
                if (n % 200 == 0) println("R24D: progress " + n + "/" + funcs.size());
                DecompileResults r = dec.decompileFunction(f, 45, monitor);
                String c = null;
                if (r != null && r.decompileCompleted() && r.getDecompiledFunction() != null) {
                    c = r.getDecompiledFunction().getC();
                }
                long va = f.getEntryPoint().getOffset();
                boolean thumb = false;
                try {
                    java.math.BigInteger v = currentProgram.getProgramContext()
                            .getValue(tmode, f.getEntryPoint(), false);
                    thumb = v != null && v.signum() != 0;
                } catch (Exception ignore) { }
                final boolean fThumb = thumb;
                String fname = String.format("%08x_%s.c", va, f.getName());
                if (c != null) {
                    ok++;
                    try (PrintWriter pw = new PrintWriter(outDir + "/decomp/" + fname)) {
                        pw.println("// " + f.getName() + " @ " + String.format("%08x", va)
                                + "  (Thumb=" + fThumb + ", " + f.getBody().getNumAddresses() + " bytes)");
                        pw.print(c);
                    }
                    all.append("//==== ").append(f.getName()).append(" @ ")
                       .append(String.format("%08x", va)).append(" ====\n");
                    all.append(c).append("\n\n");
                    idx.printf("0x%08x,%s,%d,%d,1,%s%n", va, f.getName(),
                            fThumb ? 1 : 0, f.getBody().getNumAddresses(), fname);
                } else {
                    fail++;
                    idx.printf("0x%08x,%s,%d,%d,0,%s%n", va, f.getName(),
                            fThumb ? 1 : 0, f.getBody().getNumAddresses(), "");
                }
            }
        }

        try (PrintWriter pw = new PrintWriter(outDir + "/decompiled_all.c")) {
            pw.print(all);
        }
        println("R24D: done ok=" + ok + " fail=" + fail
                + " -> " + outDir + "/decomp/ and decompiled_all.c");
        dec.dispose();
    }
}
