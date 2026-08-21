"""Integration tests: runs inside the pv-client container.

Requires docker-compose.integration.yml stack to be up:
  - virtual-accelerator: real VA model serving PVs on port 5075
  - The container runs with EPICS_PVA_ADDR_LIST=virtual-accelerator

Run via:
  docker compose -f docker-compose.integration.yml run --rm pv-client
"""

import math
import os
import threading
import time
import urllib.request

import pytest

pytestmark = pytest.mark.integration

VA_HOST = os.environ.get("VA_HOST", "virtual-accelerator")
METRICS_HOST = os.environ.get("METRICS_HOST", "virtual-accelerator")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))

# PVs served by cu_hxr_bmad model (subset)
OUTPUT_PVS = [
    "BPMS:IN20:581:TMIT",
    "BPMS:IN20:581:X",
    "BPMS:IN20:581:Y",
]

STARTUP_TIMEOUT = float(os.environ.get("VA_STARTUP_TIMEOUT", "180"))


@pytest.fixture(scope="module")
def pva_context():
    from p4p.client.thread import Context
    ctx = Context("pva")
    yield ctx
    ctx.close()


def _wait_for_pv(ctx, pv_name: str, timeout: float) -> object:
    """Poll until PV returns a finite value or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            val = ctx.get(pv_name, timeout=10)
            if val is not None:
                return val
        except Exception:
            pass
        time.sleep(5)
    pytest.fail(f"PV {pv_name!r} did not become available within {timeout}s")


def _fetch_metrics() -> str:
    url = f"http://{METRICS_HOST}:{METRICS_PORT}/metrics"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode()


class TestPVConnectivity:
    def test_output_pvs_return_values(self, pva_context):
        """VA output PVs must return finite numeric values after warmup."""
        for pv in OUTPUT_PVS:
            val = _wait_for_pv(pva_context, pv, timeout=STARTUP_TIMEOUT)
            try:
                assert math.isfinite(float(val)), f"{pv} returned non-finite: {val}"
            except (TypeError, ValueError):
                pytest.fail(f"{pv} returned non-numeric value: {val!r}")

    def test_model_info_pv_reachable(self, pva_context):
        val = _wait_for_pv(pva_context, "model_info", timeout=STARTUP_TIMEOUT)
        assert val is not None


class TestPVUpdates:
    def test_pv_updates_received_over_time(self, pva_context):
        """Subscribe to PVs for 10s and assert updates are received."""
        updates = {pv: 0 for pv in OUTPUT_PVS}
        lock = threading.Lock()
        subs = []

        def _cb(value, pv_name):
            with lock:
                updates[pv_name] += 1

        # Wait for PVs to be up first
        _wait_for_pv(pva_context, OUTPUT_PVS[0], timeout=STARTUP_TIMEOUT)

        for pv in OUTPUT_PVS:
            sub = pva_context.monitor(pv, lambda v, n=pv: _cb(v, n))
            subs.append(sub)

        time.sleep(10)

        for sub in subs:
            sub.close()

        with lock:
            counts = dict(updates)

        for pv, count in counts.items():
            assert count > 0, f"No updates received for {pv} in 10s"


class TestMetricsEndpoint:
    def test_metrics_reachable(self):
        text = _fetch_metrics()
        assert len(text) > 0
        assert "va_rss_bytes" in text

    def test_thp_disabled_on_linux(self):
        text = _fetch_metrics()
        # THP should be disabled — va_thp_disabled must be 1
        for line in text.splitlines():
            if line.startswith("va_thp_disabled"):
                val = float(line.split()[-1])
                assert val == 1.0, f"THP not disabled! va_thp_disabled={val}"
                return
        pytest.fail("va_thp_disabled metric not found")

    def test_anon_huge_pages_zero(self):
        text = _fetch_metrics()
        for line in text.splitlines():
            if line.startswith("va_anon_huge_pages_bytes"):
                val = float(line.split()[-1])
                assert val == 0.0, f"AnonHugePages non-zero: {val} bytes"
                return
        pytest.fail("va_anon_huge_pages_bytes metric not found")

    def test_pv_posts_counter_increments(self):
        """Wait 5s and verify pv_posts counter increased."""
        text1 = _fetch_metrics()
        time.sleep(5)
        text2 = _fetch_metrics()

        def _total_posts(text):
            total = 0
            for line in text.splitlines():
                if line.startswith("va_pv_posts_total"):
                    try:
                        total += float(line.split()[-1])
                    except ValueError:
                        pass
            return total

        posts1 = _total_posts(text1)
        posts2 = _total_posts(text2)
        assert posts2 > posts1, (
            f"pv_posts_total did not increase: {posts1} → {posts2}"
        )
