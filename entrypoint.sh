#!/usr/bin/env bash
set -euo pipefail

# Base PVA port: CLI arg > PVA_PORT env var > default 5075
PORT="${1:-${PVA_PORT:-5075}}"

# EPICS_PVAS_* (with S) = server-side config (what pvxs listens on)
export EPICS_PVAS_SERVER_PORT="$PORT"
export EPICS_PVAS_BROADCAST_PORT="$((PORT + 1))"

# EPICS_PVA_* (no S) = client-side config (for any in-container clients)
export EPICS_PVA_SERVER_PORT="$PORT"
export EPICS_PVA_BROADCAST_PORT="$((PORT + 1))"

echo "PVA server listening on port: $PORT (tcp), broadcast: $((PORT + 1)) (udp)"

exec python run.py
