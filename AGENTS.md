# Development History & Agent Notes

This document captures the development history, design decisions, and operational knowledge for the Virtual Accelerator Digital Twin project.

## Project Overview

The Digital Twin (DT) runs a staged physics model (ML surrogate + Bmad) inside a Kubernetes pod, reads live machine settings from the LCLS control system via EPICS, and serves predicted beam parameters as PVAccess PVs in real time.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Kubernetes Pod                                                  │
│                                                                  │
│  entrypoint.sh → run.py                                         │
│    ├─ Resolves EPICS hostnames to IPs                           │
│    ├─ Discovers libca.so for pyepics                            │
│    └─ Starts lume-pva Runner in snapshot mode                   │
│                                                                  │
│  Snapshot Loop (thread):                                         │
│    get live inputs (PVA/CA) → run model → serve outputs (PVA)   │
│    ~20s per cycle                                                │
│                                                                  │
│  Model: StagedModel                                              │
│    Stage 0: InjectorSurrogate (ML, PyTorch)                     │
│    Stage 1: LUMEBmadModel (physics, Tao/Bmad)                   │
│                                                                  │
│  EPICS Connectivity:                                             │
│    PVA inputs: epics-proxy:5169                                  │
│    CA inputs:  epics-proxy:5065                                  │
│    PVA outputs: localhost:5075                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Snapshot mode (not continuous)

lume-pva supports two remote modes:
- **continuous**: monitors remote PVs, re-evaluates on every change
- **snapshot**: fetches all inputs with `get()`, runs model, repeats

We use snapshot mode because PVA monitors don't work reliably through the socat proxy. The snapshot loop calls `runner.take_snapshot()` (public API) sequentially — no sleep needed since the model takes ~20s per cycle.

### Input filtering

Only `:BCTRL` and `:PDES` PVs are read from the machine. `:BDES` is excluded because it writes the same physical magnet field as `:BCTRL` — having both causes last-write-wins conflicts that break dispersion calculations.

The `track_type` and `name` variables are also excluded (internal model variables, not real PVs).

### Output PV naming

Output PVs are suffixed to distinguish them from real machine PVs:
- ML model outputs: `<PV>_CU_HXR_LUME_ML_DT`
- Physics model outputs: `<PV>_CU_HXR_LUME_PH_DT`

Only outputs (`mode='ro'`) get the suffix. Inputs are not served.

### Memory management

Two root causes of OOM identified and fixed. See `docs/thp-memory-leak.md` for full investigation.

**Root cause 1 — Transparent Huge Pages (THP):**
The k8s node runs `THP=always`. libtao Fortran heap allocations get promoted to 2 MB huge pages that the kernel never returns after free → monotonic RSS growth.
Fix: `prctl(PR_SET_THP_DISABLE)` called at `run.py` startup before any pytao import.

**Root cause 2 — `snapshot_loop` queue backlog:**
Without a sleep, `take_snapshot()` ran at ~40 Hz, flooding the runner queue with `p4p.Value` objects faster than consumed → 2.3 GB accumulation over hours.
Fix: sleep `update_rate` seconds between calls (default 0.1s = 10 Hz).

**Root cause 3 — libtao leaks ~89 KB of native heap per beam track:**
An upstream bmad bug, not fixable here. Measured over three 40-minute variants under glibc
malloc with `malloc_trim(0)` before every sample: 89.31 KB/cycle with beam tracking,
89.30 KB/cycle with tracking but no comb readback, 0.34 KB/cycle with no tracking. So beam
tracking is 99.6% of it. All growth is in the `brk` heap, it survives `malloc_trim`, and it
is perfectly linear with no plateau — a missing `free()`, not fragmentation or the allocator.
Affects `bmad 20260824.0` / `pytao 1.2.4`. Report: `BMAD-LEAK-BUG-REPORT.md` in the pytao
working tree.
Mitigation: run Tao in a `pytao.SubprocessTao` child and respawn it periodically — see
**Tao subprocess recycling** below.

Additional mitigations in `kubernetes/configmap.yaml`:
- `MALLOC_ARENA_MAX=1` — single glibc arena
- `MALLOC_MMAP_THRESHOLD_=131072` — large allocs via mmap, returned to OS on free

Note: both `MALLOC_*` settings are glibc tunables and are currently **inert**, because the
Dockerfile preloads `libtcmalloc_minimal.so.4` (`LD_PRELOAD`, Dockerfile line 51) and tcmalloc
ignores them. Left in place for now since removing the preload has its own blast radius.

### Tao subprocess recycling

`tao_recycle.py` bounds the libtao leak. Validated in-cluster: memory held a **27.3 MB
sawtooth** (160.5–187.8 MB) instead of growing 237 MB/h, at **2.4s per respawn, 0.7% duty
cycle**. A non-recycled `SubprocessTao` control leaked 89.11 KB/cycle — identical to
in-process — so the benefit comes from the recycling, not from subprocess mode itself.

How it hooks in: `virtual_accelerator/bmad/factory.py` does a *function-local*
`from pytao import Tao`, so the name resolves off the `pytao` module at call time. `run.py`
sets `pytao.Tao = RecyclableTao` before building the model. No patched copy of `factory.py`
is needed, and the four existing `todo/patches/` files are untouched.

`RecyclableTao` records configuration commands as they are issued (prefixes `set beam `,
`set beam_init `, `set global track_type`, `set ele `; `set global lattice_calc_on` excluded
as a per-cycle toggle), deduped by assignment target so the log stays bounded at ~120 entries.
On recycle it calls `close_subprocess()`, `init()`, then replays that log with
`lattice_calc_on = F` held off.

**Fail-closed verification.** A respawned child returns at the *design* lattice. If
restoration were incomplete the service would publish smooth, physical, wrong numbers —
design magnets presented as live values. So after every respawn `verify_state()` re-reads
`track_type`, `track_start`, comb length, the supported-variable count, and **every writable
control variable** and compares against a pre-recycle snapshot. Stochastic read-only outputs
(emittances, centroids) are deliberately *not* compared — beam generation is unseeded and
varies at sqrt(N). Any mismatch logs the specific variables and calls `os._exit(90)` so
Kubernetes restarts from known-good state.

| Env var | Default | Meaning |
|---------|---------|---------|
| `BMAD_RADIATION_FLUCTUATIONS` | unset | `off` disables radiation fluctuations, which gate the libtao leak (see below). Unset leaves the lattice value alone |
| `TAO_RECYCLE_ENABLED` | `true` | Master switch; `false` restores in-process `Tao` |
| `TAO_RECYCLE_RSS_GROWTH_MB` | `400` | Respawn once container memory grows this far past the post-startup baseline |
| `TAO_RECYCLE_MAX_CYCLES` | `0` (off) | Cycle-count fallback trigger |

The trigger reads the **cgroup** total (`/sys/fs/cgroup/memory.current`), not the parent's
RSS — the leak lives in the child, and the cgroup total is what the kernel OOM-kills on.

### Radiation as a candidate fix at source

Upstream root cause is [bmad-ecosystem#2177](https://github.com/bmad-sim/bmad-ecosystem/issues/2177):
`transfer_ele(..., nullify_pointers=.true.)` drops the `rad_map` pointer without deallocating,
then `radiation_map_setup` reallocates — orphaning one 2048-byte `rad_map_ele_struct` **per
lattice element per beam re-track**. That model reproduces our numbers: 89.31 KB/re-track on
the `OTR2:TD11` probe (~45 elements) and 12.67 KB/re-track on the `OTR2:OTR4` service
(~6 elements).

The leak requires **radiation AND comb saving**; disabling either removes it. We need comb
saving (it is how outputs are read), but `cu_hxr/tao.init:29` sets
`bmad_com%radiation_fluctuations_on = T` (damping is already `F`), so
`BMAD_RADIATION_FLUCTUATIONS=off` is a candidate fix at source rather than containment.

**This is a reversible diagnostic, not a permanent change** — radiation is expected to be
needed again, so keep recycling enabled regardless. Note the override **changes published
physics** (energy spread / emittance), so it is a deliberate temporary configuration.

`set bmad_com ` is in `_CONFIG_PREFIXES` for a reason: if the override were not replayed after
a respawn, the first recycle would silently restore the lattice default and the leak would
return. Never ship a `bmad_com` override without that whitelist entry.

Metrics: `va_tao_recycles_total`, `va_tao_recycle_duration_seconds`,
`va_tao_recycle_failures_total` (must stay 0), `va_tao_mem_after_recycle_bytes`.

```bash
kubectl logs deployment/virtual-accelerator -n virtual-accelerator | grep -E '\[recycle\]|\[recycle-assert\]'
kubectl get pod <pod> -n virtual-accelerator -o jsonpath='{.status.containerStatuses[0].lastState}'  # exit 90 = restore failed
```

Unit tests: `tests/test_tao_recycle.py` (21 tests, no pytao needed — covers the replay log,
model discovery, and every verification failure mode).

**Unvalidated risks:** whether `SubprocessTao` marshals `ParticleGroup` efficiently across
the process boundary (`initial_particles`/`final_particles` cross it every cycle), and
per-cycle overhead from ~116 variable reads now being IPC. Both surface immediately rather
than silently — compare `va_snapshot_duration_seconds` before and after. If either is
prohibitive, the fallback is an RSS-threshold graceful restart; the ~2-week runway at
~16 MB/h makes that viable.

Memory diagnostics: `[mem]` and `[thp]` log lines to stderr every `MEM_LOG_INTERVAL_S` seconds.
Monitor: `kubectl logs deployment/virtual-accelerator | grep -E '^\[thp\]|\[mem\]'`

### EPICS connectivity

The pod uses `pvua` which auto-discovers providers (tries PVA first, falls back to CA). Both protocols are configured through a socat proxy service (`epics-proxy.epics-socat-proxy`).

`PYEPICS_LIBCA` is set in the Dockerfile ENV and also discovered dynamically in the entrypoint via `ldd $(which caget)`.

## Issues Encountered & Resolved

| Issue | Cause | Fix |
|-------|-------|-----|
| 1970 timestamps | `time.monotonic()` in lume-pva | Updated to latest lume-pva |
| Private API (`_enqueue`) | Custom polling loop | Replaced with `take_snapshot()` |
| Suffix on inputs | Mode check included `rw` | Changed to `ro` only |
| ACCL PVs unreachable | CA-only PVs, no CA config | Added CA env vars + epics-base |
| `track_type` validation | Internal var marked remote | Excluded from remote mode |
| BDES/BCTRL conflict | Both write same magnet field | Excluded `:BDES` from remote |
| OOM after 2 days (THP) | Fortran heap promoted to 2 MB huge pages never freed | `prctl(PR_SET_THP_DISABLE)` at startup |
| OOM after 2 days (queue) | `snapshot_loop` at 40 Hz flooded queue with p4p.Value objects | Throttle to `update_rate` (0.1s) |
| OOM from libtao (~89 KB/beam track) | Upstream bmad missing `free()` in beam tracking | Recycle a `SubprocessTao` worker with fail-closed state verification (`tao_recycle.py`) |
| `name` PV not real | Model metadata variable | Removed from config |

## Deploying a New Model

The Docker image supports all available models. No rebuild is needed — just create a new Kubernetes overlay.

### Supported models

| Model name | Description |
|------------|-------------|
| `cu_hxr_bmad` | CU HXR physics only (Bmad) |
| `cu_hxr_staged` | CU HXR staged (ML injector + Bmad) |
| `facet_bmad` | FACET-II physics only (Bmad) |
| `facet_staged` | FACET-II staged (ML injector + Bmad) |

### Steps to deploy a new model

1. **Create the overlay directory:**
   ```bash
   mkdir -p kubernetes/overlays/<model-name>
   ```

2. **Create `kustomization.yaml`:**
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources:
     - ../../base
   generatorOptions:
     disableNameSuffixHash: true
   configMapGenerator:
     - name: va-config
       behavior: create
       literals:
         - MODEL=<model_name>              # e.g. facet_staged
         - REMOTE_INPUTS=true
         - PV_SUFFIX_ML=<ml_suffix>        # e.g. _FACET_LUME_ML_DT
         - PV_SUFFIX_PH=<ph_suffix>        # e.g. _FACET_LUME_PH_DT
         - PV_RENAMES={}                   # JSON dict of output PV renames
         - END_ELEMENT=<element>           # e.g. ENDM
         - N_PARTICLES=10000
         - LOG_LEVEL=INFO
         - LCLS_LATTICE=/opt/lcls-lattice
         - KMP_DUPLICATE_LIB_OK=TRUE
         - OMP_NUM_THREADS=2
         - MKL_NUM_THREADS=2
         - OPENBLAS_NUM_THREADS=2
         - TORCH_NUM_THREADS=2
         - EPICS_PVA_AUTO_ADDR_LIST=NO
         - EPICS_PVA_BROADCAST_PORT=0
         - EPICS_PVA_NAME_SERVERS=epics-proxy.epics-socat-proxy:5169
         - EPICS_CA_AUTO_ADDR_LIST=NO
         - EPICS_CA_ADDR_LIST=
         - EPICS_CA_NAME_SERVERS=epics-proxy.epics-socat-proxy:5065
   ```

3. **Deploy:**
   ```bash
   kubectl apply -k kubernetes/overlays/<model-name>
   ```

4. **Verify:**
   ```bash
   kubectl logs -f deployment/virtual-accelerator -n virtual-accelerator
   # Wait for "PVA server listening on port: 5075"
   # Then test:
   kubectl exec <pod> -- env EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" \
     python -c "from p4p.client.thread import Context; ctx = Context('pva'); print(ctx.get('<output-pv>', timeout=30))"
   ```

### Naming convention for new models

Follow the pattern: `<PV>_<BEAMLINE>_LUME_<MODEL_TYPE>_DT`

| Component | Examples |
|-----------|----------|
| Beamline | `CU_HXR`, `FACET`, `CU_SXR` |
| Model type | `ML` (surrogate), `PH` (physics/Bmad) |
| Suffix | Always ends with `_DT` (Digital Twin) |

## Validation

Two scripts in `scripts/` support output validation:

1. **`capture_dt.py`** — runs inside the pod, captures input+output snapshots to JSON
2. **`validate_dt.py`** — runs on a dev server, replays inputs through a local model and compares

```bash
# Step 1: Capture from pod
kubectl cp scripts/capture_dt.py <pod>:/app/scripts/capture_dt.py
kubectl exec <pod> -- python scripts/capture_dt.py --duration 60
kubectl cp <pod>:/tmp/dt_capture.json ./dt_capture.json

# Step 2: Validate on dev server
scp dt_capture.json dev-srv09:~/
python scripts/validate_dt.py dt_capture.json
```

### Known comparison caveats

- **Stochastic outputs** (beam centroids, emittances): differ at sqrt(N) noise level due to unseeded RNG in beam generation
- **EPICS flattens N-D arrays**: images and Twiss arrays come back as 1-D waveforms — comparison script handles this via `np.ravel()`
- **Near-zero values**: use `np.isclose(rtol, atol)` not pure relative error

## CI/CD

GitHub Actions workflow (`.github/workflows/build-container.yml`):
1. Builds the Docker image
2. Runs smoke test (boots container, verifies PVs come up)
3. Pushes to `ghcr.io/<org>/virtual-accelerator-digital-twin:latest`

Manual trigger with "no-cache" checkbox available for forcing fresh dependency installs.

## Prometheus Metrics

`run.py` exposes a Prometheus `/metrics` endpoint on port `METRICS_PORT` (default 9090) using `prometheus_client`.

| Metric | Type | Description |
|--------|------|-------------|
| `va_rss_bytes` | Gauge | Process RSS |
| `va_anon_huge_pages_bytes` | Gauge | AnonHugePages — must stay 0 after THP fix |
| `va_thp_disabled` | Gauge | 1 if THP successfully disabled |
| `va_snapshot_cycles_total` | Counter | Total `take_snapshot()` calls |
| `va_snapshot_duration_seconds` | Histogram | Time per snapshot cycle |
| `va_gc_collects_total` | Counter | GC+malloc_trim invocations |
| `va_pv_posts_total{pv=...}` | Counter | SharedPV post() calls per PV |

Kubernetes `ServiceMonitor` in `kubernetes/base/servicemonitor.yaml` scrapes every 30s.

Monitor locally:
```bash
kubectl port-forward svc/virtual-accelerator 9090:9090 -n virtual-accelerator
curl http://localhost:9090/metrics | grep "^va_"
```

## Local Development (devcontainer)

`.devcontainer/` provides a VSCode devcontainer with two services:
- **devenv** — full `base` image stage with `/workspace` volume-mounted; run `run.py` live
- **mock-ioc** — serves the 16 real snapshot PVs via PVA on port 5076

```bash
# Open in VSCode
# Ctrl+Shift+P → "Dev Containers: Reopen in Container"

# Or via CLI
devcontainer up --workspace-folder .
```

### EPICS tools in devcontainer

```bash
source /workspace/scripts/dev_epics_env.sh  # configures pvget/pvput/pvmon

pvget QUAD:IN20:631:BCTRL      # read from mock-ioc
pvput QUAD:IN20:631:BCTRL 7.5  # write to mock-ioc
pvmon SOLN:IN20:121:BCTRL      # monitor updates
```

### Launch configurations (VSCode F5)

| Config | Purpose |
|--------|---------|
| Run VA (cu_hxr_staged, remote inputs) | Full VA reading from mock-ioc |
| Memory leak test — model set/get (FakeModel) | Pure numpy, no pytao |
| Memory leak test — model set/get (real cu_hxr_staged) | Real pytao/libtao heap |
| Memory leak test — take_snapshot (real model + mock-ioc) | Tests the fixed queue-backlog path |

### Memory leak test script

`scripts/model_loop_memtest.py` — standalone, no pytest, runs for hours:
```bash
# FakeModel (local, no pytao)
python scripts/model_loop_memtest.py --duration 3600 --log-interval 60

# Real model set/get
python scripts/model_loop_memtest.py --model cu_hxr_staged --duration 3600

# Snapshot mode (tests take_snapshot() path via mock-ioc)
python scripts/model_loop_memtest.py --snapshot-mode --model cu_hxr_staged \
  --snapshot-interval 0.1 --duration 3600
```

Output CSV: `elapsed_s, rss_mb, anon_huge_pages_kb, py_heap_mb, total_cycles, cycles_per_s, rss_delta_mb`

## Testing

```bash
# Local tests (no docker, no pytao)
pip install -e ".[test]"
pytest tests/ -m "not integration"

# Docker integration tests (VA + pv-client)
docker compose -f docker-compose.integration.yml run --rm pv-client
```

## Dependencies (pinned in Dockerfile)

| Package | Source | Notes |
|---------|--------|-------|
| virtual-accelerator | GitHub (pinned commit) | Model definitions + surrogate extras |
| lume-pva | GitHub (latest main) | PV server framework |
| lume-bmad | GitHub (latest main) | Bmad model wrapper |
| lume-torch | GitHub (latest main) | Torch variable types for surrogate |
| bmad, pytao | conda-forge | Lattice physics engine |
| epics-base, pvxs=1.5.2 | conda-forge | EPICS CA/PVA libraries |
| torch | PyPI (CPU only) | ML surrogate inference |
| prometheus-client | PyPI | Prometheus metrics HTTP server |
| lcls-lattice | GitHub (pinned commit) | Lattice definition files |
