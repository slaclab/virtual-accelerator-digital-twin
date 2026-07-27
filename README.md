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
docker compose up -d --build
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

## macOS host PVA access

Docker Desktop does not provide a usable direct PVAccess path between a macOS
client and a container. The `epics-client` Compose service starts the relay
automatically, reads and writes PVs on the internal Docker network, and the
host-native proxy serves them over loopback PVA.

Start Compose. The main `Dockerfile` builds only the virtual accelerator.
`Dockerfile.epics-client` extends that image with the proxy helpers; the
`epics-client` service starts `pva_proxy_relay` automatically and reconnects
to the host proxy until it becomes available.

```bash
docker compose up -d --build
```

Whenever a proxy script changes, rebuild and recreate the relay explicitly:

```bash
docker compose up -d --build --force-recreate epics-client
```

Start the host proxy in another terminal. Use `-v` while diagnosing relay or
upstream PV connectivity:

```bash
.venv/bin/python scripts/pva_proxy_host -v
```

The proxy exposes original PV names on `127.0.0.1:5078/tcp` and
`127.0.0.1:5079/udp`. It supports reads, writes, and monitor updates for
arbitrary upstream PV names. Its relay listener is unauthenticated and binds
to port `5090`; it is intended only for local Docker Desktop use.

Verify that the relay is connected:

```bash
.venv/bin/python scripts/pvget va:proxy:connected
```

The expected value is `true`. The related status PVs are
`va:proxy:subscriptions` and `va:proxy:errors`.

Then access the accelerator directly from the Mac:

```bash
.venv/bin/python scripts/pvget BPMS:IN20:581:TMIT
```

Write through the proxy with:

```bash
.venv/bin/python scripts/pvput PV_NAME VALUE
```

If an accelerator PV times out, inspect the two ends of the relay:

```bash
docker compose logs --tail=100 epics-client
```

With verbose logging, the host proxy reports dynamic PV creation and relay
subscription requests. The container relay reports either `forwarding upstream
update for PV_NAME` or an upstream monitor error. `Channel disconnected`
means the relay cannot currently connect to the requested PV in the
`virtual-accelerator` Compose network.

The `scripts/pvget` and `scripts/pvput` defaults already target the local
proxy. Set `EPICS_PVA_*` variables only when deliberately using another PVA
endpoint. Stop the proxy with `Ctrl-C`; the Compose relay reconnects
automatically when it returns.
