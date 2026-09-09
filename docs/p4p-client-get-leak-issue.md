# Draft issue for p4p: memory leak in client `Context.get()`

**Target:** https://github.com/mdavidsaver/p4p/issues
**Status:** draft. Confirmation runs still in progress; see
[Before filing](#before-filing) for the two checks worth completing first.

---

## Title

`Context.get()` leaks ~2 KB per 1,000 calls (native heap, non-reclaimable)

---

## Body

### Summary

Repeated `p4p.client.thread.Context.get()` calls leak native heap at roughly **2 KB per
1,000 gets**, linearly and without plateau. The growth is anonymous (non-reclaimable) memory
and survives `malloc_trim`, so it is not allocator retention.

A long-running service of ours polls ~180 PVs every 0.7 s, which works out to ~257 gets/s and
**1.60 MB/h** — about 1.1 GB/month. We reached the p4p client by elimination while chasing
that growth through an accelerator-physics stack; the isolated reproducer below has no
dependency on any of it.

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

Server and client in **separate processes**, so the growth can be attributed to one side.
Measures cgroup `anon` because the leak is native — `tracemalloc` reads flat throughout.

```python
# server.py -- serve 180 scalar PVs, keep posting so nothing short-circuits
import time
from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

names = [f"SPLIT:PV:{i:04d}" for i in range(180)]
pvs = {n: SharedPV(nt=NTScalar("d"), initial=float(i)) for i, n in enumerate(names)}
srv = Server(providers=[pvs])
i = 0
while True:
    for n in names:
        pvs[n].post(float(i % 1000)); i += 1
```

```python
# client.py -- get all 180 in a loop, report cgroup anon
import time
from p4p.client.thread import Context

def anon_mb():
    with open("/sys/fs/cgroup/memory.stat") as f:
        for line in f:
            k, _, v = line.partition(" ")
            if k == "anon":
                return int(v) / 1048576

names = [f"SPLIT:PV:{i:04d}" for i in range(180)]
ctx = Context("pva")
time.sleep(8)                     # let channels establish before the baseline
a0, gets, t0, tl = anon_mb(), 0, time.monotonic(), time.monotonic()
while True:
    for n in names:
        ctx.get(n, timeout=5); gets += 1
    if time.monotonic() - tl >= 60:
        d = anon_mb() - a0
        print(f"{time.monotonic()-t0:.0f}s gets={gets} anon_delta={d:+.2f}MB "
              f"kb_per_1k={d*1024/gets*1000:+.2f}")
        tl = time.monotonic()
```

Note: `EPICS_PVA_NAME_SERVERS` must be given as an **IP**, not a hostname — pvxs rejects DNS
names with *"IPv4 address too long"*.

### Result

Client, 15 minutes, 3,500 gets/s:

```
540s   gets=1904940   anon_delta=+15.80MB
600s   gets=2115360   anon_delta=+16.12MB
660s   gets=2331000   anon_delta=+16.48MB
720s   gets=2544660   anon_delta=+16.84MB
780s   gets=2760480   anon_delta=+17.20MB
840s   gets=2971440   anon_delta=+17.54MB
900s   gets=3185280   anon_delta=+17.91MB
```

Steady **+0.36 MB/min** (~21 MB/h). There is a fixed ~13 MB startup offset from interpreter
and channel setup; the slope after that is constant, giving roughly **1.7-2.1 KB per 1,000
gets**.

Server over the same period, doing **637 million posts**:

```
 960s  posts=510569460  anon_delta=+13.88MB
1200s  posts=636876000  anon_delta=+13.89MB
```

Flat — 0.01 MB across 127 million additional posts. So the server side and `SharedPV.post()`
are not implicated.

### Additional observations

- **Independent of value handling on the client.** Converting the returned `Value` to numpy
  versus discarding it made no difference (two arms, identical rates).
- **`SharedPV.post()` is clean**, tested four ways in a separate experiment: reusing one
  `Value` and mutating it in place vs. constructing a fresh `Value` per post, crossed with
  scalar and 10,000-element array payloads. Flat across **26.6 billion posts** (largest delta
  1.84 MB, and identical at minute 1 and hour 8.6).
- **Non-reclaimable.** Growth is in cgroup `anon`, survives `malloc_trim(0)`, and appears in
  the `brk` heap rather than in CPython's `mmap` arenas — so it is native, not Python objects.
  `tracemalloc` shows nothing.
- **Reproduced under glibc.** Not an allocator artifact; we removed a tcmalloc preload earlier
  in this investigation and the behaviour is unchanged.

### Impact

At our production rate (~257 gets/s) this is 1.60 MB/h, so a service is forced to restart
every few weeks. Not urgent for us, but it puts a ceiling on uptime for any long-running
polling client.

### Question

Is there a supported way to avoid this — for example, does `Context.monitor()` share the
affected path? We use repeated `get()` rather than monitors because monitors proved unreliable
through a socat proxy in our deployment, but we would switch if monitors are unaffected.

---

## Before filing

Two things worth completing so the report is not immediately bounced back:

1. **Minimise the PV count.** The reproducer uses 180 PVs. Re-run with 1 PV to establish
   whether the leak is per-`get` or per-channel-operation. If it is per-channel, the framing
   changes.
2. **Check `Context.monitor()`.** If monitors are clean that is both a workaround for us and
   useful information for the maintainer about where to look.

Also worth doing but not blocking:

- Test a second p4p version to give a comparison point.
- Confirm on a non-containerised host, in case cgroup `anon` accounting on this platform is a
  factor (unlikely — RSS moves in step).

## Supporting material

- `docs/second-leak-investigation.md` — the full bisection, including the layers ruled out
- `scripts/pva_get_leak_test.py` — combined server+client version
- `scripts/pva_post_leak_test.py` — the four-arm `post()` test
- `kubernetes/pva-split-test.yaml` — the separated client/server manifests
