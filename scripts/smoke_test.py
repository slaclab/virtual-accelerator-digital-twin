#!/usr/bin/env python
"""PVAccess smoke test for the virtual accelerator digital twin.

Connects to the running PVA server, waits for the model to warm up, and verifies
that a set of required PVs are being served with finite values. Exits 0 only if
every required PV returns a finite number within the startup window; otherwise
exits non-zero.

This is used three ways:
  * CI gate  - built image is booted and tested before it is pushed to ghcr.
  * Apptainer / server verification - `apptainer exec <sif> python scripts/smoke_test.py`
  * Mirrors the connectivity check in kubernetes/test-pod.yaml.

Configuration (environment variables):
  SMOKE_PVS               Comma-separated PV names to check.
                          Default: BPMS:IN20:581:TMIT,BPMS:IN20:581:X,BPMS:IN20:581:Y
  SMOKE_STARTUP_TIMEOUT   Seconds to keep retrying while the model warms up.
                          Default: 180
  SMOKE_GET_TIMEOUT       Per-get timeout in seconds. Default: 10
  SMOKE_POLL_INTERVAL     Seconds between retry rounds. Default: 5
"""

import math
import os
import sys
import time

from p4p.client.thread import Context

DEFAULT_PVS = "BPMS:IN20:581:TMIT,BPMS:IN20:581:X,BPMS:IN20:581:Y"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"WARN: {name}={raw!r} is not a number; using default {default}", flush=True)
        return default


def _is_finite_number(value) -> bool:
    """True if the PV value is (or contains) a usable finite number.

    p4p returns numpy scalars/arrays or plain numbers depending on the PV type.
    We accept a scalar that is finite, or an array with at least one finite entry.
    """
    try:
        # Arrays (waveforms) - accept if any element is finite.
        if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            return any(math.isfinite(float(v)) for v in value) if len(value) else False
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        # Non-numeric PV (e.g. a string/enum) - treat "connected and returned" as OK.
        return value is not None


def main() -> int:
    pvs = [pv.strip() for pv in os.environ.get("SMOKE_PVS", DEFAULT_PVS).split(",") if pv.strip()]
    startup_timeout = _env_float("SMOKE_STARTUP_TIMEOUT", 180.0)
    get_timeout = _env_float("SMOKE_GET_TIMEOUT", 10.0)
    poll_interval = _env_float("SMOKE_POLL_INTERVAL", 5.0)

    if not pvs:
        print("FAIL: no PVs configured (SMOKE_PVS is empty)", flush=True)
        return 2

    print(f"--- PVAccess smoke test ---", flush=True)
    print(f"PVs: {', '.join(pvs)}", flush=True)
    print(
        f"startup_timeout={startup_timeout:g}s get_timeout={get_timeout:g}s "
        f"poll_interval={poll_interval:g}s",
        flush=True,
    )

    ctx = Context("pva")
    deadline = time.monotonic() + startup_timeout
    remaining = set(pvs)
    last_error: dict[str, str] = {}
    attempt = 0

    try:
        # The Bmad model needs time to initialize and track before PVs return
        # values, so retry the whole set until everything is up or we time out.
        while remaining and time.monotonic() < deadline:
            attempt += 1
            for pv in sorted(remaining):
                try:
                    value = ctx.get(pv, timeout=get_timeout)
                except Exception as exc:  # noqa: BLE001 - report any get failure and retry
                    last_error[pv] = repr(exc)
                    continue

                if _is_finite_number(value):
                    print(f"OK: {pv} = {value}", flush=True)
                    remaining.discard(pv)
                    last_error.pop(pv, None)
                else:
                    last_error[pv] = f"non-finite/empty value: {value!r}"

            if remaining and time.monotonic() < deadline:
                print(
                    f"...waiting for {len(remaining)} PV(s) "
                    f"(attempt {attempt}): {', '.join(sorted(remaining))}",
                    flush=True,
                )
                time.sleep(poll_interval)
    finally:
        ctx.close()

    print("--- Result ---", flush=True)
    if remaining:
        for pv in sorted(remaining):
            print(f"FAIL: {pv} -> {last_error.get(pv, 'no value returned')}", flush=True)
        print(f"SMOKE TEST FAILED: {len(remaining)}/{len(pvs)} PV(s) did not come up", flush=True)
        return 1

    print(f"SMOKE TEST PASSED: all {len(pvs)} PV(s) served", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
