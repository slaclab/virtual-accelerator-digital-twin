"""Entry point for the virtual accelerator digital twin container.

Configurable via environment variables:
    MODEL          - Model to run (default: cu_hxr_bmad)
    END_ELEMENT    - Lattice end element (default: OTR4)
    N_PARTICLES    - Number of particles (default: 10000)
    LOG_LEVEL      - Logging level (default: INFO)
    REMOTE_INPUTS  - Read inputs from prod EPICS (default: false)
    PV_SUFFIX      - Suffix appended to served PV names (default: none)
    PV_SUFFIX_ML   - Suffix for ML model outputs in staged models (default: none)
    PV_SUFFIX_PH   - Suffix for physics model outputs in staged models (default: none)
    PV_RENAMES     - JSON dict of PV name renames applied before suffix (default: none)
    METRICS_PORT   - Port for Prometheus /metrics endpoint (default: 9090, 0=disabled)
    MEM_LOG_INTERVAL_S - Memory log interval in seconds (default: 300)
    MEMRAY_TOP_N   - Top-N Python allocation sites to log/export via memray (default: 10, 0=disabled)
"""

import ctypes
import faulthandler
import json
import os
import sys
import threading

# Dump the Python stack of every thread on SIGSEGV/SIGABRT/SIGFPE. The parent process died
# with exit 139 (SIGSEGV) on 2026-09-03 after 33 h with no traceback -- a native crash gives
# no Python exception, so without this there is nothing to go on but guesswork. Native
# suspects in the parent are p4p/pvxs, torch, numpy and h5py; Bmad is excluded because Tao
# runs in a child process. Writes to stderr, so it lands in the pod log.
faulthandler.enable()

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    start_http_server,
    REGISTRY,
    CollectorRegistry,
)

import tao_recycle

# ---------------------------------------------------------------------------
# Prometheus metrics — declared at module level, one registry per process
# ---------------------------------------------------------------------------

_VA_RSS             = Gauge("va_rss_bytes",              "Process RSS in bytes")
_VA_ANON            = Gauge("va_anon_bytes",             "Anonymous heap in bytes")
_VA_AHP             = Gauge("va_anon_huge_pages_bytes",  "AnonHugePages in bytes (0 after THP fix)")
_VA_UPTIME          = Gauge("va_uptime_seconds",         "Seconds since runner started")
_VA_THP_DISABLED    = Gauge("va_thp_disabled",           "1 if THP disabled successfully, 0 otherwise")
_VA_QUEUE_SIZE      = Gauge("va_runner_queue_size",      "Current runner queue depth")

_VA_SNAP_CYCLES     = Counter("va_snapshot_cycles",      "Total take_snapshot() calls")
_VA_SNAP_DURATION   = Histogram("va_snapshot_duration_seconds",
                                "Time per take_snapshot() call",
                                buckets=[.005, .01, .025, .05, .1, .25, .5, 1.0])
_VA_SNAP_WAIT       = Histogram("va_snapshot_queue_wait_seconds",
                                "Time waiting for queue to drain before take_snapshot()",
                                buckets=[0, .001, .005, .01, .05, .1, .5, 1.0, 5.0])
_VA_GC_COLLECT      = Counter("va_gc_collects",          "GC+malloc_trim invocations")

_VA_PY_HEAP         = Gauge("va_py_heap_mb",             "Python heap via memray high-watermark (MB)")
_VA_PY_ALLOC        = Gauge("va_py_alloc_bytes",         "Top-N Python alloc site size (bytes)", ["location"])

_VA_PV_POSTS        = Counter("va_pv_posts",             "SharedPV post() calls", ["pv"])

_VA_TAO_RECYCLES    = Counter("va_tao_recycles",         "Tao subprocess respawns")
_VA_TAO_RECYCLE_FAIL = Counter("va_tao_recycle_failures", "Respawns where state restore could not be verified")
_VA_TAO_RECYCLE_DUR = Histogram("va_tao_recycle_duration_seconds",
                                "Time to respawn Tao and replay configuration",
                                buckets=[.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0])
_VA_TAO_MEM_AFTER   = Gauge("va_tao_mem_after_recycle_bytes",
                            "Container memory immediately after the last respawn")


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

def _read_smaps_rollup() -> dict:
    """Read key fields from /proc/self/smaps_rollup. Returns empty dict on failure."""
    fields = {}
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                for key in ("Rss", "Anonymous", "AnonHugePages"):
                    if line.startswith(f"{key}:"):
                        fields[key] = int(line.split()[1])  # kB
    except OSError:
        pass
    return fields


def _log_memory(label: str) -> None:
    """Log RSS/anon/AnonHugePages to stderr and update Prometheus gauges."""
    m = _read_smaps_rollup()
    if not m:
        print(f"[mem] {label}: smaps unavailable", file=sys.stderr)
        return
    rss_mb  = m.get("Rss", 0) / 1024
    anon_mb = m.get("Anonymous", 0) / 1024
    ahp_mb  = m.get("AnonHugePages", 0) / 1024
    # child and cgroup are logged alongside the parent because the residual growth left
    # behind by each Tao respawn is in neither the parent's Python heap nor the fresh child,
    # and cannot be localized from a single total.
    child_mb = tao_recycle.descendants_rss_mb()
    cg_mb = tao_recycle.cgroup_current_bytes() / (1024.0 * 1024.0)
    # cg_anon is the leak-relevant series: cgroup total also counts reclaimable page cache
    # and slab, which swing tens of MB/h with node memory pressure and obscure the signal.
    cg_anon_mb = tao_recycle.cgroup_anon_bytes() / (1024.0 * 1024.0)
    print(
        f"[mem] {label}: RSS={rss_mb:.1f}MB  anon={anon_mb:.1f}MB  AnonHugePages={ahp_mb:.1f}MB"
        f"  child={child_mb:.1f}MB  cgroup={cg_mb:.1f}MB  cg_anon={cg_anon_mb:.1f}MB",
        file=sys.stderr, flush=True,
    )
    _VA_RSS.set(m.get("Rss", 0) * 1024)
    _VA_ANON.set(m.get("Anonymous", 0) * 1024)
    _VA_AHP.set(m.get("AnonHugePages", 0) * 1024)


# ---------------------------------------------------------------------------
# THP
# ---------------------------------------------------------------------------

def _disable_thp() -> None:
    """Disable Transparent Huge Pages for this process.

    THP (when set to 'always' on the host) promotes Fortran/C heap allocations
    from libtao to 2 MB huge pages. These pages are never returned to the OS
    after being freed, causing monotonic RSS growth over days of simulation.
    prctl(PR_SET_THP_DISABLE) opts this process out without requiring privileges.
    """
    PR_SET_THP_DISABLE = 41
    PR_GET_THP_DISABLE = 42
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        before = libc.prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0)
        rc     = libc.prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)
        after  = libc.prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0)
        if rc != 0:
            print(f"[thp] WARNING: prctl(PR_SET_THP_DISABLE) failed rc={rc}", file=sys.stderr)
            _VA_THP_DISABLED.set(0)
        else:
            status = "disabled" if after == 1 else "STILL ENABLED (unexpected)"
            print(f"[thp] THP before={before} after={after} -> {status}", file=sys.stderr, flush=True)
            _VA_THP_DISABLED.set(1 if after == 1 else 0)
    except Exception as e:
        print(f"[thp] WARNING: could not disable THP: {e}", file=sys.stderr, flush=True)
        _VA_THP_DISABLED.set(0)


# ---------------------------------------------------------------------------
# Background mem logger
# ---------------------------------------------------------------------------

def _start_mem_logger(interval_s: int, runner, top_n: int = 10) -> None:
    """Background thread: log RSS/AnonHugePages and update gauges every interval_s seconds.
    Also scans memray every 60 s when top_n > 0, exporting top-N allocation sites."""
    import time

    def _loop():
        t0 = time.monotonic()
        _prev_locations: set = set()
        tm_tick = 0.0
        _bin_path = "/tmp/memray_snap.bin"

        while True:
            time.sleep(interval_s)
            elapsed = time.monotonic() - t0
            _log_memory(f"t={elapsed / 60:.1f}min")
            _VA_UPTIME.set(elapsed)
            try:
                _VA_QUEUE_SIZE.set(runner.queue.qsize())
            except Exception:
                pass

            if top_n > 0 and (time.monotonic() - tm_tick) >= 60:
                tm_tick = time.monotonic()
                try:
                    import memray
                    # One-shot snapshot: track only long-lived allocations for a
                    # brief window. native_traces=False avoids hooking C malloc,
                    # preventing interference with the model thread.
                    with memray.Tracker(
                        destination=memray.FileDestination(_bin_path, overwrite=True),
                        native_traces=False,
                    ):
                        time.sleep(2.0)

                    total_bytes = 0
                    alloc_sites: list = []
                    with memray.FileReader(_bin_path) as reader:
                        for alloc in reader.get_leaked_allocation_records(merge_threads=True):
                            total_bytes += alloc.size
                            tb = alloc.stack_trace()
                            loc = str(tb[0]) if tb else "unknown"
                            alloc_sites.append((alloc.size, loc))

                    alloc_sites.sort(reverse=True)
                    total_mb = total_bytes / 1024 / 1024
                    _VA_PY_HEAP.set(total_mb)

                    print(f"# Top {top_n} memray allocations (total={total_mb:.1f}MB):",
                          file=sys.stderr, flush=True)
                    new_locations: set = set()
                    for size, loc in alloc_sites[:top_n]:
                        print(f"#   {size/1024:.1f} KB  {loc}", file=sys.stderr, flush=True)
                        _VA_PY_ALLOC.labels(location=loc).set(size)
                        new_locations.add(loc)
                    for old in _prev_locations - new_locations:
                        try:
                            _VA_PY_ALLOC.remove(old)
                        except Exception:
                            pass
                    _prev_locations = new_locations
                except Exception as e:
                    print(f"[memray] scan error: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_loop, daemon=True, name="mem-logger").start()


# ---------------------------------------------------------------------------
# PV post instrumentation
# ---------------------------------------------------------------------------

def _instrument_pv_posts(runner) -> None:
    """Wrap SharedPV.post() on all runner PVs to count posts via prometheus_client."""
    for var_name, pv in runner.pvs.items():
        pv_name = runner.var_to_pv.get(var_name, var_name)
        original_post = pv.post

        def _counted_post(value, _n=pv_name, _o=original_post):
            _VA_PV_POSTS.labels(pv=_n).inc()
            _o(value)

        pv.post = _counted_post


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _disable_thp()
    _log_memory("startup")

    model_name        = os.environ.get("MODEL", "cu_hxr_bmad")
    end_element       = os.environ.get("END_ELEMENT", "OTR4")
    n_particles       = int(os.environ.get("N_PARTICLES", "10000"))
    log_level         = os.environ.get("LOG_LEVEL", "INFO")
    mem_log_interval_s = int(os.environ.get("MEM_LOG_INTERVAL_S", "300"))
    remote_inputs     = os.environ.get("REMOTE_INPUTS", "").lower() in ("true", "1", "yes")
    pv_suffix         = os.environ.get("PV_SUFFIX", "")
    pv_suffix_ml      = os.environ.get("PV_SUFFIX_ML", "")
    pv_suffix_ph      = os.environ.get("PV_SUFFIX_PH", "")
    pv_renames        = json.loads(os.environ.get("PV_RENAMES", "{}"))
    metrics_port      = int(os.environ.get("METRICS_PORT", "9090"))
    top_n             = int(os.environ.get("MEMRAY_TOP_N", "10"))
    radiation_fluct   = os.environ.get("BMAD_RADIATION_FLUCTUATIONS", "").strip().lower()
    recycle_enabled   = os.environ.get("TAO_RECYCLE_ENABLED", "true").lower() in ("true", "1", "yes")
    recycle_growth_mb = float(os.environ.get("TAO_RECYCLE_RSS_GROWTH_MB", "400"))
    recycle_max_cycles = int(os.environ.get("TAO_RECYCLE_MAX_CYCLES", "0"))

    import logging
    logging.basicConfig(level=getattr(logging, log_level))
    logging.getLogger("pytao").setLevel(logging.WARNING)

    from lume_pva.runner import Runner
    from virtual_accelerator.models.cu_hxr import (
        get_cu_hxr_bmad_model,
        get_cu_hxr_staged_model,
    )
    from virtual_accelerator.models.facet2 import (
        get_facet_bmad_model,
        get_facet_staged_model,
    )

    # libtao leaks ~89 KB of native heap per beam track (upstream bmad bug). Running Tao in
    # a child process lets us respawn it to reclaim that memory. virtual_accelerator's
    # build_bmad_model does a function-local `from pytao import Tao`, so the name resolves
    # off the pytao module at call time -- substituting it here is enough, and avoids
    # vendoring a patched copy of factory.py. RecyclableTao subclasses SubprocessTao which
    # subclasses Tao, so isinstance checks and type annotations still hold.
    if recycle_enabled:
        import pytao
        pytao.Tao = tao_recycle.make_recyclable_tao_class()
        print("[recycle] pytao.Tao -> RecyclableTao (SubprocessTao)", file=sys.stderr, flush=True)

    if model_name == "cu_hxr_bmad":
        model = get_cu_hxr_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "cu_hxr_staged":
        model = get_cu_hxr_staged_model(end_element=end_element, n_particles=n_particles)
    elif model_name == "facet_bmad":
        model = get_facet_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "facet_staged":
        model = get_facet_staged_model(end_element=end_element, n_particles=n_particles)
    else:
        raise ValueError(f"Unknown model: {model_name}")

    _log_memory("model-loaded")

    # Radiation is one of the two conditions gating the libtao rad_map leak
    # (bmad-ecosystem#2177 -- the leak needs radiation AND comb saving; we need comb saving).
    # cu_hxr tao.init sets radiation_fluctuations_on = T, so overriding it to F is a candidate
    # fix at source rather than containment. This is a reversible diagnostic: radiation is
    # expected to be needed again, so recycling stays enabled either way. Unset leaves the
    # lattice value untouched.
    if radiation_fluct:
        bmad_stage = tao_recycle.find_bmad_model(model)
        if bmad_stage is None:
            print("[radiation] WARNING no Bmad model found; override NOT applied",
                  file=sys.stderr, flush=True)
        else:
            flag = "T" if radiation_fluct in ("on", "true", "1", "yes") else "F"
            # Issued through tao.cmd so RecyclableTao records it for replay after a respawn.
            bmad_stage.tao.cmd(f"set bmad_com radiation_fluctuations_on = {flag}")
            print(f"[radiation] override: bmad_com radiation_fluctuations_on = {flag} "
                  f"(lattice default is T)", file=sys.stderr, flush=True)

    tao_recycle.install_recycling(
        model,
        enabled=recycle_enabled,
        growth_mb=recycle_growth_mb,
        max_cycles=recycle_max_cycles,
        on_recycle=lambda duration, mem_bytes: (
            _VA_TAO_RECYCLES.inc(),
            _VA_TAO_RECYCLE_DUR.observe(duration),
            _VA_TAO_MEM_AFTER.set(mem_bytes),
        ),
        on_failure=_VA_TAO_RECYCLE_FAIL.inc,
    )

    config = Runner.generate_config(model, remote_inputs=remote_inputs)
    config["protocol"] = ["pva"]
    config["update_rate"] = 0

    for k, v in config['variables'].items():
        if v['pv'] in pv_renames:
            v['pv'] = pv_renames[v['pv']]

    skip_suffix = {"name"}

    if (pv_suffix_ml or pv_suffix_ph) and hasattr(model, 'lume_model_instances'):
        ml_vars = set(model.lume_model_instances[0].supported_variables)
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                v['pv'] = v['pv'] + (pv_suffix_ml if k in ml_vars else pv_suffix_ph)
    elif pv_suffix:
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                v['pv'] = v['pv'] + pv_suffix

    config["remote_model_mode"] = "snapshot"

    for k, v in config["variables"].items():
        if k == "track_type" or k.endswith(":BDES"):
            v["mode"] = "rw"

    runner = Runner(model, config=config)

    _instrument_pv_posts(runner)

    if metrics_port > 0:
        start_http_server(metrics_port)
        print(f"[metrics] Prometheus HTTP server on :{metrics_port}/metrics", file=sys.stderr, flush=True)

    if remote_inputs:
        import gc
        import time as _time
        import torch

        _libc = ctypes.CDLL("libc.so.6")

        # Throttle to the runner's update_rate so the queue never backlogs.
        # Without a sleep, take_snapshot() runs as fast as network allows (~40 Hz),
        # flooding the queue with p4p Value objects faster than the runner consumes
        # them. Each queued item holds a C++ PVStructure in p4p's heap — the
        # resulting backlog accumulates 2+ GB over hours.
        _snapshot_interval = runner.update_rate if runner.update_rate > 0 else 0.1

        def snapshot_loop(runner):
            cycle = 0
            with torch.no_grad():
                while True:
                    t0 = _time.monotonic()

                    # Wait for queue to drain before producing next item.
                    # If consumer (model) is slower than producer (snapshot),
                    # the queue backlog grows — each item holds a p4p.Value
                    # (C++ PVStructure) that accumulates in the heap.
                    _wait_t0 = _time.monotonic()
                    while runner.queue.qsize() > 1:
                        _time.sleep(0.01)
                    _VA_SNAP_WAIT.observe(_time.monotonic() - _wait_t0)
                    _VA_QUEUE_SIZE.set(runner.queue.qsize())

                    try:
                        runner.take_snapshot()
                    except (TimeoutError, Exception) as e:
                        print(f"[snapshot] WARNING: take_snapshot() failed: {e}",
                              file=sys.stderr, flush=True)
                        _time.sleep(_snapshot_interval)
                        continue
                    cycle += 1

                    _VA_SNAP_CYCLES.inc()
                    _VA_SNAP_DURATION.observe(_time.monotonic() - t0)

                    if cycle % 50 == 0:
                        gc.collect()
                        _libc.malloc_trim(0)
                        _VA_GC_COLLECT.inc()

                    elapsed = _time.monotonic() - t0
                    sleep = _snapshot_interval - elapsed
                    if sleep > 0:
                        _time.sleep(sleep)

        threading.Thread(target=snapshot_loop, args=(runner,), daemon=True).start()

    _log_memory("runner-started")
    _start_mem_logger(interval_s=mem_log_interval_s, runner=runner, top_n=top_n)

    runner.run()


if __name__ == "__main__":
    main()
