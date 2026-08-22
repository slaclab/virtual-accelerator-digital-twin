# Memory Leak Analysis — lume_bmad / lume upstream misuse

**Date:** 2026-08-22  
**Observed:** RSS grows ~180 MB/hr on k8s pod (confirmed via /proc/1/smaps heap region monitoring).  
**Confirmed NOT:** glibc arena fragmentation (MALLOC_ARENA_MAX=1 already set in prod), THP (AnonHugePages=0 confirmed in pod), Python heap (tracemalloc flat at ~35 MB).  
**Root cause:** Fortran/libtao heap growth from redundant Tao commands issued every simulation cycle.

---

## Pod evidence

```
Pod: virtual-accelerator-7df57f86d6-cv6fj  (namespace: virtual-accelerator)
Heap [heap] RSS:   763 MB → 775 MB in ~4 min = +3 MB/min = ~180 MB/hr
MALLOC_ARENA_MAX:  1  (already minimum)
AnonHugePages:     0  (THP disabled successfully)
Python heap:       ~35 MB flat (not the leak)
```

---

## Per-cycle call chain (staged model, beam mode)

```
runner._run()
  ├─ model.get(settable_var_names)              # pre-snapshot for rollback cache
  └─ StagedModel._set(values)                  # lume/staged_model.py:96
       for each downstream model (i > 0):
         model.initial_particles = particles    # ← BUG CLUSTER (see below)
           particles.write("/app/input_beam.h5")
           tao.cmd("set beam_init position_file = ...")   # Tao reallocates beam arrays
           tao.cmd("set beam comb_ds_save = 0.1")         # Tao reallocates comb arrays  ← BUG 1
           _refresh_dynamic_action_variables()             # reads bunch_comb("s")        ← BUG 3a
           update_state()                                  # reads all 116 vars from Tao  ← BUG 2
         model.set(model_values)
           tao.cmd("set global lattice_calc_on = F")
           super()._set(values)                            # sets each ele var in Tao
           tao.cmd("set global lattice_calc_on = T")       # triggers full Tao recalc
           _refresh_dynamic_action_variables()             # DUPLICATE                    ← BUG 3b
           update_state()                                  # reads all 116 vars again     ← (correct)
```

Per cycle: **~12 Tao commands** instead of ~5. Each redundant command causes Fortran heap realloc.

---

## Bug list

### BUG 1 — `set beam comb_ds_save` in `initial_particles.setter` (biggest impact)

**File:** `lume_bmad/model.py` — `initial_particles.setter` (~line 251)

```python
# CURRENT (wrong):
self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")

# FIX: delete this line entirely.
# comb_ds_save is already set once at __init__ (line 64). It is static config.
# Calling it every cycle forces Tao to re-init all comb storage arrays:
#   comb_ds_save=0.1 on a ~30m lattice = ~300 save points × 1000 particles
#   × 10 coordinates = ~3M doubles reallocated every cycle → Fortran heap fragmentation.
```

---

### BUG 2 — `update_state()` called in `initial_particles.setter` (redundant)

**File:** `lume_bmad/model.py` — `initial_particles.setter` (~line 255)

```python
# CURRENT (wrong):
self.update_state()   # ← reads all 116 vars from Tao

# FIX: delete this line.
# The setter is only ever called from StagedModel._set(), which immediately
# calls model.set() → LUMEBmadModel._set() → update_state() at the end.
# So this update_state() is always followed by another one 1 line later.
# Removing it halves the number of full Tao state reads per cycle.
```

---

### BUG 3 — `_refresh_dynamic_action_variables()` called twice per cycle

**File:** `lume_bmad/model.py` — `LUMEBmadModel._set()` (~line 170)

```python
# CURRENT (wrong):
def _set(self, values):
    ...
    self._refresh_dynamic_action_variables()   # ← DUPLICATE: setter already called this
    self.update_state()

# FIX: remove the _refresh call from _set() — the setter already ran it.
# Each call to _refresh_dynamic_action_variables():
#   - calls tao_global()["track_type"]  (1 Tao cmd)
#   - if beam mode: calls get_tao_comb_output_variables() → tao.bunch_comb("s")  (1 Tao cmd)
#   - re-registers all comb variables in _action_variable_by_name dict
```

---

### BUG 4 — `initial_particles` set unconditionally in staged model

**File:** `lume/staged_model.py` — `StagedModel._set()` (~line 110)

```python
# CURRENT (wrong):
if i > 0 and incoming_particles is not None:
    model.initial_particles = incoming_particles

# FIX: guard with identity check to skip when particles object is same:
if i > 0 and incoming_particles is not None:
    if model.initial_particles is not incoming_particles:
        model.initial_particles = incoming_particles

# Note: model.initial_particles getter calls tao.particles() which allocates
# a new ParticleGroup each time — so identity check will always differ.
# Better guard: cache last-set particles on the model instance:
#
#   if i > 0 and incoming_particles is not None:
#       if getattr(model, '_last_initial_particles_id', None) != id(incoming_particles):
#           model.initial_particles = incoming_particles
#           model._last_initial_particles_id = id(incoming_particles)
```

---

## Proposed minimal patch (apply in run.py as monkey-patch until upstream PRs land)

```python
# run.py — after imports, before main()

from os import getcwd
from lume_bmad.model import LUMEBmadModel

def _patched_initial_particles_setter(self, particles):
    """Patched setter: removes redundant comb_ds_save + update_state calls."""
    if self.simulator.tao_global()["track_type"] == "beam":
        fname = getcwd() + "/input_beam.h5"
        particles.write(fname)
        self.simulator.cmd(f"set beam_init position_file = {fname}")
        # REMOVED: self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")
        self._refresh_dynamic_action_variables()
        # REMOVED: self.update_state()
    else:
        raise ValueError("Cannot set initial_particles when track_type is not 'beam'")

def _patched_bmad_set(self, values):
    """Patched _set: removes duplicate _refresh_dynamic_action_variables call."""
    from lume.actions import ActionModel
    self.simulator.cmd("set global lattice_calc_on = F")
    try:
        ActionModel._set(self, values)
    except Exception:
        raise
    finally:
        self.simulator.cmd("set global lattice_calc_on = T")
    # REMOVED duplicate: self._refresh_dynamic_action_variables()
    self.update_state()

LUMEBmadModel.initial_particles = LUMEBmadModel.initial_particles.setter(
    _patched_initial_particles_setter
)
LUMEBmadModel._set = _patched_bmad_set
```

**Warning:** monkey-patching `initial_particles` property requires reconstructing the property object. Safer to vendor the files — see next section.

---

## Recommended approach: vendor the two files

1. Copy `lume_bmad/model.py` → `virtual_accelerator/patches/lume_bmad_model.py`
2. Copy `lume/staged_model.py` → `virtual_accelerator/patches/lume_staged_model.py`
3. Apply fixes above
4. In `run.py`, import patched versions and swap before model load:
   ```python
   import virtual_accelerator.patches.lume_bmad_model as _lbm
   import lume_bmad.model as _orig
   _orig.LUMEBmadModel._set = _lbm.LUMEBmadModel._set
   _orig.LUMEBmadModel.initial_particles = _lbm.LUMEBmadModel.initial_particles
   ```
5. Open upstream PRs to `lume-bmad` and `lume` repos with same fixes

---

## Expected improvement

| Metric | Before | After (estimated) |
|---|---|---|
| Tao cmds per cycle | ~12 | ~5 |
| Fortran heap growth | ~180 MB/hr | ~0 MB/hr |
| `update_state()` calls/cycle | 2 | 1 |
| `_refresh_dynamic_action_variables()` calls/cycle | 2 | 1 |
| h5 writes/cycle | 1 (unchanged — needed) | 1 |

The h5 write + `set beam_init position_file` per cycle is still needed (particles change each cycle from ML model output). Only the redundant comb realloc and duplicate state reads are removed.

---

## Files to change upstream

| Repo | File | Change |
|---|---|---|
| `slaclab/lume-bmad` | `lume_bmad/model.py` | Remove `comb_ds_save` cmd + `update_state()` from `initial_particles.setter`; remove duplicate `_refresh_dynamic_action_variables()` from `_set()` |
| `slaclab/lume` | `lume/staged_model.py` | Add particle identity guard in `StagedModel._set()` |
