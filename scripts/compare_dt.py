"""Compare local VA model outputs against the deployed DT.

Fetches live input PVs from the accelerator, runs the staged model locally
with those inputs, and prints outputs side-by-side with the DT's served values.

Requires:
    - EPICS access to live PVs (run from a machine with CA/PVA access)
    - EPICS_PVA_NAME_SERVERS set to reach the DT pod (e.g. via port-forward)

Usage:
    python scripts/compare_dt.py
"""

import os
import sys

from pvua import Context
from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model

DT_PVA_SERVER = os.environ.get("DT_PVA_SERVER", "127.0.0.1:5075")
# When running inside the DT pod, live PVs come from the proxy and
# DT outputs are on localhost:5075. No port-forwarding needed.
END_ELEMENT = os.environ.get("END_ELEMENT", "OTR4")
N_PARTICLES = int(os.environ.get("N_PARTICLES", "10000"))

PV_RENAMES = {
    "sigma_z": "OTRS:IN20:571:ZRMS",
    "norm_emit_x": "OTRS:IN20:571:EMITN_X",
    "norm_emit_y": "OTRS:IN20:571:EMITN_Y",
}
PV_SUFFIX_ML = "_CU_HXR_LUME_ML_DT"
PV_SUFFIX_PH = "_CU_HXR_LUME_PH_DT"


def main():
    print("Loading staged model...")
    model = get_cu_hxr_staged_model(end_element=END_ELEMENT, n_particles=N_PARTICLES)

    # Collect all input variable names
    inputs = {}
    for name, var in model.supported_variables.items():
        if not var.read_only and name != "track_type":
            inputs[name] = var

    # Fetch live values from the accelerator
    print(f"\nFetching {len(inputs)} live inputs from accelerator...")
    ctx = Context()
    live_values = {}
    for name in inputs:
        val = ctx.get(name)
        if val is not None:
            live_values[name] = val
            print(f"  {name} = {val}")
        else:
            print(f"  {name} = None (UNAVAILABLE, using default)")

    # Set live values on the model
    if live_values:
        model.set(live_values)
    print(f"\nSet {len(live_values)} live inputs on model.")

    # Evaluate by setting inputs (each stage runs update_state internally)
    print("Running model...")
    model.set(live_values if live_values else {})

    # Get output values from the model
    output_names = [name for name, var in model.supported_variables.items() if var.read_only]
    local_outputs = model.get(output_names)

    # Determine which outputs are ML vs physics
    ml_vars = set(model.lume_model_instances[0].supported_variables)

    # Print local outputs and compare with DT
    print(f"\nFetching DT outputs from {DT_PVA_SERVER}...")
    from p4p.client.thread import Context as PVAContext
    dt_ctx = PVAContext("pva", conf={"EPICS_PVA_NAME_SERVERS": DT_PVA_SERVER})

    print(f"\n{'OUTPUT PV':<55} {'LOCAL':>15} {'DT':>15} {'MATCH':>7}")
    print("-" * 95)

    for name in output_names:
        local_val = local_outputs.get(name)

        # Build the DT PV name with suffix and renames
        pv_name = PV_RENAMES.get(name, name)
        if name in ml_vars:
            dt_pv = pv_name + PV_SUFFIX_ML
        else:
            dt_pv = pv_name + PV_SUFFIX_PH

        try:
            dt_raw = dt_ctx.get(dt_pv, timeout=5)
            if hasattr(dt_raw, 'raw') and 'value' in dt_raw.raw:
                dt_val = dt_raw.raw.value
            elif hasattr(dt_raw, 'value'):
                dt_val = dt_raw.value
            else:
                dt_val = float(str(dt_raw).split()[-1])
        except Exception:
            dt_val = "TIMEOUT"

        import numpy as np
        try:
            if isinstance(local_val, (int, float)) and isinstance(dt_val, (int, float)):
                rel_err = abs(local_val - dt_val) / max(abs(local_val), 1e-30)
                match = "OK" if rel_err < 0.01 else f"{rel_err:.1%}"
            elif hasattr(local_val, 'shape') or hasattr(dt_val, 'shape'):
                match = "ARRAY"
            elif local_val == dt_val:
                match = "OK"
            else:
                match = "DIFF"
        except (ValueError, TypeError):
            match = "SKIP"

        local_str = f"{local_val:.6g}" if isinstance(local_val, (int, float)) else type(local_val).__name__
        if isinstance(dt_val, str):
            dt_str = dt_val
        elif isinstance(dt_val, (int, float)):
            dt_str = f"{dt_val:.6g}"
        else:
            dt_str = type(dt_val).__name__
        print(f"  {dt_pv:<53} {local_str:>15} {dt_str:>15} {match:>7}")


if __name__ == "__main__":
    main()
