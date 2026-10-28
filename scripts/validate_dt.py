"""Validate DT outputs by replaying captured inputs through a local VA.

Loads a capture file (from capture_dt.py, capture_dt_gated.py, or
capture_dt_aligned.py), runs the local model with each snapshot's inputs,
and compares against the DT's outputs.

The script auto-detects the capture mode from the JSON:
  - "gated"   : snapshots use snap["inputs"] (already coherent with outputs)
  - "aligned" : per-output alignment is available; --align-strategy chooses
      per-output  : replay each output with the inputs the DT saw at its ts
      oldest      : replay ONCE with inputs at the oldest output's ts (fast)
      current     : replay with inputs_at_capture (equivalent to legacy)
  - legacy    : snap["inputs"] as before

Usage (on dev-srv09):
    python scripts/validate_dt.py /path/to/dt_capture.json
    python scripts/validate_dt.py /path/to/dt_capture_aligned.json --align-strategy oldest
    python scripts/validate_dt.py /path/to/dt_capture_aligned.json --align-strategy per-output
"""

import argparse
import json
import os
import sys

import numpy as np
_orig = np.random.default_rng
np.random.default_rng = lambda *a, **k: _orig(12345)

DT_MODEL = os.environ.get("DT_MODEL", "cu_hxr_staged")


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


def clean_inputs(inputs):
    return {k: v for k, v in (inputs or {}).items()
            if v is not None and not k.endswith(":BDES")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_file", help="Path to dt_capture.json")
    parser.add_argument("--rtol", type=float, default=0.01, help="Relative tolerance (default: 1%%)")
    parser.add_argument("--atol", type=float, default=1e-6, help="Absolute tolerance (default: 1e-6)")
    parser.add_argument("--include-stochastic", action="store_true",
                        help="Include stochastic beam-derived outputs (noisy comparison)")
    parser.add_argument("--align-strategy",
                        choices=["auto", "current", "oldest", "per-output"],
                        default="auto",
                        help="For aligned captures: which inputs to replay. "
                             "auto=oldest if available, else current. "
                             "per-output runs the model once per output (slow, most rigorous).")
    args = parser.parse_args()

    with open(args.capture_file) as f:
        data = json.load(f)

    capture_mode = data.get("capture_mode", "legacy")
    print(f"Loaded capture: {data['n_snapshots']} snapshots from {data['capture_time']} "
          f"(mode={capture_mode})")
    print(f"Tolerances: rtol={args.rtol}, atol={args.atol}")
    print(f"Loading model {DT_MODEL}...")
    if DT_MODEL == "cu_hxr_bmad":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_bmad_model
        model = get_cu_hxr_bmad_model(end_element="OTR4", track_beam=True)
    elif DT_MODEL == "cu_hxr_staged":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model
        model = get_cu_hxr_staged_model(end_element="OTR4", n_particles=10000)
    else:
        raise ValueError(f"Unknown DT_MODEL: {DT_MODEL}")

    output_names = data["output_names"]

    strategy = args.align_strategy
    if strategy == "auto":
        if capture_mode == "aligned":
            strategy = "oldest"
        else:
            strategy = "current"
    print(f"Input-selection strategy: {strategy}")

    total_ok = 0
    total_diff = 0
    total_skip = 0
    total_stochastic = 0
    diffs = []
    skipped = []

    for snap in data["snapshots"]:
        cycle = snap["cycle"]
        dt_outputs = snap["outputs"]

        if strategy == "per-output" and capture_mode == "aligned":
            # Run the model once per output using per-output-aligned inputs.
            aligned_map = snap.get("inputs_aligned_per_output", {}) or {}
            for name in output_names:
                dt_val = dt_outputs.get(name)
                if is_stochastic(name) and not args.include_stochastic:
                    total_stochastic += 1
                    continue
                inputs_for_this = clean_inputs(aligned_map.get(name))
                if not inputs_for_this:
                    # No per-output inputs (output had no timestamp) -- fall back
                    inputs_for_this = clean_inputs(
                        snap.get("inputs_at_oldest_output_ts")
                        or snap.get("inputs_at_capture")
                        or snap.get("inputs")
                    )
                model.set(inputs_for_this)
                local_outputs = model.get([name])
                local_val = local_outputs.get(name)
                match, detail = compare_values(local_val, dt_val, rtol=args.rtol, atol=args.atol)
                if match is None:
                    total_skip += 1
                    skipped.append((cycle, name, detail))
                elif match:
                    total_ok += 1
                else:
                    total_diff += 1
                    diffs.append((cycle, name, detail))
            continue

        # Non per-output: pick a single input set, replay once.
        if strategy == "oldest":
            inputs = clean_inputs(
                snap.get("inputs_at_oldest_output_ts")
                or snap.get("inputs_at_capture")
                or snap.get("inputs")
            )
        elif strategy == "current":
            inputs = clean_inputs(
                snap.get("inputs_at_capture")
                or snap.get("inputs")
            )
        else:
            inputs = clean_inputs(snap.get("inputs") or snap.get("inputs_at_capture"))

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
