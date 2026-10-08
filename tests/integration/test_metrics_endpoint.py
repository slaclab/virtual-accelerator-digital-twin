"""Integration tests for the Prometheus /metrics endpoint.

Migrated from scripts/metrics_smoke_test.py.
Runs inside the pv-client container via docker-compose.integration.yml.

Can also be invoked as a standalone script:
    python tests/integration/test_metrics_endpoint.py
"""

import os
import sys
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

METRICS_HOST = os.environ.get("METRICS_HOST", "localhost")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))
METRICS_TIMEOUT = float(os.environ.get("METRICS_TIMEOUT", "10"))
METRICS_RETRIES = int(os.environ.get("METRICS_RETRIES", "12"))
METRICS_RETRY_INTERVAL = float(os.environ.get("METRICS_RETRY_INTERVAL", "5"))

REQUIRED_METRICS = [
    "va_rss_bytes",
    "va_anon_bytes",
    "va_anon_huge_pages_bytes",
    "va_uptime_seconds",
    "va_thp_disabled",
    "va_runner_queue_size",
    "va_snapshot_cycles_total",
    "va_gc_collect_total",
    "va_pv_posts_total",
]


def fetch_metrics(host: str = METRICS_HOST, port: int = METRICS_PORT,
                  timeout: float = METRICS_TIMEOUT) -> str:
    url = f"http://{host}:{port}/metrics"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def parse_metrics(text: str) -> dict:
    """Parse Prometheus text format → {metric_name: value}."""
    metrics = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0].split("{")[0]
            try:
                metrics[name] = float(parts[-1])
            except ValueError:
                metrics[name] = parts[-1]
    return metrics


def wait_for_metrics(retries: int = METRICS_RETRIES,
                     interval: float = METRICS_RETRY_INTERVAL) -> str:
    """Poll /metrics until reachable or retries exhausted."""
    for attempt in range(1, retries + 1):
        try:
            return fetch_metrics()
        except Exception as e:
            if attempt == retries:
                pytest.fail(f"Could not reach /metrics after {retries} attempts: {e}")
            time.sleep(interval)


# ---------------------------------------------------------------------------
# Pytest tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def metrics_text():
    return wait_for_metrics()


@pytest.fixture(scope="module")
def metrics(metrics_text):
    return parse_metrics(metrics_text)


class TestRequiredMetrics:
    @pytest.mark.parametrize("name", REQUIRED_METRICS)
    def test_metric_present(self, metrics, name):
        assert name in metrics, f"Required metric missing: {name}"

    def test_rss_bytes_positive(self, metrics):
        assert metrics["va_rss_bytes"] > 0

    def test_thp_disabled(self, metrics):
        assert metrics["va_thp_disabled"] == 1.0, (
            f"THP not disabled — memory leak risk! va_thp_disabled={metrics['va_thp_disabled']}"
        )

    def test_anon_huge_pages_zero(self, metrics):
        assert metrics["va_anon_huge_pages_bytes"] == 0.0, (
            f"AnonHugePages non-zero: {metrics['va_anon_huge_pages_bytes']} bytes — THP may be active"
        )

    def test_content_type_header(self):
        url = f"http://{METRICS_HOST}:{METRICS_PORT}/metrics"
        with urllib.request.urlopen(url, timeout=METRICS_TIMEOUT) as resp:
            ct = resp.headers.get("Content-Type", "")
            assert "text/plain" in ct


# ---------------------------------------------------------------------------
# Standalone entry point (backward compat with docker-compose.yml metrics-check)
# ---------------------------------------------------------------------------

def _standalone_main() -> int:
    print("--- Prometheus metrics smoke test ---", flush=True)
    print(f"Endpoint: http://{METRICS_HOST}:{METRICS_PORT}/metrics", flush=True)

    text = None
    for attempt in range(1, METRICS_RETRIES + 1):
        try:
            text = fetch_metrics()
            print(f"OK: /metrics reachable ({len(text)} bytes)", flush=True)
            break
        except Exception as e:
            print(f"Attempt {attempt}/{METRICS_RETRIES}: {e}", flush=True)
            if attempt < METRICS_RETRIES:
                time.sleep(METRICS_RETRY_INTERVAL)

    if text is None:
        print("FAIL: could not reach /metrics endpoint", flush=True)
        return 1

    metrics = parse_metrics(text)
    failures = []

    for name in REQUIRED_METRICS:
        if name in metrics:
            print(f"OK: {name} = {metrics[name]}", flush=True)
        else:
            print(f"FAIL: {name} missing", flush=True)
            failures.append(name)

    thp = metrics.get("va_thp_disabled", -1)
    if thp == 1.0:
        print("OK: va_thp_disabled=1 (THP fix active)", flush=True)
    elif thp == 0.0:
        print("WARN: va_thp_disabled=0 (THP still enabled — memory leak risk)", flush=True)

    ahp = metrics.get("va_anon_huge_pages_bytes", -1)
    if ahp == 0.0:
        print("OK: va_anon_huge_pages_bytes=0 (no huge pages)", flush=True)
    elif ahp > 0:
        print(f"WARN: va_anon_huge_pages_bytes={ahp} (huge pages present)", flush=True)

    print("--- Result ---", flush=True)
    if failures:
        print(f"METRICS SMOKE TEST FAILED: missing {failures}", flush=True)
        return 1

    print(f"METRICS SMOKE TEST PASSED: all {len(REQUIRED_METRICS)} metrics present", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(_standalone_main())
