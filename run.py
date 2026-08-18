"""Entry point for the virtual accelerator digital twin container.

Configurable via environment variables:
    MODEL          - Model to run (default: cu_hxr_bmad)
    END_ELEMENT    - Lattice end element (default: OTR4)
    N_PARTICLES    - Number of particles (default: 10000)
    LOG_LEVEL      - Logging level (default: INFO)
"""

import ctypes
import os
import sys


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

    config = Runner.generate_config(model)
    config["protocol"] = ["pva"]

    runner = Runner(model, config=config)
    _log_memory("runner-started")
    _start_mem_logger(interval_s=mem_log_interval_s)
    runner.run()


if __name__ == "__main__":
    main()
