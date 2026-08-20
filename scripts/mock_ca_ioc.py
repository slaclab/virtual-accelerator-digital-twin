#!/usr/bin/env python
"""Mock Channel Access / PVA IOC for development.

Serves the 16 real input PVs that the DT reads via take_snapshot().
Values are initialized to LCLS design values and drift with realistic
slow Gaussian noise (~0.1% per update) at configurable rate.

All PVs are served via both CA (pcaspy) and PVA (p4p) so pvua can
connect via either protocol, matching real machine behaviour.

Environment variables:
    UPDATE_RATE_HZ   - PV update frequency (default: 1)
    LOG_LEVEL        - Python logging level (default: INFO)
    NOISE_SIGMA_PCT  - Gaussian noise as % of nominal (default: 0.1)
"""

import logging
import os
import random
import threading
import time

LOG = logging.getLogger("mock_ca_ioc")

# 16 real snapshot PVs with design nominals and units.
# Nominally congruent with LCLS injector operating point.
INPUT_PVS = [
    # name,                       nominal,       unit,       noise_pct
    ("SOLN:IN20:121:BCTRL",       0.477969,      "kG*m",     0.05),
    ("QUAD:IN20:121:BCTRL",      -0.00149923,    "kG",       0.1),
    ("QUAD:IN20:122:BCTRL",      -0.000687299,   "kG",       0.1),
    ("ACCL:IN20:300:L0A_PDES",   -9.53597,       "deg",      0.02),
    ("ACCL:IN20:400:L0B_PDES",    9.85566,       "deg",      0.02),
    ("QUAD:IN20:361:BCTRL",      -2.00059,       "kG",       0.1),
    ("QUAD:IN20:371:BCTRL",       2.00059,       "kG",       0.1),
    ("QUAD:IN20:425:BCTRL",      -1.08076,       "kG",       0.1),
    ("QUAD:IN20:441:BCTRL",      -0.179388,      "kG",       0.1),
    ("QUAD:IN20:511:BCTRL",       2.85217,       "kG",       0.1),
    ("QUAD:IN20:525:BCTRL",      -3.2184,        "kG",       0.1),
    ("QUAD:IN20:631:BCTRL",       7.33536,       "kG",       0.1),
    ("XCOR:IN20:641:BCTRL",       0.0,           "kG*m",     0.001),
    ("YCOR:IN20:642:BCTRL",       0.0,           "kG*m",     0.001),
    ("QUAD:IN20:651:BCTRL",      -5.82109,       "kG",       0.1),
    ("BEND:IN20:661:BCTRL",       0.134999,      "GeV/c",    0.01),
]


def _start_pva_server(pvs: list) -> None:
    """Serve all PVs via PVA using p4p SharedPV."""
    from p4p.nt import NTScalar
    from p4p.server import Server
    from p4p.server.thread import SharedPV

    providers = {}
    pv_objects = {}

    for name, nominal, unit, _ in pvs:
        nt = NTScalar("d")
        pv = SharedPV(initial=nt.wrap(float(nominal)))
        providers[name] = pv
        pv_objects[name] = (pv, nominal)

    server = Server(providers=[providers])  # noqa: F841
    LOG.info("PVA server started for %d PVs", len(pvs))
    return pv_objects


def _start_ca_server(pvs: list) -> object:
    """Serve all PVs via CA using pcaspy."""
    try:
        import pcaspy
        import pcaspy.cas
    except ImportError:
        LOG.warning("pcaspy not available — CA server disabled, PVA only")
        return None

    pvdb = {}
    for name, nominal, unit, _ in pvs:
        pvdb[name] = {
            "type": "float",
            "value": float(nominal),
            "unit": unit,
            "prec": 6,
            "lolim": nominal * 2 if nominal < 0 else -abs(nominal) * 2,
            "hilim": abs(nominal) * 2 if nominal != 0 else 1.0,
        }

    class _Driver(pcaspy.Driver):
        pass

    server = pcaspy.SimpleServer()
    server.createPV("", pvdb)
    driver = _Driver()

    def _ca_loop():
        while True:
            server.process(0.05)

    threading.Thread(target=_ca_loop, daemon=True, name="ca-server").start()
    LOG.info("CA server started for %d PVs", len(pvs))
    return driver


def _update_loop(pva_pvs: dict, ca_driver, pvs: list, rate_hz: float, noise_pct: float) -> None:
    """Update all PVs with slow Gaussian noise at rate_hz."""
    from p4p.nt import NTScalar

    interval = 1.0 / rate_hz
    # Track current drifted values
    current = {name: nominal for name, nominal, _, _ in pvs}

    while True:
        t0 = time.monotonic()
        for name, nominal, unit, pv_noise_pct in pvs:
            effective_noise = abs(nominal) * (pv_noise_pct / 100.0) * noise_pct / 0.1
            if effective_noise == 0:
                effective_noise = 1e-6
            # Slow random walk — drift toward nominal with damping
            current[name] += random.gauss(0, effective_noise)
            current[name] = current[name] * 0.999 + nominal * 0.001  # mean-revert

            # Update PVA
            if name in pva_pvs:
                pv, _ = pva_pvs[name]
                try:
                    pv.post(NTScalar("d").wrap(current[name]))
                except Exception as e:
                    LOG.debug("PVA post error for %s: %s", name, e)

            # Update CA
            if ca_driver is not None:
                try:
                    ca_driver.setParam(name, current[name])
                except Exception as e:
                    LOG.debug("CA setParam error for %s: %s", name, e)

        if ca_driver is not None:
            try:
                ca_driver.updatePVs()
            except Exception:
                pass

        elapsed = time.monotonic() - t0
        sleep = interval - elapsed
        if sleep > 0:
            time.sleep(sleep)


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rate_hz = float(os.environ.get("UPDATE_RATE_HZ", "1"))
    noise_pct = float(os.environ.get("NOISE_SIGMA_PCT", "0.1"))

    LOG.info("Starting mock CA/PVA IOC for %d input PVs at %.1f Hz", len(INPUT_PVS), rate_hz)
    LOG.info("Noise: %.2f%% of nominal per update", noise_pct)

    pva_pvs = _start_pva_server(INPUT_PVS)
    ca_driver = _start_ca_server(INPUT_PVS)

    update_thread = threading.Thread(
        target=_update_loop,
        args=(pva_pvs, ca_driver, INPUT_PVS, rate_hz, noise_pct),
        daemon=True,
        name="pv-updater",
    )
    update_thread.start()

    LOG.info("Mock IOC running — Ctrl+C to stop")
    LOG.info("PVs served:")
    for name, nominal, unit, _ in INPUT_PVS:
        LOG.info("  %-35s = %10.6g  %s", name, nominal, unit)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOG.info("Shutting down")


if __name__ == "__main__":
    main()
