# Deployments

## Models

Active Digital Twin models and their output PV suffixes. This table is deployment-agnostic — the only reference needed if you just want to know what's running and how to find its PVs.

| Model | Description | Output PV suffix |
|-------|-------------|------------------|
| `cu_hxr_staged` | CU injector ML surrogate → Bmad CU HXR physics, ends at OTR4 | `_LUME_SM1` |
| `cu_hxr_bmad` | CU HXR physics only (Bmad), ends at OTR4 | `_LUME_PH1` |
| `cu_hxr_rmat` | CU HXR transfer-matrix (linear optics) over WS27644–WS28144 | `_LUME_PH2` |

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

## Next steps

- Prod image tag should not be a generic/moving tag like `main`. Use the commit hash instead so prod deployments are pinned and reproducible.

## Verify

```bash
# From inside the pod (bypasses the socat proxy):
kubectl exec <pod> -n virtual-accelerator -- env EPICS_PVA_NAME_SERVERS="127.0.0.1:5075" \
  pvxmonitor OTRS:IN20:571:XRMS_LUME_SM1
```
