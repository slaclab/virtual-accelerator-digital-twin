"""Bound the libtao beam-tracking memory leak by recycling a SubprocessTao worker.

Tao/Bmad leaks ~89 KB of native heap per beam track (measured: 89.31 KB/cycle with beam
tracking, 0.34 KB/cycle without; all of it in the brk heap, surviving malloc_trim). The bug
is upstream in bmad and cannot be fixed here. It can be bounded: with Tao running in a child
process, killing and respawning that child returns every leaked byte to the OS.

A respawned child comes back at the *design* lattice with none of our configuration. If
restoration were incomplete the service would keep publishing smooth, physical, wrong
numbers -- design magnet settings presented as live-machine values. So restoration is
verified by reading state back out of Tao and comparing it against a pre-recycle snapshot;
any mismatch exits the process so Kubernetes restarts from a known-good state. The
verification, not the replay, is what makes this safe.

pytao is imported lazily so this module stays importable on dev machines without it.
"""

from __future__ import annotations

import os
import sys
import time

MB = 1024.0 * 1024.0

EXIT_RESTORE_FAILED = 90

# Commands that carry configuration which must survive a respawn. Everything else (reads,
# plotting, lattice_calc_on toggles) is either stateless or re-issued every cycle anyway.
_CONFIG_PREFIXES = (
    "set beam ",
    "set beam_init ",
    "set global track_type",
    "set ele ",
)

# A per-cycle toggle, not state. Recording it would replay a stale value and, worse, leave
# lattice_calc_on off after a recycle.
_CONFIG_EXCLUDE = ("set global lattice_calc_on",)

_MISSING = object()


def _log(msg: str) -> None:
    print(f"[recycle] {msg}", file=sys.stderr, flush=True)


def _log_assert(msg: str) -> None:
    print(f"[recycle-assert] {msg}", file=sys.stderr, flush=True)


def record_config_command(log: dict, cmd) -> bool:
    """Add cmd to log if it carries configuration. Returns True if recorded.

    Keys on the assignment target so a newer value replaces an older one while keeping its
    original position. That bounds the log at the number of distinct settings (~120) rather
    than letting it grow once per cycle, and it captures control-element writes for free.
    """
    if not isinstance(cmd, str):
        return False
    text = cmd.strip()
    lowered = text.lower()
    if lowered.startswith(_CONFIG_EXCLUDE):
        return False
    if not lowered.startswith(_CONFIG_PREFIXES):
        return False
    key = lowered.split("=", 1)[0].strip() if "=" in text else lowered
    log[key] = text
    return True


def cgroup_current_bytes() -> float:
    """Total memory charged to this container, parent plus children.

    The parent's own RSS is useless as a trigger here: the leak accumulates entirely in the
    Tao child. This is also the number the kernel OOM-kills on.
    """
    for path in ("/sys/fs/cgroup/memory.current",
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            with open(path) as fp:
                return float(int(fp.read().strip()))
        except (OSError, ValueError):
            continue
    return float("nan")


def make_recyclable_tao_class():
    """Build the RecyclableTao class. Imports pytao, so call only where pytao exists."""
    from pytao import SubprocessTao

    class RecyclableTao(SubprocessTao):
        """SubprocessTao that can respawn its worker and replay its configuration.

        Configuration is captured by observing commands as they are issued rather than from
        a hardcoded list, because the setup is spread across three places -- the model
        factory (track_start, custom commands, aliases, beam file, track_type),
        LUMEBmadModel.__init__ (comb_ds_save, saved_at), and the per-cycle control writes.
        A hardcoded list would silently miss anything not anticipated.
        """

        def __init__(self, *args, **kwargs):
            # Must exist before super().__init__, which issues commands.
            self._config_log: dict[str, str] = {}
            self._recycling = False
            self._init_args = args
            self._init_kwargs = dict(kwargs)
            super().__init__(*args, **kwargs)

        def cmd(self, cmd, *args, **kwargs):
            result = super().cmd(cmd, *args, **kwargs)
            if not self._recycling:
                record_config_command(self._config_log, cmd)
            return result

        @property
        def config_command_count(self) -> int:
            return len(self._config_log)

        def recycle(self) -> float:
            """Respawn the worker and replay configuration. Returns seconds taken."""
            started = time.monotonic()
            init_kwargs = {k: v for k, v in self._init_kwargs.items() if k != "env"}
            commands = list(self._config_log.values())

            self._recycling = True
            try:
                self.close_subprocess()
                self.init(*self._init_args, **init_kwargs)
                # Replaying ~120 element writes with eager recalculation would trigger a
                # lattice calc per command.
                super().cmd("set global lattice_calc_on = F")
                try:
                    for command in commands:
                        super().cmd(command)
                finally:
                    super().cmd("set global lattice_calc_on = T")
            finally:
                self._recycling = False

            return time.monotonic() - started

    return RecyclableTao


def find_bmad_model(model):
    """Return the LUMEBmadModel, whether wrapped in a StagedModel or used directly."""
    if hasattr(model, "tao"):
        return model
    for stage in getattr(model, "lume_model_instances", []) or []:
        if hasattr(stage, "tao"):
            return stage
    return None


def _control_variable_names(bmad) -> list[str]:
    """Writable variables whose values we replay, so they must match after a recycle.

    Read-only outputs are excluded because beam generation is unseeded and varies at
    sqrt(N) between tracks. BeamAtElementVariable is excluded for the same reason
    LUMEBmadModel.reset() excludes it -- it is not writable.
    """
    names = []
    for name, variable in bmad.supported_variables.items():
        if getattr(variable, "read_only", True):
            continue
        if type(variable).__name__ == "BeamAtElementVariable":
            continue
        if name in bmad._state:
            names.append(name)
    return names


def capture_state(bmad) -> dict:
    tao = bmad.tao
    track_type = tao.tao_global()["track_type"]
    return {
        "track_type": track_type,
        "track_start": tao.beam(0)["track_start"],
        "n_supported": len(bmad.supported_variables),
        "comb_len": len(tao.bunch_comb("s")) if track_type == "beam" else None,
        "controls": {n: bmad._state[n] for n in _control_variable_names(bmad)},
    }


def _values_match(expected, actual) -> bool:
    if actual is _MISSING:
        return False
    if isinstance(expected, str) or isinstance(actual, str):
        return expected == actual
    try:
        import numpy as np

        a = np.asarray(expected, dtype=float)
        b = np.asarray(actual, dtype=float)
        if a.shape != b.shape:
            return False
        return bool(np.allclose(a, b, rtol=1e-9, atol=0.0, equal_nan=True))
    except (TypeError, ValueError):
        return expected == actual


def verify_state(bmad, snapshot: dict) -> list[str]:
    """Compare restored Tao state against the pre-recycle snapshot.

    Returns a list of human-readable mismatches; empty means the respawn is trustworthy.
    """
    tao = bmad.tao
    problems: list[str] = []

    track_type = tao.tao_global()["track_type"]
    if track_type != snapshot["track_type"]:
        problems.append(f"track_type {snapshot['track_type']!r} -> {track_type!r}")

    track_start = tao.beam(0)["track_start"]
    if track_start != snapshot["track_start"]:
        problems.append(f"track_start {snapshot['track_start']!r} -> {track_start!r}")

    n_supported = len(bmad.supported_variables)
    if n_supported != snapshot["n_supported"]:
        problems.append(f"supported_variables {snapshot['n_supported']} -> {n_supported}")

    # Encodes comb_ds_save and the lattice slice together, so one check covers both.
    if snapshot["comb_len"] is not None and track_type == "beam":
        comb_len = len(tao.bunch_comb("s"))
        if comb_len != snapshot["comb_len"]:
            problems.append(f"comb length {snapshot['comb_len']} -> {comb_len}")

    bmad.update_state()
    mismatched = []
    for name, expected in snapshot["controls"].items():
        actual = bmad._state.get(name, _MISSING)
        if not _values_match(expected, actual):
            mismatched.append((name, expected, actual))

    if mismatched:
        problems.append(
            f"{len(mismatched)} of {len(snapshot['controls'])} control variables differ"
        )
        for name, expected, actual in mismatched[:10]:
            shown = "<missing>" if actual is _MISSING else actual
            problems.append(f"  {name}: expected {expected!r} got {shown!r}")

    return problems


def install_recycling(
    model,
    *,
    enabled: bool = True,
    growth_mb: float = 400.0,
    max_cycles: int = 0,
    on_recycle=None,
    on_failure=None,
) -> bool:
    """Wrap model.set so Tao is respawned once memory growth exceeds growth_mb.

    Recycling runs at the start of set(), before the model is evaluated, so every published
    value comes from a fully restored and verified Tao. set() is called on the runner's
    consumer thread, the only thread that touches Tao, so no lock is needed.

    Returns True if recycling was installed.
    """
    if not enabled:
        _log("disabled by configuration")
        return False

    bmad = find_bmad_model(model)
    if bmad is None:
        _log("WARNING no LUMEBmadModel found; recycling NOT installed")
        return False
    if not hasattr(bmad.tao, "recycle"):
        _log(
            "WARNING Tao is not a RecyclableTao (pytao.Tao substitution did not take "
            "effect); recycling NOT installed"
        )
        return False

    # The top-level model must be wrapped: StagedModel._set only forwards to the Bmad stage
    # when that cycle's values target it, so wrapping the stage gives an unreliable hook.
    original_set = model.set
    state = {"baseline": cgroup_current_bytes(), "cycles": 0}

    _log(
        f"installed: growth_mb={growth_mb} max_cycles={max_cycles or 'off'} "
        f"baseline={state['baseline'] / MB:.1f}MB "
        f"tracked_commands={bmad.tao.config_command_count}"
    )

    def _should_recycle() -> bool:
        if max_cycles and state["cycles"] >= max_cycles:
            return True
        current = cgroup_current_bytes()
        baseline = state["baseline"]
        if current != current or baseline != baseline:  # NaN: cgroup unreadable
            return False
        return (current - baseline) / MB >= growth_mb

    def _recycle_now() -> None:
        tao = bmad.tao
        before = cgroup_current_bytes()
        snapshot = capture_state(bmad)

        try:
            duration = tao.recycle()
        except Exception as exc:
            _log_assert(f"FATAL respawn raised {type(exc).__name__}: {exc}")
            if on_failure is not None:
                on_failure()
            sys.stderr.flush()
            os._exit(EXIT_RESTORE_FAILED)

        problems = verify_state(bmad, snapshot)
        if problems:
            _log_assert("FATAL Tao state not restored after respawn:")
            for problem in problems:
                _log_assert(f"  {problem}")
            _log_assert(
                f"exiting {EXIT_RESTORE_FAILED} so the pod restarts from known-good state "
                "rather than publishing unverified physics"
            )
            if on_failure is not None:
                on_failure()
            sys.stderr.flush()
            os._exit(EXIT_RESTORE_FAILED)

        after = cgroup_current_bytes()
        state["baseline"] = after
        state["cycles"] = 0
        _log(
            f"respawned in {duration:.1f}s, replayed {tao.config_command_count} commands, "
            f"memory {before / MB:.1f}MB -> {after / MB:.1f}MB "
            f"({(after - before) / MB:+.1f}MB), state verified"
        )
        if on_recycle is not None:
            on_recycle(duration, after)

    def set_with_recycle(*args, **kwargs):
        state["cycles"] += 1
        if _should_recycle():
            _recycle_now()
        return original_set(*args, **kwargs)

    model.set = set_with_recycle
    return True
