# Locating the second memory leak

**Period:** 2026-09-08 to 2026-09-09
**Outcome:** localised to the PVA get-request path (p4p 4.2.2). `libtao`, `lume_bmad`,
`beamphysics`, torch, h5py, numpy and `SharedPV.post()` all exonerated.
**Status:** confirmation runs in progress; one discriminating test still outstanding before
this can be filed upstream (see [Open question](#open-question-client-or-server)).

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

## Open questions

Still untested:
- Whether it scales with PV count or is strictly per-`get` (the test uses 180 PVs).
- Whether `Context.monitor()` avoids it. Production uses snapshot mode because monitors were
  unreliable through the socat proxy, but monitors are the obvious workaround if they are clean.
- Which p4p/pvxs versions are affected.

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
