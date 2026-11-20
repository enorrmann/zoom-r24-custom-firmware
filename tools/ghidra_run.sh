#!/bin/bash
# Run the Ghidra headless analysis/decompilation for the R24 image.
#
#   ./tools/ghidra_run.sh analyze            -> functions.csv
#   ./tools/ghidra_run.sh decompile          -> decomp/*.c + decomp_index.csv
#   ./tools/ghidra_run.sh both               -> both
#
# Environment:
#   GHIDRA        Ghidra install dir (default: /home/emilio/ghidras/ghidra_11.3.2_PUBLIC)
#   JAVA_HOME     JDK (default: autodetected from `which java`)
#   R24_OUT       output dir (default: ./decomp)
#
# Notes
#  * ~/.config may be read-only in some environments; Ghidra insists on
#    writing its preferences there, so XDG_CONFIG_HOME points at a temp dir.
#  * -loader-baseAddr 0x207ffc0 is REQUIRED: the file begins with a 64-byte
#    container header, so the payload's 0x02080000 starts 0x40 into the file.
#    Without it every address is off by 64.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
GHIDRA=${GHIDRA:-/home/emilio/ghidras/ghidra_11.3.2_PUBLIC}
JAVA_HOME=${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(which java)")")")}
OUT=${R24_OUT:-$REPO/decomp}
MODE=${1:-analyze}

if [ ! -x "$GHIDRA/support/analyzeHeadless" ]; then
    echo "error: analyzeHeadless not found at $GHIDRA" >&2
    echo "       set GHIDRA=/path/to/ghidra_X.Y.Z_PUBLIC" >&2
    exit 1
fi
if [ ! -f "$REPO/R24SYS.BIN" ]; then
    echo "error: R24SYS.BIN not found in $REPO" >&2
    exit 1
fi

export JAVA_HOME
# Ghidra's launcher reads JAVA_HOME_OVERRIDE from launch.properties and will
# otherwise try to *prompt* for a JDK, which fails without a TTY.
export JAVA_HOME_OVERRIDE="$JAVA_HOME"
# Use our own config dir unconditionally: if XDG_CONFIG_HOME is already set to
# a read-only location (~/.config in some sandboxes), Ghidra fails to write its
# preferences and then tries to *prompt* for a JDK, which fails with no TTY.
export XDG_CONFIG_HOME="${R24_GHIDRA_CFG:-/tmp/ghidra-cfg}"
export _JAVA_OPTIONS="-Duser.home=/tmp/ghidra-home"
mkdir -p "$XDG_CONFIG_HOME" /tmp/ghidra-home "$OUT"

PROJ=$(mktemp -d)
trap 'rm -rf "$PROJ"' EXIT

SCRIPTS=()
case "$MODE" in
    analyze)  SCRIPTS=(-postScript R24Analyze.java "$OUT") ;;
    decompile)SCRIPTS=(-postScript R24Analyze.java "$OUT" -postScript R24Decompile.java "$OUT") ;;
    both)     SCRIPTS=(-postScript R24Analyze.java "$OUT" -postScript R24Decompile.java "$OUT") ;;
    *) echo "usage: $0 {analyze|decompile|both}" >&2; exit 2 ;;
esac

echo "ghidra : $GHIDRA"
echo "java   : $JAVA_HOME"
echo "output : $OUT"
echo "mode   : $MODE"
echo

"$GHIDRA/support/analyzeHeadless" "$PROJ" R24 \
    -import "$REPO/R24SYS.BIN" \
    -processor "ARM:LE:32:v4t" \
    -loader BinaryLoader \
    -loader-baseAddr 0x207ffc0 \
    -noanalysis \
    -scriptPath "$HERE" \
    "${SCRIPTS[@]}" 2>&1 | grep -E 'R24A:|R24D:|ERROR|Exception' | sed 's/ (GhidraScript)$//'

echo
if [ -f "$OUT/functions.csv" ]; then
    echo "function map : $OUT/functions.csv ($(($(wc -l < "$OUT/functions.csv") - 1)) functions)"
fi
if [ -f "$OUT/decomp_index.csv" ]; then
    echo "decomp index : $OUT/decomp_index.csv"
fi
