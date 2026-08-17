import json

  # Simulate what generate_config returns for a staged model
config = {
      "variables": {
          "OTRS:IN20:571:XRMS": {"pv": "OTRS:IN20:571:XRMS", "mode": "ro"},
          "OTRS:IN20:571:YRMS": {"pv": "OTRS:IN20:571:YRMS", "mode": "ro"},
          "sigma_z":            {"pv": "sigma_z", "mode": "ro"},
          "norm_emit_x":        {"pv": "norm_emit_x", "mode": "ro"},
          "norm_emit_y":        {"pv": "norm_emit_y", "mode": "ro"},
          "BPMS:IN20:581:TMIT": {"pv": "BPMS:IN20:581:TMIT", "mode": "ro"},
          "BPMS:IN20:581:X":    {"pv": "BPMS:IN20:581:X", "mode": "ro"},
          "SOLN:IN20:121:BACT": {"pv": "SOLN:IN20:121:BACT", "mode": "rw"},
      }
  }

  # Simulated ML model supported_variables (instance[0])
ml_vars = {"OTRS:IN20:571:XRMS", "OTRS:IN20:571:YRMS", "sigma_z", "norm_emit_x", "norm_emit_y"}

pv_renames = {"sigma_z": "OTRS:IN20:571:ZRMS", "norm_emit_x": "OTRS:IN20:571:EMITN_X", "norm_emit_y":
  "OTRS:IN20:571:EMITN_Y"}

  # Apply renames
for k, v in config['variables'].items():
    if v['pv'] in pv_renames:
        v['pv'] = pv_renames[v['pv']]

  # Apply suffixes
for k, v in config['variables'].items():
    if v['mode'] in ['ro', 'rw']:
        if k in ml_vars:
              v['pv'] += "_CU_HXR_LUME_ML_DT"
        else:
            v['pv'] += "_CU_HXR_LUME_PH_DT"

print("=== Output PVs ===")
for k, v in config['variables'].items():
    if v['mode'] in ['ro', 'rw']:
        src = "ML" if k in ml_vars else "PH"
        print(f"  [{src}] {k:30s} -> {v['pv']}")