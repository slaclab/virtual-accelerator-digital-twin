# glibc Multi-Arena Memory Leak

## Summary

The virtual accelerator container exhibited steady RSS growth of ~90 MB/hr during
long-running simulations. The root cause was glibc's per-thread memory arena
behaviour, not a true heap leak in libtao or Python. Setting `MALLOC_ARENA_MAX=2`
before Python starts eliminates the growth entirely.

## Symptom

Running `model_loop_memtest.py` against a real model (`cu_hxr_staged` or
`cu_hxr_bmad`) produced linear RSS growth of approximately 2–2.5 MB per 100 s:

```
elapsed_s,  rss_mb,  rss_delta_mb
      103,   739.2,        +2.4
      633,   754.2,       +17.4
     1289,   768.2,       +31.5
     2455,   793.8,       +57.0
```

Python heap (`tracemalloc`) remained stable at ~34 MB throughout — confirming
the growth was entirely in native memory.

## Root Cause

glibc's `malloc` implementation maintains multiple independent memory arenas to
reduce lock contention in multi-threaded programs. By default it creates up to
`8 × CPU_cores` arenas. Each arena manages its own pool of freed memory.

When libtao (Fortran/C) allocates and frees heap blocks during each simulation
cycle, glibc returns freed memory to its per-arena free list but does **not**
release those pages back to the operating system. `malloc_trim(0)` only reclaims
the main (first) arena; all other arenas retain their pages indefinitely.

With many arenas active:

- Each simulation cycle allocates ~20–50 KB of Fortran/C data across several threads.
- Freed blocks land in whichever arena the allocating thread owns.
- `malloc_trim()` reclaims only arena 0; arenas 1–N hold onto pages.
- RSS accumulates at a rate proportional to the number of active arenas.

At 1.1 cycles/s and ~8 cores, this produces the observed ~90 MB/hr growth.

## Diagnosis

Three things confirmed glibc arenas as the cause:

1. **Python heap stable** — `tracemalloc` showed ~34 MB throughout. No Python
   objects were leaking.
2. **Linear, non-accelerating growth** — True Fortran leaks (e.g. an unbounded
   list) grow faster over time. Fragmentation grows at a fixed rate per cycle.
3. **`MALLOC_ARENA_MAX=2` eliminated growth** — With arenas capped at 2, freed
   pages in either arena are reclaimed by `malloc_trim()`. RSS held flat at
   1545.5 MB for 2976 s (50 min) with zero growth.

## Fix

Set `MALLOC_ARENA_MAX=2` **before Python starts**. glibc reads this variable at
the very first `malloc()` call, which occurs during Python interpreter
initialisation — any `os.environ` assignment inside Python is always too late.

### Dockerfile

```dockerfile
ENV MALLOC_ARENA_MAX=2
```

Added to the `ENV` block alongside other runtime tunables
(`OMP_NUM_THREADS`, `TORCH_NUM_THREADS`, etc.).

### entrypoint.sh

```bash
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
exec python run.py
```

Belt-and-suspenders: the `entrypoint.sh` export ensures the value is set even
when the container is launched in ways that bypass the Dockerfile `ENV` (e.g.
`docker run -e` overrides, Kubernetes env injection, or running scripts directly
inside the container).

### Running the memtest manually

```bash
# In the container — MALLOC_ARENA_MAX must prefix the Python invocation
MALLOC_ARENA_MAX=2 python scripts/model_loop_memtest.py --duration 3600
```

## Result

After the fix, a 50-minute run of `cu_hxr_staged` showed:

```
RSS: baseline=1545.5 MB  final=1545.5 MB  growth=+0.0 MB
# OK: RSS growth within bounds
```

## Remaining Noise

`AnonHugePages=12288 kB` appears in every log interval. This is 12 MB of
Transparent Huge Pages allocated by the kernel for libtao's working set. It is
a **fixed cost at startup**, not a leak — the value never increases during the
run. The THP warning in `model_loop_memtest.py` is intentional: it would flag
regressions if the kernel policy changed and huge-page usage started growing.

To suppress it entirely, disable THP at the host level:

```bash
echo madvise > /sys/kernel/mm/transparent_hugepage/enabled
```

The container already calls `prctl(PR_SET_THP_DISABLE)` at startup; the
residual 12 MB is allocated before that call completes.

## Related

- `docs/thp-memory-leak.md` — earlier investigation into Transparent Huge Pages
  as a separate (now resolved) source of RSS growth.
- `scripts/model_loop_memtest.py` — long-running memory regression test.
- `entrypoint.sh` — production entry point where the fix is applied.
- `Dockerfile` — `MALLOC_ARENA_MAX=2` in the `ENV` block.
