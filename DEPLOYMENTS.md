# Deployments

Active Digital Twin deployments and their output PV suffixes.

## Dev

| Model | Overlay | Output PV suffix | Image tag |
|-------|---------|------------------|-----------|
| `cu_hxr_staged` | `kubernetes/overlays/dev/cu_hxr_staged/` | `_LUME_SM1` | `feature-prometheus` |
| `cu_hxr_bmad` | `kubernetes/overlays/dev/bmad/` | `_LUME_PH1` | `feature-prometheus` |
| `cu_hxr_rmat` | `kubernetes/overlays/dev/rmat/` | `_LUME_PH2` | `feature-prometheus` |

## Prod

| Model | Overlay | Output PV suffix | Image tag |
|-------|---------|------------------|-----------|
| `cu_hxr_staged` | `kubernetes/overlays/prod/` | `_LUME_SM1` | `main` |

## Deploy / redeploy

```bash
kubectl apply -k kubernetes/overlays/<env>/<model>/
kubectl rollout restart deployment/<deployment-name> -n virtual-accelerator
kubectl rollout status deployment/<deployment-name> -n virtual-accelerator
```

Deployment names:
- `cu_hxr_staged` / prod → `virtual-accelerator`
- `cu_hxr_bmad` → `virtual-accelerator-bmad`
- `cu_hxr_rmat` → `virtual-accelerator-rmat`

## Verify

```bash
# From inside the pod (bypasses the socat proxy):
kubectl exec <pod> -n virtual-accelerator -- env EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" \
  pvxmonitor OTRS:IN20:571:XRMS_LUME_SM1
```
