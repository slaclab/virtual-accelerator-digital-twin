# [Enhancement] Add per-cycle heap cleanup in `StagedModel._set()`

## Summary

`StagedModel._set()` runs all N stages sequentially but performs no heap cleanup afterward. Each stage accumulates glibc heap fragmentation and HDF5 internal cache entries that are never reclaimed, causing ~20-40 MB/hr RSS growth in long-running processes.

## Context

In our virtual accelerator digital twin deployment, `StagedModel` composes a `LUMEBmadModel` (physics via pytao/Fortran) with an ML surrogate model. The model runs continuously at ~1800 cycles/hr serving live PV updates. Without cleanup, RSS grows monotonically until OOM.

This is one of several contributing factors to an overall ~180 MB/hr leak we diagnosed. The primary cause was in `lume-bmad` (see separate issue), but `StagedModel` cleanup eliminates an additional 20-40 MB/hr.

## Problem

After `StagedModel._set()` completes all stages:
1. **glibc heap fragmentation** — Fortran (libtao) and numpy temporaries fragment the C heap across multiple glibc arenas. Without `malloc_trim()`, freed pages are never returned to the OS.
2. **HDF5 internal caches** — `initial_particles.setter` passes particle data between stages via HDF5 files. Each `H5Fopen`/`H5Fclose` cycle accumulates ~6.2 KB of metadata in libhdf5's internal free list.
3. **Python reference cycles** — numpy arrays and pytao objects can form cycles that the refcount collector misses without explicit `gc.collect()`.

## Proposed Fix

Add cleanup at the end of `StagedModel._set()`:

```python
import ctypes
import gc

try:
    _libc = ctypes.CDLL("libc.so.6")
except OSError:
    _libc = None


class StagedModel(LUMEModel):

    def _set(self, values: dict[str, Any]) -> None:
        incoming_particles = None
        for i, model in enumerate(self.lume_model_instances):
            model_values = {
                k: v for k, v in values.items() if k in model.supported_variables
            }

            if i > 0 and incoming_particles is not None:
                model.initial_particles = incoming_particles

            if model_values:
                model.set(model_values)

            if isinstance(model, FinalParticlesMixIn):
                incoming_particles = model.final_particles

        # Reclaim heap fragmentation and HDF5 caches accumulated across all stages
        gc.collect()
        if _libc is not None:
            _libc.malloc_trim(0)
        try:
            import h5py
            h5py.h5.garbage_collect()
        except Exception:
            pass
```

## Design Notes

- `_libc` is loaded once at module import — falls back gracefully on non-Linux (macOS, Windows) where `libc.so.6` doesn't exist.
- `h5py` import is inside try/except since it's an optional dependency of lume-base.
- `gc.collect()` is called before `malloc_trim()` so Python releases references first, then glibc can return the freed pages.
- The overhead is minimal (~1-2 ms per cycle) compared to the model evaluation time (~200-300 ms per cycle).

## Impact

| Metric | Without cleanup | With cleanup |
|--------|----------------|--------------|
| RSS growth | ~20-40 MB/hr | ~0 MB/hr |
| Per-cycle overhead | 0 ms | ~1-2 ms |
| HDF5 cache accumulation | ~11 MB/hr | 0 |

## Environment

- `lume` (lume-base) installed via pip
- Python 3.12, Linux (glibc 2.36+)
- `StagedModel` composing `LUMEBmadModel` + ML surrogate
- ~1800 cycles/hour continuous operation
- Container with 6Gi memory limit
