# Virtual Accelerator Digital Twin

Containerized deployment of the LCLS CU HXR virtual accelerator as an EPICS PVAccess server.

Runs the Bmad/Tao physics model (OTR2 → OTR4) and serves live PVs via `lume-pva`.

## Quick Start

### Build

```bash
docker build -t va-digital-twin .
```

### Demo
```bash
docker build -t va-digital-twin .
docker run --rm --name va-test --network host va-digital-twin
```

and then in another terminal 
```bash
docker exec va-test python -c "
from p4p.client.thread import Context
ctx = Context('pva')
val = ctx.get('BPMS:IN20:581:TMIT', timeout=5)
print('BPMS:IN20:581:TMIT =', val)
"
```

### Run locally (auto-selects a free port)

```bash
./scripts/launch.sh
```

This finds an open port starting from 5075, prints it, and starts the container.
Then in a **second terminal**:

```bash
source scripts/epics_env.sh <port>   # use the port printed by launch.sh
pvget BPMS:IN20:581:TMIT
```

Options:
```bash
./scripts/launch.sh --name my-va      # set container name
./scripts/launch.sh --detach          # run in background
./scripts/launch.sh --image ghcr.io/org/va-digital-twin:latest
```

### Run on a specific port (manual)

Only the TCP port needs to be mapped — clients connect via `EPICS_PVA_NAME_SERVERS` (TCP-direct),
so UDP broadcast is not needed.

```bash
# Docker — via argument
docker run --rm -p 5175:5175 va-digital-twin 5175

# Docker — via environment variable
docker run --rm -p 5175:5175 -e PVA_PORT=5175 va-digital-twin

# Apptainer (host networking) — pass port as argument
apptainer run va-digital-twin.sif 5175
```

Then in a **second terminal**, configure the client to connect to that port:

```bash
source scripts/epics_env.sh 5175
pvget BPMS:IN20:581:TMIT
```

### Multiple simultaneous instances

Each `launch.sh` invocation automatically picks a different free port:

```bash
# Terminal 1
./scripts/launch.sh --name va-A

# Terminal 2
./scripts/launch.sh --name va-B

# Terminal 3 — query instance A (use port printed by first launch)
source scripts/epics_env.sh 5075
pvget BPMS:IN20:581:TMIT

# Switch to instance B (use port printed by second launch)
source scripts/epics_env.sh 5077
pvget BPMS:IN20:581:TMIT
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

## Server Deployment (Apptainer)

To run on a shared server (e.g. SLAC `dev-srv09`) via Apptainer — including how to
update the image and automate updates — see [`docs/server-deployment.md`](docs/server-deployment.md).

## CI/CD

On push to `main`, the GitHub Actions workflow **builds the image, smoke-tests it
(boots the container and verifies PVs are served over PVA), and only then pushes**
to:
```
ghcr.io/<org>/virtual-accelerator-digital-twin:latest
```
A build whose smoke test fails is never published. See `scripts/smoke_test.py` and
[the CI gate section](docs/server-deployment.md#4-how-images-are-tested-before-publish-ci-gate).

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
