#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_pva_leak_valgrind.sh
#
# Run tests/test_pva_get_leak.py under Valgrind memcheck.
#
# Outputs (all in same directory as this script):
#   pva_leak_live.txt   — live RSS + progress  (tail -f to watch)
#   valgrind_pva.xml    — full Valgrind XML report
#   valgrind_pva.txt    — human-readable Valgrind summary
#
# Usage:
#   bash scripts/run_pva_leak_valgrind.sh [--gets N] [--cycles N] [--soak N]
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_SCRIPT="$REPO_ROOT/tests/test_pva_get_leak.py"

REPORT_FILE="$SCRIPT_DIR/pva_leak_live.txt"
VALGRIND_XML="$SCRIPT_DIR/valgrind_pva.xml"
VALGRIND_TXT="$SCRIPT_DIR/valgrind_pva.txt"

# Default test parameters
GETS=500
CYCLES=50
SOAK=60

# Parse overrides
while [[ $# -gt 0 ]]; do
    case "$1" in
        --gets)   GETS="$2";   shift 2 ;;
        --cycles) CYCLES="$2"; shift 2 ;;
        --soak)   SOAK="$2";   shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

PYTHON="${PYTHON:-python3}"

echo "============================================================"
echo "  p4p PVA get() leak test under Valgrind"
echo "  gets=$GETS  cycles=$CYCLES  soak=${SOAK}s  pvs=100"
echo "  live report : $REPORT_FILE"
echo "  valgrind xml: $VALGRIND_XML"
echo "  valgrind txt: $VALGRIND_TXT"
echo "============================================================"
echo ""
echo "  Tip: tail -f $REPORT_FILE"
echo ""

if ! command -v valgrind &>/dev/null; then
    echo "ERROR: valgrind not found. Install: apt-get install valgrind" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Build a Python suppressions file to quiet known-noisy false positives:
#   - Python's pymalloc arena (still-reachable at interpreter exit)
#   - pthread / glibc one-time-init blocks
# ---------------------------------------------------------------------------
SUPP_FILE="$SCRIPT_DIR/pva_leak.supp"
cat > "$SUPP_FILE" << 'EOF'
# Python pymalloc arena — still-reachable at exit, not a real leak
{
   python_pymalloc_arena
   Memcheck:Leak
   match-leak-kinds: reachable
   fun:malloc
   ...
   fun:_PyObject_Malloc
}
{
   python_pymalloc_arena2
   Memcheck:Leak
   match-leak-kinds: reachable
   fun:malloc
   ...
   fun:PyMem_Malloc
}
# glibc pthread one-time init
{
   pthread_once_init
   Memcheck:Leak
   match-leak-kinds: reachable
   fun:calloc
   fun:allocate_dtv
   ...
}
{
   dl_open_reachable
   Memcheck:Leak
   match-leak-kinds: reachable
   fun:malloc
   ...
   fun:dl_open_worker
}
EOF

VALGRIND_OPTS=(
    --tool=memcheck
    --leak-check=full
    --track-origins=yes
    --show-leak-kinds=definite,indirect,possible
    --num-callers=20
    --xml=yes
    --xml-file="$VALGRIND_XML"
    --log-file="$VALGRIND_TXT"
    --suppressions="$SUPP_FILE"
    --error-exitcode=1
)

set +e
valgrind "${VALGRIND_OPTS[@]}" \
    "$PYTHON" "$TEST_SCRIPT" \
        --gets   "$GETS" \
        --cycles "$CYCLES" \
        --soak   "$SOAK" \
        --report "$REPORT_FILE"
EXIT_CODE=$?
set -e

echo ""
echo "============================================================"
echo "  Valgrind exit code: $EXIT_CODE"
echo "============================================================"

if [[ -f "$VALGRIND_TXT" ]]; then
    echo ""
    echo "--- Valgrind summary ---"
    grep -E "ERROR SUMMARY|definitely lost|indirectly lost|possibly lost|still reachable|Invalid|Uninitialised" \
        "$VALGRIND_TXT" || echo "(no matching lines — clean run)"
fi

echo ""
echo "Files:"
echo "  live RSS  : $REPORT_FILE"
echo "  vg xml    : $VALGRIND_XML"
echo "  vg text   : $VALGRIND_TXT"
echo "  vg supps  : $SUPP_FILE"

exit $EXIT_CODE
