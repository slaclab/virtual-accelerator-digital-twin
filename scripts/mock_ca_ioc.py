#!/usr/bin/env python
"""Mock Channel Access / PVA IOC for development.

Serves all PVs declared in scripts/pv_list.txt (next to this file).
PVs are auto-classified by name pattern into:
  - float scalars  (updated with slow Gaussian noise)
  - int scalars    (static initial value)
  - string scalars (static, e.g. track_type)
  - float arrays   (Image:ArrayData, mat6, vec0 — static)

Values are initialized to LCLS design values where known, otherwise to
sensible nominal values derived from the PV name.

All PVs are served via PVA (p4p); CA (pcaspy) is opt-in via ENABLE_CA=true.

Environment variables:
    UPDATE_RATE_HZ   - PV update frequency (default: 1)
    LOG_LEVEL        - Python logging level (default: INFO)
    NOISE_SIGMA_PCT  - Gaussian noise as % of nominal (default: 0.1)
    PV_LIST          - Path to PV list file (default: pv_list.txt next to script)
"""

import logging
import os
import pathlib
import random
import threading
import time

import numpy as np

os.environ.setdefault("EPICS_PVA_AUTO_ADDR_LIST", "YES")

LOG = logging.getLogger("mock_ca_ioc")

from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

# ---------------------------------------------------------------------------
# Known BCTRL nominals (LCLS injector design values)
# ---------------------------------------------------------------------------

_BCTRL_NOMINALS = {
    "SOLN:IN20:121": 0.477969,
    "QUAD:IN20:121": -0.00149923,
    "QUAD:IN20:122": -0.000687299,
    "QUAD:IN20:361": -2.00059,
    "QUAD:IN20:371":  2.00059,
    "QUAD:IN20:425": -1.08076,
    "QUAD:IN20:441": -0.179388,
    "QUAD:IN20:511":  2.85217,
    "QUAD:IN20:525": -3.2184,
    "QUAD:IN20:631":  7.33536,
    "QUAD:IN20:651": -5.82109,
    "XCOR:IN20:641":  0.0,
    "YCOR:IN20:642":  0.0,
    "BEND:IN20:661":  0.134999,
}

# Image size for OTR screen ArrayData PVs
_IMAGE_ROWS = 50
_IMAGE_COLS = 50


# ---------------------------------------------------------------------------
# PV classification helpers
# ---------------------------------------------------------------------------

def _device_prefix(name: str) -> str:
    """Extract device prefix (part before first field separator after last colon block)."""
    # e.g. "QUAD:IN20:631:BCTRL_CU_HXR_LUME_PH_DT" → "QUAD:IN20:631"
    parts = name.split(":")
    if len(parts) >= 3:
        return ":".join(parts[:3])
    return name


def _field_base(name: str) -> str:
    """Return the field portion without suffix, e.g. 'BCTRL', 'Image:ArrayData', 'mat6'."""
    # Strip known environment suffixes
    for sfx in ("_CU_HXR_LUME_ML_DT", "_CU_HXR_LUME_PH_DT", "_DEV_LUME_DT"):
        if name.endswith(sfx):
            name = name[: -len(sfx)]
            break
    # Field is everything after the last colon-separated device block
    parts = name.split(":")
    if len(parts) >= 4:
        return ":".join(parts[3:])
    if len(parts) == 1:
        return name  # bare name like "track_type"
    return parts[-1]


def _classify(name: str):
    """Return (pv_type, nominal, unit, noise_pct).

    pv_type: "f" | "i" | "s" | "af" | "ai"
    nominal: scalar or list depending on pv_type
    """
    field = _field_base(name)
    dev = _device_prefix(name)
    ctrl_nom = _BCTRL_NOMINALS.get(dev, 0.0)

    # ---- array types -------------------------------------------------------
    if "Image:ArrayData" in name:
        return ("af", [0.0] * (_IMAGE_ROWS * _IMAGE_COLS), "counts", 0.0)
    if field in ("mat6", "mat6"):
        return ("af", [0.0] * 36, "", 0.0)
    if field in ("vec0",):
        return ("af", [0.0] * 6, "", 0.0)

    # ---- int scalars -------------------------------------------------------
    if field in ("ix_branch", "ix_ele"):
        return ("i", 0, "", 0.0)
    if field.startswith("n_particle_live"):
        return ("i", 1000, "", 0.0)
    if "ArraySize0_RBV" in name:
        return ("i", _IMAGE_ROWS, "", 0.0)
    if "ArraySize1_RBV" in name:
        return ("i", _IMAGE_COLS, "", 0.0)

    # ---- string scalars ----------------------------------------------------
    if name == "track_type" or field == "track_type":
        return ("s", "beam", "", 0.0)

    # ---- float scalars — control/magnet fields -----------------------------
    if "BCTRL.DRVH" in field:
        return ("f", abs(ctrl_nom) * 1.5 if ctrl_nom != 0 else 1.0, "kG", 0.0)
    if "BCTRL.DRVL" in field:
        return ("f", -abs(ctrl_nom) * 1.5 if ctrl_nom != 0 else -1.0, "kG", 0.0)
    if field.startswith("BCTRL"):
        return ("f", ctrl_nom, "kG", 0.1)
    if field.startswith("BDES"):
        return ("f", ctrl_nom, "kG", 0.0)
    if field.startswith("BACT"):
        return ("f", ctrl_nom, "kG", 0.05)
    if field.startswith("BMAX"):
        return ("f", abs(ctrl_nom) * 1.5 if ctrl_nom != 0 else 1.0, "kG", 0.0)
    if field.startswith("BMIN"):
        return ("f", -abs(ctrl_nom) * 1.5 if ctrl_nom != 0 else -1.0, "kG", 0.0)
    if field.startswith("CTRL"):
        return ("f", 0.0, "", 0.0)
    if field.startswith("STATCTRLSUB.T"):
        return ("f", 0.0, "s", 0.0)

    # ---- float scalars — beam diagnostics ----------------------------------
    if "TMIT" in field:
        return ("f", 1e10, "e-", 0.5)
    if field in ("X", "Y") and name.startswith("BPMS"):
        return ("f", 0.0, "mm", 1.0)
    if "EMITN_X" in field:
        return ("f", 1.0e-6, "m*rad", 0.5)
    if "EMITN_Y" in field:
        return ("f", 1.0e-6, "m*rad", 0.5)
    if "XRMS" in field:
        return ("f", 0.1, "mm", 1.0)
    if "YRMS" in field:
        return ("f", 0.1, "mm", 1.0)
    if "ZRMS" in field:
        return ("f", 0.05, "mm", 1.0)
    if "RESOLUTION" in field:
        return ("f", 0.00365, "mm/px", 0.0)
    if "R_DIST" in field:
        return ("f", 0.0, "mm", 1.0)
    if "CHRG_S" in field:
        return ("f", 250e-12, "C", 0.5)
    if "Pulse_length" in field or "L0A_ADES" in field or "L0B_ADES" in field:
        return ("f", 3e-12 if "Pulse" in field else 9.5, "s" if "Pulse" in field else "MV/m", 0.1)
    if field in ("X", "Y") and name.startswith("OTRS"):
        return ("f", 0.0, "mm", 1.0)

    # ---- Twiss / optics ----------------------------------------------------
    if field.startswith(("a.alpha", "b.alpha", "x.alpha", "y.alpha")):
        return ("f", 0.0, "", 0.0)
    if field.startswith(("a.beta", "b.beta", "x.beta", "y.beta")):
        return ("f", 1.0, "m", 0.0)
    if field.startswith(("a.eta", "b.eta", "x.eta", "y.eta")):
        return ("f", 0.0, "m", 0.0)
    if field.startswith(("a.etap", "b.etap", "x.etap", "y.etap")):
        return ("f", 0.0, "", 0.0)
    if field.startswith(("a.gamma", "b.gamma")):
        return ("f", 1.0, "1/m", 0.0)
    if field.startswith(("a.phi", "b.phi")):
        return ("f", 0.0, "rad", 0.0)
    if field.startswith(("x.emit", "y.emit")):
        return ("f", 1e-9, "m*rad", 0.0)
    if field.startswith(("x.norm_emit", "y.norm_emit")):
        return ("f", 1e-6, "m*rad", 0.0)
    if field.startswith("e_tot"):
        return ("f", 135e6, "eV", 0.0)
    if field.startswith("p0c"):
        return ("f", 135e6, "eV/c", 0.0)
    if field.startswith(("px", "py")):
        return ("f", 0.0, "rad", 0.0)
    if field.startswith("s_ele"):
        return ("f", 0.0, "m", 0.0)
    if field.startswith("s"):
        return ("f", 0.0, "m", 0.0)
    if field.startswith("t"):
        return ("f", 0.0, "s", 0.0)
    if field.startswith("l"):
        return ("f", 0.0, "m", 0.0)
    if field in ("x", "y"):
        return ("f", 0.0, "m", 0.0)

    # default
    return ("f", 0.0, "", 0.1)


# ---------------------------------------------------------------------------
# Load PV list
# ---------------------------------------------------------------------------

def _load_pv_names() -> list:
    """Read pv_list.txt, deduplicate, return sorted unique names."""
    default = pathlib.Path(__file__).parent / "pv_list.txt"
    path = pathlib.Path(os.environ.get("PV_LIST", str(default)))
    if not path.exists():
        LOG.warning("PV list not found at %s — using built-in 16 PVs only", path)
        return [name for name, *_ in _BUILTIN_16]
    names = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                names.add(line)
    return sorted(names)


# Fallback if pv_list.txt missing
_BUILTIN_16 = [
    ("SOLN:IN20:121:BCTRL",   0.477969,    "kG*m",  0.05),
    ("QUAD:IN20:121:BCTRL",  -0.00149923,  "kG",    0.1),
    ("QUAD:IN20:122:BCTRL",  -0.000687299, "kG",    0.1),
    ("ACCL:IN20:300:L0A_PDES", -9.53597,   "deg",   0.02),
    ("ACCL:IN20:400:L0B_PDES",  9.85566,   "deg",   0.02),
    ("QUAD:IN20:361:BCTRL",  -2.00059,     "kG",    0.1),
    ("QUAD:IN20:371:BCTRL",   2.00059,     "kG",    0.1),
    ("QUAD:IN20:425:BCTRL",  -1.08076,     "kG",    0.1),
    ("QUAD:IN20:441:BCTRL",  -0.179388,    "kG",    0.1),
    ("QUAD:IN20:511:BCTRL",   2.85217,     "kG",    0.1),
    ("QUAD:IN20:525:BCTRL",  -3.2184,      "kG",    0.1),
    ("QUAD:IN20:631:BCTRL",   7.33536,     "kG",    0.1),
    ("XCOR:IN20:641:BCTRL",   0.0,         "kG*m",  0.001),
    ("YCOR:IN20:642:BCTRL",   0.0,         "kG*m",  0.001),
    ("QUAD:IN20:651:BCTRL",  -5.82109,     "kG",    0.1),
    ("BEND:IN20:661:BCTRL",   0.134999,    "GeV/c", 0.01),
]


# ---------------------------------------------------------------------------
# PVA server
# ---------------------------------------------------------------------------

def _nt_for(pv_type: str, nominal):
    """Return an NTScalar wrapper with the initial value packed in."""
    if pv_type == "f":
        return NTScalar("d").wrap(float(nominal))
    if pv_type == "i":
        return NTScalar("i").wrap(int(nominal))
    if pv_type == "s":
        return NTScalar("s").wrap(str(nominal))
    if pv_type == "af":
        return NTScalar("ad").wrap(np.asarray(nominal, dtype=np.float64))
    if pv_type == "ai":
        return NTScalar("ai").wrap(np.asarray(nominal, dtype=np.int32))
    return NTScalar("d").wrap(0.0)


def _start_pva_server(specs: dict) -> dict:
    """specs: {name: (pv_type, nominal, unit, noise_pct)}"""
    pva_port = int(os.environ.get("EPICS_PVA_SERVER_PORT", "5076"))
    providers = {}
    pv_objects = {}

    for name, (pv_type, nominal, unit, noise_pct) in specs.items():
        pv = SharedPV(initial=_nt_for(pv_type, nominal))
        providers[name] = pv
        pv_objects[name] = pv

    server = Server(providers=[providers])
    pv_objects["__server__"] = server  # prevent GC
    LOG.info("PVA server started for %d PVs on TCP port %d", len(specs), pva_port)
    return pv_objects


# ---------------------------------------------------------------------------
# CA server (opt-in)
# ---------------------------------------------------------------------------

def _start_ca_server(specs: dict):
    if os.environ.get("ENABLE_CA", "").lower() not in ("true", "1", "yes"):
        LOG.info("CA server disabled (set ENABLE_CA=true to enable)")
        return None
    try:
        import pcaspy
        import pcaspy.cas
    except ImportError:
        LOG.warning("pcaspy not available — CA server disabled")
        return None

    pvdb = {}
    for name, (pv_type, nominal, unit, _) in specs.items():
        if pv_type == "f":
            pvdb[name] = {"type": "float", "value": float(nominal), "unit": unit, "prec": 6}
        elif pv_type == "i":
            pvdb[name] = {"type": "int", "value": int(nominal)}
        elif pv_type == "s":
            pvdb[name] = {"type": "string", "value": str(nominal)}
        elif pv_type in ("af", "ai"):
            arr = nominal if isinstance(nominal, list) else list(nominal)
            pvdb[name] = {"type": "float", "count": len(arr), "value": arr}

    class _Driver(pcaspy.Driver):
        pass

    server = pcaspy.SimpleServer()
    server.createPV("", pvdb)
    driver = _Driver()

    def _ca_loop():
        while True:
            server.process(0.05)

    threading.Thread(target=_ca_loop, daemon=True, name="ca-server").start()
    LOG.info("CA server started for %d PVs", len(pvdb))
    return driver


# ---------------------------------------------------------------------------
# Update loop — only float scalars with noise_pct > 0
# ---------------------------------------------------------------------------

def _update_loop(pva_pvs: dict, ca_driver, specs: dict, rate_hz: float, noise_pct: float) -> None:
    interval = 1.0 / rate_hz
    current = {}
    updatable = {}  # name → (nominal, effective_noise)

    for name, (pv_type, nominal, unit, pv_noise) in specs.items():
        if pv_type == "f" and pv_noise > 0:
            nom = float(nominal)
            current[name] = nom
            eff = abs(nom) * (pv_noise / 100.0) * noise_pct / 0.1
            if eff == 0:
                eff = 1e-6
            updatable[name] = (nom, eff)

    while True:
        t0 = time.monotonic()
        for name, (nom, eff) in updatable.items():
            current[name] += random.gauss(0, eff)
            current[name] = current[name] * 0.999 + nom * 0.001  # mean-revert

            if name in pva_pvs:
                try:
                    pva_pvs[name].post(NTScalar("d").wrap(current[name]))
                except Exception as e:
                    LOG.debug("PVA post error %s: %s", name, e)

            if ca_driver is not None:
                try:
                    ca_driver.setParam(name, current[name])
                except Exception as e:
                    LOG.debug("CA setParam error %s: %s", name, e)

        if ca_driver is not None:
            try:
                ca_driver.updatePVs()
            except Exception:
                pass

        elapsed = time.monotonic() - t0
        sleep = interval - elapsed
        if sleep > 0:
            time.sleep(sleep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rate_hz = float(os.environ.get("UPDATE_RATE_HZ", "1"))
    noise_pct = float(os.environ.get("NOISE_SIGMA_PCT", "0.1"))

    pv_names = _load_pv_names()
    specs = {name: _classify(name) for name in pv_names}

    n_float  = sum(1 for t, *_ in specs.values() if t == "f")
    n_int    = sum(1 for t, *_ in specs.values() if t == "i")
    n_str    = sum(1 for t, *_ in specs.values() if t == "s")
    n_arr    = sum(1 for t, *_ in specs.values() if t in ("af", "ai"))
    LOG.info("Loaded %d PVs: %d float, %d int, %d string, %d array",
             len(specs), n_float, n_int, n_str, n_arr)
    LOG.info("Served PV list (%d total):", len(specs))
    for name in sorted(specs):
        print(f"[mock_ca_ioc] PV: {name}", file=__import__("sys").stderr, flush=True)

    pva_pvs   = _start_pva_server(specs)
    ca_driver = _start_ca_server(specs)

    update_thread = threading.Thread(
        target=_update_loop,
        args=(pva_pvs, ca_driver, specs, rate_hz, noise_pct),
        daemon=True,
        name="pv-updater",
    )
    update_thread.start()

    LOG.info("Mock IOC running — Ctrl+C to stop")
    LOG.info("Serving %d PVs at %.1f Hz (noise=%.2f%%)", len(specs), rate_hz, noise_pct)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOG.info("Shutting down")


if __name__ == "__main__":
    main()
