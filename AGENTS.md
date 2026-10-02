# Development History & Agent Notes

This document captures the architecture, design decisions, and operational knowledge for the Virtual Accelerator Digital Twin project.

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

### Snapshot mode (default) vs continuous

lume-pva supports two remote modes:
- **snapshot**: `runner.take_snapshot()` pvget()s every input each cycle. Coherent (all inputs latched together), slower.
- **continuous**: inputs subscribed via monitors at startup, updates arrive async. ~10x faster per cycle, inputs arrive independently.

Snapshot is the default (`REMOTE_MODEL_MODE=snapshot`) for whole-beamline sims. Continuous is used for the rmat overlay where strict input coherency isn't required.

### Input filtering

Only `:BCTRL` and `:PDES` PVs are read from the machine. `:BDES` is excluded because it writes the same physical magnet field as `:BCTRL` — having both causes last-write-wins conflicts that break dispersion calculations.

The `track_type` and `name` variables are also excluded (internal model variables, not real PVs).

### Output PV naming

Output PVs are suffixed to distinguish them from real machine PVs. The suffix is set per-overlay via `PV_SUFFIX` (e.g. `_LUME_SM1` for cu_hxr_staged, `_LUME_PH1` for bmad, `_LUME_PH2` for rmat). Only outputs (`mode='ro'`) get the suffix.

### Radiation fluctuations override

`BMAD_RADIATION_FLUCTUATIONS` (unset by default) overrides the lattice's `radiation_fluctuations_on` setting to `on`/`off`. cu_hxr's `tao.init` sets it `T` by default; models that don't need stochastic radiation noise (e.g. rmat's linear-optics calc) set it `off`. Unset leaves the lattice value untouched.

### EPICS connectivity

The pod uses `pvua` which auto-discovers providers (tries PVA first, falls back to CA). Both protocols are configured through a socat proxy service (`epics-proxy.epics-socat-proxy`).

`PYEPICS_LIBCA` is set in the Dockerfile ENV and also discovered dynamically in the entrypoint via `ldd $(which caget)`.

## Deploying a New Model

The Docker image supports all available models. No rebuild is needed — just create a new Kubernetes overlay.

### Supported models

| Model name | Description |
|------------|-------------|
| `cu_hxr_bmad` | CU HXR physics only (Bmad) |
| `cu_hxr_staged` | CU HXR staged (ML injector + Bmad) |
| `cu_hxr_rmat` | CU HXR transfer-matrix (linear optics, WS-WS range) |
| `facet_bmad` | FACET-II physics only (Bmad) |
| `facet_staged` | FACET-II staged (ML injector + Bmad) |

### Steps to deploy a new model

1. **Create the overlay directory:**
   ```bash
   mkdir -p kubernetes/overlays/<env>/<model-name>
   ```

2. **Create `kustomization.yaml`:**
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   namespace: virtual-accelerator
   resources:
     - ../../../base
   generatorOptions:
     disableNameSuffixHash: true
   configMapGenerator:
     - name: va-config
       behavior: create
       literals:
         - MODEL=<model_name>
         - REMOTE_INPUTS=true
         - PV_SUFFIX=<suffix>              # e.g. _LUME_SM1
         - PV_RENAMES={}                   # JSON dict of output PV renames
         - END_ELEMENT=<element>           # e.g. OTR4
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
   kubectl apply -k kubernetes/overlays/<env>/<model-name>
   ```

4. **Verify:**
   ```bash
   kubectl logs -f deployment/virtual-accelerator -n virtual-accelerator
   # Wait for "PVA server listening on port: 5075"
   kubectl exec <pod> -- env EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" \
     python -c "from p4p.client.thread import Context; ctx = Context('pva'); print(ctx.get('<output-pv>', timeout=30))"
   ```

## Validation

Two scripts in `scripts/` support output validation:

1. **`capture_dt.py`** — runs inside the pod, captures input+output snapshots to JSON
2. **`validate_dt.py`** — runs on a dev server, replays inputs through a local model and compares

```bash
kubectl cp scripts/capture_dt.py <pod>:/app/scripts/capture_dt.py
kubectl exec <pod> -- python scripts/capture_dt.py --duration 60
kubectl cp <pod>:/tmp/dt_capture.json ./dt_capture.json
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

`run.py` exposes a Prometheus `/metrics` endpoint on port `METRICS_PORT` (default 9090).

| Metric | Type | Description |
|--------|------|-------------|
| `va_thp_disabled` | Gauge | 1 if THP successfully disabled |
| `va_runner_queue_size` | Gauge | Current runner queue depth |
| `va_snapshot_cycles_total` | Counter | Total `take_snapshot()` calls |
| `va_snapshot_duration_seconds` | Histogram | Time per snapshot cycle |
| `va_snapshot_queue_wait_seconds` | Histogram | Time waiting for queue to drain |
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
# Open in VSCode: Ctrl+Shift+P → "Dev Containers: Reopen in Container"
# Or via CLI
devcontainer up --workspace-folder .
```

### EPICS tools in devcontainer

```bash
source /workspace/scripts/dev_epics_env.sh
pvget QUAD:IN20:631:BCTRL
pvput QUAD:IN20:631:BCTRL 7.5
pvmon SOLN:IN20:121:BCTRL
```

## Testing

```bash
pip install -e ".[test]"
pytest tests/ -m "not integration"

# Docker integration tests
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
