# Memory Leak Root Cause: Transparent Huge Pages

## Symptom

The `virtual-accelerator` pod was OOMKilled every 2–3 days:

```
Last State: Terminated
  Reason:     OOMKilled  Exit Code: 137
  Started:    Fri, 14 Aug 2026 10:10:48 -0700
  Finished:   Sun, 16 Aug 2026 11:46:19 -0700
```

Memory grew at ~30 MB/hour sustained, ~460 MB/hour under peak simulation load.
At pod startup: ~850 Mi. At OOM: 4 Gi (old limit) / 6 Gi (new limit).

---

## Investigation

### Step 1 — Ruled out Python heap

`tracemalloc` snapshots inside the running process showed Python heap stable at **< 1 MB** across all observation windows. Python GC was not the issue.

### Step 2 — Ruled out p4p / lume_pva

Built a mock PVA IOC (`scripts/mock_pva_ioc.py`) serving all 175 real PVs via pure `p4p` — no pytao, no bmad. Ran `scripts/mem_monitor.py` against it for 15+ minutes at 10 Hz update rate.

Result: mock RSS **flat at 72 MB**. p4p SharedPV posting does not leak.

### Step 3 — Isolated the heap segment

Read `/proc/1/smaps` inside the production pod and broke down private dirty RSS by segment:

```
   710 MB  [heap]          ← glibc malloc arena
   218 MB  0               ← anonymous mmap
    62 MB  libtorch_cpu.so
    12 MB  libtao.so
```

The `[heap]` segment grew from ~300 MB at startup to 710 MB after 7.6 hours — **+410 MB**, all in the glibc malloc arena. `libtao.so` itself was stable at 12 MB dirty; the Fortran allocations live inside the heap segment, not mapped separately.

### Step 4 — Found the cause: Transparent Huge Pages

Read `/proc/1/smaps_rollup` and found:

```
AnonHugePages:    188416 kB   (~184 MB)
```

Cross-checked the kernel THP setting visible from the pod:

```
/sys/kernel/mm/transparent_hugepage/enabled: [always] madvise never
```

**THP is set to `always` on the k8s node.**

Observed `AnonHugePages` growing by **12 MB (6 × 2 MB pages) in 3 minutes** of active simulation.

### Why THP causes the leak

With THP `always`:

1. libtao (Fortran) allocates large beam-tracking arrays via glibc `malloc`
2. glibc requests anonymous memory from the kernel via `mmap`
3. The kernel promotes any 2 MB-aligned anonymous region to a **Transparent Huge Page** (2 MB physical page)
4. libtao frees the arrays after each simulation cycle → glibc marks the arena free
5. The kernel **cannot split a 2 MB huge page** back into 4 KB pages — the entire page stays resident
6. On the next allocation cycle, new huge pages are promoted if the arena grows
7. Net result: `AnonHugePages` count grows monotonically with simulation cycles

This is not a Python-level or application-level leak — it is a kernel memory management behavior triggered by large Fortran allocations inside libtao.

### Verification

`prctl(PR_GET_THP_DISABLE)` confirmed THP was **enabled (=0)** for the runner process. After calling `prctl(PR_SET_THP_DISABLE, 1)` the value changed to **1** — confirming the per-process opt-out works without special capabilities.

---

## Fix

### 1. Disable THP per-process at startup (`run.py`)

`prctl(PR_SET_THP_DISABLE)` opts the process out of THP promotion without requiring `CAP_SYS_ADMIN` or changing the node-level setting (which would affect all pods).

Called as the **first operation in `main()`**, before any pytao/bmad imports, so no Fortran allocation can happen before THP is disabled.

```python
PR_SET_THP_DISABLE = 41   # linux/prctl.h
libc.prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0)
```

With THP disabled, all Fortran allocations use standard 4 KB pages. When libtao frees beam arrays, glibc returns the pages to the OS normally via `madvise(MADV_FREE)` or `munmap`.

### 2. Limit glibc malloc arenas (`kubernetes/configmap.yaml`)

```
MALLOC_ARENA_MAX=1
MALLOC_MMAP_THRESHOLD_=131072
```

- `MALLOC_ARENA_MAX=1` — prevents glibc from creating per-thread arenas (default is 8× CPU count). Multiple arenas fragment memory across threads and each retains its own pool.
- `MALLOC_MMAP_THRESHOLD_=131072` (128 KB) — allocations above this size use `mmap` directly instead of sbrk/arena. `mmap` allocations are immediately returned to the OS on free.

### 3. Memory logging (`run.py`)

Added structured memory logging to stderr at key milestones and every 5 minutes (configurable via `MEM_LOG_INTERVAL_S`):

```
[thp] THP before=0 after=1 -> disabled
[mem] startup: RSS=...MB  anon=...MB  AnonHugePages=0.0MB
[mem] model-loaded: RSS=...MB  anon=...MB  AnonHugePages=0.0MB
[mem] runner-started: RSS=...MB  anon=...MB  AnonHugePages=0.0MB
[mem] t=5.0min: RSS=...MB  anon=...MB  AnonHugePages=0.0MB
```

Monitor with:
```bash
kubectl logs -n virtual-accelerator deployment/virtual-accelerator | grep -E '^\[thp\]|\[mem\]'
```

**Fix is working if:** `AnonHugePages` stays at `0.0MB` across all periodic log lines.  
**Fix failed if:** `AnonHugePages` grows → THP disable did not take effect, investigate node configuration.

---

## Files Changed

| File | Change |
|---|---|
| `run.py` | `_disable_thp()`, `_log_memory()`, `_start_mem_logger()` added |
| `kubernetes/configmap.yaml` | `MALLOC_ARENA_MAX=1`, `MALLOC_MMAP_THRESHOLD_=131072`, `MEM_LOG_INTERVAL_S=300` |

## Test Infrastructure Added

| File | Purpose |
|---|---|
| `scripts/mock_pva_ioc.py` | Pure-p4p PVA server for 175 PVs — used to rule out p4p as leak source |
| `scripts/mem_monitor.py` | Client-side RSS/heap CSV logger — subscribe to PVs and track memory over time |
| `scripts/va_stimulator.py` | Drives simulation cycles by writing writable PVs at configurable Hz |
| `scripts/va_pvlist.txt` | List of actual VA model output PVs for `mem_monitor.py` |
| `Dockerfile.mock` | Minimal image for mock IOC and monitor tools |
| `docker-compose.mock.yml` | Local test stack: mock IOC + mem-monitor + VA + VA monitor + stimulator |

Run local leak test:
```bash
docker compose -f docker-compose.yml -f docker-compose.mock.yml up -d
docker logs -f mem-monitor-va   # watch RSS/heap CSV
```
