# Virtual Accelerator Digital Twin

Containerized deployment of the LCLS virtual accelerator as an EPICS PVAccess server. Reads live machine settings, runs a staged physics model (ML surrogate + Bmad), and serves predicted beam parameters in real time.

## Quick Start

### Docker (local development)

```bash
docker build -t va-digital-twin .
docker run --rm -p 5075:5075 va-digital-twin
```

Query PVs:
```bash
EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" pvget BPMS:IN20:581:TMIT
```

### Kubernetes (Digital Twin with live inputs)

```bash
kubectl apply -k kubernetes/overlays/cu-hxr-staged/
```

Verify:
```bash
kubectl exec <pod> -- env EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" \
  pvget OTRS:IN20:571:XRMS_CU_HXR_LUME_ML_DT
```

## Supported Models

| Model | Description | Command |
|-------|-------------|---------|
| `cu_hxr_bmad` | CU HXR physics only (Bmad, OTR2→end) | `MODEL=cu_hxr_bmad` |
| `cu_hxr_staged` | CU HXR staged (ML injector + Bmad) | `MODEL=cu_hxr_staged` |
| `facet_bmad` | FACET-II physics only | `MODEL=facet_bmad` |
| `facet_staged` | FACET-II staged (ML + Bmad) | `MODEL=facet_staged` |

## Deploying a New Model

The image is model-agnostic. No rebuild needed — just create a Kubernetes overlay:

```bash
mkdir -p kubernetes/overlays/<model-name>
```

Create `kustomization.yaml` with model-specific env vars (see `kubernetes/overlays/cu-hxr-staged/` as a template), then:

```bash
kubectl apply -k kubernetes/overlays/<model-name>
```

See [AGENTS.md](AGENTS.md) for full deployment steps and naming conventions.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Kubernetes Pod                                              │
│                                                              │
│  Snapshot Loop (every ~20s):                                 │
│    Read live inputs (PVA/CA via proxy)                       │
│      → Run staged model (ML surrogate + Bmad physics)       │
│        → Serve output PVs (PVAccess on port 5075)           │
│                                                              │
│  Inputs: :BCTRL and :PDES only (not :BDES)                  │
│  Outputs: suffixed _CU_HXR_LUME_ML_DT / _PH_DT             │
└─────────────────────────────────────────────────────────────┘
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL` | `cu_hxr_bmad` | Model to run |
| `END_ELEMENT` | `OTR4` | Lattice end element |
| `N_PARTICLES` | `10000` | Particles for beam simulation |
| `REMOTE_INPUTS` | `false` | Read live inputs from accelerator |
| `PV_SUFFIX_ML` | (none) | Suffix for ML outputs (e.g. `_CU_HXR_LUME_ML_DT`) |
| `PV_SUFFIX_PH` | (none) | Suffix for physics outputs (e.g. `_CU_HXR_LUME_PH_DT`) |
| `PV_SUFFIX` | (none) | Single suffix for all outputs (if not using ML/PH split) |
| `PV_RENAMES` | `{}` | JSON dict of output PV name remapping |
| `LOG_LEVEL` | `INFO` | Logging level |

## PV Naming Convention

Output PVs follow: `<PV>_<BEAMLINE>_LUME_<MODEL_TYPE>_DT`

- `CU_HXR` — beamline
- `LUME` — project identifier  
- `ML` / `PH` — model type (ML surrogate or physics)
- `DT` — Digital Twin

Example: `OTRS:IN20:571:XRMS_CU_HXR_LUME_ML_DT`

## Input Filtering

Only `:BCTRL` and `:PDES` PVs are read from the live machine. `:BDES` is excluded to avoid conflicting writes to the same physical magnet field.

## Validation

Scripts for comparing DT outputs against a local model run:

```bash
# Capture from pod
kubectl exec <pod> -- python scripts/capture_dt.py --duration 60
kubectl cp <pod>:/tmp/dt_capture.json ./dt_capture.json

# Validate on dev server
python scripts/validate_dt.py dt_capture.json
```

## CI/CD

On push to `main`, GitHub Actions builds, smoke-tests, and pushes to:
```
ghcr.io/<org>/virtual-accelerator-digital-twin:latest
```

Manual trigger with "no-cache" option available for forcing fresh dependency installs.

## Directory Structure

```
├── run.py                          # Container entry point
├── entrypoint.sh                   # EPICS env setup + launch
├── Dockerfile                      # Image definition
├── scripts/
│   ├── smoke_test.py               # CI smoke test
│   ├── capture_dt.py               # Capture DT inputs/outputs
│   └── validate_dt.py              # Validate against local model
├── kubernetes/
│   ├── base/                       # Shared deployment template
│   └── overlays/
│       └── cu-hxr-staged/          # CU HXR staged model (prod)
└── .github/workflows/
    └── build-container.yml         # CI/CD pipeline
```

## Development Notes

See [AGENTS.md](AGENTS.md) for full development history, design decisions, issues encountered, and operational knowledge.
