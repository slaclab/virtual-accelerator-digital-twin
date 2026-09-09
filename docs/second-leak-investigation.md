# Locating the second memory leak

**Period:** 2026-09-08 to 2026-09-09
**Outcome:** localised to the p4p client `Context.get()` path (p4p 4.2.2). Scaling
across N=1/10/60/180 channels shows the **per-`get` leak grows roughly linearly with
channel count** (~0.02 bytes per get per channel), pointing at a per-channel data
structure touched inside each `get()`. `heaptrack` names the accumulating
allocations as Python-level (`dictresize`, `PyUnicode_New`, **`_PyType_AllocNoTrack`
firing 1,441× in 5 min**), i.e. Python type-objects manufactured per operation
inside p4p's Cython layer — not a pure C++ leak in pvxs. `libtao`, `lume_bmad`,
`beamphysics`, torch, h5py, numpy, `SharedPV.post()`, `unpack_value`, the p4p
**server**, and `Context.monitor()` are all exonerated.
**Status:** enough evidence to file upstream; draft at
`docs/p4p-client-get-leak-issue.md`.

---

## 1. Starting point

After [bmad-ecosystem#2176](https://github.com/bmad-sim/bmad-ecosystem/pull/2176) fixed the
`rad_map` leak and radiation was re-enabled, production was stable but still growing:

| | |
|---|---|
| Growth | **+1.60 MB/h** cgroup `anon`, over 88 h |
| Engine respawns | 0 in 88 h (was one every 6.5 h pre-fix) |
| Tao child | flat |
| Runway | ~140 days |

Small, but the standing instruction was that all leaks get fixed. The job was to find it.

---

## 2. Method

Layered bisection. Each probe adds one layer to the one below and runs the same workload, so
the first arm that grows localises the leak. All probes are bare pods (`restartPolicy: Never`,
label `app=pytao-leak-probe`) in a separate namespace slot from production, which was never
touched.

Two rules learned the hard way earlier in this investigation and applied throughout:

- **Measure cgroup `anon`, not `memory.current` or RSS.** `memory.current` includes
  reclaimable page cache and slab, which swing from +7 to −21 MB/h purely with node memory
  pressure. That is far larger than the signal, and it is why five earlier estimates of this
  leak disagreed by 10x.
- **Do not trust `tracemalloc` or `memray` here.** Both are Python-only unless configured
  otherwise; this leak is native, so they read flat regardless.

---

## 3. Round 1-5: a void series

Five rounds of bisection were run on 2026-09-08 and **all results involving Bmad were
invalid.** They are recorded here because the failure mode is worth not repeating.

| Round | Arm | Result |
|---|---|---|
| 1 | staged model, set/get | +44.60 MB/h |
| 1 | + full PVA/EPICS | +42.91 MB/h → "PV layer contributes nothing" |
| 2 | FakeModel (numpy only) | +0.00 MB/h |
| 2 | `cu_hxr_bmad` (no h5 write) | +78.6 MB/h → "HDF5 hypothesis dead" |
| 3 | `cu_hxr_bmad`, 73 min | +75.4 MB/h, dead linear |
| 5 | pure pytao, `OTR2:TD11` | **+89.65 KB/cycle** → "libtao still leaks!" |

Round 5 looked alarming: 89.65 KB/cycle against the 89.31 KB/cycle this same script measured
*before* #2176. Apparently the upstream fix had done nothing.

### The error

The probe pods ran **bmad 20260821.0**, not the 20260904.1 that production runs. Checking
image digests across pods:

```
production        sha256:345f…   bmad 20260904.1
probe-pure-pytao  sha256:a741…   bmad 20260821.0
probe-bmad-short  sha256:f25d…   (older still)
```

Three different images under one tag. The probe manifests referenced the **mutable tag**
`:feature-prometheus` with **no `imagePullPolicy`**, so each node reused whatever stale copy
it had cached. The production overlay explicitly patches `imagePullPolicy: Always` — which
was the signal that this tag gets reused — and the pod events said
*"image already present on machine"* in plain text.

So round 5 was measuring the pre-fix library. Of course it matched the pre-fix number: it
*was* the pre-fix library. Filing that upstream would have wasted the maintainers' time on a
bug they had already fixed.

Rounds 1-3 were also most likely measuring the old `rad_map` leak rather than the residual,
which invalidated the round-1 conclusion that the PV layer was innocent — a ~44 MB/h leak
comfortably buries a ~1.6 MB/h signal.

**Fix adopted:** pin the image **digest**, not the tag. A digest reference cannot be
satisfied by a different cached image, which is stronger than `imagePullPolicy: Always`. And
verify `conda list bmad` *inside every pod* before reading a single number.

Two results survived, because neither involves Bmad: FakeModel +0.00 MB/h, and
`VARIANT=B` (no beam tracking) +0.01 KB/cycle.

---

## 4. The redo, digest-pinned

All arms pinned to `sha256:345f7ca5…`, bmad `20260904.1` verified in each pod first.

| Arm | Adds | `anon` |
|---|---|---|
| `probe-pure` | pure pytao, beam track, **no LUME** | **+0.00 MB/h** (132 MB flat, 58 min) |
| `probe-lume` | LUME Bmad model | −1.24 MB/h |
| `probe-staged` | + torch, h5py, inter-stage particles | **+0.35 MB/h** |
| `probe-pva` | + PVA/EPICS serving layer | **+6.86 MB/h** ← leaks |

Two conclusions:

**libtao is fixed.** `probe-pure` ran the identical script and lattice slice that leaked
89.31 KB/cycle pre-#2176, and held at exactly 132 MB for 58 minutes.

**The leak is in the PVA layer** — the one layer round 1 had wrongly cleared.

`probe-lume` and `probe-pva` both died mid-run with
`ValueError: cannot reshape array of size 0 into shape (29,)` at `lume_bmad/actions.py:71`.
That is a harness artifact: `model_loop_memtest.py` feeds random values, one combination made
the lattice calculation fail, and `actions.py` reshapes the empty result without checking.
Worth a small upstream robustness note — a failed lattice calc surfacing as a reshape error
is needlessly confusing — but not a leak.

---

## 5. Isolating `SharedPV.post()`

Commit `0ccc338` had already patched *"lume-pva `SharedPV.post()` C++ heap leak via Value
object caching"* in exactly this area, by caching one `Value` per PV and mutating it in place.
Prime suspect: either that fix is incomplete, or the in-place mutation opened a second path.

`scripts/pva_post_leak_test.py` reproduces just the post loop — no Tao, no LUME, no model —
with four arms crossing two variables, mirroring `lume_pva/variables.py` exactly (scalars at
line 314, arrays at line 434 including the `doubleValue` union selector from
`_NUMPY_TYPECODES`):

| Arm | Posts in 8.6 h | `anon` delta |
|---|---|---|
| cached-scalar (production behaviour) | **26.6 billion** | +1.84 MB |
| fresh-scalar (pre-`0ccc338`) | 14.6 billion | **+0.00 MB** |
| cached-array | 2.5 billion | −0.41 MB |
| fresh-array | 1.8 billion | +0.30 MB |

**`post()` does not leak.** 26.6 billion posts for 1.84 MB is 0.00007 KB/post, and the deltas
were identical at minute 1 and hour 8.6 — constant offsets, not growth.

**`0ccc338` is exonerated.** Cached and fresh are indistinguishable. This hypothesis had been
pursued for days, originally on the strength of the void round-1 result.

---

## 6. Isolating the get path

If `post()` is clean, the remaining half of `Runner.take_snapshot()` is ~180 client `get()`
calls per cycle via `pvua_context.get()`.

`scripts/pva_get_leak_test.py` serves 180 PVs on loopback and reads them back in a loop —
deliberately loopback, so it puts zero additional load on the shared `epics-proxy`. Two arms:
`get`, and `get-unpack` (which also converts the returned `Value` to numpy as
`unpack_value` does).

```
elapsed_s  gets       gets/s  anon_mb  anon_delta  kb_per_1k_gets
    242.0    879480    3634.6     29.4      +2.07          +2.41
    362.0   1328940    3671.2     30.2      +2.87          +2.21
    482.0   1777500    3687.5     31.1      +3.75          +2.16
    542.1   2002320    3693.9     31.5      +4.16          +2.13
    722.1   2686320    3720.0     32.7      +5.41          +2.06
```

Linear, no plateau, and `kb_per_1k_gets` converging on a stable **~2.1 KB per 1,000 gets**.
A one-time cost would keep falling toward zero instead (as it did in `probe-pure`).

Both arms are identical, so **`unpack_value` is innocent**.

### It accounts for production exactly

| | Gets/s | Leak |
|---|---|---|
| Probe | ~3,690 | ~25 MB/h |
| Production (180 PVs ÷ 0.7 s) | ~257 | **1.7 MB/h predicted** |
| Production measured | | **1.60 MB/h** |

---

## 7. Chain of evidence

| Layer | Result | Verdict |
|---|---|---|
| libtao / Tao (pure pytao) | +0.00 MB/h | Fixed by #2176 |
| `lume_bmad` Bmad model | −1.24 MB/h | Clean |
| torch, h5py, inter-stage particles | +0.35 MB/h | Clean |
| numpy, harness loop (FakeModel) | +0.00 MB/h | Clean |
| `SharedPV.post()`, 4 ways | flat over 26.6 B posts | Clean |
| `unpack_value` | identical to plain get | Clean |
| p4p **server** get handling | flat over 637 M posts | Clean |
| **p4p client `Context.get()`** | **~1.7-2.1 KB / 1,000 gets** | **Leaks** |
| `Context.monitor()`, 180 PVs | +3.03 MB over 541 M callbacks | Clean |
| `Context.get()`, N=1 | +0.10 MB over 22 M gets | Clean |
| `Context.get()`, N=10 | +2.25 MB, +0.169 KB/1k over 13.6 M gets | Leaks |
| `Context.get()`, N=60 | +11.73 MB, +0.807 KB/1k over 14.9 M gets | Leaks |

---

## 8. Client or server? — resolved: client

`scripts/pva_get_leak_test.py` runs the server and client in one process, so it could not
attribute the growth. `kubernetes/pva-split-test.yaml` splits them into separate pods, hence
separate cgroups, so `anon` is measured per role. The client reaches the server by pod IP —
pvxs rejects DNS names in `EPICS_PVA_NAME_SERVERS` (*"IPv4 address too long"*).

| Role | Work done | `anon` |
|---|---|---|
| **Client** (`Context.get()` loop) | 1.9 M → 3.2 M gets | **+15.80 → +17.91 MB, climbing** |
| **Server** (`post()` only) | 510 M → 637 M posts | **+13.88 → +13.89 MB, flat** |

The server absorbed **637 million posts** and moved 0.01 MB. The client climbs ~0.36 MB/min
at 3,500 gets/s — about **21.4 MB/h**.

Both roles carry a fixed ~13 MB startup offset (interpreter, PVA channel setup for 180 PVs),
which is why `kb_per_1k_ops` declines while the slope stays constant. It is the slope that
matters.

**Conclusion: the leak is in the p4p client `Context.get()`.** Server-side get handling and
`SharedPV.post()` are both clean.

---

## 9. Scaling curve — resolved: per-channel-touched-per-`get`

Five arms of `scripts/pva_get_variants.py` against the same server, `ARM` selecting
channel count (`n1`/`n10`/`n60`/`n180`) or mode (`monitor` on 180 PVs). All rates
stable, `kb_per_1k` converged.

| Arm | PVs | Path | Ops | Rate | Δ anon | Per 1k ops | MB/h |
|---|---|---|---|---|---|---|---|
| `pva-n1` | 1 | `get()` | 22.09 M | 1,850/s | +0.10 MB | +0.005 KB | 0.03 |
| `pva-n10` | 10 | `get()` | 13.63 M | 2,227/s | +2.25 MB | +0.169 KB | 1.35 |
| `pva-n60` | 60 | `get()` | 14.89 M | 2,457/s | +11.73 MB | +0.807 KB | 6.97 |
| `pva-n180` | 180 | `get()` | 17.47 M | 1,461/s | +70.42 MB | +4.127 KB | 21.71 |
| `pva-monitor` | 180 | `monitor()` | 546.7 M cb | 45,692/s | +3.03 MB | +0.006 KB | 0.91 |

Two clean readings from the shape:

**Per-`get` leak scales linearly with channel count above N=1.** Bytes per get:
0.005 (N=1) → 0.17 (N=10) → 0.81 (N=60) → 4.13 (N=180). Dividing by N: 0.017,
0.013, 0.023 bytes/get/PV. So each `get()` leaks ≈ 0.02 · N bytes on average.
That is what a client-side mechanism produces if every `get()` traverses a
per-channel structure (a channel map lookup, an operation state list, a request
builder that walks known channels) and each traversal-step leaks a small
increment.

**MB/h is close to linear in N once N ≥ 10.** Slopes MB/h/PV: 0.135, 0.116, 0.121 for
N=10/60/180 → about **0.12 MB/h per channel at ~2000 gets/s**. N=1 sits well below
that line — the first channel is essentially free.

**`monitor()` remains flat.** 541 M callbacks against 180 PVs, +3.03 MB pinned
since t=782 s. The affected path is `get()`-specific.

### Residual: reproducer over-predicts production

The 0.12 MB/h/PV rule at ~2000 gets/s predicts production (180 PVs, 257 gets/s) at
~2.7 MB/h if bytes/get held constant, or ~2.9 MB/h if we linearly scaled `n180`
down by rate. Production actually shows **1.60 MB/h** — about half. Rate ratio 5.7×,
leak ratio 13.6×, so leak grows faster than linearly in rate. Possible causes:
socat proxy in production affects request shape, or the production `gets/s` figure
is derived from wall-clock cycle time rather than per-op timing. Worth noting;
does not change the diagnosis.

---

## 10. Native profile — resolved: Python-object retention in the Cython layer

`kubernetes/pva-heaptrack.yaml` runs the `n180` workload under
`heaptrack 1.5.0` inside the digest-pinned production image. The pod
`apt install`s heaptrack at start (no image rebuild), executes 300 s of
workload under `heaptrack python /probe/getvar.py`, then runs
`heaptrack_print` and sleeps so the summary can be pulled via `kubectl
exec` / `kubectl cp`.

Headline numbers, aligned across the two independent measurements:

| Method | Duration | Rate | Notes |
|---|---|---|---|
| heaptrack "leaked at exit" | 307 s | ~17 MB/h | 1.45 MB unfreed at process exit |
| cg_anon delta (same pod) | 307 s | ~22 MB/h | +0.36 MB/min in steady state |
| `pva-n180` baseline (no heaptrack) | 199 min | 21.71 MB/h | Table in §9 |

The heaptrack rate agrees with the black-box `anon` rate to within
heaptrack's own overhead — so the profile captured the actual leak, not a
distinct artefact.

### Top accumulating allocation sites at exit

Startup-only allocations (Python arenas from interpreter init, 786 KB over 4
calls, one-shot) omitted. The multi-call entries are the accumulating ones:

| Leaked bytes | # calls | Leaf function | Meaning |
|---|---|---|---|
| 219 KB | 1,200 | `dictresize::new_keys_object` | Python dict grew (resized 1,200×) |
| 199 KB | 161 | `_PyUnicode_JoinArray` | string-join results retained |
| 170 KB | 1,177 | `PyUnicode_New` | new Python strings retained |
| 152 KB | **1,441** | `_PyType_AllocNoTrack` | **new Python type objects** |
| 139 KB | 4,312 | `PyObject_Malloc` | small Python objects |

The 1,441 leaked type-object allocations are the smoking gun. Python types
are usually built once per class. Creating ~1,441 of them in 5 minutes,
never freed, implies a distinct Python type is being manufactured per
operation (or per channel × operation) and cached under a key that doesn't
dedupe.

### Call-count profile: what fires per-`get()`

Top per-op allocators from the same trace, at 445 k gets in 307 s:

| Calls | Per get | Site |
|---|---|---|
| 890 k | 2× | `pvxs::client::GetBuilder::_exec_get()` |
| 890 k | 2× | `pvxs::client::detail::CommonBase::_buildReq()` |
| 890 k | 2× | `pvxs::TypeDef::TypeDef(TypeCode, initializer_list<>)` |
| 890 k | 2× | `pvxs::client::Channel::createOperations()` |
| 890 k | 2× | `pvxs::client::gpr_setup(...)` |
| 890 k | 2× | `pvxs::Value::Value(shared_ptr<>)` |

The C++ side rebuilds a `TypeDef` per operation (2× per `get()`); the
Cython wrapper builds a Python type from it; one in every ~617 `TypeDef`
constructions results in a new Python type that is not freed
(1,441 leaked types / 890 k TypeDefs). Most `get()`s reuse a cached type;
some don't — the cache is likely keyed by Channel identity or `TypeDef`
pointer rather than by structural equality, so N distinct channels
gradually populate their own type entries.

### Why `tracemalloc` missed this

`_PyType_AllocNoTrack` bypasses CPython's GC tracker, and `tracemalloc`
hooks into the same tracker for non-arena allocations. So type objects
allocated through this path do not appear in `tracemalloc` snapshots. Dict
growth is also cache-behaviour — the dict itself is a long-lived object
whose backing array grows — which doesn't register as new
`tracemalloc`-visible allocations at the Python object level.

### Verdict

- The leak is retained Python state inside p4p's Cython bindings, not a
  pure C++ leak in pvxs.
- Signature-based hypothesis from §9 (per-channel structure walked per
  `get()`) is refined: the per-channel structure is a **schema→PyType
  cache** that fails to dedupe across channels sharing the same NT type.
- Suspected file: `p4p/_p4p.pyx`, around the `ClientOperation`
  construction and the type-wrapping path where `pvxs::Value` schemas
  become Python NT types.

---

## Open questions

Remaining:
- Exact `p4p/_p4p.pyx` function and line — needs the heaptrack flamegraph
  in `heaptrack_gui` or a code walk.
- Which p4p/pvxs versions are affected.
- Whether the super-linear rate dependence between reproducer and
  production reflects a proxy artefact or a real second-order effect.

---

## Environment

```
p4p          4.2.2 (pypi)   bundles its own pvxs: p4p/../pvxslibs/lib/libpvxs.so.1.5
pvxs         1.5.2 (conda-forge)  -- present but NOT what p4p links against
epics-base   7.0.9.0
bmad         20260904.1 (nompi_h3858d3f_100)
pytao        1.2.4
Python       3.12.14, Linux x86_64
allocator    glibc (tcmalloc preload removed)
image        sha256:345f7ca5b2d944804e97325172ce4baa19877a6ece5878ac08d190c77143311f
```

---

## Lessons

1. **Pin digests, not tags, for anything compared against production** — and verify the
   dependency version inside the pod before reading numbers. Cost: five void rounds.
2. **Measure the right memory class.** `anon` for leaks; `memory.current` includes
   reclaimable cache that swings ±20 MB/h with node load.
3. **Choose the instrument after establishing the memory category.** `tracemalloc` and
   `memray` with `native_traces=False` are blind to native allocations. memray also
   segfaulted the service twice (`alloc.stack_trace()`), and disabling it fixed those crashes.
4. **Fit rates over many periods at a fixed phase.** Short windows produced three wrong rate
   calls during this investigation.
5. **Capture perishable evidence immediately.** Pod logs are destroyed on the next restart
   and by reaping. Snapshot with write-to-temp-then-`mv`, never `cmd > file`, which truncates
   before the command runs and blanks good data when the pod is gone.
6. **A negative result on a large effect can hide a small one.** Round 1 cleared the PV layer
   while a 44 MB/h leak masked the 1.6 MB/h signal being hunted.
