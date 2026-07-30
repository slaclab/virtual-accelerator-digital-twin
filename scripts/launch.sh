#!/usr/bin/env bash
set -euo pipefail

# Finds an available port and launches the virtual accelerator container.
# Usage: ./scripts/launch.sh [--image NAME] [--name NAME] [--detach]

IMAGE="${VA_IMAGE:-va-digital-twin}"
CONTAINER_NAME=""
DETACH=""
START_PORT=5075
STEP=2
MAX_PORT=65533

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)  IMAGE="$2"; shift 2 ;;
        --name)   CONTAINER_NAME="$2"; shift 2 ;;
        --detach|-d) DETACH="-d"; shift ;;
        -h|--help)
            echo "Usage: $0 [--image NAME] [--name NAME] [--detach]"
            echo "  Finds a free port starting from $START_PORT and launches the container."
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

find_free_port() {
    local port=$START_PORT
    while [ "$port" -lt "$MAX_PORT" ]; do
        if python3 -c "import socket; s=socket.socket(); s.bind(('',$port)); s.close()" 2>/dev/null; then
            echo "$port"
            return 0
        fi
        port=$((port + STEP))
    done
    echo "ERROR: no free port found in range $START_PORT–$MAX_PORT" >&2
    return 1
}

PORT=$(find_free_port)

DOCKER_ARGS=(run --rm -p "$PORT:$PORT")
[ -n "$DETACH" ] && DOCKER_ARGS+=($DETACH)
[ -n "$CONTAINER_NAME" ] && DOCKER_ARGS+=(--name "$CONTAINER_NAME")
DOCKER_ARGS+=("$IMAGE" "$PORT")

echo "==> Free port found: $PORT"
echo "==> Starting: docker ${DOCKER_ARGS[*]}"
echo ""
echo "To connect from another terminal:"
echo "  source scripts/epics_env.sh $PORT"
echo ""

if [ -n "$DETACH" ]; then
    docker "${DOCKER_ARGS[@]}"
    echo "Container running in background on port $PORT."
else
    exec docker "${DOCKER_ARGS[@]}"
fi
