"""Local tests for Prometheus metrics using prometheus_client.

No pytao/bmad/p4p required — tests only the metrics definitions and HTTP server.
"""

import socket
import time
import urllib.request

import pytest
from prometheus_client import REGISTRY, generate_latest, CollectorRegistry

REQUIRED_METRICS = [
    "va_rss_bytes",
    "va_anon_bytes",
    "va_anon_huge_pages_bytes",
    "va_uptime_seconds",
    "va_thp_disabled",
    "va_runner_queue_size",
    "va_snapshot_cycles_total",
    "va_gc_collects_total",
    "va_pv_posts_total",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _parse_metric_names(text: str) -> set:
    names = set()
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        names.add(line.split("{")[0].split()[0])
    return names


class TestMetricsRegistered:
    def test_all_required_metrics_registered(self, run_module):
        """All va_* metrics must be registered in the default prometheus registry.

        prometheus_client only emits label-based counters (va_pv_posts) after
        the first .labels().inc() call. Trigger one to force emission.
        """
        run_module._VA_PV_POSTS.labels(pv="__test__").inc(0)
        text = generate_latest(REGISTRY).decode()
        names = _parse_metric_names(text)
        for metric in REQUIRED_METRICS:
            # prometheus_client appends _total to counters, _sum/_count to histograms
            base = metric.replace("_total", "")
            found = any(n == metric or n.startswith(base) for n in names)
            assert found, f"Metric {metric!r} not found in registry. Available: {sorted(names)}"

    def test_prometheus_format_valid(self, run_module):
        text = generate_latest(REGISTRY).decode()
        assert len(text) > 0
        help_lines = [l for l in text.splitlines() if l.startswith("# HELP va_")]
        type_lines = [l for l in text.splitlines() if l.startswith("# TYPE va_")]
        assert len(help_lines) > 0
        assert len(type_lines) > 0


class TestGaugeUpdates:
    def test_rss_gauge_set(self, run_module):
        run_module._VA_RSS.set(123456789)
        text = generate_latest(REGISTRY).decode()
        assert "va_rss_bytes 1.23456789e+08" in text or "123456789.0" in text or "1.23456789e+08" in text

    def test_thp_disabled_gauge(self, run_module):
        run_module._VA_THP_DISABLED.set(1)
        text = generate_latest(REGISTRY).decode()
        assert "va_thp_disabled 1.0" in text

    def test_anon_huge_pages_zero(self, run_module):
        run_module._VA_AHP.set(0)
        text = generate_latest(REGISTRY).decode()
        assert "va_anon_huge_pages_bytes 0.0" in text


class TestCounterUpdates:
    def test_snapshot_counter_increments(self, run_module):
        run_module._VA_SNAP_CYCLES.inc(10)
        text = generate_latest(REGISTRY).decode()
        # Counter value must be > 0
        for line in text.splitlines():
            if line.startswith("va_snapshot_cycles_total"):
                assert float(line.split()[-1]) > 0
                return
        pytest.fail("va_snapshot_cycles_total not found")

    def test_pv_posts_counter_with_label(self, run_module):
        run_module._VA_PV_POSTS.labels(pv="BPMS:IN20:581:TMIT").inc(5)
        text = generate_latest(REGISTRY).decode()
        assert 'pv="BPMS:IN20:581:TMIT"' in text


class TestMetricsHTTPServer:
    def test_metrics_endpoint_returns_200(self, run_module):
        from prometheus_client import start_http_server
        port = _free_port()
        start_http_server(port)
        time.sleep(0.2)

        url = f"http://localhost:{port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/plain" in content_type
            body = resp.read().decode()
            assert "va_rss_bytes" in body

    def test_metrics_endpoint_content(self, run_module):
        from prometheus_client import start_http_server
        port = _free_port()
        start_http_server(port)
        time.sleep(0.2)

        run_module._VA_THP_DISABLED.set(1)
        with urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=5) as resp:
            body = resp.read().decode()
        assert "va_thp_disabled" in body
        assert "va_rss_bytes" in body
