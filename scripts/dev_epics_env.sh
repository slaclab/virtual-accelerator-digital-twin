#!/usr/bin/env bash
# Source this in the devcontainer terminal to configure EPICS CLI tools.
# Connects pvget/pvput/caget/caput to the mock-ioc and VA output PVs.
#
# Usage:
#   source scripts/dev_epics_env.sh            # connect to mock-ioc inputs + VA outputs
#   source scripts/dev_epics_env.sh --va-only  # connect only to VA outputs (localhost:5075)
#   source scripts/dev_epics_env.sh --ioc-only # connect only to mock-ioc inputs

MODE="${1:-}"

# p4p provides PVA CLI via python -m p4p.client.cli
alias pvget='python -m p4p.client.cli get'
alias pvput='python -m p4p.client.cli put'
alias pvmon='python -m p4p.client.cli monitor'

# CA tools — libca from conda
export PYEPICS_LIBCA="/opt/conda/epics/lib/linux-x86_64/libca.so"
export LD_LIBRARY_PATH="/opt/conda/epics/lib/linux-x86_64:${LD_LIBRARY_PATH:-}"
export PATH="/opt/conda/epics/bin/linux-x86_64:/opt/conda/bin:$PATH"

case "$MODE" in
  --va-only)
    # VA output PVs served on localhost:5075
    export EPICS_PVA_AUTO_ADDR_LIST="NO"
    export EPICS_PVA_ADDR_LIST="127.0.0.1"
    export EPICS_CA_AUTO_ADDR_LIST="NO"
    export EPICS_CA_ADDR_LIST="127.0.0.1"
    echo "EPICS → VA outputs (localhost:5075)"
    echo "  pvget BPMS:IN20:581:TMIT"
    ;;
  --ioc-only)
    # Mock IOC input PVs served on mock-ioc
    export EPICS_PVA_AUTO_ADDR_LIST="NO"
    export EPICS_PVA_ADDR_LIST="mock-ioc"
    export EPICS_CA_AUTO_ADDR_LIST="NO"
    export EPICS_CA_ADDR_LIST="mock-ioc"
    echo "EPICS → mock-ioc inputs"
    echo "  pvget QUAD:IN20:631:BCTRL"
    echo "  pvput QUAD:IN20:631:BCTRL 7.4"
    ;;
  *)
    # Both — pvua searches mock-ioc for inputs, localhost for VA outputs
    export EPICS_PVA_AUTO_ADDR_LIST="NO"
    export EPICS_PVA_ADDR_LIST="mock-ioc 127.0.0.1"
    export EPICS_CA_AUTO_ADDR_LIST="NO"
    export EPICS_CA_ADDR_LIST="mock-ioc 127.0.0.1"
    echo "EPICS → mock-ioc (inputs) + localhost (VA outputs)"
    ;;
esac

echo ""
echo "=== Input PVs (mock-ioc) ==="
echo "  SOLN:IN20:121:BCTRL     QUAD:IN20:121:BCTRL     QUAD:IN20:122:BCTRL"
echo "  ACCL:IN20:300:L0A_PDES  ACCL:IN20:400:L0B_PDES"
echo "  QUAD:IN20:361/371/425/441/511/525/631/651:BCTRL"
echo "  XCOR:IN20:641:BCTRL     YCOR:IN20:642:BCTRL     BEND:IN20:661:BCTRL"
echo ""
echo "=== Output PVs (VA, localhost:5075) ==="
echo "  BPMS:IN20:581:TMIT  BPMS:IN20:581:X  BPMS:IN20:581:Y"
echo ""
echo "Examples:"
echo "  pvget QUAD:IN20:631:BCTRL"
echo "  pvput QUAD:IN20:631:BCTRL 7.5"
echo "  pvget BPMS:IN20:581:TMIT"
