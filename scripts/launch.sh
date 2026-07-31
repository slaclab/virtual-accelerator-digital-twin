#!/usr/bin/env bash
set -euo pipefail

# Finds an available port and launches the virtual accelerator container.
# Supports both Docker and Apptainer (auto-detected or via --runtime).
# Usage: ./scripts/launch.sh [--runtime docker|apptainer] [--image NAME] [--name NAME] [--detach]

IMAGE=""
CONTAINER_NAME=""
DETACH=""
RUNTIME=""
START_PORT=5075
STEP=2
MAX_PORT=65533

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime)  RUNTIME="$2"; shift 2 ;;
        --image)    IMAGE="$2"; shift 2 ;;
        --name)     CONTAINER_NAME="$2"; shift 2 ;;
        --detach|-d) DETACH="-d"; shift ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --runtime docker|apptainer   Container runtime (auto-detected if omitted)"
            echo "  --image NAME                 Image name or SIF path"
            echo "  --name NAME                  Container/instance name"
            echo "  --detach, -d                 Run in background"
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Auto-detect runtime
if [ -z "$RUNTIME" ]; then
    if command -v apptainer &>/dev/null; then
        RUNTIME="apptainer"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        echo "ERROR: neither apptainer nor docker found in PATH" >&2
        exit 1
    fi
fi

# Default image based on runtime
if [ -z "$IMAGE" ]; then
    case "$RUNTIME" in
        apptainer) IMAGE="${VA_IMAGE:-virtual-accelerator.sif}" ;;
        docker)    IMAGE="${VA_IMAGE:-va-digital-twin}" ;;
    esac
fi

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

echo "==> Free port found: $PORT"
echo "==> Runtime: $RUNTIME"
echo ""
echo "To connect from another terminal:"
echo "  source scripts/epics_env.sh $PORT"
echo ""

case "$RUNTIME" in
    docker)
        ARGS=(run --rm -p "$PORT:$PORT")
        [ -n "$DETACH" ] && ARGS+=($DETACH)
        [ -n "$CONTAINER_NAME" ] && ARGS+=(--name "$CONTAINER_NAME")
        ARGS+=("$IMAGE" "$PORT")
        echo "==> docker ${ARGS[*]}"
        if [ -n "$DETACH" ]; then
            docker "${ARGS[@]}"
            echo "Container running in background on port $PORT."
        else
            exec docker "${ARGS[@]}"
        fi
        ;;
    apptainer)
        ARGS=(run --writable-tmpfs --pwd /app)
        if [ -n "$DETACH" ]; then
            # Apptainer uses "instance start" for background
            INST_NAME="${CONTAINER_NAME:-va-dt-$PORT}"
            apptainer instance start --writable-tmpfs "$IMAGE" "$INST_NAME"
            apptainer exec "instance://$INST_NAME" /app/entrypoint.sh "$PORT" &
            echo "Apptainer instance '$INST_NAME' running in background on port $PORT."
            echo "  Stop with: apptainer instance stop $INST_NAME"
        else
            [ -n "$CONTAINER_NAME" ] && echo "(note: --name ignored in foreground apptainer mode)"
            exec apptainer run --writable-tmpfs --pwd /app "$IMAGE" "$PORT"
        fi
        ;;
    *)
        echo "ERROR: unknown runtime '$RUNTIME' (use docker or apptainer)" >&2
        exit 1
        ;;
esac
