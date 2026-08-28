# [Bug] Redundant Tao commands in `LUMEBmadModel` cause ~180 MB/hr RSS growth

## Summary

`LUMEBmadModel` executes 3 redundant Tao commands every simulation cycle. These commands allocate and free large Fortran arrays, fragmenting the glibc heap in a way that prevents pages from being returned to the OS. Over hours of continuous operation, RSS grows monotonically until OOM.

**Observed:** ~180 MB/hr RSS growth in a Kubernetes pod running `cu_hxr_staged` with `REMOTE_INPUTS=true` (1800 cycles/hr). Pod OOMs at 6Gi after ~24 hours.

## Root Cause

Three redundant operations per cycle:

### Bug A: `comb_ds_save` set every cycle in `initial_particles.setter`

```python
# initial_particles.setter (currently ~line 251)
self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")
```

This is **static configuration** — it's already set once in `__init__` (line 70) and never changes. But the setter is called every cycle by `StagedModel._set()` when passing particles between stages.

Each call forces Tao to reallocate its internal comb arrays:
- 0.1 m resolution × ~300 save points × 1000 particles × 10 coordinates = **~3 million doubles**
- Fortran allocates/deallocates these arrays, fragmenting the C heap

### Bug B: `update_state()` called twice per cycle

```python
# initial_particles.setter (currently ~line 255)
self.update_state()
```

This reads all ~116 Tao output variables. But `_set()` already calls `update_state()` immediately after — making this call completely redundant. Two full Tao state reads per cycle instead of one.

### Bug C: `_refresh_dynamic_action_variables()` called twice per cycle

```python
# _set() (currently ~line 170)
self._refresh_dynamic_action_variables()
```

This is called in the `initial_particles.setter`, then again in `_set()`. Each call executes `tao_global()` + `bunch_comb("s")` — both require Tao round-trips and temporary allocations.

## Impact

| Metric | Before fix | After fix |
|--------|-----------|-----------|
| Tao commands per cycle | ~12 | ~5 |
| RSS growth | ~180 MB/hr | ~0 MB/hr |
| Fortran array allocations | 3M doubles/cycle (redundant) | 0 (eliminated) |

## Proposed Fix

**Delete 3 lines:**

1. In `initial_particles.setter` — remove `self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")` (static config, already set in `__init__`)

2. In `initial_particles.setter` — remove `self.update_state()` (redundant, `_set()` calls it right after)

3. In `_set()` — remove `self._refresh_dynamic_action_variables()` (already called in the setter)

## Additional Improvement: `malloc_trim` after `update_state()`

Even after removing the redundant commands, pytao output buffers and numpy temporaries from `update_state()` fragment the heap between live allocations. Adding `malloc_trim(0)` at the end of `_set()` compacts the heap and returns freed pages to the OS:

```python
def _set(self, values):
    # ... existing code ...
    self.update_state()

    # Return fragmented heap pages to OS
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass
```

## Additional Improvement: `h5py.h5.garbage_collect()` after beam file read

In `initial_particles.setter`, `set beam_init position_file` triggers Tao to open/close an HDF5 file. libhdf5 accumulates ~6.2 KB of metadata cache entries per open/close cycle that are never purged. At 1800 cycles/hr this leaks ~11 MB/hr.

```python
@initial_particles.setter
def initial_particles(self, particles):
    if self.simulator.tao_global()["track_type"] == "beam":
        fname = getcwd() + "/input_beam.h5"
        particles.write(fname)
        self.simulator.cmd(f"set beam_init position_file = {fname}")

        # Reclaim HDF5 internal free lists
        try:
            import h5py
            h5py.h5.garbage_collect()
        except Exception:
            pass

        self._refresh_dynamic_action_variables()
```

## Reproduction

We have a reproduction script at `scripts/reproduce_leak.py` that demonstrates the memory growth with and without the fix:

```bash
# Shows RSS growing (~70 MB in 10 min, queue unbounded)
MALLOC_ARENA_MAX=2 python scripts/reproduce_leak.py --mode leak --duration 600

# Shows RSS stable (queue at 2, flat memory)
MALLOC_ARENA_MAX=2 python scripts/reproduce_leak.py --mode fixed --duration 600
```

## Environment

- `lume-bmad` installed via pip from `git+https://github.com/lume-science/lume-bmad.git`
- Python 3.12, pytao (conda-forge), bmad (conda-forge)
- Running in container on Linux (glibc 2.36+)
- Model: `cu_hxr_staged` (StagedModel with ML surrogate + LUMEBmadModel)
- ~1800 cycles/hour with `REMOTE_INPUTS=true`
