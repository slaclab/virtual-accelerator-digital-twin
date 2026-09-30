# Adding descriptions to Digital Twin PVs

## Context

The Digital Twin serves PVs (via lume-pva) whose names carry element info but nothing else. There is no way for a client (pvget, Phoebus, physicists) to tell what a PV represents, which model produced it, or which DT deployment it came from.

Two levels of context are relevant:
- **Semantic** — what the PV *is* (e.g. "quadrupole BDES setpoint"). Property of the action class, shared with the real VA.
- **Deployment** — which model/end-element/DT instance produced it (e.g. "staged model @ TD11"). DT-specific.

Split (per supervisor):
1. Semantic descriptions live on the action class definitions in the **VA repo** (`virtual_accelerator/bmad/actions.py`).
2. Deployment context is appended in the **DT repo** at runtime from an env var, and provisioned per-overlay in the Kubernetes config.

Both flow into the NTScalar `display.description` field of the main PV — no companion `:DESC` PVs, no downstream client changes.

## Enablers (already in place)

- **lume-base** upgraded — `Variable` base class now carries `description: str = ""`, inherited by every action class through `ScalarVariable` / `NDVariable` / `EnumVariable`.
- **lume-pva PR #57** (merged 2026-09-16 into `main`) — `ScalarVariableHandler.set_metadata` reads `getattr(variable, "description", None)` and writes it to the served NTScalar's `display.description`; `NDVariableHandler` gets a matching NT slot. The DT's Dockerfile installs lume-pva unpinned from git HEAD, so the next image rebuild picks this up automatically.

No changes to lume-pva or lume-base are required.

## Changes

### 1. VA repo (`virtual-accelerator`)

**File:** `virtual_accelerator/bmad/actions.py`

Set `description` as a plain class-level string on each action class. No validators, no templates — element name is already carried by the PV name, so the description just states *what kind of PV* this is.

| Class | Description |
|---|---|
| `QuadrupoleBCTRLVariable` | `"Quadrupole magnet field integral setpoint (BDES)"` |
| `QuadrupoleBACTVariable` | `"Quadrupole magnet field integral readback (BACT)"` |
| `SolenoidBCTRLVariable` | `"Solenoid field integral setpoint (BDES)"` |
| `SolenoidBACTVariable` | `"Solenoid field integral readback (BACT)"` |
| `SBendBCTRLVariable` | `"SBend momentum setpoint (BDES)"` |
| `SBendBACTVariable` | `"SBend momentum readback (BACT)"` |
| `KickerBCTRLVariable` | `"Corrector kicker field integral setpoint (BDES)"` |
| `KickerBACTVariable` | `"Corrector kicker field integral readback (BACT)"` |
| `StatusVariable` | `"Device status word"` |
| `BminVariable` | `"Device soft low limit (DRVL)"` |
| `BmaxVariable` | `"Device soft high limit (DRVH)"` |
| `ControlStateVariable` | `"Device control state"` |
| `BPMXVariable` | `"BPM horizontal orbit position"` |
| `BPMYVariable` | `"BPM vertical orbit position"` |
| `BPMTMITDummyVariable` | `"BPM beam intensity (dummy)"` |
| `KlystronENLDVariable` | `"Klystron amplitude (ENLD)"` |
| `KlystronPDESVariable` | `"Klystron phase setpoint"` |
| `KlystronPACTVariable` | `"Klystron phase readback"` |
| `KlystronStatVariable` | `"Klystron on/off status"` |
| `CavityAREQVariable` | `"Cavity amplitude request"` |
| `CavityAREQReadbackVariable` | `"Cavity amplitude readback"` |
| `CavityPREQVariable` | `"Cavity phase request"` |
| `CavityPREQReadbackVariable` | `"Cavity phase readback"` |
| `CavityMODECFGVariable` | `"Cavity mode configuration"` |
| `RMatrixAction` | `"6x6 R-matrix between start and end elements"` |
| `DummyEnumVariable` | *(skip — test-only)* |

*(Text is a first pass — supervisor is welcome to refine wording before commit.)*

Because `Variable.description` is already inherited, each concrete class just adds one line, e.g.:

```python
class QuadrupoleBCTRLVariable(_QuadrupoleGradientVariable, WritableActionMixin):
    read_only: bool = False
    unit: str = "kG"
    description: str = "Quadrupole magnet field integral setpoint (BDES)"
    ...
```

### 2. DT repo (`virtual-accelerator-digital-twin`)

**`run.py`** — after the model is built and before `Runner(model, config=config)`:

- Read env `DT_CONTEXT`. Sensible default when unset: `f"{model_name} @ {end_element}"`. Empty string disables the suffix.
- Iterate `model.supported_variables.values()`. For each variable whose `description` is truthy, set `variable.description = f"{variable.description} [{DT_CONTEXT}]"`.

Placement note: the existing config-mutation loop in `run.py` mutates `config['variables'][k]` dicts, but lume-pva reads `description` from the Variable object itself. So the suffix must be applied to the variables directly, not to config entries.

**`Dockerfile`** — bump `VIRTUAL_ACCELERATOR_REF` (line 3) to the merged SHA of the VA change. If merging upstream is not in scope for this iteration, add a second `ARG VIRTUAL_ACCELERATOR_REPO=…` to point at the fork instead of `slaclab/virtual-accelerator`.

**`kubernetes/overlays/*/kustomization.yaml`** — add one line per overlay's `configMapGenerator.literals`, e.g. `- DT_CONTEXT=staged model @ TD11`. Overlays affected:
- `kubernetes/overlays/dev/rmat/kustomization.yaml`
- `kubernetes/overlays/dev/bmad/kustomization.yaml`
- `kubernetes/overlays/dev/cu_hxr_staged/kustomization.yaml`
- `kubernetes/overlays/prod/kustomization.yaml`

`kubernetes/base/deployment.yaml` is unchanged (already pulls env from the `va-config` ConfigMap).

## Verification

1. **VA-side unit check** (Python REPL):
   ```python
   from virtual_accelerator.bmad.actions import QuadrupoleBCTRLVariable
   v = QuadrupoleBCTRLVariable(name="QM11:BDES", element_name="QM11")
   assert v.description == "Quadrupole magnet field integral setpoint (BDES)"
   ```
   Spot-check a cavity variable and `RMatrixAction`.

2. **DT container end-to-end:**
   - Rebuild the DT image with the bumped `VIRTUAL_ACCELERATOR_REF` and a fresh lume-pva pull.
   - Deploy one overlay with `DT_CONTEXT` set.
   - From a client host:
     ```
     pvget -M raw <QUAD-BDES-PV>
     ```
     `display.description` should read `"Quadrupole magnet field integral setpoint (BDES) [staged model @ TD11]"`.
   - Repeat for a BPM, a klystron, and the RMatrix ND PV.

3. **Regression:** existing `pvget` results should still return correct `value`, `display.units`, alarms, and control limits — none of those code paths are touched.

## Out of scope

- Modifying lume-pva or lume-base.
- Companion `:DESC` PVs (unneeded — descriptions flow via the NTScalar struct).
- Per-element templating in descriptions (element name is already in the PV name).

## Open questions for review

1. Description wording — supervisor's preferred phrasing?
2. `DT_CONTEXT` format — free-form string, or structured (e.g. always `"<model> @ <end_element>"`)?
3. Should we land the VA change upstream in `slaclab/virtual-accelerator` first, or point the DT at the fork for an interim test?
