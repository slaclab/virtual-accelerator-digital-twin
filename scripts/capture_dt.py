"""Capture DT inputs and outputs over time.

Runs inside the DT pod. Saves snapshots of live inputs (from proxy) and
DT outputs (from localhost:5075) to a JSON file.

For each PV, records both value and the PV's own timeStamp (when available),
so validate_dt.py can detect stale outputs and input/output skew.

Usage:
    python scripts/capture_dt.py [--duration 60] [--output /tmp/dt_capture.json]
"""

import argparse
import json
import os
import time

import numpy as np
from pvua import Context
from p4p.client.thread import Context as PVAContext


PV_RENAMES = {
    "sigma_z": "OTRS:IN20:571:ZRMS",
    "norm_emit_x": "OTRS:IN20:571:EMITN_X",
    "norm_emit_y": "OTRS:IN20:571:EMITN_Y",
}

# Suffix scheme: single PV_SUFFIX (used by pure models like cu_hxr_bmad) takes
# precedence. If unset, fall back to the ML/PH split used by staged models.
PV_SUFFIX = os.environ.get("PV_SUFFIX", "")
PV_SUFFIX_ML = os.environ.get("PV_SUFFIX_ML", "_CU_HXR_LUME_ML_DT")
PV_SUFFIX_PH = os.environ.get("PV_SUFFIX_PH", "_CU_HXR_LUME_PH_DT")
DT_MODEL = os.environ.get("DT_MODEL", "cu_hxr_staged")


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


def extract_p4p_timestamp(raw):
    """Return secondsPastEpoch + nanoseconds/1e9 from a p4p Value, or None."""
    ts_obj = None
    if hasattr(raw, 'raw') and hasattr(raw.raw, 'timeStamp'):
        ts_obj = raw.raw.timeStamp
    elif hasattr(raw, 'timeStamp'):
        ts_obj = raw.timeStamp
    if ts_obj is None:
        return None
    try:
        secs = float(getattr(ts_obj, 'secondsPastEpoch', 0) or 0)
        nsec = float(getattr(ts_obj, 'nanoseconds', 0) or 0)
        if secs == 0.0 and nsec == 0.0:
            return None
        return secs + nsec / 1e9
    except Exception:
        return None


def extract_pvua_timestamp(raw):
    """Best-effort timestamp from a pvua Context.get() result."""
    if raw is None:
        return None
    for attr in ("timestamp", "timeStamp", "time"):
        ts = getattr(raw, attr, None)
        if ts is None:
            continue
        try:
            f = float(ts)
            return f if f > 0 else None
        except (TypeError, ValueError):
            pass
    return None


def extract_pvua_value(raw):
    """Best-effort value from a pvua Context.get() result."""
    if raw is None:
        return None
    for attr in ("value", "val"):
        if hasattr(raw, attr):
            return getattr(raw, attr)
    return raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60, help="Capture duration in seconds")
    parser.add_argument("--output", default="/tmp/dt_capture.json", help="Output file path")
    args = parser.parse_args()

    print(f"Loading model {DT_MODEL} to get variable names...")
    if DT_MODEL == "cu_hxr_bmad":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_bmad_model
        model = get_cu_hxr_bmad_model(end_element="OTR4", track_beam=True)
    elif DT_MODEL == "cu_hxr_staged":
        from virtual_accelerator.models.cu_hxr import get_cu_hxr_staged_model
        model = get_cu_hxr_staged_model(end_element="OTR4", n_particles=10000)
    else:
        raise ValueError(f"Unknown DT_MODEL: {DT_MODEL}")

    input_names = [n for n, v in model.supported_variables.items()
                   if not v.read_only and n != "track_type"
                   and not n.endswith(":BDES")]
    output_names = [n for n, v in model.supported_variables.items() if v.read_only]

    # Drop huge array PVs (OTR camera images ~30 MB each). validate_dt.py
    # already flags them as stochastic, so we lose no signal.
    SKIP_SUBSTRINGS = ("Image:ArrayData",)
    output_names = [n for n in output_names
                    if not any(s in n for s in SKIP_SUBSTRINGS)]

    # Build output PV name mapping. Single PV_SUFFIX wins if set (pure models);
    # otherwise split on ML vs physics variables (staged models).
    if PV_SUFFIX:
        output_pv_map = {name: PV_RENAMES.get(name, name) + PV_SUFFIX
                         for name in output_names}
    else:
        ml_vars = set(model.lume_model_instances[0].supported_variables)
        output_pv_map = {}
        for name in output_names:
            pv_name = PV_RENAMES.get(name, name)
            output_pv_map[name] = pv_name + (PV_SUFFIX_ML if name in ml_vars else PV_SUFFIX_PH)

    # Contexts
    input_ctx = Context()
    dt_ctx = PVAContext("pva", conf={"EPICS_PVA_NAME_SERVERS": "127.0.0.1:5075"})

    snapshots = []
    start = time.time()
    cycle = 0

    print(f"Capturing for {args.duration}s...")
    while time.time() - start < args.duration:
        cycle += 1
        ts_pre = time.time()

        # Capture outputs from DT first so their timestamps are as close as
        # possible to the input read below. Each output records its own PV
        # timeStamp so we can detect staleness relative to inputs.
        outputs = {}
        output_ts = {}
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
                output_ts[name] = extract_p4p_timestamp(raw)
            except Exception:
                outputs[name] = None
                output_ts[name] = None

        ts_mid = time.time()

        # Capture inputs from live accelerator
        inputs = {}
        input_ts = {}
        for name in input_names:
            raw = input_ctx.get(name)
            inputs[name] = to_serializable(extract_pvua_value(raw))
            input_ts[name] = extract_pvua_timestamp(raw)

        ts_post = time.time()

        # Skew diagnostics: how far behind the DT outputs are vs input reads.
        out_ts_vals = [t for t in output_ts.values() if t is not None]
        in_ts_vals = [t for t in input_ts.values() if t is not None]
        if out_ts_vals:
            out_age_min = ts_post - max(out_ts_vals)
            out_age_max = ts_post - min(out_ts_vals)
            out_spread = max(out_ts_vals) - min(out_ts_vals)
        else:
            out_age_min = out_age_max = out_spread = None
        if in_ts_vals and out_ts_vals:
            in_out_skew = np.median(in_ts_vals) - np.median(out_ts_vals)
        else:
            in_out_skew = None

        snapshots.append({
            "timestamp": ts_pre,
            "cycle": cycle,
            "read_wall_output_start": ts_pre,
            "read_wall_input_start": ts_mid,
            "read_wall_end": ts_post,
            "inputs": inputs,
            "input_timestamps": input_ts,
            "outputs": outputs,
            "output_timestamps": output_ts,
            "diagnostics": {
                "n_output_ts_present": len(out_ts_vals),
                "n_output_ts_missing": len(output_ts) - len(out_ts_vals),
                "n_input_ts_present": len(in_ts_vals),
                "output_age_min_s": out_age_min,
                "output_age_max_s": out_age_max,
                "output_ts_spread_s": out_spread,
                "input_vs_output_median_skew_s": in_out_skew,
            },
        })

        stamp_report = "no-ts" if not out_ts_vals else (
            f"out_age={out_age_min:.2f}s..{out_age_max:.2f}s spread={out_spread:.2f}s"
        )
        skew_report = "" if in_out_skew is None else f" skew(in-out)={in_out_skew:+.2f}s"
        print(f"  Cycle {cycle} at {time.strftime('%H:%M:%S', time.localtime(ts_pre))}: "
              f"{stamp_report}{skew_report}")
        time.sleep(15)

    # Roll-up diagnostics across the run.
    all_out_ages = [s["diagnostics"]["output_age_max_s"] for s in snapshots
                    if s["diagnostics"]["output_age_max_s"] is not None]
    all_skews = [s["diagnostics"]["input_vs_output_median_skew_s"] for s in snapshots
                 if s["diagnostics"]["input_vs_output_median_skew_s"] is not None]
    n_ts_missing_any = sum(1 for s in snapshots
                           if s["diagnostics"]["n_output_ts_missing"] > 0)

    summary = {
        "n_snapshots_with_output_ts": len(all_out_ages),
        "n_snapshots_missing_any_output_ts": n_ts_missing_any,
        "output_age_max_s_median": float(np.median(all_out_ages)) if all_out_ages else None,
        "output_age_max_s_p95": float(np.percentile(all_out_ages, 95)) if all_out_ages else None,
        "input_output_skew_s_median": float(np.median(all_skews)) if all_skews else None,
    }

    result = {
        "capture_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": args.duration,
        "n_snapshots": len(snapshots),
        "input_names": input_names,
        "output_names": output_names,
        "output_pv_map": output_pv_map,
        "summary": summary,
        "snapshots": snapshots,
    }

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nSaved {len(snapshots)} snapshots to {args.output}")
    print(f"Summary: {json.dumps(summary, indent=2)}")


if __name__ == "__main__":
    main()
