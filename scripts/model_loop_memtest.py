#!/usr/bin/env python
"""Long-running memory leak test for the model set/get loop.

Uses the REAL model (cu_hxr_bmad, cu_hxr_staged, etc.) when pytao/bmad are
available, falls back to FakeModel locally. Designed to run for hours inside
the container to detect slow Fortran/libtao heap growth.

Usage:
    # Inside container — real model, 1 hour
    python scripts/model_loop_memtest.py --duration 3600

    # Locally — FakeModel only
    python scripts/model_loop_memtest.py --duration 300

    # Run forever (Ctrl+C to stop)
    python scripts/model_loop_memtest.py --duration 0

Output (CSV to stdout):
    elapsed_s,rss_mb,py_heap_mb,total_cycles,cycles_per_s,rss_delta_mb

Environment variables:
    MODEL                   Model name (default: cu_hxr_bmad)
    END_ELEMENT             Lattice end element (default: OTR4)
    N_PARTICLES             Number of particles (default: 1000)
    LCLS_LATTICE            Path to lattice files (required for real model)
    MEMTEST_DURATION_S      Total run time in seconds (default: 3600, 0=forever)
    MEMTEST_LOG_INTERVAL_S  Seconds between log lines (default: 60)
    MEMTEST_CYCLES_BATCH    Cycles per batch (default: 10 real, 500 fake)
    MEMTEST_RSS_LIMIT_MB    Fail if RSS grows beyond baseline + this (default: 0=no limit)
"""

import argparse
import ctypes
import gc
import os
import resource
import sys
import time
import tracemalloc

import numpy as np


def _disable_thp() -> None:
    """Disable THP for this process — same as run.py."""
    PR_SET_THP_DISABLE = 41
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)
        after = libc.prctl(42, 0, 0, 0, 0)
        status = "disabled" if after == 1 else "STILL ENABLED"
        print(f"[thp] THP {status} (rc={rc})", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[thp] WARNING: {e}", file=sys.stderr, flush=True)


def _malloc_trim() -> None:
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FakeModel — fallback when pytao/bmad not available
# ---------------------------------------------------------------------------

class _FakeVar:
    def __init__(self, name, read_only=False):
        self.name = name
        self.read_only = read_only


class FakeModel:
    def __init__(self, n_outputs: int = 50, array_size: int = 1000):
        self._array_size = array_size
        self._state = {}
        rw_names = [
            "QUAD:IN20:631:BCTRL", "QUAD:IN20:651:BCTRL",
            "XCOR:IN20:641:BCTRL", "YCOR:IN20:642:BCTRL",
            "BEND:IN20:661:BCTRL",
        ]
        ro_names = [f"output_{i}" for i in range(n_outputs)]
        self.supported_variables = {
            **{n: _FakeVar(n, read_only=False) for n in rw_names},
            **{n: _FakeVar(n, read_only=True)  for n in ro_names},
        }
        for name in self.supported_variables:
            self._state[name] = np.zeros(array_size)

    def set(self, values: dict) -> None:
        for k, v in values.items():
            if k in self._state:
                self._state[k] = np.asarray(v, dtype=np.float64)

    def get(self, names) -> dict:
        if isinstance(names, dict):
            names = list(names.keys())
        result = {}
        for n in names:
            if n not in self._state:
                continue
            var = self.supported_variables[n]
            result[n] = self._state[n].copy() if not var.read_only else np.random.rand(self._array_size)
        return result

    def reset(self) -> None:
        for name in self._state:
            self._state[name] = np.zeros(self._array_size)


# ---------------------------------------------------------------------------
# Real model loader
# ---------------------------------------------------------------------------

def _load_real_model(model_name: str, end_element: str, n_particles: int):
    """Load the real VA model. Returns None if not available."""
    try:
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
            print(f"# Unknown model {model_name!r}, using FakeModel", file=sys.stderr)
            return None
    except Exception as e:
        print(f"# Could not load real model ({e}), using FakeModel", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Runner loop — mirrors lume_pva runner._run() allocation pattern
# ---------------------------------------------------------------------------

def _numeric_settable(model) -> list:
    """Return writable variable names that hold numeric values (skip enums/strings)."""
    result = []
    probe = model.get(list(model.supported_variables.keys()))
    for n, v in model.supported_variables.items():
        if v.read_only:
            continue
        val = probe.get(n)
        if val is None:
            continue
        try:
            arr = np.asarray(val)
            if np.issubdtype(arr.dtype, np.number):
                result.append(n)
        except Exception:
            pass
    return result


def run_batch(model, n_cycles: int) -> None:
    settable = _numeric_settable(model)
    all_vars = list(model.supported_variables.keys())
    for _ in range(n_cycles):
        cached = model.get(settable)
        new_values = {}
        for k in settable:
            try:
                arr = np.asarray(cached[k])
                new_values[k] = arr + np.random.randn(*arr.shape) * 0.001
            except Exception:
                pass
        model.set(new_values)
        out_values = model.get(all_vars)
        for v in out_values.values():
            try:
                arr = np.asarray(v)
                if np.issubdtype(arr.dtype, np.number):
                    _ = arr.sum()
            except Exception:
                pass
        del cached, new_values, out_values


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

def _rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def _py_heap_mb() -> float:
    snap = tracemalloc.take_snapshot()
    return sum(s.size for s in snap.statistics("lineno")) / 1024 / 1024


def _smaps_rollup() -> dict:
    fields = {}
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                for key in ("Rss", "AnonHugePages"):
                    if line.startswith(f"{key}:"):
                        fields[key] = int(line.split()[1])
    except OSError:
        pass
    return fields


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _build_snapshot_runner(model):
    """Build a lume_pva Runner in snapshot mode for take_snapshot() testing."""
    from lume_pva.runner import Runner
    config = Runner.generate_config(model, remote_inputs=True)
    config["protocol"] = ["pva"]
    config["update_rate"] = 0
    config["remote_model_mode"] = "snapshot"
    for k, v in config["variables"].items():
        if k == "track_type" or k.endswith(":BDES"):
            v["mode"] = "rw"
    return Runner(model, config=config)


def run_snapshot_batch(runner, n_cycles: int, interval_s: float = 0.1) -> None:
    """Call runner.take_snapshot() n_cycles times with throttling and periodic GC."""
    for i in range(n_cycles):
        t0 = time.monotonic()
        runner.take_snapshot()
        elapsed = time.monotonic() - t0
        sleep = interval_s - elapsed
        if sleep > 0:
            time.sleep(sleep)
        if i > 0 and i % 50 == 0:
            gc.collect()
            _malloc_trim()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duration",         type=float,
                   default=float(os.environ.get("MEMTEST_DURATION_S",    "3600")))
    p.add_argument("--log-interval",     type=float,
                   default=float(os.environ.get("MEMTEST_LOG_INTERVAL_S", "60")))
    p.add_argument("--cycles-per-batch", type=int,   default=0,
                   help="0 = auto (10 real model, 500 fake)")
    p.add_argument("--rss-limit",        type=float,
                   default=float(os.environ.get("MEMTEST_RSS_LIMIT_MB",  "0")))
    p.add_argument("--model",            default=os.environ.get("MODEL", "cu_hxr_bmad"))
    p.add_argument("--end-element",      default=os.environ.get("END_ELEMENT", "OTR4"))
    p.add_argument("--n-particles",      type=int,
                   default=int(os.environ.get("N_PARTICLES", "1000")))
    p.add_argument("--snapshot-mode",    action="store_true",
                   help="Test Runner.take_snapshot() loop instead of model set/get. "
                        "Requires EPICS PVA connection to mock-ioc or real machine.")
    p.add_argument("--snapshot-interval", type=float, default=0.1,
                   help="Seconds between take_snapshot() calls (default: 0.1)")
    return p.parse_args()


def main() -> int:
    _disable_thp()
    args = parse_args()

    # Try real model first
    model = _load_real_model(args.model, args.end_element, args.n_particles)
    using_real = model is not None
    if not using_real:
        model = FakeModel(n_outputs=50, array_size=1000)

    cycles_per_batch = args.cycles_per_batch
    if cycles_per_batch == 0:
        cycles_per_batch = 10 if using_real else 500

    # Snapshot mode: build Runner and use take_snapshot() loop
    runner = None
    if args.snapshot_mode:
        if not using_real:
            print("# ERROR: --snapshot-mode requires a real model (pytao unavailable)",
                  file=sys.stderr, flush=True)
            return 1
        print("# Mode: SNAPSHOT (Runner.take_snapshot() → pvua → mock-ioc/real machine)",
              file=sys.stderr, flush=True)
        print(f"# snapshot_interval={args.snapshot_interval}s", file=sys.stderr, flush=True)
        runner = _build_snapshot_runner(model)
        cycles_per_batch = max(1, int(args.log_interval / args.snapshot_interval / 10))
    else:
        print(f"# Mode: MODEL SET/GET", file=sys.stderr, flush=True)

    print(f"# Model: {'REAL (' + args.model + ')' if using_real else 'FakeModel (pytao unavailable)'}",
          file=sys.stderr, flush=True)
    print(f"# duration={args.duration}s  log_interval={args.log_interval}s  "
          f"cycles_per_batch={cycles_per_batch}", file=sys.stderr, flush=True)
    print(f"# rss_limit={args.rss_limit}MB (0=disabled)", file=sys.stderr, flush=True)
    print(f"# Press Ctrl+C to stop early", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    # CSV header
    print("elapsed_s,rss_mb,anon_huge_pages_kb,py_heap_mb,total_cycles,cycles_per_s,rss_delta_mb",
          flush=True)

    tracemalloc.start()

    # Warm-up
    print("# warming up...", file=sys.stderr, flush=True)
    for _ in range(3):
        if runner is not None:
            run_snapshot_batch(runner, cycles_per_batch, args.snapshot_interval)
        else:
            run_batch(model, cycles_per_batch)
    gc.collect()

    t_start = time.monotonic()
    t_last_log = t_start
    total_cycles = 0
    rss_baseline = _rss_mb()
    rss_last = rss_baseline

    print(f"# baseline RSS after warmup: {rss_baseline:.1f} MB", file=sys.stderr, flush=True)

    deadline = t_start + args.duration if args.duration > 0 else float("inf")

    try:
        while time.monotonic() < deadline:
            if runner is not None:
                run_snapshot_batch(runner, cycles_per_batch, args.snapshot_interval)
            else:
                run_batch(model, cycles_per_batch)
            total_cycles += cycles_per_batch

            now = time.monotonic()
            if now - t_last_log >= args.log_interval:
                gc.collect()
                elapsed = now - t_start
                rss = _rss_mb()
                py_heap = _py_heap_mb()
                smaps = _smaps_rollup()
                ahp_kb = smaps.get("AnonHugePages", 0)
                cps = total_cycles / elapsed if elapsed > 0 else 0
                rss_delta = rss - rss_baseline

                print(f"{elapsed:.1f},{rss:.1f},{ahp_kb},{py_heap:.3f},"
                      f"{total_cycles},{cps:.1f},{rss_delta:+.1f}", flush=True)

                # Warn on RSS spikes
                if rss > rss_last + 20:
                    print(f"# WARN: RSS jumped +{rss - rss_last:.1f} MB "
                          f"({rss_last:.1f} → {rss:.1f} MB)", file=sys.stderr, flush=True)

                # Warn if AnonHugePages > 0 (THP regression)
                if ahp_kb > 0:
                    print(f"# WARN: AnonHugePages={ahp_kb} kB — THP may be active!",
                          file=sys.stderr, flush=True)

                # Hard fail if limit exceeded
                if args.rss_limit > 0 and (rss - rss_baseline) > args.rss_limit:
                    print(f"# FAIL: RSS growth {rss - rss_baseline:.1f} MB exceeds "
                          f"limit {args.rss_limit:.1f} MB", file=sys.stderr, flush=True)
                    return 1

                rss_last = rss
                t_last_log = now

    except KeyboardInterrupt:
        print(f"\n# Stopped by user after {time.monotonic() - t_start:.0f}s",
              file=sys.stderr, flush=True)

    elapsed = time.monotonic() - t_start
    rss_final = _rss_mb()
    rss_growth = rss_final - rss_baseline
    print(f"\n# Done: {total_cycles} cycles in {elapsed:.0f}s ({total_cycles/elapsed:.0f} cyc/s)",
          file=sys.stderr, flush=True)
    print(f"# RSS: baseline={rss_baseline:.1f}MB  final={rss_final:.1f}MB  "
          f"growth={rss_growth:+.1f}MB", file=sys.stderr, flush=True)

    if rss_growth > 50:
        print(f"# WARN: RSS grew {rss_growth:.1f} MB — possible leak", file=sys.stderr, flush=True)
        return 1

    print("# OK: RSS growth within bounds", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
