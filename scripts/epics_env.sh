#!/usr/bin/env bash
# Source this script in a client terminal to connect to a specific PVA instance.
# Usage: source scripts/epics_env.sh <port>
#   e.g. source scripts/epics_env.sh 5175

if [ -z "${1:-}" ]; then
    echo "Usage: source epics_env.sh <base_port>"
    echo "  e.g. source epics_env.sh 5175"
    return 1 2>/dev/null || exit 1
fi

export EPICS_PVA_NAME_SERVERS="localhost:$1"
export EPICS_PVA_ADDR_LIST=""
export EPICS_PVA_AUTO_ADDR_LIST="NO"
unset EPICS_PVA_SERVER_PORT 2>/dev/null
unset EPICS_PVA_BROADCAST_PORT 2>/dev/null

echo "EPICS PVA client configured — name server: localhost:$1 (TCP only, no UDP)"
