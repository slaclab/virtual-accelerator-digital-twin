"""Validate DT outputs by replaying captured inputs through a local VA.

Loads a capture file (from capture_dt.py), runs the local staged model with
each snapshot's inputs, and compares against the DT's outputs.

Usage (on dev-srv09):
    python scripts/validate_dt.py /path/to/dt_capture.json
"""

import argparse
import json
import sys

import numpy as np
_orig = np.random.default_rng
np.random.default_rng = lambda *a, **k: _orig(12345)

from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model


# Beam-derived outputs that are stochastic (no seed) and can't be compared
# snapshot-for-snapshot at tight tolerances.
STOCHASTIC_OUTPUTS = {
    "x", "px", "y", "py", "x.emit", "y.norm_emit", "n_particle_live",
    "output_beam",
}


def is_stochastic(name):
    """Check if an output is derived from stochastic beam sampling."""
    base = name.split(":")[-1] if ":" in name else name
    return base in STOCHASTIC_OUTPUTS or "Image:ArrayData" in name


def to_numeric(val):
    """Convert a value to float or numpy array."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, (np.floating, np.integer)):
        return float(val)
    if isinstance(val, list):
        return np.array(val, dtype=float)
    if isinstance(val, np.ndarray):
        return val.astype(float) if val.ndim > 0 else float(val)
    # Handle torch tensors
    if hasattr(val, 'item') and hasattr(val, 'dim'):
        if val.dim() == 0 or val.numel() == 1:
            return float(val.item())
        return val.detach().cpu().numpy().astype(float)
    if hasattr(val, 'numpy'):
        arr = val.numpy()
        return float(arr) if arr.ndim == 0 else arr.astype(float)
    return None


def compare_values(local_val, dt_val, rtol=0.01, atol=1e-6):
    """Compare two values with combined absolute+relative tolerance.

    Returns (match: bool, detail: str)
    """
    if local_val is None or dt_val is None:
        return None, "skip:null"

    local_arr = to_numeric(local_val)
    dt_arr = to_numeric(dt_val)

    if local_arr is None or dt_arr is None:
        return None, "skip:type"

    if isinstance(local_arr, np.ndarray) and isinstance(dt_arr, np.ndarray):
        # Flatten both to handle EPICS N-D → 1-D flattening
        local_flat = np.ravel(local_arr)
        dt_flat = np.ravel(dt_arr)

        if local_flat.size != dt_flat.size:
            return False, f"size mismatch: {local_flat.size} vs {dt_flat.size}"

        if np.allclose(local_flat, dt_flat, rtol=rtol, atol=atol):
            return True, "ok"
        else:
            # Report max error with context
            abs_diff = np.abs(local_flat - dt_flat)
            worst_idx = np.argmax(abs_diff)
            worst_abs = abs_diff[worst_idx]
            worst_local = local_flat[worst_idx]
            worst_dt = dt_flat[worst_idx]
            return False, f"max_abs_diff={worst_abs:.4g} at idx={worst_idx} (local={worst_local:.4g}, dt={worst_dt:.4g})"

    elif isinstance(local_arr, (int, float, np.floating, np.integer)):
        local_f = float(local_arr)
        dt_f = float(dt_arr)
        if np.isclose(local_f, dt_f, rtol=rtol, atol=atol):
            return True, "ok"
        else:
            abs_diff = abs(local_f - dt_f)
            return False, f"local={local_f:.6g} dt={dt_f:.6g} abs_diff={abs_diff:.4g}"

    return None, "skip:type"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_file", help="Path to dt_capture.json")
    parser.add_argument("--rtol", type=float, default=0.01, help="Relative tolerance (default: 1%%)")
    parser.add_argument("--atol", type=float, default=1e-6, help="Absolute tolerance (default: 1e-6)")
    parser.add_argument("--include-stochastic", action="store_true",
                        help="Include stochastic beam-derived outputs (noisy comparison)")
    args = parser.parse_args()

    with open(args.capture_file) as f:
        data = json.load(f)

    print(f"Loaded capture: {data['n_snapshots']} snapshots from {data['capture_time']}")
    print(f"Tolerances: rtol={args.rtol}, atol={args.atol}")
    print(f"Loading staged model...")
    model = get_cu_hxr_staged_model(end_element="OTR4", n_particles=10000)

    output_names = data["output_names"]

    total_ok = 0
    total_diff = 0
    total_skip = 0
    total_stochastic = 0
    diffs = []
    skipped = []

    for snap in data["snapshots"]:
        cycle = snap["cycle"]
        inputs = {k: v for k, v in snap["inputs"].items()
                  if v is not None and not k.endswith(":BDES")}
        dt_outputs = snap["outputs"]

        # Run local model with captured inputs
        model.set(inputs)
        local_outputs = model.get(output_names)

        for name in output_names:
            local_val = local_outputs.get(name)
            dt_val = dt_outputs.get(name)

            # Skip stochastic outputs unless explicitly included
            if is_stochastic(name) and not args.include_stochastic:
                total_stochastic += 1
                continue

            match, detail = compare_values(local_val, dt_val, rtol=args.rtol, atol=args.atol)

            if match is None:
                total_skip += 1
                skipped.append((cycle, name, detail))
            elif match:
                total_ok += 1
            else:
                total_diff += 1
                diffs.append((cycle, name, detail))

    # Summary
    print(f"\n{'='*80}")
    print(f"RESULTS: {total_ok} OK | {total_diff} DIFF | {total_skip} SKIP | {total_stochastic} STOCHASTIC (excluded)")
    print(f"{'='*80}")

    if diffs:
        print(f"\nDifferences:")
        for cycle, name, detail in diffs[:30]:
            print(f"  [cycle {cycle}] {name}: {detail}")
        if len(diffs) > 30:
            print(f"  ... and {len(diffs) - 30} more")
    else:
        print("\nAll deterministic outputs match!")

    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for cycle, name, detail in skipped:
            print(f"  [cycle {cycle}] {name}: {detail}")


if __name__ == "__main__":
    main()
