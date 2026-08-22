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

# pvxs requires IP addresses (not hostnames) in EPICS_PVA_NAME_SERVERS.
# Resolve any hostname to its IP at startup.
if [ -n "${EPICS_PVA_NAME_SERVERS:-}" ]; then
    resolved=""
    for entry in $EPICS_PVA_NAME_SERVERS; do
        host="${entry%%:*}"
        port="${entry##*:}"
        # If it's already an IP, keep it; otherwise resolve via getent
        if echo "$host" | grep -qP '^\d+\.\d+\.\d+\.\d+$'; then
            resolved="${resolved:+$resolved }${entry}"
        else
            ip=$(getent hosts "$host" | awk '{print $1; exit}')
            if [ -n "$ip" ]; then
                resolved="${resolved:+$resolved }${ip}:${port}"
                echo "Resolved $host -> $ip"
            else
                echo "WARNING: Could not resolve $host, skipping"
            fi
        fi
    done
    export EPICS_PVA_NAME_SERVERS="$resolved"
    echo "EPICS_PVA_NAME_SERVERS=$EPICS_PVA_NAME_SERVERS"
fi

echo "PVA server listening on port: $PORT (tcp), broadcast: $((PORT + 1)) (udp)"

# Limit glibc to 2 arenas. Must be set before Python starts — glibc reads
# MALLOC_ARENA_MAX at first malloc(), which happens during interpreter init,
# so os.environ.setdefault() inside Python is always too late.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

exec python run.py
