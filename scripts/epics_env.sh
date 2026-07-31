#!/usr/bin/env bash
# Source this script in a client terminal to connect to a specific PVA instance.
# Usage: source scripts/epics_env.sh <port>
#   e.g. source scripts/epics_env.sh 5175

if [ -z "${1:-}" ]; then
    echo "Usage: source epics_env.sh <base_port>"
    echo "  e.g. source epics_env.sh 5175"
    return 1 2>/dev/null || exit 1
fi

export EPICS_PVA_NAME_SERVERS="127.0.0.1:$1"
export EPICS_PVA_ADDR_LIST="127.0.0.1"
export EPICS_PVA_AUTO_ADDR_LIST="NO"
export EPICS_PVA_SERVER_PORT="$1"
export EPICS_PVA_BROADCAST_PORT="$(($1 + 1))"

echo "EPICS PVA client configured — name server: 127.0.0.1:$1, addr_list: 127.0.0.1"
