# Virtual Accelerator Digital Twin

Containerized deployment of the LCLS CU HXR virtual accelerator as an EPICS PVAccess server.

Runs the Bmad/Tao physics model (OTR2 → OTR4) and serves live PVs via `lume-pva`.

## Quick Start

### Build

```bash
docker build -t va-digital-twin .
```

### Run locally

```bash
docker run --rm -p 5075:5075 -p 5076:5076/udp va-digital-twin
```

### Override model parameters via environment

```bash
docker run --rm \
  -e MODEL=cu_hxr_bmad \
  -e END_ELEMENT=OTR4 \
  -e N_PARTICLES=10000 \
  -e LOG_LEVEL=DEBUG \
  va-digital-twin
```

## Kubernetes Deployment

```bash
kubectl apply -k kubernetes/
```

This creates:
- Namespace `virtual-accelerator`
- ConfigMap with model parameters and thread-pinning env vars
- Single-replica Deployment running the virtual accelerator
- Service exposing PVAccess ports (5075/tcp, 5076/udp)

### Customizing

Edit `kubernetes/configmap.yaml` to change model parameters (MODEL, END_ELEMENT, etc.) without rebuilding the image.

## CI/CD

On push to `main`, the GitHub Actions workflow builds and pushes the image to:
```
ghcr.io/<org>/virtual-accelerator-digital-twin:latest
```

## Architecture

```
┌──────────────────────────────────────────┐
│  Container                               │
│                                          │
│  run.py                                  │
│    └─ virtual_accelerator.models.runners │
│         └─ cu_hxr_bmad model (Tao/Bmad)  │
│              └─ lume-pva Runner           │
│                   └─ EPICS PVAccess :5075 │
└──────────────────────────────────────────┘
```

The container initializes a Bmad lattice simulation using PyTao, then serves all beamline PVs (magnet settings, beam parameters) over EPICS PVAccess protocol. Clients can read/write PVs using standard EPICS tools (`pvget`, `pvput`, `p4p`).
