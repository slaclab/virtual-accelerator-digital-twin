"""Minimal pytao-only loop: randomize quads, track OTR2->TD11, read emittance/beamsize.

Uses the same lattice/init file as get_cu_hxr_bmad (CU_HXR tao.init), with no LUME
wrappers. Each iteration sets 5 quads to random K1 values within a range, tracks the
250 pC OTR2 beam to TD11, and reports the emittances and RMS beam sizes there.

Runs continuously (while True) logging RSS every LOG_INTERVAL_S seconds to isolate
whether Fortran/libtao leaks memory independently of lume wrappers.

Usage:
    LCLS_LATTICE=/opt/lcls-lattice python scripts/pytao_test_script.py
    # Or with custom duration:
    DURATION_S=3600 LOG_INTERVAL_S=60 python scripts/pytao_test_script.py
"""

import ctypes
import os
import resource
import sys
import time

import numpy as np
from pytao import Tao

LCLS_LATTICE = os.environ.get("LCLS_LATTICE", "/opt/lcls-lattice")
INIT_FILE = os.path.join(LCLS_LATTICE, "bmad/models/cu_hxr/tao.init")

TRACK_START = "OTR2"
MEAS_ELE = "TD11"
QUADS = ["QA11", "QA12", "Q21201", "QM11", "QM12"]

N_PARTICLE = 1000  # subsample the 100k OTR2 beam file down to this many particles
DURATION_S = float(os.environ.get("DURATION_S", "0"))  # 0 = run forever
LOG_INTERVAL_S = float(os.environ.get("LOG_INTERVAL_S", "60"))
K1_REL_RANGE = 0.3  # sample each quad K1 uniformly within design * (1 +/- this)
COMB_DS_SAVE = 0.1  # comb sampling step in meters (matches LUMEBmadModel default)

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


def _rss_mb() -> float:
    """Current RSS in MB (works on Linux and macOS)."""
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / (1024 * 1024) if sys.platform == "darwin" else ru / 1024


def _smaps_rss_mb() -> float:
    """RSS from /proc/self/smaps_rollup (more accurate on Linux)."""
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if line.startswith("Rss:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return _rss_mb()


def make_tao():
    tao = Tao(f"-init {INIT_FILE} -noplot -slice_lattice {TRACK_START}:{MEAS_ELE}")
    tao.cmd(f"set beam track_start = {TRACK_START}")
    tao.cmd(f"set beam_init n_particle = {N_PARTICLE}")
    tao.cmd(f"set beam comb_ds_save = {COMB_DS_SAVE}")
    tao.cmd("set global track_type = beam")
    return tao


def set_quads(tao, k1_values):
    """Set quad K1 values in one batch, re-tracking once (LUMEBmadModel._set style)."""
    tao.cmd("set global lattice_calc_on = F")
    try:
        for ele, value in k1_values.items():
            tao.cmd(f"set ele {ele} K1 = {value}")
    finally:
        tao.cmd("set global lattice_calc_on = T")


def read_td11(tao):
    """Read beam moments at TD11 from the comb (CombStatVariable._get style)."""
    s = np.asarray(tao.bunch_comb("s"))
    idx = int(np.argmin(np.abs(s - tao.ele_head(MEAS_ELE)["s"])))

    comb = lambda name: np.asarray(tao.bunch_comb(name))[idx]
    beta_x, emit_x = comb("x.beta"), comb("x.emit")
    beta_y, emit_y = comb("y.beta"), comb("y.emit")
    return {
        "norm_emit_x": comb("x.norm_emit"),  # m-rad
        "norm_emit_y": comb("y.norm_emit"),
        "sigma_x": np.sqrt(beta_x * emit_x),  # m
        "sigma_y": np.sqrt(beta_y * emit_y),
        "n_live": int(comb("n_particle_live")),
    }


def main():
    rng = np.random.default_rng()

    print(f"# LCLS_LATTICE={LCLS_LATTICE}", file=sys.stderr, flush=True)
    print(f"# DURATION_S={DURATION_S} (0=forever)", file=sys.stderr, flush=True)
    print(f"# LOG_INTERVAL_S={LOG_INTERVAL_S}", file=sys.stderr, flush=True)
    print(f"# N_PARTICLE={N_PARTICLE}", file=sys.stderr, flush=True)
    print(f"# MALLOC_ARENA_MAX={os.environ.get('MALLOC_ARENA_MAX', 'not set')}", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    tao = make_tao()
    design_k1 = {q: tao.ele_gen_attribs(q)["K1"] for q in QUADS}

    # CSV header
    print("elapsed_s,rss_mb,total_cycles,cycles_per_s,rss_delta_mb", flush=True)

    t_start = time.monotonic()
    t_last_log = t_start
    total_cycles = 0
    rss_baseline = _smaps_rss_mb()

    print(f"# Baseline RSS: {rss_baseline:.1f} MB", file=sys.stderr, flush=True)
    print(f"# Press Ctrl+C to stop", file=sys.stderr, flush=True)
    print(file=sys.stderr, flush=True)

    deadline = t_start + DURATION_S if DURATION_S > 0 else float("inf")

    try:
        while time.monotonic() < deadline:
            k1_values = {
                q: rng.uniform(*sorted((k1 * (1 - K1_REL_RANGE), k1 * (1 + K1_REL_RANGE))))
                for q, k1 in design_k1.items()
            }
            set_quads(tao, k1_values)
            r = read_td11(tao)
            total_cycles += 1

            now = time.monotonic()
            if now - t_last_log >= LOG_INTERVAL_S:
                elapsed = now - t_start
                rss = _smaps_rss_mb()
                cps = total_cycles / elapsed
                rss_delta = rss - rss_baseline
                print(f"{elapsed:.1f},{rss:.1f},{total_cycles},{cps:.1f},{rss_delta:+.1f}", flush=True)

                # Log to stderr for container logs
                print(
                    f"[pytao-memtest] t={elapsed/60:.1f}min  RSS={rss:.1f}MB "
                    f"(delta={rss_delta:+.1f}MB)  cycles={total_cycles}  "
                    f"rate={cps:.1f}/s  n_live={r['n_live']}",
                    file=sys.stderr, flush=True,
                )
                t_last_log = now

    except KeyboardInterrupt:
        print(f"\n# Stopped by user", file=sys.stderr, flush=True)

    elapsed = time.monotonic() - t_start
    rss_final = _smaps_rss_mb()
    rss_growth = rss_final - rss_baseline
    print(f"\n# Done: {total_cycles} cycles in {elapsed:.0f}s ({total_cycles/elapsed:.1f} cyc/s)",
          file=sys.stderr, flush=True)
    print(f"# RSS: baseline={rss_baseline:.1f}MB  final={rss_final:.1f}MB  "
          f"growth={rss_growth:+.1f}MB", file=sys.stderr, flush=True)

    if rss_growth > 50:
        print(f"# VERDICT: LEAK DETECTED — RSS grew {rss_growth:.1f} MB", file=sys.stderr, flush=True)
        print(f"# This is pytao/Fortran only (no lume wrappers). Report to pytao team.",
              file=sys.stderr, flush=True)
        sys.exit(1)
    else:
        print(f"# VERDICT: OK — no significant leak from pytao/Fortran layer",
              file=sys.stderr, flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()