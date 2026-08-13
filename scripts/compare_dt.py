"""Compare local VA model outputs against the deployed DT.

Fetches live input PVs from the accelerator, runs the staged model locally
with those inputs, and prints scalar outputs side-by-side with the DT's served values.
For tensor/array outputs, compares element-wise and reports max relative error.

Usage (from inside the DT pod):
    python scripts/compare_dt.py
"""

import os

import numpy as np
from pvua import Context
from p4p.client.thread import Context as PVAContext
from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model

DT_PVA_SERVER = os.environ.get("DT_PVA_SERVER", "127.0.0.1:5075")
END_ELEMENT = os.environ.get("END_ELEMENT", "OTR4")
N_PARTICLES = int(os.environ.get("N_PARTICLES", "10000"))

PV_RENAMES = {
    "sigma_z": "OTRS:IN20:571:ZRMS",
    "norm_emit_x": "OTRS:IN20:571:EMITN_X",
    "norm_emit_y": "OTRS:IN20:571:EMITN_Y",
}
PV_SUFFIX_ML = "_CU_HXR_LUME_ML_DT"
PV_SUFFIX_PH = "_CU_HXR_LUME_PH_DT"


def to_numpy(val):
    """Convert a value to a numeric type (scalar or numpy array)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, (np.floating, np.integer)):
        return float(val)
    if isinstance(val, np.ndarray):
        if val.ndim == 0:
            return float(val)
        return val
    # Handle torch tensors
    if hasattr(val, 'item') and hasattr(val, 'dim'):
        if val.dim() == 0 or val.numel() == 1:
            return float(val.item())
        return val.detach().cpu().numpy()
    if hasattr(val, 'numpy'):
        arr = val.numpy()
        if arr.ndim == 0:
            return float(arr)
        return arr
    if hasattr(val, '__array__'):
        arr = np.asarray(val)
        if arr.ndim == 0:
            return float(arr)
        return arr
    return val


def extract_pv_value(raw):
    """Extract the numeric value from a p4p PV result."""
    if raw is None:
        return None
    # p4p wraps values in NTScalar/NTNDArray - dig out the raw value
    try:
        # For NTScalar, access the underlying value directly
        if hasattr(raw, 'raw') and hasattr(raw.raw, 'value'):
            return raw.raw.value
        if hasattr(raw, 'value'):
            return raw.value
        # Try converting string repr for simple scalars
        val = float(str(raw).split()[-1])
        return val
    except (ValueError, IndexError, TypeError):
        return raw


def main():
    print("Loading staged model...")
    model = get_cu_hxr_staged_model(end_element=END_ELEMENT, n_particles=N_PARTICLES)

    # Fetch live inputs
    inputs = {}
    for name, var in model.supported_variables.items():
        if not var.read_only and name != "track_type":
            inputs[name] = var

    print(f"\nFetching {len(inputs)} live inputs from accelerator...")
    ctx = Context()
    live_values = {}
    unavailable = []
    for name in inputs:
        val = ctx.get(name)
        if val is not None:
            live_values[name] = val
        else:
            unavailable.append(name)

    print(f"  Got {len(live_values)} inputs, {len(unavailable)} unavailable")
    if unavailable:
        print(f"  Unavailable: {', '.join(unavailable[:5])}{'...' if len(unavailable) > 5 else ''}")

    # Run model with live inputs
    print("\nRunning model...")
    model.set(live_values if live_values else {})

    # Get outputs
    output_names = [name for name, var in model.supported_variables.items() if var.read_only]
    local_outputs = model.get(output_names)

    # Determine ML vs physics outputs
    ml_vars = set(model.lume_model_instances[0].supported_variables)

    # Fetch DT outputs and compare
    print(f"Fetching DT outputs from {DT_PVA_SERVER}...\n")
    dt_ctx = PVAContext("pva", conf={"EPICS_PVA_NAME_SERVERS": DT_PVA_SERVER})

    print(f"{'OUTPUT PV':<55} {'LOCAL':>15} {'DT':>15} {'MATCH':>8}")
    print("-" * 96)

    n_ok = 0
    n_diff = 0
    n_skip = 0

    for name in output_names:
        local_val = local_outputs.get(name)

        # Build DT PV name
        pv_name = PV_RENAMES.get(name, name)
        if name in ml_vars:
            dt_pv = pv_name + PV_SUFFIX_ML
        else:
            dt_pv = pv_name + PV_SUFFIX_PH

        # Fetch DT value
        try:
            dt_raw = dt_ctx.get(dt_pv, timeout=5)
            dt_val = extract_pv_value(dt_raw)
        except Exception:
            dt_val = None

        # Convert to numpy for comparison
        local_np = to_numpy(local_val)
        dt_np = to_numpy(dt_val)

        # Compare
        if local_np is None or dt_val is None:
            match = "TIMEOUT"
            n_skip += 1
        elif isinstance(local_np, np.ndarray) and isinstance(dt_np, np.ndarray):
            if local_np.shape != dt_np.shape:
                match = f"SHAPE {local_np.shape} vs {dt_np.shape}"
                n_diff += 1
            else:
                denom = np.maximum(np.abs(local_np), 1e-30)
                max_rel_err = np.max(np.abs(local_np - dt_np) / denom)
                if max_rel_err < 0.01:
                    match = "OK"
                    n_ok += 1
                else:
                    match = f"{max_rel_err:.1%}"
                    n_diff += 1
        elif isinstance(local_np, (int, float, np.floating, np.integer)) and isinstance(dt_np, (int, float, np.floating, np.integer)):
            local_f = float(local_np)
            dt_f = float(dt_np)
            rel_err = abs(local_f - dt_f) / max(abs(local_f), 1e-30)
            if rel_err < 0.01:
                match = "OK"
                n_ok += 1
            else:
                match = f"{rel_err:.1%}"
                n_diff += 1
        else:
            match = "SKIP"
            n_skip += 1

        # Format display
        if isinstance(local_np, np.ndarray):
            local_str = f"arr{local_np.shape}"
        elif isinstance(local_np, (int, float, np.floating, np.integer)):
            local_str = f"{float(local_np):.6g}"
        else:
            local_str = str(type(local_val).__name__)

        if dt_val is None:
            dt_str = "TIMEOUT"
        elif isinstance(dt_np, np.ndarray):
            dt_str = f"arr{dt_np.shape}"
        elif isinstance(dt_np, (int, float, np.floating, np.integer)):
            dt_str = f"{float(dt_np):.6g}"
        else:
            dt_str = str(type(dt_val).__name__)

        print(f"  {dt_pv:<53} {local_str:>15} {dt_str:>15} {match:>8}")

    print("-" * 96)
    print(f"  OK: {n_ok}  |  DIFF: {n_diff}  |  SKIP/TIMEOUT: {n_skip}")


if __name__ == "__main__":
    main()
