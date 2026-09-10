#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_test.sh — Run PVA get leak test, optionally under Valgrind.
#
# Usage (inside container):
#   ./run_test.sh --gets 2000 --cycles 50 --soak 120
#   VALGRIND=1 ./run_test.sh --gets 500 --cycles 20 --soak 30
# ---------------------------------------------------------------------------
set -euo pipefail

REPORT="/tmp/pva_leak_report.txt"

echo "============================================================"
echo "  pvxs cacheClean fix — PVA get() leak test"
echo "============================================================"
python3 -c "
import p4p, sys
print(f'  Python    : {sys.version}')
print(f'  p4p       : {getattr(p4p, \"__version__\", \"unknown\")}')
try:
    from pvxslibs import path
    print(f'  pvxslibs  : {path.lib_path}')
except:
    print('  pvxslibs  : (not directly importable)')
" 2>/dev/null || true
echo ""

if [[ "${VALGRIND:-}" == "1" ]]; then
    echo "  Running under Valgrind..."
    echo ""
    exec valgrind \
        --tool=memcheck \
        --leak-check=full \
        --show-leak-kinds=definite,indirect,possible \
        --num-callers=20 \
        --log-file=/tmp/valgrind_pva.txt \
        python3 /test/test_pva_get_leak.py \
            --report "$REPORT" \
            "$@"
else
    exec python3 /test/test_pva_get_leak.py \
        --report "$REPORT" \
        "$@"
fi
