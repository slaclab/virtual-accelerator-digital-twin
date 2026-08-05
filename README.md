# Virtual Accelerator Digital Twin

Containerized deployment of the LCLS CU HXR virtual accelerator as an EPICS PVAccess server.

Runs the Bmad/Tao physics model (OTR2 → OTR4) and serves live PVs via `lume-pva`.

## Running on dev-srv09

### Terminal 1 — Start instance A

```bash
git clone https://github.com/slaclab/virtual-accelerator-digital-twin.git
cd virtual-accelerator-digital-twin/scripts
./launch.sh --runtime apptainer --image /sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif
```

Wait until the PVA server starts up (you will see some WARNINGs — that's normal).
The script will print something like:

```
==> Free port found: 5077

To connect from another terminal:
  source scripts/epics_env.sh 5077
```

Note the port number.

### Terminal 2 — Start instance B (port isolation)

```bash
cd virtual-accelerator-digital-twin/scripts
./launch.sh --runtime apptainer --image /sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif
```

This will automatically pick a different free port (e.g. 5079).

### Terminal 3 — Query PVs

```bash
cd virtual-accelerator-digital-twin
source scripts/epics_env.sh 5077
pvget BPMS:IN20:581:TMIT
```

To switch to instance B, re-source with its port:

```bash
source scripts/epics_env.sh 5079
pvget BPMS:IN20:581:TMIT
```

## Docker (local development)

### Build

```bash
docker build -t va-digital-twin .
```

### Run (auto-selects a free port)

```bash
./scripts/launch.sh
```

Then in a second terminal:

```bash
source scripts/epics_env.sh <port>   # use the port printed by launch.sh
pvget BPMS:IN20:581:TMIT
```

### Run on a specific port

```bash
docker run --rm -p 5175:5175 va-digital-twin 5175
```

### Override model parameters

```bash
docker run --rm \
  -e MODEL=cu_hxr_bmad \
  -e END_ELEMENT=OTR4 \
  -e N_PARTICLES=10000 \
  -e LOG_LEVEL=DEBUG \
  va-digital-twin
```

## Kubernetes Deployment (Digital Twin)

The `kubernetes/` directory uses kustomize with a shared base and per-model overlays.

### Deploy the CU HXR staged Digital Twin

```bash
kubectl apply -k kubernetes/overlays/cu-hxr-staged/
```

This creates:
- Namespace `virtual-accelerator`
- ConfigMap with DT-specific parameters (`REMOTE_INPUTS=true`, `PV_SUFFIX=_LUME`)
- Single-replica Deployment with `hostNetwork: true` (for EPICS accessibility)
- Service exposing PVAccess ports (5075/tcp, 5076/udp)

The DT reads live inputs from production EPICS and writes model outputs with `_LUME` appended to all PV names.

### Verify from lcls-srv02

```bash
export EPICS_PVA_ADDR_LIST="<node-ip>"
export EPICS_PVA_AUTO_ADDR_LIST=NO
pvget BPMS:IN20:581:TMIT_LUME
```

### Directory structure

```
kubernetes/
  base/                         # Shared deployment template
  overlays/
    cu-hxr-staged/              # CU HXR staged model (prod DT)
    facet-staged/               # (future) FACET staged model
```

### Adding a new model

Copy `kubernetes/overlays/cu-hxr-staged/` and change `MODEL` in the kustomization literals.

### Environment variables for DT mode

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOTE_INPUTS` | `false` | Read inputs from prod EPICS instead of serving them |
| `PV_SUFFIX` | (none) | Suffix appended to all served PV names (e.g. `_LUME`) |

These are in addition to the standard `MODEL`, `END_ELEMENT`, `N_PARTICLES`, `LOG_LEVEL` variables.

### Legacy VA deployment example

The previous standalone VA deployment files are preserved in `examples/kubernetes-va/` for reference.

## Server Deployment (Apptainer)

For full details on image updates, automation, and CI — see [`docs/server-deployment.md`](docs/server-deployment.md).

## CI/CD

On push to `main`, GitHub Actions **builds, smoke-tests, and pushes** to:
```
ghcr.io/<org>/virtual-accelerator-digital-twin:latest
```
A build whose smoke test fails is never published. See `scripts/smoke_test.py`.

## Architecture

```
┌──────────────────────────────────────────┐
│  Container                               │
│                                          │
│  run.py                                  │
│    └─ virtual_accelerator.models.runners │
│         └─ cu_hxr_bmad model (Tao/Bmad)  │
│              └─ lume-pva Runner           │
│                   └─ EPICS PVAccess       │
└──────────────────────────────────────────┘
```

The container initializes a Bmad lattice simulation using PyTao, then serves all beamline PVs (magnet settings, beam parameters) over EPICS PVAccess protocol. Clients can read/write PVs using standard EPICS tools (`pvget`, `pvput`, `p4p`).
