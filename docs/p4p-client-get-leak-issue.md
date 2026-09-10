# Draft issue for p4p: client `Context.get()` leaks when polling many distinct channels

**Target:** https://github.com/mdavidsaver/p4p/issues
**Status:** draft. Scaling curve between the two known data points (1 channel, 180
channels) is not yet characterised — see [Before filing](#before-filing).

---

## Title

`Context.get()` leaks native heap when polling many distinct channels; single-channel gets and `Context.monitor()` are clean

---

## Body

### Summary

Repeated `p4p.client.thread.Context.get()` calls across ~180 distinct PVs leak native
heap linearly and without plateau. The **same script issuing more gets against a single
PV** shows no growth over 10 M+ calls, and `Context.monitor()` on the same 180 PVs is
flat over 250 M+ callbacks. So the leak is bound to the combination of many channels
and the get-request path — it is **not per-`get`**, **not present in `monitor()`**, and
**not on the server side**.

Growth is anonymous (non-reclaimable) memory, survives `malloc_trim(0)`, appears in the
`brk` heap rather than CPython's `mmap` arenas, and is invisible to `tracemalloc` — so
this is native, not Python objects.

In our long-running service (~180 PVs polled at ~257 gets/s) the leak runs at **1.60
MB/h**, ~1.1 GB/month.

### Environment

```
p4p          4.2.2 (pypi)
pvxs         bundled with p4p: p4p/../pvxslibs/lib/libpvxs.so.1.5
epics-base   7.0.9.0 (conda-forge)
Python       3.12.14
OS           Linux x86_64 (container)
allocator    glibc (no tcmalloc preload)
```

### Reproducer

Server and client run in **separate processes** so that anon growth can be attributed
to one side. Three client arms exercised against the same server: `n1`, `n180`, and
`monitor`. Client is byte-for-byte the same code path except for `N_PV` and
`get()` vs `monitor()`.

```python
# server.py -- serve N scalar PVs and keep posting
import time
from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

N = 180
names = [f"SPLIT:PV:{i:04d}" for i in range(N)]
pvs = {n: SharedPV(nt=NTScalar("d"), initial=float(i)) for i, n in enumerate(names)}
srv = Server(providers=[pvs])
i = 0
while True:
    for n in names:
        pvs[n].post(float(i % 1000)); i += 1
```

```python
# client.py -- ARM in {n1, n180, monitor}
import os, sys, time, threading
from p4p.client.thread import Context

MB = 1024.0 * 1024.0
ARM = os.environ.get("ARM", "n180")
N_PV = {"n1": 1, "n180": 180, "monitor": 180}[ARM]

def anon_mb():
    with open("/sys/fs/cgroup/memory.stat") as f:
        for l in f:
            k, _, v = l.partition(" ")
            if k == "anon":
                return int(v) / MB

names = [f"SPLIT:PV:{i:04d}" for i in range(N_PV)]
ctx = Context("pva")
time.sleep(10)                              # let channels establish
a0, ops, t0, tl = anon_mb(), 0, time.monotonic(), time.monotonic()

if ARM == "monitor":
    counter = {"n": 0}; lock = threading.Lock()
    def cb(v):
        with lock: counter["n"] += 1
    subs = [ctx.monitor(n, cb) for n in names]
    while True:
        time.sleep(1)
        if time.monotonic() - tl >= 60:
            with lock: ops = counter["n"]
            d = anon_mb() - a0
            print(f"{time.monotonic()-t0:.0f}s ops={ops} anon_delta={d:+.2f}MB "
                  f"kb_per_1k={d*1024/max(ops,1)*1000:+.4f}")
            tl = time.monotonic()
else:
    while True:
        for n in names:
            ctx.get(n, timeout=5); ops += 1
        if time.monotonic() - tl >= 60:
            d = anon_mb() - a0
            print(f"{time.monotonic()-t0:.0f}s ops={ops} anon_delta={d:+.2f}MB "
                  f"kb_per_1k={d*1024/max(ops,1)*1000:+.4f}")
            tl = time.monotonic()
```

Note: `EPICS_PVA_NAME_SERVERS` must be given as an **IP**, not a hostname — pvxs
rejects DNS names with *"IPv4 address too long"*.

### Result

Same server, three client arms, ~93 min each:

| Arm | PVs | Path | Ops | Rate | Δ anon | Per 1k ops | Verdict |
|---|---|---|---|---|---|---|---|
| `n180` | 180 | `get()` | 8.56 M | 1,532/s | **+32.96 MB** | **+3.94 KB** | **leaks** |
| `n1` | 1 | `get()` | 10.85 M | 1,943/s | +0.10 MB | +0.010 KB | flat |
| `monitor` | 180 | `monitor()` | 257 M callbacks | 45,968/s | +3.02 MB | +0.012 KB | flat |

The `n1` arm did **more** gets than `n180` (10.85 M vs 8.56 M in the same window) and
grew 300× less. So the leak is **not per-`get`** — it requires multiple distinct
channels.

Server, same period, measured separately:

| Path | PVs | Ops | Rate | Δ anon | Per 1k ops | Verdict |
|---|---|---|---|---|---|---|
| `SharedPV.post()` | 180 | 2.93 B | 381,515/s | +42.84 MB | +0.015 KB | flat |

Flat per op — the ~43 MB delta over 2.93 billion posts is 3 orders of magnitude below
the client rate; the extrapolated production-rate contribution is effectively zero.

### Additional observations

- **`SharedPV.post()` is clean**, tested four ways in a separate experiment: reusing
  one `Value` and mutating it in place vs. constructing a fresh `Value` per post,
  crossed with scalar and 10,000-element array payloads. Flat across **26.6 billion
  posts** (largest delta 1.84 MB, identical at minute 1 and hour 8.6).
- **`unpack_value` is not the culprit.** Combined server+client arms with
  (`pva-get`) and without (`pva-get-unpack`) unpack give identical rates.
- **Non-reclaimable.** Growth is in cgroup `anon`, survives `malloc_trim(0)`, and
  appears in the `brk` heap rather than in CPython's `mmap` arenas. `tracemalloc`
  reads flat.
- **Reproduced under glibc.** Not an allocator artifact; a tcmalloc `LD_PRELOAD` was
  removed earlier in this investigation and behaviour is unchanged.

### Impact

At 257 gets/s across 180 PVs this is 1.60 MB/h, so a long-running polling client is
forced to restart every few weeks. Not urgent for us, but it puts a ceiling on uptime
for any client that polls a moderately sized PV set.

### Workaround

`Context.monitor()` on the same 180 PVs is flat over 250 M+ callbacks (see table). We
have not deployed it as our workaround because monitors are unreliable through a
socat proxy in front of our IOCs (that is a proxy issue, not a p4p one). Anyone whose
monitors work should prefer them.

### Root cause (found 2026-09-09)

The bug is in **pvxs** (`src/client.cpp`, `ContextImpl::cacheClean()`), not p4p.

`ContextImpl` maintains a strong-reference channel cache (`chanByName`). A periodic
timer (every 10 s) calls `cacheClean()` to garbage-collect unused channels via a
two-phase mark/sweep. The implementation contains a logic bug that makes the mark phase
dead code:

```cpp
// pvxs src/client.cpp ~line 1350 (v1.5.2)
else if(action!=Context::Clean || cur->second.use_count()<=1) {
    cur->second->garbage = true;           // BUG: always sets garbage = true

    if(action==Context::Clean && !cur->second->garbage) {  // DEAD CODE: always false
        // mark for next sweep — never executes
```

Line 1351 sets `garbage = true`. Line 1353 tests `!garbage` — always false. The
"mark for next sweep" branch never runs. Every cleanup tick either sweeps immediately
(if `use_count() <= 1`) or does nothing (if `use_count() > 1`).

Under a high-frequency `get()` workload across N channels, each in-flight `GPROp`
holds a `shared_ptr<Channel>`, keeping `use_count() >= 2` during cleanup ticks. The
broken mark phase means these channels are never marked and therefore never swept on
subsequent ticks — they accumulate in `chanByName` indefinitely.

This explains all observations:
- **Single-channel flat**: 1 cache entry, cleanup always sees `use_count()==1`
- **180-channel leak**: N entries, many have `use_count()>1` during cleanup ticks
- **`monitor()` flat**: subscriptions hold channels alive intentionally; the broken GC
  doesn't matter because the channels are *meant* to stay alive
- **Non-reclaimable native memory**: channels hold Connection objects, socket state,
  type registries — C++ heap, invisible to Python and `malloc_trim`

**Fix** (one logical change, `src/client.cpp`):

```cpp
// BEFORE (broken)
else if(action!=Context::Clean || cur->second.use_count()<=1) {
    cur->second->garbage = true;
    if(action==Context::Clean && !cur->second->garbage) {

// AFTER (fixed)
else if(action!=Context::Clean || cur->second.use_count()<=1) {
    if(action==Context::Clean && !cur->second->garbage) {
        cur->second->garbage = true;   // moved here — mark phase now works
```

See `docs/pvxs-cache-clean-bug.md` for the full technical analysis and proposed fix.

The fix has been applied in `scripts/pvxs-fix-test/Dockerfile.fixed`. Short-duration
tests (< 5 min) do not show a measurable difference because the 10 s cache cleaner
timer fires too rarely to accumulate divergence. The production signature (hours,
~1.6 MB/h at 257 gets/s, 180 PVs) is the correct benchmark.

### Question

~~Given the shape — leak triggered by channel count, not by call rate, not present in
`monitor()` — is there a shared client-side get-request or channel-cache path that
retains something per-channel-per-`get`?~~

**Answered**: the leak path is `chanByName` channel cache in pvxs `ContextImpl`, with
a broken mark/sweep GC in `cacheClean()`. File against
https://github.com/epics-base/pvxs.

---

## Before filing

Two more data points are needed to state the scaling law rather than the two-point
lower/upper bound:

| | PVs | gets/s | Leak |
|---|---|---|---|
| production | 180 | 257 | 1.60 MB/h |
| `n180` | 180 | 1,541 | 21.8 MB/h |

Same PV count, 6× the rate, 13× the leak. On a per-`get` basis they are 2.3× apart.
Neither pure per-channel-per-second nor pure per-get fits. **`pva-n10` and `pva-n60`
arms** (identical script, `ARM` maps to PV count in `scripts/pva_get_variants.py`)
would fix the shape before we assert it upstream.

Also worth doing but not blocking:

- Test a second p4p version to give a comparison point.
- Confirm on a non-containerised host, in case cgroup `anon` accounting on this
  platform is a factor (unlikely — RSS moves in step).

## Supporting material

- `docs/second-leak-investigation.md` — the full bisection, including the layers ruled
  out (`libtao`, `lume_bmad`, torch, h5py, numpy, `SharedPV.post()`, `unpack_value`)
- `scripts/pva_get_variants.py` — the `n1`/`n180`/`monitor` arms in one file
- `scripts/pva_post_leak_test.py` — the four-arm `post()` test
- `scripts/pva_get_leak_test.py` — combined server+client get test
- `kubernetes/pva-split-test.yaml` — separated client/server manifests
- `kubernetes/pva-get-variants.yaml` — the three variant arms
