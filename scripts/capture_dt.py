"""Capture DT inputs and outputs over time.

Runs inside the DT pod. Saves snapshots of live inputs (from proxy) and
DT outputs (from localhost:5075) to a JSON file.

Usage:
    python scripts/capture_dt.py [--duration 60] [--output /tmp/dt_capture.json]
"""

import argparse
import json
import time

import numpy as np
from pvua import Context
from p4p.client.thread import Context as PVAContext
from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model


PV_RENAMES = {
    "sigma_z": "OTRS:IN20:571:ZRMS",
    "norm_emit_x": "OTRS:IN20:571:EMITN_X",
    "norm_emit_y": "OTRS:IN20:571:EMITN_Y",
}
PV_SUFFIX_ML = "_CU_HXR_LUME_ML_DT"
PV_SUFFIX_PH = "_CU_HXR_LUME_PH_DT"


def to_serializable(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, (np.floating, np.integer)):
        return float(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    if hasattr(val, 'item') and hasattr(val, 'dim'):
        if val.dim() == 0 or val.numel() == 1:
            return float(val.item())
        return val.detach().cpu().numpy().tolist()
    if hasattr(val, 'numpy'):
        return val.numpy().tolist()
    return str(val)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60, help="Capture duration in seconds")
    parser.add_argument("--output", default="/tmp/dt_capture.json", help="Output file path")
    args = parser.parse_args()

    print("Loading model to get variable names...")
    model = get_cu_hxr_staged_model(end_element="OTR4", n_particles=10000)

    input_names = [n for n, v in model.supported_variables.items()
                   if not v.read_only and n != "track_type"
                   and not n.endswith(":BDES")]
    output_names = [n for n, v in model.supported_variables.items() if v.read_only]
    ml_vars = set(model.lume_model_instances[0].supported_variables)

    # Build output PV name mapping
    output_pv_map = {}
    for name in output_names:
        pv_name = PV_RENAMES.get(name, name)
        if name in ml_vars:
            output_pv_map[name] = pv_name + PV_SUFFIX_ML
        else:
            output_pv_map[name] = pv_name + PV_SUFFIX_PH

    # Contexts
    input_ctx = Context()
    dt_ctx = PVAContext("pva", conf={"EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075"})

    snapshots = []
    start = time.time()
    cycle = 0

    print(f"Capturing for {args.duration}s...")
    while time.time() - start < args.duration:
        cycle += 1
        ts = time.time()

        # Capture inputs from live accelerator
        inputs = {}
        for name in input_names:
            val = input_ctx.get(name)
            inputs[name] = to_serializable(val)

        # Capture outputs from DT
        outputs = {}
        for name, dt_pv in output_pv_map.items():
            try:
                raw = dt_ctx.get(dt_pv, timeout=5)
                if hasattr(raw, 'raw') and hasattr(raw.raw, 'value'):
                    val = raw.raw.value
                elif hasattr(raw, 'value'):
                    val = raw.value
                else:
                    val = float(str(raw).split()[-1])
                outputs[name] = to_serializable(val)
            except Exception:
                outputs[name] = None

        snapshots.append({
            "timestamp": ts,
            "cycle": cycle,
            "inputs": inputs,
            "outputs": outputs,
        })

        print(f"  Cycle {cycle} captured at {time.strftime('%H:%M:%S', time.localtime(ts))}")
        time.sleep(15)

    result = {
        "capture_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": args.duration,
        "n_snapshots": len(snapshots),
        "input_names": input_names,
        "output_names": output_names,
        "output_pv_map": output_pv_map,
        "snapshots": snapshots,
    }

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved {len(snapshots)} snapshots to {args.output}")


if __name__ == "__main__":
    main()
