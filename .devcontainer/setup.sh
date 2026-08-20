#!/bin/bash
# Runs once after container creation (postCreateCommand).
# Installs workspace Python deps on top of the Dockerfile base.
set -e

echo "[devcontainer] Installing workspace dependencies..."

# Install runtime + test deps declared in pyproject.toml
# (prometheus-client, pytest, numpy — everything else came from Dockerfile)
pip install -e ".[test]" \
    --quiet \
    --root-user-action=ignore \
    --no-warn-script-location

echo "[devcontainer] Dependencies installed."

# Verify key imports work
python -c "
import prometheus_client, pytest, numpy
print('[devcontainer] prometheus_client OK')
print('[devcontainer] pytest OK')
print('[devcontainer] numpy OK')
"

# Verify model stack is importable
python -c "
import os
os.environ.setdefault('LCLS_LATTICE', '/opt/lcls-lattice')
from lume_pva.runner import Runner
from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model
print('[devcontainer] lume_pva + virtual_accelerator OK')
" 2>/dev/null || echo "[devcontainer] WARNING: model stack not fully available (expected during build)"

echo "[devcontainer] Setup complete."
