"""Shared pytest fixtures.

Imports from run.py use importlib to avoid triggering pytao/lume_pva imports
that are not available in the local dev environment.
"""

import importlib.util
import sys
import types
import pytest


def _load_run_module():
    """Load run.py as a module without executing main() or importing model deps."""
    spec = importlib.util.spec_from_file_location(
        "run",
        "run.py",
        submodule_search_locations=[],
    )
    mod = importlib.util.module_from_spec(spec)
    # Stub heavy imports so the module-level code doesn't fail locally
    for name in ("lume_pva", "lume_pva.runner", "virtual_accelerator",
                 "virtual_accelerator.models", "virtual_accelerator.models.cu_hxr",
                 "virtual_accelerator.models.facet2", "torch"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def run_module():
    return _load_run_module()
