"""Entry point for the virtual accelerator digital twin container.

Configurable via environment variables:
    MODEL          - Model to run (default: cu_hxr_bmad)
    END_ELEMENT    - Lattice end element (default: OTR4)
    N_PARTICLES    - Number of particles (default: 10000)
    LOG_LEVEL      - Logging level (default: INFO)
    REMOTE_INPUTS  - Read inputs from prod EPICS (default: false)
    PV_SUFFIX      - Suffix appended to served PV names (default: none)
"""

import os
import sys


def main():
    model_name = os.environ.get("MODEL", "cu_hxr_bmad")
    end_element = os.environ.get("END_ELEMENT", "OTR4")
    n_particles = int(os.environ.get("N_PARTICLES", "10000"))
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    remote_inputs = os.environ.get("REMOTE_INPUTS", "").lower() in ("true", "1", "yes")
    pv_suffix = os.environ.get("PV_SUFFIX", "")

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

    if pv_suffix:
        for k, v in config['variables'].items():
            if v['mode'] in ['ro', 'rw']:
                v['pv'] = v['pv'] + pv_suffix

    runner = Runner(model, config=config)

    if remote_inputs:
        import threading
        import time

        def sequential_loop(runner):
            """get inputs -> run model -> write outputs -> repeat"""
            log = logging.getLogger("LumePva")
            remote_pvs = list(runner.subs.keys())
            log.info(f"Starting sequential loop with {len(remote_pvs)} remote PVs")
            while True:
                new_values = {}
                for pv in remote_pvs:
                    var_name = runner.pv_to_var.get(pv, pv)
                    try:
                        val = runner.pvua_context.get(pv, timeout=1)
                        if val is not None:
                            new_values[var_name] = {"value": val, "ts": time.monotonic()}
                    except Exception as e:
                        log.warning(f"Failed to get {pv}: {e}")
                log.info(f"Fetched {len(new_values)} inputs, running model...")
                runner._enqueue(new_values)

        t = threading.Thread(target=sequential_loop, args=(runner,), daemon=True)
        t.start()

    runner.run()


if __name__ == "__main__":
    main()
