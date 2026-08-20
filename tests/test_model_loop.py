"""Test model set/get loop memory behaviour using a FakeModel.

No pytao/bmad/p4p required — tests the allocation pattern of the runner
loop using pure numpy. Verifies RSS stays bounded after N cycles.
"""

import resource
import threading
import time

import numpy as np
import pytest


class FakeVariable:
    """Minimal variable descriptor matching lume Variable interface."""
    def __init__(self, name, read_only=False):
        self.name = name
        self.read_only = read_only


class FakeModel:
    """Pure-numpy model that mimics the LUMEModel set/get interface."""

    def __init__(self, n_outputs=20, array_size=100):
        self._state = {}
        self._n_outputs = n_outputs
        self._array_size = array_size
        self.supported_variables = {
            "QUAD:IN20:631:BCTRL": FakeVariable("QUAD:IN20:631:BCTRL", read_only=False),
            "QUAD:IN20:651:BCTRL": FakeVariable("QUAD:IN20:651:BCTRL", read_only=False),
            "XCOR:IN20:641:BCTRL": FakeVariable("XCOR:IN20:641:BCTRL", read_only=False),
            **{f"output_{i}": FakeVariable(f"output_{i}", read_only=True) for i in range(n_outputs)},
        }
        for name in self.supported_variables:
            self._state[name] = np.zeros(array_size)

    def set(self, values: dict) -> None:
        for k, v in values.items():
            if k in self._state:
                self._state[k] = np.asarray(v)

    def get(self, names) -> dict:
        if isinstance(names, dict):
            names = list(names.keys())
        result = {}
        for n in names:
            if n not in self._state:
                continue
            # writable inputs return stored state; outputs return new random arrays
            var = self.supported_variables.get(n)
            if var and not var.read_only:
                result[n] = self._state[n].copy()
            else:
                result[n] = np.random.rand(self._array_size)
        return result

    def reset(self) -> None:
        for name in self._state:
            self._state[name] = np.zeros(self._array_size)


def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS returns bytes; Linux returns KB
    import sys
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def _simulate_runner_loop(model: FakeModel, n_cycles: int) -> None:
    """Mimic the lume_pva runner._run() loop without any network/PVA."""
    settable = [n for n, v in model.supported_variables.items() if not v.read_only]
    all_vars = list(model.supported_variables.keys())

    for _ in range(n_cycles):
        # Mimic _set_cached_state
        cached = model.get(settable)
        # Mimic model.set() with new values
        new_values = {k: np.random.rand(model._array_size) for k in settable}
        model.set(new_values)
        # Mimic model.get(supported_variables) -> out_values
        out_values = model.get(all_vars)
        # Mimic pv.post() — just consume the value
        for v in out_values.values():
            _ = v.sum()
        del cached, new_values, out_values


class TestFakeModelLoop:
    def test_model_set_get_correct(self):
        model = FakeModel(n_outputs=5, array_size=10)
        model.set({"QUAD:IN20:631:BCTRL": np.ones(10) * 7.3})
        state = model.get(["QUAD:IN20:631:BCTRL"])
        assert np.allclose(state["QUAD:IN20:631:BCTRL"], 7.3)

    def test_model_get_returns_all_vars(self):
        model = FakeModel(n_outputs=5, array_size=10)
        result = model.get(model.supported_variables)
        assert set(result.keys()) == set(model.supported_variables.keys())

    def test_runner_loop_rss_bounded(self):
        """RSS after 500 cycles must not exceed RSS before + 50 MB."""
        model = FakeModel(n_outputs=50, array_size=1000)

        # Warm up — let allocator stabilize
        _simulate_runner_loop(model, n_cycles=20)
        import gc
        gc.collect()

        rss_before = _rss_mb()
        _simulate_runner_loop(model, n_cycles=500)
        gc.collect()
        rss_after = _rss_mb()

        growth_mb = rss_after - rss_before
        assert growth_mb < 50, (
            f"RSS grew {growth_mb:.1f} MB over 500 cycles "
            f"(before={rss_before:.1f} MB, after={rss_after:.1f} MB). "
            "Possible memory accumulation in model loop."
        )

    def test_runner_loop_threaded_rss_bounded(self):
        """Same test but runner loop runs in a background thread (like real runner)."""
        model = FakeModel(n_outputs=20, array_size=500)
        _simulate_runner_loop(model, n_cycles=10)  # warm up

        import gc
        gc.collect()
        rss_before = _rss_mb()

        done = threading.Event()

        def _thread():
            _simulate_runner_loop(model, n_cycles=300)
            done.set()

        t = threading.Thread(target=_thread, daemon=True)
        t.start()
        done.wait(timeout=30)
        gc.collect()
        rss_after = _rss_mb()

        growth_mb = rss_after - rss_before
        assert growth_mb < 50, (
            f"Threaded loop: RSS grew {growth_mb:.1f} MB over 300 cycles"
        )


class TestSmapsRollup:
    def test_read_smaps_rollup_returns_dict(self, run_module):
        result = run_module._read_smaps_rollup()
        # On Linux returns populated dict; on macOS returns empty (file missing) — both valid
        assert isinstance(result, dict)

    def test_log_memory_does_not_raise(self, run_module):
        # Should not raise on any platform
        run_module._log_memory("test-label")
