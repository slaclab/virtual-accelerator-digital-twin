# libtao beam-tracking memory leak: investigation, mitigation, and results

**Date:** 2026-08-28
**Affected:** `virtual-accelerator-digital-twin`, all models with `track_beam=True`
**Versions:** `bmad 20260824.0` (conda-forge, `nompi_h3858d3f_100`), `pytao 1.2.4`, Python 3.12
**Status:** Mitigated and deployed. One contributing cause remains open (see [Second leak](#the-second-leak-open)).

---

## 1. Summary

Tao/Bmad leaks **~89 KB of native heap per beam track**. It is a defect in third-party
Fortran code, not in this repository and not in the container configuration. Any
long-running process that repeatedly tracks a beam grows without bound.

Because the defect is upstream, it cannot be fixed here. It is instead **bounded**: Tao now
runs in a child process that is automatically respawned once memory growth exceeds a
threshold. Terminating a process returns all its memory to the OS regardless of the defect.

| Measure | Before | After |
|---|---|---|
| Time to 6 GiB limit | 3.5 days | **14.2 days** |
| Unplanned pod restarts | ~1/day (OOMKilled) | 0 |
| Cost of mitigation | — | 0.89 s per respawn (0.004% of runtime) |

The improvement is 4×, not complete, because a **second unrelated leak of ~16 MB/h** was
discovered in the parent process during validation. That one is Python-side and precisely
localised, so it is far more tractable than the first.

---

## 2. Symptom

The pod grew from ~270 MB to 4595 MB in 17 hours and was OOMKilled (exit 137) at its 6 GiB
limit after 23 hours, then restarted and repeated. Historical reports in
`docs/memory-leak-issue.md` describe ~180 MB/h and OOM every 24–30 hours.

A colleague running comparable code outside a container reported no growth, which initially
pointed suspicion at the container.

---

## 3. Investigation

### 3.1 The comparison that prompted the investigation was not valid

The colleague's script (`lcls_live_model/live_model.py`) contains **zero** beam-tracking
calls — no `track_type = beam`, no `beam_init`, no `bunch_comb`, no `comb_ds_save`. It only
computes optics (Twiss parameters and R-matrices) via `lat_list`. It therefore never
executes the faulty code path.

The two programs were doing different work. The comparison carried no diagnostic
information, and following it would have wasted effort on the container.

### 3.2 Container and environment causes ruled out

| Hypothesis | Evidence against |
|---|---|
| cgroup page-cache accounting | `anon 5.55 GB`, `file 42 MB` — real anonymous memory |
| `/dev/shm` or shared-memory segment leak | `shmem 0` |
| Thread leak | Thread count constant at 2 |
| File-descriptor leak | 4 descriptors |
| glibc arena fragmentation | `MALLOC_ARENA_MAX` already set; reproduced with a single arena |
| Memory allocator | Reproduced under **both** glibc malloc and `tcmalloc_minimal` |
| Python-side leak | `[heap]` was **5.39 GB of 5.46 GB RSS** (see below) |
| Transparent Huge Pages | `anon_thp` only 146 MB of 5550 MB (2.6%) |

The decisive measurement is the last-but-one. CPython allocates its object arenas via
`mmap`, which appear as separate anonymous regions; C and Fortran `malloc` uses the `brk`
heap, which appears as `[heap]`. With `[heap]` accounting for 98.7% of RSS, the growth
cannot be Python objects. This also explains why the existing `tracemalloc` instrumentation
reported nothing — it only observes Python allocations.

### 3.3 Controlled experiment

Three variants, 40 minutes each, identical loops, glibc malloc, `malloc_trim(0)` called
before every sample. Only the beam/readback configuration differed.

| Variant | Configuration | ms/cycle | Cycles | Growth | Leak/cycle |
|---|---|---|---|---|---|
| A | `track_type=beam`, `bunch_comb` read | 1323 | 1,814 | +158.8 MB | **89.31 KB** |
| B | `track_type=single`, no readback | 3.5 | 682,308 | +228.4 MB | 0.343 KB |
| C | `track_type=beam`, no readback | 1325 | 1,811 | +158.1 MB | **89.30 KB** |

Attribution of the 89.31 KB lost per iteration:

- **beam tracking: 88.96 KB — 99.61%**
- `bunch_comb` readback: 0.01 KB — 0.01%
- command / lattice-recalculation path: 0.343 KB — 0.38%

Key inferences:

1. **A and C agree to within 0.01 KB/cycle.** Variant C never reads the comb, so the leak is
   in the tracking itself, not in the readback or the C-interface array marshalling. C's
   cycle time (1325 ms) matches A's (1323 ms), confirming tracking genuinely occurred in C —
   this was a built-in validity check.
2. **It survives `malloc_trim(0)`.** Every sample trims first. Memory that does not return
   was never passed to `free()`. This is a leak, not fragmentation or allocator retention.
3. **Perfectly linear, no plateau.** First- and second-half growth rates agree to three
   significant figures in all three variants (A: 237.2 vs 237.2 MB/h). Not a cache filling.
4. **tcmalloc only delays onset.** Under `tcmalloc_minimal` the first ~13 minutes appear flat
   because leaked allocations are satisfied from its startup free pool. Under glibc, growth
   begins on iteration one. The asymptotic rate is the same.
5. **A second, independent leak exists** on the command path: variant B never tracks a beam
   yet leaked 0.343 KB/cycle (~43 bytes per Tao command) without plateauing. Negligible here
   (0.38%) but relevant to any tight command loop.

### 3.4 Model validation

The measured rate reproduces the production failure quantitatively:

| | Predicted | Observed |
|---|---|---|
| Growth rate | 239 MB/h | 258 MB/h |
| Time to OOM from 269 MB baseline | 22.8 h | **23.0 h** |

---

## 4. Mitigation

### 4.1 Principle

When a process terminates, the OS reclaims **all** of its memory, regardless of whether the
program called `free()`. Tao therefore runs in a child process (`pytao.SubprocessTao`) which
is respawned when it has grown too much.

The PVA server remains in the parent process, so **published PVs stay available** throughout
a respawn — clients see a sub-second pause rather than an outage. This is why respawning the
engine was chosen over restarting the whole pod.

### 4.2 Injection point — no patching required

`virtual_accelerator/bmad/factory.py:55` performs a **function-local**
`from pytao import Tao`, then constructs at line 56:

```python
tao = Tao(f"-init {init_file} -noplot -slice_lattice {start_element}:{end_element}")
```

Because that import is function-local, the name resolves off the `pytao` module at call
time. Setting `pytao.Tao = RecyclableTao` in `run.py` before building the model is therefore
sufficient. There is no injection hook in `get_cu_hxr_staged_model` /
`get_cu_hxr_bmad_model`, and patching `factory.py` would have required vendoring a full copy.
**The four existing patches in `todo/patches/` are untouched.**

`RecyclableTao` subclasses `SubprocessTao`, which subclasses `Tao`, so `isinstance` checks and
the `tao: Tao` annotation in `lume_bmad/model.py` remain satisfied.

### 4.3 The state-restoration problem

A respawned child returns at the **design lattice** with none of our configuration. Eight
settings must be replayed, spread across three files owned by three packages:

| # | State | Set at | Consequence if lost |
|---|---|---|---|
| 1 | `-init … -slice_lattice OTR2:OTR4` | `factory.py:56` | Wrong lattice extent — usually errors |
| 2 | `set beam track_start = OTR2` | `factory.py:59` | **Silent.** Tracks from lattice start |
| 3 | Custom Tao commands | `factory.py:62-64` | **Silent.** Configuration lost |
| 4 | Element aliases | `factory.py:67-70` | Usually errors |
| 5 | `set beam comb_ds_save = 0.1` | `lume_bmad/model.py:70` | **Silent.** Outputs read at wrong position |
| 6 | `set beam saved_at = …` | `lume_bmad/model.py:95` | Usually errors |
| 7 | `set beam_init position_file = …` | `factory.py:117` | **Silent.** Tracks default beam, not the ML stage output |
| 8 | `track_type = beam` | `factory.py:118` | Reverts to single-particle; comb PVs vanish |

And most importantly:

**9. Every live control value** (quad and RF settings read from EPICS). If not re-applied,
the model sits at **design magnet settings while continuing to publish as though it reflects
the live machine**. Output remains smooth, physical, and plausible. This failure would be
extremely difficult to detect by inspection.

### 4.4 Configuration replay

Configuration is captured by observing commands as they are issued, rather than from a
hardcoded list — the setup is spread across three files and a hardcoded list would silently
miss anything unanticipated.

```python
def cmd(self, cmd, *args, **kwargs):
    result = super().cmd(cmd, *args, **kwargs)
    if not self._recycling:
        record_config_command(self._config_log, cmd)
    return result
```

- **Whitelist:** `set beam `, `set beam_init `, `set global track_type`, `set ele `
- **Excluded:** `set global lattice_calc_on` — a per-cycle toggle, not state. Recording it
  would replay a stale value and leave lattice calculation disabled after a respawn.
- **Deduplicated by assignment target,** so the newest value replaces the older one while
  keeping its original position. This bounds the log at ~120 entries regardless of runtime;
  without it the log would grow once per cycle and constitute a second leak.

On respawn: `close_subprocess()` → `init()` → replay the log with `lattice_calc_on = F` held
off (otherwise each of ~120 element writes triggers a full lattice calculation).

### 4.5 Fail-closed verification

The replay is not trusted; it is verified. After every respawn, state is read back out of Tao
and compared against a snapshot taken immediately before:

1. `track_type` unchanged
2. `track_start` unchanged
3. Comb length unchanged — encodes `comb_ds_save` and the lattice slice together
4. Supported-variable count unchanged
5. **Every writable control variable** matches the pre-respawn snapshot (`np.allclose`,
   rtol 1e-9)

On any mismatch: the specific variables are logged and the process calls `os._exit(90)`, so
Kubernetes restarts from a known-good state rather than publishing unverified results.
`os._exit` is used rather than an exception so it cannot be swallowed by the runner thread.

Stochastic read-only outputs (emittances, centroids) are **deliberately not compared** —
beam generation is unseeded and varies at sqrt(N), as already documented in `AGENTS.md`.
Comparing them would produce false alarms.

**Design rationale:** every silently-wrong outcome in §4.3 is converted into a loud restart.
A crash is recoverable in minutes; incorrect data published to control-room PVs may go
unnoticed for weeks.

### 4.6 Timing

Recycling runs at the **start** of `model.set()`, before evaluation, so every published value
comes from a fully restored and verified engine. `set()` runs on the runner's consumer
thread — the only thread that touches Tao — so no lock is required.

The **top-level** model is wrapped, not the Bmad stage: `StagedModel._set` (see
`todo/patches/lume_staged_model.patch.py:122`) only forwards to the Bmad stage when that
cycle's values target it (`if model_values:`), so wrapping the stage would give an unreliable
per-cycle hook.

---

## 5. Files, configuration, metrics

### Changed in this repository

| File | Change |
|---|---|
| `tao_recycle.py` | **New.** `RecyclableTao`, `install_recycling`, `capture_state`, `verify_state` |
| `tests/test_tao_recycle.py` | **New.** 21 unit tests, no pytao required |
| `run.py` | `pytao.Tao` substitution, install call, 4 metrics, 3 env vars |
| `Dockerfile` | `COPY tao_recycle.py .` |
| `kubernetes/overlays/dev/kustomization.yaml` | Three `TAO_RECYCLE_*` literals |
| `AGENTS.md` | Root cause 3, mechanism, metrics, exit code 90 |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `TAO_RECYCLE_ENABLED` | `true` | Master switch; `false` restores in-process `Tao` |
| `TAO_RECYCLE_RSS_GROWTH_MB` | `400` | Respawn once container memory grows this far past the post-startup baseline |
| `TAO_RECYCLE_MAX_CYCLES` | `0` (off) | Cycle-count fallback trigger; useful to force early validation |

The trigger reads the **cgroup total** (`/sys/fs/cgroup/memory.current`), not the parent's
RSS — the leak accumulates in the child, and the cgroup total is what the kernel OOM-kills on.

### Metrics

`va_tao_recycles_total`, `va_tao_recycle_duration_seconds`,
`va_tao_recycle_failures_total` (**must stay 0**), `va_tao_mem_after_recycle_bytes`.

```bash
kubectl exec <pod> -n virtual-accelerator -- \
  curl -s localhost:9090/metrics | grep '^va_tao_recycle'
kubectl logs deployment/virtual-accelerator -n virtual-accelerator | grep -E '\[recycle'
kubectl get pod <pod> -n virtual-accelerator \
  -o jsonpath='{.status.containerStatuses[0].lastState}'   # exitCode 90 = restore failed
```

---

## 6. Validation

### 6.1 Pre-deployment probe

Two pods, 40 minutes each, identical except for recycling. The control establishes that
subprocess mode alone changes nothing.

| | Control (no respawn) | Respawn every 250 cycles |
|---|---|---|
| Growth | **+237.2 MB/h** | **−6.4 MB/h** |
| Per cycle | +89.11 KB | −2.39 KB |
| Memory envelope | 175 → 328 MB monotonic | **27.3 MB sawtooth** (160.5–187.8 MB) |
| Parent RSS | 65.0 MB flat | 65.5 MB flat |
| Respawn cost | — | 2.4 s, 0.7% duty cycle |

The control leaking at exactly the in-process rate confirms the benefit comes from the
recycling, and that all leaked memory accumulates in the child while the parent stays flat.

### 6.2 Deployed service

Measured over 6h37m against live machine data:

| Measure | Result |
|---|---|
| Engine respawns | 1 |
| Verification failures | **0** |
| Respawn duration | **0.89 s** |
| Memory reclaimed | **~331 MB** (1066 → 735.7 MB) |
| Unplanned pod restarts | **0** |
| Simulation cycles | 32,839 at 0.71 s/cycle |
| Observed total growth | 62.6 MB/h (12.67 KB/cycle) |

Note the per-cycle leak in the real service (12.67 KB) is **14% of the probe's 89.31 KB**.
The probe sliced the lattice `OTR2:TD11` while the service uses `OTR2:OTR4` — a different
lattice extent means a different per-track allocation. The per-cycle `malloc_trim` and the
existing `lume_bmad` patches likely absorb some as well.

`SubprocessTao` handles the real staged model correctly, including passing `ParticleGroup`
beams between the ML and Bmad stages across the process boundary every cycle. This had been
flagged as the principal unvalidated risk.

---

## 7. The second leak (open)

Post-respawn memory settled at **735.7 MB, not the original 666.4 MB baseline** — 69 MB
remained. Direct per-process measurement:

| Process | Memory |
|---|---|
| Tao child | 194.5 MB — reclaimed by respawning |
| Parent (`run.py`) | 668.6 MB, up from 564.5 MB at startup |

That is **~16.1 MB/h in the parent, which respawning the child cannot address.** The
consequence is that the sawtooth *floor* ratchets upward at the parent's rate: memory is
bounded in the child, not overall.

`memray` identifies the responsible allocation site in the parent:

```
34044.1 KB  ('histogramdd', numpy/lib/_histograms_impl.py, 1067)
```

`histogramdd` builds the multi-dimensional histograms used to render simulated beam images
for the screen variables, one set per cycle. Totals grew from 33.8 MB to 56.1 MB across
successive scans, indicating retained references to images that should be released after
publishing.

This is a distinct class of problem from the first. The Fortran leak is memory that is
genuinely unreachable (`malloc` without `free`); this is memory that remains **reachable but
never released** — a retention bug. Both look identical from outside; the fixes are opposite.

**Why it is more tractable:** it is Python, it is visible to profiling tools, and the exact
file and line are known. The Fortran leak had to be found by elimination across three
40-minute experiments.

It is not among the five causes recorded in `docs/memory-leak-issue.md`.

---

## 8. Outstanding work

1. **Report upstream.** A complete bug report with a self-contained reproducer is prepared at
   `BMAD-LEAK-BUG-REPORT.md` (pytao working tree). This affects any long-running
   beam-tracking application, not only this project.
2. **Fix the parent-side retention leak** at `_histograms_impl.py:1067` — the reason the
   improvement is 4× rather than complete.
3. **Confirm output correctness across a respawn.** Verification covers engine *settings*;
   a comparison of published *values* either side of a respawn is still recommended, using
   the existing `scripts/capture_dt.py` and `scripts/compare_dt.py`. Expect sqrt(N) scatter
   on emittances but no systematic shift.
4. **Reduce `LOG_LEVEL` from `DEBUG` to `INFO`.** At 1.4 cycles/s the p4p debug output
   rotates away our own `[recycle]` diagnostics within hours.
5. **Investigate the ~43 bytes/command leak** (variant B) — negligible here, relevant to
   tight command loops.
6. **Remove the inert allocator settings.** `MALLOC_ARENA_MAX` and
   `MALLOC_MMAP_THRESHOLD_` are glibc tunables, but the Dockerfile preloads
   `libtcmalloc_minimal.so.4` (line 51), which ignores them. They are not causing the leak
   but provide false reassurance.

---

## 9. Reproducing the investigation

Artifacts in the pytao working tree (`~/Documents/pytao/pytao/`):

| File | Purpose |
|---|---|
| `leak_variants.py` | Three-variant reproducer (A/B/C), self-contained, no LUME wrappers |
| `leak-probe-pods.yaml` | Manifests for the three isolation pods |
| `leak-probe-results.txt` | Full 40-minute logs, all three variants |
| `leak_mitigation.py` | Recycling validation with no-recycle control |
| `leak-mitigation-pods.yaml` | Manifests for the two mitigation pods |
| `leak-mitigation-results.txt` | Full 40-minute logs, both pods |
| `BMAD-LEAK-BUG-REPORT.md` | Upstream bug report |

Method notes for anyone repeating this:

- Clear `LD_PRELOAD` so glibc actually returns freed memory; tcmalloc hoards it and masks
  whether `free()` was called at all.
- Call `malloc_trim(0)` before every RSS sample, so allocator retention cannot be mistaken
  for a leak.
- Read `[heap]` from `/proc/<pid>/smaps` separately from total RSS — that distinguishes
  native from Python allocations without needing `tracemalloc`.
- Change one variable at a time and include a validity check (here, comparing cycle times
  between variants A and C to confirm tracking really occurred in both).
- Run experiments in **separate** pods. Never exec a second Python+Tao process into a pod
  that is near its memory limit; it will OOM and destroy the evidence.
