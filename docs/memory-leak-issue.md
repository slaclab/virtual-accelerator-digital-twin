# Memory leak in virtual-accelerator-digital-twin (RSS growth ~180 MB/hr → OOM)

## Summary

The virtual accelerator digital twin pod (`cu_hxr_staged` model) experiences monotonic RSS growth of ~180 MB/hr when running with `REMOTE_INPUTS=true`. With a 6Gi memory limit, the pod OOMs and restarts after ~24 hours of continuous operation.

## Environment

- **Pod:** `virtual-accelerator` in `virtual-accelerator` namespace (dev vcluster)
- **Model:** `cu_hxr_staged` (ML surrogate + physics model via lume-bmad/pytao)
- **Image:** `ghcr.io/slaclab/virtual-accelerator-digital-twin`
- **Observed:** RSS starts at ~1 GB after model load, reaches 6 Gi limit within 24-30 hours

## Root Causes Identified

We identified **5 independent sources** of memory growth:

---

### 1. Redundant Tao commands in `lume-bmad` (primary — ~180 MB/hr)

**Package:** `lume-bmad` (`lume_bmad/model.py`)

Three redundant Tao commands execute every simulation cycle, causing Fortran heap fragmentation that glibc never returns to the OS:

| Bug | Location | Issue |
|-----|----------|-------|
| A | `initial_particles.setter` | `self.simulator.cmd(f"set beam comb_ds_save = {self.comb_ds_save}")` called every cycle. This is static config — should only be set once at `__init__`. Each call forces Tao to reallocate ~3M doubles (0.1m resolution x 300 save points x 1000 particles x 10 coords). |
| B | `initial_particles.setter` | `self.update_state()` called here, then called again immediately in `_set()`. Reads all 116 Tao variables twice per cycle for no reason. |
| C | `_set()` | `self._refresh_dynamic_action_variables()` called in the setter AND again in `_set()`. Duplicates `tao_global()` + `bunch_comb("s")` commands. |

**Result:** ~12 Tao commands per cycle instead of ~5. Each redundant command allocates/frees Fortran arrays, fragmenting the process heap. glibc's `malloc` cannot return these fragmented pages to the OS.

**Fix:** Delete the 3 redundant lines.

---

### 2. Snapshot queue backlog in `lume-pva` Runner (~2+ GB over hours)

**Package:** `lume-pva` (`lume_pva/runner.py`)

When `REMOTE_INPUTS=true`, `runner.take_snapshot()` is called in a loop to fetch live PV values. Without rate limiting, it runs at ~53 Hz — far faster than the model can consume items from the queue. Each queued item holds a `p4p.Value` (C++ `PVStructure`, ~2 KB). Over hours, 100k+ items accumulate.

**Fix:** Throttle `take_snapshot()` to the runner's update rate (0.1s) and wait for queue to drain (`qsize <= 1`) before producing the next item.

---

### 3. glibc arena fragmentation (~90 MB/hr)

**Package:** glibc (system allocator)

By default, glibc creates up to `8 x CPU_cores` independent memory arenas. `malloc_trim(0)` only reclaims from arena 0. Freed blocks in secondary arenas are never returned to the OS.

**Fix:** Set `MALLOC_ARENA_MAX=2` before Python starts (must be in environment at process launch, not set from Python). Additionally, call `malloc_trim(0)` after each simulation cycle to compact the heap.

---

### 4. HDF5 internal metadata cache (~11 MB/hr)

**Package:** `h5py` / `libhdf5`

Every cycle, `set beam_init position_file` opens and closes an HDF5 file via Tao. libhdf5 accumulates metadata cache entries (~6.2 KB per open/close) in an internal free list that is never purged automatically.

**Fix:** Call `h5py.h5.garbage_collect()` after `set beam_init position_file` each cycle.

---

### 5. StagedModel missing per-cycle cleanup (~20-40 MB/hr)

**Package:** `lume` (`lume/staged_model.py`)

`StagedModel._set()` runs all N stages sequentially but never performs any heap cleanup afterward. Each stage can leak through glibc arenas and h5py caches.

**Fix:** Add `gc.collect()` + `malloc_trim(0)` + `h5py.h5.garbage_collect()` at the end of `StagedModel._set()`.

---

### 6. Transparent Huge Pages (THP) — unbounded RSS on some hosts

**Package:** Linux kernel (host-level)

When the host has THP set to `always`, glibc heap allocations from Fortran/libtao get promoted to 2 MB huge pages. These pages are never demoted or returned to the OS after being freed.

**Fix:** Call `prctl(PR_SET_THP_DISABLE, 1)` at process startup via ctypes.

---

## Reproduction

```bash
# Demonstrates the leak (no fixes): RSS grows, queue unbounded
MALLOC_ARENA_MAX=2 python scripts/reproduce_leak.py --mode leak --duration 600

# Demonstrates the fix: RSS stable, queue at 2
MALLOC_ARENA_MAX=2 python scripts/reproduce_leak.py --mode fixed --duration 600
```

**Leak mode results (10 min):**

| Metric | Start | End |
|--------|-------|-----|
| Queue | 0 | 32,023 (never drained) |
| RSS | 1030 MB | 1100 MB (+70 MB) |
| Snapshot rate | 53 cycles/s | 53 cycles/s (unthrottled) |

**Fixed mode results (10 min):**

| Metric | Start | End |
|--------|-------|-----|
| Queue | 2 | 2 (stable) |
| RSS | 1037 MB | 1054 MB (flat, +/- 15 MB oscillation) |
| Snapshot rate | 1.6 cycles/s | 1.6 cycles/s (throttled) |

## Current Mitigations (applied in container image)

| Layer | Fix | Delivery |
|-------|-----|----------|
| lume-bmad | Remove 3 redundant Tao commands + add malloc_trim + h5py GC | Vendored patch via Dockerfile COPY |
| lume (staged_model) | Add gc + malloc_trim + h5py GC at end of `_set()` | Vendored patch via Dockerfile COPY |
| System allocator | Replace glibc with tcmalloc via `LD_PRELOAD` | Dockerfile |
| Environment | `MALLOC_ARENA_MAX=1` | Dockerfile ENV + entrypoint.sh |
| run.py | Snapshot throttle, queue drain, THP disable, periodic gc/malloc_trim | Application code |
| Observability | Prometheus metrics (`va_rss_bytes`, `va_snapshot_duration_seconds`, `va_py_heap_mb`) | Application code |

## Upstream PRs Needed

1. **`lume-bmad`** — Remove redundant `comb_ds_save` / `update_state()` / `_refresh_dynamic_action_variables()` calls. This is a 3-line deletion that fixes the primary leak.
2. **`lume` (staged_model)** — Add gc/malloc_trim/h5py cleanup at end of `StagedModel._set()`.
3. **Discussion:** Should `lume-bmad` call `h5py.h5.garbage_collect()` internally after any HDF5 file operation, or should this be the caller's responsibility?
