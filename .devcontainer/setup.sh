#!/bin/bash
# Runs once after container creation (postCreateCommand).
# Test deps are already installed in the devcontainer image stage.
# This script just verifies the environment is complete.
set -e

echo "[devcontainer] Verifying environment..."

python -c "
import prometheus_client, pytest, numpy
print('[devcontainer] prometheus_client OK')
print('[devcontainer] pytest OK')
print('[devcontainer] numpy OK')
"

python -c "
import os
os.environ.setdefault('LCLS_LATTICE', '/opt/lcls-lattice')
from lume_pva.runner import Runner
from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model
print('[devcontainer] lume_pva + virtual_accelerator OK')
" 2>/dev/null || echo "[devcontainer] WARNING: model stack check failed"

echo "[devcontainer] Environment ready."
