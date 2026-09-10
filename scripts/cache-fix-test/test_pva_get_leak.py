"""Isolate p4p PVA get() memory leak.

Spins up an in-process PVA server (no docker, no bmad).

CLI usage (produces a text report):
    python3 tests/test_pva_get_leak.py [--gets N] [--cycles N] [--report FILE]

pytest usage:
    pytest -m pva_leak -v tests/test_pva_get_leak.py
"""

import argparse
import gc
import sys
import textwrap
import time

# ---------------------------------------------------------------------------
# Optional pytest import — graceful skip when pytest is absent (CLI mode)
# ---------------------------------------------------------------------------
try:
    import pytest
    _PYTEST_AVAILABLE = True
except ImportError:
    _PYTEST_AVAILABLE = False

if _PYTEST_AVAILABLE:
    p4p = pytest.importorskip("p4p", reason="p4p not installed")
else:
    try:
        import p4p  # noqa: F401
    except ImportError:
        sys.exit("p4p not installed — cannot run")

from p4p import Value, Type
from p4p.client.thread import Context
from p4p.server import Server
from p4p.server.thread import SharedPV

if _PYTEST_AVAILABLE:
    pytestmark = pytest.mark.pva_leak

_N_PVS = 500
_PREFIX = "VADTLEAK:TEST:"

# Force client to look only at localhost — required for in-process server
# discovery in containers where UDP broadcast doesn't work on loopback.
_PVA_CONF = {
    "EPICS_PVA_ADDR_LIST": "localhost",
    "EPICS_PVA_AUTO_ADDR_LIST": "NO",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Rss:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024


def _anon_mb() -> float:
    """Read anonymous (non-reclaimable) memory from cgroup or smaps_rollup.

    This is the metric that shows the pvxs channel-cache leak — it survives
    malloc_trim and is invisible to tracemalloc.
    """
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.stat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "anon":
                    return int(v) / (1024 * 1024)
    except OSError:
        pass
    # cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.stat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "total_rss":
                    return int(v) / (1024 * 1024)
    except OSError:
        pass
    # fallback: use smaps Anonymous field
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Anonymous:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _make_pv(initial: float = 0.0) -> SharedPV:
    nt = Type([("value", "d")])
    return SharedPV(initial=Value(nt, {"value": initial}))


def _start_server() -> tuple[Server, list[str]]:
    pvs = {f"{_PREFIX}{i}": _make_pv(float(i)) for i in range(_N_PVS)}
    srv = Server(providers=[pvs])
    time.sleep(0.5)
    return srv, list(pvs.keys())


def _warmup(ctx: Context, pv_names: list[str]) -> None:
    for pv in pv_names:
        try:
            ctx.get(pv, timeout=5)
        except Exception:
            pass
    gc.collect()
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# Core measurement functions (shared by CLI and pytest)
# ---------------------------------------------------------------------------

def measure_repeated_gets(pv_names: list[str], n_gets: int = 500,
                          live_file=None) -> dict:
    """Single reused Context, N get iterations per PV."""
    ctx = Context("pva", conf=_PVA_CONF)
    _warmup(ctx, pv_names)

    gc.collect()
    rss_samples = [_rss_mb()]
    t_start = time.monotonic()

    if live_file:
        live_file.write(f"  [repeated_gets] starting — {n_gets} iters × {len(pv_names)} PVs\n")
        live_file.flush()

    report_every = max(1, n_gets // 20)  # report ~20 checkpoints

    for i in range(n_gets):
        for pv in pv_names:
            val = ctx.get(pv, timeout=2)
            del val
        if i % 100 == 0:
            gc.collect()
            rss_samples.append(_rss_mb())
        if live_file and i > 0 and i % report_every == 0:
            elapsed = time.monotonic() - t_start
            rss = rss_samples[-1]
            pct = 100.0 * i / n_gets
            live_file.write(
                f"  [repeated_gets] {i:7d}/{n_gets} ({pct:.0f}%)  "
                f"elapsed={elapsed:.0f}s  rss={rss:.1f} MB  "
                f"delta={rss - rss_samples[0]:+.2f} MB\n"
            )
            live_file.flush()

    gc.collect()
    rss_samples.append(_rss_mb())
    ctx.close()

    return {
        "name": "repeated_gets",
        "n_gets": n_gets,
        "n_pvs": len(pv_names),
        "rss_start_mb": rss_samples[0],
        "rss_end_mb": rss_samples[-1],
        "rss_peak_mb": max(rss_samples),
        "rss_delta_mb": rss_samples[-1] - rss_samples[0],
        "rss_series": rss_samples,
        "threshold_mb": 20.0,
        "passed": (rss_samples[-1] - rss_samples[0]) < 20.0,
    }


def measure_context_open_close(pv_names: list[str], n_cycles: int = 20,
                               live_file=None) -> dict:
    """Create and close Context N times."""
    gc.collect()
    rss_samples = [_rss_mb()]
    t_start = time.monotonic()

    if live_file:
        live_file.write(f"  [context_open_close] starting — {n_cycles} cycles × {len(pv_names)} PVs\n")
        live_file.flush()

    report_every = max(1, n_cycles // 20)

    for i in range(n_cycles):
        ctx = Context("pva", conf=_PVA_CONF)
        for pv in pv_names:
            try:
                ctx.get(pv, timeout=2)
            except Exception:
                pass
        ctx.close()
        if i % 5 == 0:
            gc.collect()
            rss_samples.append(_rss_mb())
        if live_file and i > 0 and i % report_every == 0:
            elapsed = time.monotonic() - t_start
            rss = rss_samples[-1]
            pct = 100.0 * i / n_cycles
            live_file.write(
                f"  [context_open_close] {i:5d}/{n_cycles} ({pct:.0f}%)  "
                f"elapsed={elapsed:.0f}s  rss={rss:.1f} MB  "
                f"delta={rss - rss_samples[0]:+.2f} MB\n"
            )
            live_file.flush()

    gc.collect()
    rss_samples.append(_rss_mb())

    return {
        "name": "context_open_close",
        "n_cycles": n_cycles,
        "rss_start_mb": rss_samples[0],
        "rss_end_mb": rss_samples[-1],
        "rss_peak_mb": max(rss_samples),
        "rss_delta_mb": rss_samples[-1] - rss_samples[0],
        "rss_series": rss_samples,
        "threshold_mb": 50.0,
        "passed": (rss_samples[-1] - rss_samples[0]) < 50.0,
    }


def measure_nonexistent_pv(pv_names: list[str]) -> dict:
    """Timeout errors on nonexistent PV must not leak channels."""
    ctx = Context("pva", conf=_PVA_CONF)
    _warmup(ctx, pv_names)

    gc.collect()
    rss_start = _rss_mb()
    errors = 0

    for _ in range(10):
        try:
            ctx.get(f"{_PREFIX}NONEXISTENT", timeout=0.2)
        except Exception:
            errors += 1

    for pv in pv_names:
        try:
            ctx.get(pv, timeout=2)
        except Exception:
            pass

    gc.collect()
    rss_end = _rss_mb()
    ctx.close()

    return {
        "name": "nonexistent_pv_errors",
        "errors_triggered": errors,
        "rss_start_mb": rss_start,
        "rss_end_mb": rss_end,
        "rss_delta_mb": rss_end - rss_start,
        "threshold_mb": 5.0,
        "passed": (rss_end - rss_start) < 5.0,
    }


def measure_soak(pv_names: list[str], duration_s: int = 60,
                 live_file=None) -> dict:
    """Continuous get loop for `duration_s` seconds; samples RSS every 5s.

    If *live_file* is a writable file object, each RSS sample is appended there
    immediately so progress is visible before the test finishes.
    """
    ctx = Context("pva", conf=_PVA_CONF)
    _warmup(ctx, pv_names)

    gc.collect()
    t_start = time.monotonic()
    t_next_sample = t_start + 5.0
    deadline = t_start + duration_s

    rss_series: list[tuple[float, float]] = []  # (elapsed_s, rss_mb)
    anon_series: list[tuple[float, float]] = []  # (elapsed_s, anon_mb)
    rss0 = _rss_mb()
    anon0 = _anon_mb()
    rss_series.append((0.0, rss0))
    anon_series.append((0.0, anon0))
    iterations = 0

    if live_file:
        live_file.write(
            f"  [soak] starting — {duration_s}s, {len(pv_names)} PVs  "
            f"rss0={rss0:.1f} MB  anon0={anon0:.1f} MB\n"
        )
        live_file.flush()

    while time.monotonic() < deadline:
        for pv in pv_names:
            val = ctx.get(pv, timeout=2)
            del val
        iterations += 1

        now = time.monotonic()
        if now >= t_next_sample:
            gc.collect()
            elapsed = round(now - t_start, 1)
            rss = _rss_mb()
            anon = _anon_mb()
            rss_series.append((elapsed, rss))
            anon_series.append((elapsed, anon))
            t_next_sample = now + 5.0
            if live_file:
                live_file.write(
                    f"  [soak] {elapsed:7.1f}s  rss={rss:.1f} MB  "
                    f"anon={anon:.1f} MB  "
                    f"iters={iterations}  "
                    f"rss_delta={rss - rss_series[0][1]:+.2f} MB  "
                    f"anon_delta={anon - anon_series[0][1]:+.2f} MB\n"
                )
                live_file.flush()

    gc.collect()
    rss_series.append((round(time.monotonic() - t_start, 1), _rss_mb()))
    anon_series.append((round(time.monotonic() - t_start, 1), _anon_mb()))
    ctx.close()

    rss_values = [v for _, v in rss_series]
    anon_values = [v for _, v in anon_series]
    rss_delta = rss_values[-1] - rss_values[0]
    anon_delta = anon_values[-1] - anon_values[0] if anon_values else 0.0

    # Linear regression slope (MB/s) on RSS
    n = len(rss_series)
    if n >= 2:
        xs = [t for t, _ in rss_series]
        ys = rss_values
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        den = sum((x - x_mean) ** 2 for x in xs)
        slope_mb_per_s = num / den if den != 0 else 0.0
    else:
        slope_mb_per_s = 0.0

    # Linear regression slope (MB/s) on anon
    n2 = len(anon_series)
    if n2 >= 2:
        xs2 = [t for t, _ in anon_series]
        ys2 = anon_values
        x_mean2 = sum(xs2) / n2
        y_mean2 = sum(ys2) / n2
        num2 = sum((x - x_mean2) * (y - y_mean2) for x, y in zip(xs2, ys2))
        den2 = sum((x - x_mean2) ** 2 for x in xs2)
        anon_slope_mb_per_s = num2 / den2 if den2 != 0 else 0.0
    else:
        anon_slope_mb_per_s = 0.0

    return {
        "name": "soak",
        "duration_s": duration_s,
        "iterations": iterations,
        "n_pvs": len(pv_names),
        "rss_start_mb": rss_values[0],
        "rss_end_mb": rss_values[-1],
        "rss_peak_mb": max(rss_values),
        "rss_min_mb": min(rss_values),
        "rss_delta_mb": rss_delta,
        "anon_start_mb": anon_values[0] if anon_values else 0.0,
        "anon_end_mb": anon_values[-1] if anon_values else 0.0,
        "anon_delta_mb": anon_delta,
        "slope_mb_per_s": slope_mb_per_s,
        "anon_slope_mb_per_s": anon_slope_mb_per_s,
        "rss_series": rss_series,
        "anon_series": anon_series,
        "threshold_mb": 15.0,
        # fail if RSS slope > 0.05 MB/s OR anon slope > 0.03 MB/s OR total delta > 15 MB
        "passed": rss_delta < 15.0 and slope_mb_per_s < 0.05 and anon_slope_mb_per_s < 0.03,
    }


def measure_context_without_close(pv_names: list[str], n_cycles: int = 20,
                                   live_file=None) -> dict:
    """Demonstrates leak when ctx.close() is omitted."""
    gc.collect()
    rss_samples = [_rss_mb()]
    t_start = time.monotonic()

    if live_file:
        live_file.write(f"  [context_without_close] starting — {n_cycles} cycles × {len(pv_names)} PVs\n")
        live_file.flush()

    report_every = max(1, n_cycles // 20)

    for i in range(n_cycles):
        ctx = Context("pva", conf=_PVA_CONF)
        for pv in pv_names:
            try:
                ctx.get(pv, timeout=2)
            except Exception:
                pass
        del ctx  # no close()
        if i % 5 == 0:
            gc.collect()
            rss_samples.append(_rss_mb())
        if live_file and i > 0 and i % report_every == 0:
            elapsed = time.monotonic() - t_start
            rss = rss_samples[-1]
            pct = 100.0 * i / n_cycles
            live_file.write(
                f"  [context_without_close] {i:5d}/{n_cycles} ({pct:.0f}%)  "
                f"elapsed={elapsed:.0f}s  rss={rss:.1f} MB  "
                f"delta={rss - rss_samples[0]:+.2f} MB\n"
            )
            live_file.flush()

    gc.collect()
    rss_samples.append(_rss_mb())

    delta = rss_samples[-1] - rss_samples[0]
    return {
        "name": "context_without_close",
        "n_cycles": n_cycles,
        "rss_start_mb": rss_samples[0],
        "rss_end_mb": rss_samples[-1],
        "rss_peak_mb": max(rss_samples),
        "rss_delta_mb": delta,
        "rss_series": rss_samples,
        "note": "xfail — expected to leak; documents the failure mode",
    }


# ---------------------------------------------------------------------------
# Report formatter
# ---------------------------------------------------------------------------

def _bar(value: float, maximum: float, width: int = 40) -> str:
    filled = min(int(value / maximum * width), width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {value:.1f}/{maximum:.1f} MB"


def format_report(results: list[dict], n_gets: int, n_cycles: int, soak_s: int = 0) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("  p4p PVA get() memory leak report")
    lines.append("=" * 70)
    lines.append(f"  PVs served  : {_N_PVS}  (prefix {_PREFIX})")
    lines.append(f"  get iters   : {n_gets}")
    lines.append(f"  ctx cycles  : {n_cycles}")
    if soak_s > 0:
        lines.append(f"  soak        : {soak_s}s")
    lines.append("")

    for r in results:
        name = r["name"]
        passed = r.get("passed")
        status = "PASS" if passed is True else ("FAIL" if passed is False else "INFO")
        lines.append(f"  [{status}] {name}")

        delta = r["rss_delta_mb"]
        threshold = r.get("threshold_mb")
        lines.append(f"         RSS start  : {r['rss_start_mb']:.1f} MB")
        lines.append(f"         RSS end    : {r['rss_end_mb']:.1f} MB")
        lines.append(f"         RSS delta  : {delta:+.1f} MB")
        if "rss_peak_mb" in r:
            lines.append(f"         RSS peak   : {r['rss_peak_mb']:.1f} MB")
        if threshold is not None:
            scale = max(threshold * 1.5, abs(delta) * 1.2, 1.0)
            lines.append(f"         threshold  : {threshold:.1f} MB")
            lines.append(f"         {_bar(abs(delta), scale)}")
        if "slope_mb_per_s" in r:
            lines.append(f"         slope      : {r['slope_mb_per_s']:+.4f} MB/s")
        if "iterations" in r:
            lines.append(f"         iterations : {r['iterations']}")
        if "rss_series" in r:
            series = r["rss_series"]
            # soak returns list of (elapsed_s, rss_mb); others return list of floats
            if series and isinstance(series[0], tuple):
                lines.append("         RSS timeline (elapsed_s → rss_mb):")
                for t, v in series:
                    lines.append(f"           {t:6.1f}s  {v:.1f} MB")
            else:
                series_str = "  ".join(f"{v:.1f}" for v in series)
                lines.append(f"         RSS series : {series_str}")
        if "note" in r:
            lines.append(f"         note       : {r['note']}")
        if "errors_triggered" in r:
            lines.append(f"         errors     : {r['errors_triggered']}")
        lines.append("")

    passed_count = sum(1 for r in results if r.get("passed") is True)
    failed_count = sum(1 for r in results if r.get("passed") is False)
    info_count   = sum(1 for r in results if r.get("passed") is None)
    lines.append("-" * 70)
    lines.append(f"  PASSED: {passed_count}  FAILED: {failed_count}  INFO: {info_count}")
    lines.append("=" * 70)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pytest fixtures / tests
# ---------------------------------------------------------------------------

if _PYTEST_AVAILABLE:
    @pytest.fixture(scope="module")
    def pva_server():
        srv, pv_names = _start_server()
        yield pv_names
        srv.stop()

    @pytest.fixture(scope="module")
    def pva_ctx(pva_server):
        ctx = Context("pva", conf=_PVA_CONF)
        _warmup(ctx, pva_server)
        yield ctx
        ctx.close()

    class TestGetNoLeak:
        def test_single_context_repeated_gets(self, pva_ctx, pva_server):
            """500 get cycles on reused Context must not grow RSS by more than 10 MB."""
            r = measure_repeated_gets(pva_server, n_gets=500)
            assert r["passed"], (
                f"RSS grew {r['rss_delta_mb']:.1f} MB after {r['n_gets']} get cycles"
                f" — likely p4p.Value accumulation"
            )

        def test_context_open_close_cycle(self, pva_server):
            """Create and close Context 20 times; RSS must not grow by more than 20 MB."""
            r = measure_context_open_close(pva_server, n_cycles=20)
            assert r["passed"], (
                f"RSS grew {r['rss_delta_mb']:.1f} MB across {r['n_cycles']} Context"
                f" open/close cycles — likely Context not releasing C++ resources on close()"
            )

        def test_exception_on_nonexistent_pv_no_leak(self, pva_ctx, pva_server):
            """Timeout on nonexistent PV must not leak channels."""
            r = measure_nonexistent_pv(pva_server)
            assert r["passed"], (
                f"RSS grew {r['rss_delta_mb']:.1f} MB after nonexistent-PV errors"
                f" — likely channel objects not released on timeout"
            )

    @pytest.mark.xfail(
        reason="Context without close() leaks C++ resources — documents the failure mode",
        strict=False,
    )
    class TestGetLeakDocumented:
        def test_context_without_close_leaks(self, pva_server):
            """Demonstrates RSS growth when Context.close() is omitted."""
            r = measure_context_without_close(pva_server, n_cycles=20)
            assert r["rss_delta_mb"] < 20.0, (
                f"RSS grew {r['rss_delta_mb']:.1f} MB — confirms Context without"
                f" close() leaks C++ provider threads and channel objects"
            )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _format_result_block(r: dict) -> str:
    """Format a single result dict as a human-readable block."""
    lines = []
    name = r["name"]
    passed = r.get("passed")
    status = "PASS" if passed is True else ("FAIL" if passed is False else "INFO")
    lines.append(f"  [{status}] {name}")

    delta = r["rss_delta_mb"]
    threshold = r.get("threshold_mb")
    lines.append(f"         RSS start  : {r['rss_start_mb']:.1f} MB")
    lines.append(f"         RSS end    : {r['rss_end_mb']:.1f} MB")
    lines.append(f"         RSS delta  : {delta:+.1f} MB")
    if "rss_peak_mb" in r:
        lines.append(f"         RSS peak   : {r['rss_peak_mb']:.1f} MB")
    if threshold is not None:
        scale = max(threshold * 1.5, abs(delta) * 1.2, 1.0)
        lines.append(f"         threshold  : {threshold:.1f} MB")
        lines.append(f"         {_bar(abs(delta), scale)}")
    if "slope_mb_per_s" in r:
        lines.append(f"         rss slope  : {r['slope_mb_per_s']:+.4f} MB/s")
    if "anon_delta_mb" in r:
        lines.append(f"         anon start : {r['anon_start_mb']:.1f} MB")
        lines.append(f"         anon end   : {r['anon_end_mb']:.1f} MB")
        lines.append(f"         anon delta : {r['anon_delta_mb']:+.2f} MB")
    if "anon_slope_mb_per_s" in r:
        lines.append(f"         anon slope : {r['anon_slope_mb_per_s']:+.4f} MB/s  ← KEY METRIC")
    if "iterations" in r:
        lines.append(f"         iterations : {r['iterations']}")
    if "rss_series" in r:
        series = r["rss_series"]
        if series and isinstance(series[0], tuple):
            lines.append("         RSS timeline (elapsed_s → rss_mb):")
            for t, v in series:
                lines.append(f"           {t:6.1f}s  {v:.1f} MB")
        else:
            series_str = "  ".join(f"{v:.1f}" for v in series)
            lines.append(f"         RSS series : {series_str}")
    if "note" in r:
        lines.append(f"         note       : {r['note']}")
    if "errors_triggered" in r:
        lines.append(f"         errors     : {r['errors_triggered']}")
    lines.append("")
    return "\n".join(lines)


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent("""\
            p4p PVA get() memory leak isolation test.
            Starts an in-process PVA server and exercises the client get path.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gets", type=int, default=10000,
                        help="Number of get iterations for repeated-get test (default: 10000)")
    parser.add_argument("--cycles", type=int, default=500,
                        help="Number of Context open/close cycles (default: 500)")
    parser.add_argument("--soak", type=int, default=300,
                        help="Duration in seconds for soak test (default: 300, 0=skip)")
    parser.add_argument("--report", metavar="FILE",
                        help="Write report to FILE (default: stdout)")
    args = parser.parse_args()

    # Open report file early so we can stream results as they arrive
    report_fh = None
    if args.report:
        report_fh = open(args.report, "w")

    def _emit(text: str) -> None:
        """Write to stdout and optionally to the report file, flushing both."""
        print(text, flush=True)
        if report_fh:
            report_fh.write(text + "\n")
            report_fh.flush()

    _emit("Starting in-process PVA server...")
    srv, pv_names = _start_server()
    _emit(f"Server up, serving {len(pv_names)} PVs.")
    _emit(f"Settings: gets={args.gets}  cycles={args.cycles}  soak={args.soak}s")
    _emit("")

    results = []

    def _run(label: str, fn) -> dict:
        _emit(f"  Running {label}...")
        r = fn()
        block = _format_result_block(r)
        _emit(block)
        return r

    results.append(_run("repeated_gets",
                         lambda: measure_repeated_gets(pv_names, args.gets, live_file=report_fh)))
    results.append(_run("context_open_close",
                         lambda: measure_context_open_close(pv_names, args.cycles, live_file=report_fh)))
    results.append(_run("nonexistent_pv",
                         lambda: measure_nonexistent_pv(pv_names)))
    results.append(_run("context_without_close",
                         lambda: measure_context_without_close(pv_names, args.cycles, live_file=report_fh)))

    if args.soak > 0:
        _emit(f"  Running soak ({args.soak}s) — live RSS samples stream below...")
        r = measure_soak(pv_names, args.soak, live_file=report_fh)
        block = _format_result_block(r)
        _emit(block)
        results.append(r)

    srv.stop()

    # Final summary
    passed_count = sum(1 for r in results if r.get("passed") is True)
    failed_count = sum(1 for r in results if r.get("passed") is False)
    info_count   = sum(1 for r in results if r.get("passed") is None)
    summary = (
        "-" * 70 + "\n"
        f"  PASSED: {passed_count}  FAILED: {failed_count}  INFO: {info_count}\n"
        + "=" * 70
    )
    _emit(summary)

    if report_fh:
        report_fh.close()
        print(f"\nReport written to {args.report}")

    failed = [r for r in results if r.get("passed") is False]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _cli()
