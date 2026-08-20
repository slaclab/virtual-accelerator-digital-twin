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
"""

import json
import os
import sys
import ctypes

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
    """Log RSS, anonymous heap, and AnonHugePages to stderr."""
    m = _read_smaps_rollup()
    if not m:
        print(f"[mem] {label}: smaps unavailable", file=sys.stderr)
        return
    rss_mb = m.get("Rss", 0) / 1024
    anon_mb = m.get("Anonymous", 0) / 1024
    ahp_mb = m.get("AnonHugePages", 0) / 1024
    print(
        f"[mem] {label}: RSS={rss_mb:.1f}MB  anon={anon_mb:.1f}MB  AnonHugePages={ahp_mb:.1f}MB",
        file=sys.stderr,
        flush=True,
    )


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
        rc = libc.prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)
        after = libc.prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0)

        if rc != 0:
            print(f"[thp] WARNING: prctl(PR_SET_THP_DISABLE) failed rc={rc}", file=sys.stderr)
        else:
            status = "disabled" if after == 1 else "STILL ENABLED (unexpected)"
            print(
                f"[thp] THP before={before} after={after} -> {status}",
                file=sys.stderr,
                flush=True,
            )
    except Exception as e:
        print(f"[thp] WARNING: could not disable THP: {e}", file=sys.stderr, flush=True)


def _start_mem_logger(interval_s: int = 300) -> None:
    """Background thread: log RSS/AnonHugePages every interval_s seconds."""
    import threading
    import time

    def _loop():
        t0 = time.monotonic()
        while True:
            time.sleep(interval_s)
            elapsed_min = (time.monotonic() - t0) / 60
            _log_memory(f"t={elapsed_min:.1f}min")

    t = threading.Thread(target=_loop, daemon=True, name="mem-logger")
    t.start()


def main():
    _disable_thp()
    _log_memory("startup")
    model_name = os.environ.get("MODEL", "cu_hxr_bmad")
    end_element = os.environ.get("END_ELEMENT", "OTR4")
    n_particles = int(os.environ.get("N_PARTICLES", "10000"))
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    mem_log_interval_s = int(os.environ.get("MEM_LOG_INTERVAL_S", "300"))
    remote_inputs = os.environ.get("REMOTE_INPUTS", "").lower() in ("true", "1", "yes")
    pv_suffix = os.environ.get("PV_SUFFIX", "")
    pv_suffix_ml = os.environ.get("PV_SUFFIX_ML", "")
    pv_suffix_ph = os.environ.get("PV_SUFFIX_PH", "")
    pv_renames = json.loads(os.environ.get("PV_RENAMES", "{}"))

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

    config = Runner.generate_config(model, remote_inputs=remote_inputs)
    config["protocol"] = ["pva"]
    config["update_rate"] = 0

    # Apply PV renames before suffix
    for k, v in config['variables'].items():
        if v['pv'] in pv_renames:
            v['pv'] = pv_renames[v['pv']]

    # Internal model variables that aren't real PVs — skip suffix
    skip_suffix = {"name"}

    # Apply differentiated suffixes for staged models (ML vs physics)
    if (pv_suffix_ml or pv_suffix_ph) and hasattr(model, 'lume_model_instances'):
        ml_vars = set(model.lume_model_instances[0].supported_variables)
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                if k in ml_vars:
                    v['pv'] = v['pv'] + pv_suffix_ml
                else:
                    v['pv'] = v['pv'] + pv_suffix_ph
    elif pv_suffix:
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                v['pv'] = v['pv'] + pv_suffix

    config["remote_model_mode"] = "snapshot"

    # Exclude variables that shouldn't be read remotely:
    # - track_type: internal model variable, not a real PV
    # - :BDES: conflicts with :BCTRL for the same physical field (last-write-wins)
    for k, v in config["variables"].items():
        if k == "track_type" or k.endswith(":BDES"):
            v["mode"] = "rw"

    runner = Runner(model, config=config)
    

    if remote_inputs:
        import ctypes
        import gc
        import threading
        import torch

        _libc = ctypes.CDLL("libc.so.6")

        import time as _time

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
                    runner.take_snapshot()
                    cycle += 1
                    if cycle % 50 == 0:
                        gc.collect()
                        _libc.malloc_trim(0)
                    elapsed = _time.monotonic() - t0
                    sleep = _snapshot_interval - elapsed
                    if sleep > 0:
                        _time.sleep(sleep)

        t = threading.Thread(target=snapshot_loop, args=(runner,), daemon=True)
        t.start()

    _log_memory("runner-started")
    _start_mem_logger(interval_s=mem_log_interval_s)

    runner.run()


if __name__ == "__main__":
    main()
