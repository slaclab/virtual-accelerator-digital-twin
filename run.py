"""Entry point for the virtual accelerator digital twin container.

Configurable via environment variables:
    MODEL          - Model to run (default: cu_hxr_bmad)
    END_ELEMENT    - Lattice end element (default: OTR4)
    N_PARTICLES    - Number of particles (default: 10000)
    LOG_LEVEL      - Logging level (default: INFO)
"""

import os
import sys


def main():
    model = os.environ.get("MODEL", "cu_hxr_bmad")
    end_element = os.environ.get("END_ELEMENT", "OTR4")
    n_particles = os.environ.get("N_PARTICLES", "10000")
    log_level = os.environ.get("LOG_LEVEL", "INFO")

    sys.argv = [
        "runners",
        model,
        "--end-element", end_element,
        "--n-particles", n_particles,
        "--log-level", log_level,
    ]

    from virtual_accelerator.models.runners import main as runner_main
    runner_main()


if __name__ == "__main__":
    main()
