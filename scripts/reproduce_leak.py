#!/usr/bin/env python
"""Reproduce the RSS memory leak by running take_snapshot() without mitigations.

Demonstrates the two root causes of memory growth:
  1. Queue backlog: take_snapshot() enqueues p4p.Value objects faster than the
     runner consumes them. Each queued item holds a C++ PVStructure (~40 KB).
  2. THP promotion: glibc heap allocations get promoted to 2 MB huge pages that
     are never returned to the OS.

Run modes:
  --mode leak    No fixes: tight loop, no queue drain, no gc, THP enabled
  --mode fixed   All fixes: throttle, queue drain, gc+malloc_trim, THP disabled

Usage (inside container or via kubectl exec):
    python scripts/reproduce_leak.py --mode leak --duration 600
    python scripts/reproduce_leak.py --mode fixed --duration 600

Output: CSV to stdout (pipe to file for plotting)
    elapsed_s,rss_mb,queue_size,total_cycles,cycles_per_s

Environment:
    MODEL, END_ELEMENT, N_PARTICLES, LCLS_LATTICE (same as run.py)
"""

import argparse
import ctypes
import gc
import os
import resource
import sys
import threading
import time


def _rss_mb() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024


def _smaps_rss_mb() -> float:
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Rss:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return _rss_mb()


def _disable_thp():
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(41, 1, 0, 0, 0)
        after = libc.prctl(42, 0, 0, 0, 0)
        print(f"[thp] disabled={after == 1} (rc={rc})", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[thp] WARNING: {e}", file=sys.stderr, flush=True)


def _malloc_trim():
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _load_model(model_name, end_element, n_particles):
    if model_name == "cu_hxr_bmad":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_bmad_model
        return get_cu_hxr_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "cu_hxr_staged":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model
        return get_cu_hxr_staged_model(end_element=end_element, n_particles=n_particles)
    elif model_name == "facet_bmad":
        from virtual_accelerator.models.facet2 import get_facet_bmad_model
        return get_facet_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "facet_staged":
        from virtual_accelerator.models.facet2 import get_facet_staged_model
        return get_facet_staged_model(end_element=end_element, n_particles=n_particles)
    else:
        raise ValueError(f"Unknown model: {model_name}")


def _build_runner(model):
    from lume_pva.runner import Runner
    config = Runner.generate_config(model, remote_inputs=True)
    config["protocol"] = ["pva"]
    config["update_rate"] = 0
    config["remote_model_mode"] = "snapshot"
    for k, v in config["variables"].items():
        if k == "track_type" or k.endswith(":BDES"):
            v["mode"] = "rw"
    runner = Runner(model, config=config)
    threading.Thread(target=runner.run, daemon=True, name="runner-consumer").start()
    time.sleep(1)
    return runner


def run_leak_mode(runner, duration, log_interval):
    """No fixes: tight loop, no throttle, no queue drain, no gc/malloc_trim."""
    print("# MODE: LEAK (no fixes — expect RSS to grow)", file=sys.stderr, flush=True)
    print("# - No throttle (snapshot as fast as possible)", file=sys.stderr, flush=True)
    print("# - No queue drain wait", file=sys.stderr, flush=True)
    print("# - No gc.collect() / malloc_trim()", file=sys.stderr, flush=True)
    print("# - THP NOT disabled", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    print("elapsed_s,rss_mb,queue_size,total_cycles,cycles_per_s", flush=True)

    t_start = time.monotonic()
    t_last_log = t_start
    cycles = 0
    deadline = t_start + duration if duration > 0 else float("inf")

    try:
        while time.monotonic() < deadline:
            runner.take_snapshot()
            cycles += 1

            now = time.monotonic()
            if now - t_last_log >= log_interval:
                elapsed = now - t_start
                rss = _smaps_rss_mb()
                qsize = runner.queue.qsize()
                cps = cycles / elapsed
                print(f"{elapsed:.1f},{rss:.1f},{qsize},{cycles},{cps:.1f}", flush=True)
                t_last_log = now
    except KeyboardInterrupt:
        pass

    elapsed = time.monotonic() - t_start
    rss = _smaps_rss_mb()
    print(f"\n# DONE: {cycles} cycles in {elapsed:.0f}s, final RSS={rss:.1f}MB, "
          f"queue={runner.queue.qsize()}", file=sys.stderr, flush=True)


def run_fixed_mode(runner, duration, log_interval):
    """All fixes: THP disabled, throttle, queue drain, gc+malloc_trim."""
    _disable_thp()

    snapshot_interval = 0.1
    print("# MODE: FIXED (all mitigations active — expect RSS stable)", file=sys.stderr, flush=True)
    print(f"# - Throttle: {snapshot_interval}s between snapshots", file=sys.stderr, flush=True)
    print("# - Queue drain: wait for qsize <= 1 before snapshot", file=sys.stderr, flush=True)
    print("# - gc.collect() + malloc_trim() every 50 cycles", file=sys.stderr, flush=True)
    print("# - THP disabled", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    print("elapsed_s,rss_mb,queue_size,total_cycles,cycles_per_s", flush=True)

    libc = ctypes.CDLL("libc.so.6")
    t_start = time.monotonic()
    t_last_log = t_start
    cycles = 0
    deadline = t_start + duration if duration > 0 else float("inf")

    try:
        while time.monotonic() < deadline:
            t0 = time.monotonic()

            while runner.queue.qsize() > 1:
                time.sleep(0.01)

            runner.take_snapshot()
            cycles += 1

            if cycles % 50 == 0:
                gc.collect()
                libc.malloc_trim(0)

            elapsed_cycle = time.monotonic() - t0
            sleep = snapshot_interval - elapsed_cycle
            if sleep > 0:
                time.sleep(sleep)

            now = time.monotonic()
            if now - t_last_log >= log_interval:
                elapsed = now - t_start
                rss = _smaps_rss_mb()
                qsize = runner.queue.qsize()
                cps = cycles / elapsed
                print(f"{elapsed:.1f},{rss:.1f},{qsize},{cycles},{cps:.1f}", flush=True)
                t_last_log = now
    except KeyboardInterrupt:
        pass

    elapsed = time.monotonic() - t_start
    rss = _smaps_rss_mb()
    print(f"\n# DONE: {cycles} cycles in {elapsed:.0f}s, final RSS={rss:.1f}MB, "
          f"queue={runner.queue.qsize()}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["leak", "fixed"], required=True,
                        help="'leak' = no fixes, 'fixed' = all mitigations")
    parser.add_argument("--duration", type=float, default=600,
                        help="Run time in seconds (default: 600, 0=forever)")
    parser.add_argument("--log-interval", type=float, default=30,
                        help="Seconds between CSV rows (default: 30)")
    parser.add_argument("--model", default=os.environ.get("MODEL", "cu_hxr_bmad"))
    parser.add_argument("--end-element", default=os.environ.get("END_ELEMENT", "OTR4"))
    parser.add_argument("--n-particles", type=int,
                        default=int(os.environ.get("N_PARTICLES", "1000")))
    args = parser.parse_args()

    print(f"# Loading model: {args.model} (end={args.end_element}, n={args.n_particles})",
          file=sys.stderr, flush=True)
    model = _load_model(args.model, args.end_element, args.n_particles)

    print("# Building runner...", file=sys.stderr, flush=True)
    runner = _build_runner(model)

    rss_start = _smaps_rss_mb()
    print(f"# Baseline RSS: {rss_start:.1f} MB", file=sys.stderr, flush=True)
    print(f"# Duration: {args.duration}s (Ctrl+C to stop early)", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    if args.mode == "leak":
        run_leak_mode(runner, args.duration, args.log_interval)
    else:
        run_fixed_mode(runner, args.duration, args.log_interval)


if __name__ == "__main__":
    main()
