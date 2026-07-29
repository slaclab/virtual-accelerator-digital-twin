#!/usr/bin/env bash
set -euo pipefail

# Base PVA port: CLI arg > PVA_PORT env var > default 5075
PORT="${1:-${PVA_PORT:-5075}}"

export EPICS_PVA_SERVER_PORT="$PORT"
export EPICS_PVA_BROADCAST_PORT="$((PORT + 1))"

echo "PVA server port: $EPICS_PVA_SERVER_PORT (tcp), broadcast: $EPICS_PVA_BROADCAST_PORT (udp)"

exec python run.py
