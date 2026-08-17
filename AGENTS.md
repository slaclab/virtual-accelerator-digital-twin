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

The model creates new ParticleGroup objects (10k particles) every cycle. To prevent OOM:
- `torch.no_grad()` wraps the snapshot loop (prevents computation graph accumulation)
- `gc.collect()` runs every 50 cycles (~17 min)
- Upstream fix needed: `beam_output.py` should reuse Generator instances

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
| OOM after 2 days | ParticleGroup/tensor accumulation | `torch.no_grad()` + `gc.collect()` |
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

## Dependencies (pinned in Dockerfile)

| Package | Source | Notes |
|---------|--------|-------|
| virtual-accelerator | GitHub (pinned commit) | The model definitions |
| lume-pva | GitHub (latest main) | PV server framework |
| lume-bmad | GitHub (latest main) | Bmad model wrapper |
| bmad, pytao | conda-forge | Lattice physics engine |
| epics-base, pvxs | conda-forge | EPICS CA/PVA libraries |
| torch | PyPI (CPU only) | ML surrogate inference |
| lcls-lattice | GitHub (pinned commit) | Lattice definition files |
