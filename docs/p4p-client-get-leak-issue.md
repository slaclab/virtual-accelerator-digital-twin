# Draft issue for p4p: client `Context.get()` leaks when polling many distinct channels

**Target:** https://github.com/mdavidsaver/p4p/issues
**Status:** draft. Scaling curve characterised across four channel counts
(1 / 10 / 60 / 180); leak is **per-channel-touched-per-`get()`**. A native-heap
profile with `heaptrack` names the accumulated allocations as Python-level
(`dictresize`, `PyUnicode_New`, `_PyType_AllocNoTrack`), so the leak is Python
state retained through the Cython binding rather than a pure C++ leak in pvxs.
One residual subtlety (super-linear rate dependence vs. production) called out
below but not blocking.

---

## Title

`Context.get()` leaks native heap when polling many distinct channels; single-channel gets and `Context.monitor()` are clean

---

## Body

### Summary

Repeated `p4p.client.thread.Context.get()` calls leak native heap in a shape that
scales with the number of distinct channels the client is aware of. The average
amount leaked per `get()` is roughly proportional to **N** (channel count):

- N=1 → ~0.005 bytes/get (effectively zero over 22 M gets)
- N=10 → 0.17 bytes/get
- N=60 → 0.81 bytes/get
- N=180 → 4.13 bytes/get

That signature — the per-`get` amount growing linearly with N — is what a client-side
mechanism would produce if every `get()` touches or walks a per-channel structure
(one iteration/entry per known channel) and leaks a small increment per touch. It is
**not per-`get`** in the sense the earlier framing implied (N=1 does 22 M gets flat),
**not per-channel-time** (`monitor()` on 180 PVs is flat over 541 M callbacks), and
**not on the server side** (`SharedPV.post()` flat over 26.6 B posts).

Growth is anonymous (non-reclaimable) memory and survives `malloc_trim(0)`.
`tracemalloc` reads flat, which initially suggested the leak was native. A
`heaptrack` profile of the reproducer (see [Native profile](#native-profile-heaptrack))
identifies the accumulating allocations as **Python-level**: growing dicts,
retained strings, and — most tellingly — new Python **type** objects allocated
via `_PyType_AllocNoTrack`, which bypasses `tracemalloc`'s GC-based tracker.
So the leak is retained Python state inside p4p's Cython layer, not a pure
C++ leak in pvxs.

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
N_PV = {"n1": 1, "n10": 10, "n60": 60, "n180": 180, "monitor": 180}[ARM]

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

Same server, five client arms, all reading `SPLIT:PV:*`. `n1`/`n180`/`monitor` ran
~199 min; `n10`/`n60` ran ~102 min. All rates stable, `kb_per_1k` converged.

| Arm | PVs | Path | Ops | Rate | Δ anon | Per 1k ops | MB/h | Verdict |
|---|---|---|---|---|---|---|---|---|
| `n1` | 1 | `get()` | 22.09 M | 1,850/s | +0.10 MB | +0.005 KB | **0.03** | flat |
| `n10` | 10 | `get()` | 13.63 M | 2,227/s | +2.25 MB | +0.169 KB | **1.35** | leaks |
| `n60` | 60 | `get()` | 14.89 M | 2,457/s | +11.73 MB | +0.807 KB | **6.97** | leaks |
| `n180` | 180 | `get()` | 17.47 M | 1,461/s | +70.42 MB | +4.127 KB | **21.71** | leaks |
| `monitor` | 180 | `monitor()` | 546.7 M cb | 45,692/s | +3.03 MB | +0.006 KB | 0.91 | flat |

The `n1` arm did **more** gets than any other (22 M) and grew 300× less than
`n180`. So the leak is not per-`get`. But it is not purely per-channel either — the
per-`get` amount grows with channel count (bytes/get ≈ 0.02·N in the 10-180 range),
which is the signature of something inside each `get()` traversing a per-channel
structure.

Above N=1, MB/h is close to linear in N: 0.135, 0.116, 0.121 MB/h/PV for N=10/60/180.
So a first-order estimate at fixed rate (~2000 gets/s) is roughly **0.12 MB/h per
channel** for N ≥ 10.

Server, same period, measured separately:

| Path | PVs | Ops | Rate | Δ anon | Per 1k ops | Verdict |
|---|---|---|---|---|---|---|
| `SharedPV.post()` | 180 | 2.93 B | 381,515/s | +42.84 MB | +0.015 KB | flat |

Flat per op — the ~43 MB delta over 2.93 billion posts is 3 orders of magnitude below
the client rate; the extrapolated production-rate contribution is effectively zero.

### Native profile (heaptrack)

An `n180`-equivalent workload run under `heaptrack 1.5.0` inside the same
digest-pinned image (`heaptrack python /probe/getvar.py`, `ARM=n180`,
`DURATION_S=300`) captured **48.6 M allocations** and reported **1.45 MB
leaked** at process exit over 307 s — a rate of ~17 MB/h, agreeing with the
independent `cg_anon` measurement of ~21.7 MB/h to within `heaptrack`'s
overhead. Top accumulating allocation sites at exit (excluding one-shot
startup arenas):

| Leaked bytes | # calls | Leaf function | Meaning |
|---|---|---|---|
| 219 KB | 1,200 | `dictresize::new_keys_object` | Python dict grew (resized 1,200×) |
| 199 KB | 161 | `_PyUnicode_JoinArray` | string-join results retained |
| 170 KB | 1,177 | `PyUnicode_New` | new Python strings retained |
| 152 KB | **1,441** | `_PyType_AllocNoTrack` | **new Python type objects** |
| 139 KB | 4,312 | `PyObject_Malloc` | small Python objects |

The **1,441 leaked type-object allocations** are the strongest single clue.
Python types are normally created once per class — creating thousands of them
in a 5-minute run implies the client is manufacturing a distinct Python type
per operation (or per channel × operation) instead of reusing a cached one.

Cross-referencing with the call-count profile from the same trace, the
following pvxs entry points fire ~2× per `get()` (890 k calls in 445 k gets):

- `pvxs::client::GetBuilder::_exec_get()`
- `pvxs::client::detail::CommonBase::_buildReq()`
- `pvxs::TypeDef::TypeDef(TypeCode, std::initializer_list<>)`
- `pvxs::client::Channel::createOperations()`
- `pvxs::client::gpr_setup(...)`

So the C++ side rebuilds a `TypeDef` per operation, the Cython wrapper builds
a Python type from that `TypeDef`, and — critically — one in every ~617
`TypeDef` constructions results in a new Python type that is not freed.
A per-channel keying of the schema→PyType cache (instead of structural
equality) would produce exactly this pattern.

Suspected file: `p4p/_p4p.pyx`, around the `ClientOperation.__init__` /
result-value wrapping path where `pvxs::Value` schemas become Python
`NTScalar`-family types. Precise line requires walking the flamegraph in
`heaptrack_gui`; happy to attach the raw `heap.zst` on request.

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

### Question

Combining the shape (per-`get()` leak linear in channel count, `monitor()` clean)
with the heaptrack profile (Python types and dicts accumulating, `TypeDef`
rebuilt 2× per `get()`), the most plausible mechanism is that the Cython layer
constructs a schema-derived Python type per operation and caches it under a
key (Channel identity, or the `TypeDef` pointer address) that doesn't dedupe
across channels sharing the same schema — so N distinct channels each build up
their own copy of what should be a shared type. Does that match how
`p4p/_p4p.pyx` wires `pvxs::Value` schemas into `NTScalar` / dynamic Python
types today, and is there a supported way to key that cache on structural
equality?

### Residual: super-linear rate dependence vs. production

The scaling above holds at ~2000 gets/s. Comparing back to production:

| | PVs | gets/s | Leak | Bytes/get |
|---|---|---|---|---|
| `n180` | 180 | 1,461 | 21.71 MB/h | 4.13 |
| production | 180 | 257 | 1.60 MB/h | 1.73 |

Same channel count, ~5.7× lower rate, ~13.6× lower leak, ~2.4× lower per-`get` — so
the per-`get` amount is *not* independent of rate the way a pure per-channel-touch
model would predict. Possible sources: production sits behind a socat proxy which
may affect request pipelining or batching, or some accounting is off (production
`gets/s` is derived from wall-clock cycle time, not per-op timing). Not blocking —
the shape and the mechanism are clear enough to file — but worth flagging.

---

## Not blocking but worth doing

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
