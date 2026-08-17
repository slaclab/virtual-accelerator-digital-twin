"""Entry point for the virtual accelerator digital twin container.

Configurable via environment variables:
    MODEL          - Model to run (default: cu_hxr_bmad)
    END_ELEMENT    - Lattice end element (default: OTR4)
    N_PARTICLES    - Number of particles (default: 10000)
    LOG_LEVEL      - Logging level (default: INFO)
    REMOTE_INPUTS  - Read inputs from prod EPICS (default: false)
    PV_SUFFIX      - Suffix appended to served PV names (default: none)
    PV_SUFFIX_ML   - Suffix for ML model outputs in staged models (default: none)
    PV_SUFFIX_PH   - Suffix for physics model outputs in staged models (default: none)
    PV_RENAMES     - JSON dict of PV name renames applied before suffix (default: none)
"""

import json
import os
import sys


def main():
    model_name = os.environ.get("MODEL", "cu_hxr_bmad")
    end_element = os.environ.get("END_ELEMENT", "OTR4")
    n_particles = int(os.environ.get("N_PARTICLES", "10000"))
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    remote_inputs = os.environ.get("REMOTE_INPUTS", "").lower() in ("true", "1", "yes")
    pv_suffix = os.environ.get("PV_SUFFIX", "")
    pv_suffix_ml = os.environ.get("PV_SUFFIX_ML", "")
    pv_suffix_ph = os.environ.get("PV_SUFFIX_PH", "")
    pv_renames = json.loads(os.environ.get("PV_RENAMES", "{}"))

    import logging
    logging.basicConfig(level=getattr(logging, log_level))
    logging.getLogger("pytao").setLevel(logging.WARNING)

    from lume_pva.runner import Runner
    from virtual_accelerator.models.cu_hxr import (
        get_cu_hxr_bmad_model,
        get_cu_hxr_staged_model,
    )
    from virtual_accelerator.models.facet2 import (
        get_facet_bmad_model,
        get_facet_staged_model,
    )

    if model_name == "cu_hxr_bmad":
        model = get_cu_hxr_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "cu_hxr_staged":
        model = get_cu_hxr_staged_model(end_element=end_element, n_particles=n_particles)
    elif model_name == "facet_bmad":
        model = get_facet_bmad_model(end_element=end_element, track_beam=True)
    elif model_name == "facet_staged":
        model = get_facet_staged_model(end_element=end_element, n_particles=n_particles)
    else:
        raise ValueError(f"Unknown model: {model_name}")

    config = Runner.generate_config(model, remote_inputs=remote_inputs)
    config["protocol"] = ["pva"]
    config["update_rate"] = 0

    # Apply PV renames before suffix
    for k, v in config['variables'].items():
        if v['pv'] in pv_renames:
            v['pv'] = pv_renames[v['pv']]

    # Internal model variables that aren't real PVs — skip suffix
    skip_suffix = {"name"}

    # Apply differentiated suffixes for staged models (ML vs physics)
    if (pv_suffix_ml or pv_suffix_ph) and hasattr(model, 'lume_model_instances'):
        ml_vars = set(model.lume_model_instances[0].supported_variables)
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                if k in ml_vars:
                    v['pv'] = v['pv'] + pv_suffix_ml
                else:
                    v['pv'] = v['pv'] + pv_suffix_ph
    elif pv_suffix:
        for k, v in config['variables'].items():
            if v['mode'] == 'ro' and k not in skip_suffix:
                v['pv'] = v['pv'] + pv_suffix

    config["remote_model_mode"] = "snapshot"

    # Exclude variables that shouldn't be read remotely:
    # - track_type: internal model variable, not a real PV
    # - :BDES: conflicts with :BCTRL for the same physical field (last-write-wins)
    for k, v in config["variables"].items():
        if k == "track_type" or k.endswith(":BDES"):
            v["mode"] = "rw"

    runner = Runner(model, config=config)

    if remote_inputs:
        import gc
        import threading
        import torch

        def snapshot_loop(runner):
            cycle = 0
            with torch.no_grad():
                while True:
                    runner.take_snapshot()
                    cycle += 1
                    if cycle % 50 == 0:
                        gc.collect()

        t = threading.Thread(target=snapshot_loop, args=(runner,), daemon=True)
        t.start()

    runner.run()


if __name__ == "__main__":
    main()
