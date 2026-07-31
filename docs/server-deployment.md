# Server Deployment (Apptainer)

How to run the virtual accelerator digital twin on a shared server (e.g. SLAC
`dev-srv09`) using [Apptainer](https://apptainer.org/), how to update it, and how
updates can be automated.

The image built by CI (`ghcr.io/slaclab/virtual-accelerator-digital-twin`) runs the
Bmad/Tao model and serves all beamline PVs over EPICS **PVAccess**. On a real Linux
server, Apptainer shares the host network, so PVA discovery works from other
processes on the same host (unlike Docker Desktop on macOS — see the repo `Issues`
notes).

---

## 1. Run it with Apptainer

### Get the image as a `.sif`

Apptainer runs SIF files, so first convert (pull) the OCI image from ghcr:

```bash
# :latest tracks main. Prefer pinning to a specific build for reproducibility:
apptainer pull virtual-accelerator.sif \
  docker://ghcr.io/slaclab/virtual-accelerator-digital-twin:latest

# Pinned to a specific commit (recommended for anything people depend on):
apptainer pull virtual-accelerator_sha-803277f.sif \
  docker://ghcr.io/slaclab/virtual-accelerator-digital-twin:sha-803277f
```

The `:sha-<short>` tags are produced by CI for every build, so a SIF named after
its sha (as in `virtual-accelerator-digital-twin_sha-803277f.sif`) always maps back
to exact source.

### Start the server (terminal 1)

```bash
apptainer run --writable-tmpfs --pwd /app virtual-accelerator.sif
```

- `--writable-tmpfs` gives the container a writable overlay (the model writes scratch files).
- `--pwd /app` matches the image `WORKDIR` so `run.py` and `scripts/` resolve.

Model parameters can be overridden with environment variables (see `run.py`):
`MODEL`, `END_ELEMENT`, `N_PARTICLES`, `LOG_LEVEL`. Pass them through Apptainer with
`--env`, e.g. `apptainer run --env LOG_LEVEL=DEBUG ...`.

### Query PVs (terminal 2)

From an EPICS-capable environment (e.g. the `rhel7_devel` conda env on SLAC):

```bash
source /sdf/sw/epics/package/anaconda/envs/rhel7_devel/bin/activate

python -c "
from p4p.client.thread import Context
print(Context('pva').get('BPMS:IN20:581:TMIT', timeout=10))
"

pvget BPMS:IN20:581:TMIT
```

### Self-check a deployment

The image ships the same smoke test CI uses. Point it at the running server to
confirm the expected PVs are up:

```bash
apptainer exec virtual-accelerator.sif python scripts/smoke_test.py
```

It exits `0` when all required PVs return values, non-zero otherwise. Configure the
PV list / timeouts via `SMOKE_PVS`, `SMOKE_STARTUP_TIMEOUT`, `SMOKE_GET_TIMEOUT`
(see the script header).

---

## 2. Update the image

There are two halves: rebuild the image from source, then refresh the SIF on the server.

### a) Source → image (automatic via CI)

1. Change the code and open a PR.
2. Merge to `main`.
3. GitHub Actions (`.github/workflows/build-container.yml`) rebuilds the image,
   **runs the smoke test against it**, and only then pushes `:latest` and
   `:sha-<short>` to ghcr. If the smoke test fails, nothing is published — so a
   broken image never reaches the registry.

You can also trigger a build manually from the Actions tab (`workflow_dispatch`).

### b) Image → server (manual)

Re-pull and swap the SIF, then restart:

```bash
# Overwrite the existing SIF with the latest image
apptainer pull --force virtual-accelerator.sif \
  docker://ghcr.io/slaclab/virtual-accelerator-digital-twin:latest

# Restart whatever is running the old SIF (Ctrl-C terminal 1, then re-run),
# or if using a named instance:
apptainer instance stop va-dt
apptainer instance start --writable-tmpfs virtual-accelerator.sif va-dt
```

> Tip: for anything others rely on, pull a pinned `:sha-<short>` into a new SIF
> rather than overwriting `:latest`, so a rollback is just pointing back at the
> previous file.

---

## 3. Automating updates (optional)

Today updates are manual (step 2b). To close the loop:

- **CI side** — already done: pushes to `main` rebuild, test, and publish the image.
- **Server side** — periodically check ghcr for a new digest and re-pull. This repo
  ships an example: [`scripts/update-sif.sh`](../scripts/update-sif.sh). It compares
  the remote image digest to the deployed SIF's recorded digest and, only when they
  differ, pulls a fresh SIF and restarts the Apptainer instance.

> **Note:** `update-sif.sh` is an example to adapt — review the paths (`VA_SIF`),
> the instance name (`VA_INSTANCE`), and the restart logic for your server before
> enabling it. It uses `skopeo` for a pull-free digest check if available.

### Run it on a schedule

**cron** (check every 15 min):

```cron
*/15 * * * * VA_SIF=/sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif \
  /sdf/group/cds/sw/epics/users/gopikab/va-dt/update-sif.sh >> /tmp/va-update.log 2>&1
```

**systemd** (user timer):

```ini
# ~/.config/systemd/user/va-update.service
[Unit]
Description=Update virtual-accelerator SIF from ghcr

[Service]
Type=oneshot
Environment=VA_SIF=/sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif
Environment=VA_INSTANCE=va-dt
ExecStart=/sdf/group/cds/sw/epics/users/gopikab/va-dt/update-sif.sh
```

```ini
# ~/.config/systemd/user/va-update.timer
[Unit]
Description=Periodically check for a new virtual-accelerator image

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now va-update.timer
```

---

## 4. How images are tested before publish (CI gate)

To avoid shipping broken images, `build-container.yml` does **build → test → push**:

1. **Build** the image locally in the runner (`load`, not pushed).
2. **Smoke test**: boot the container and run
   [`scripts/smoke_test.py`](../scripts/smoke_test.py), which connects over PVA and
   waits for the required PVs (`BPMS:IN20:581:TMIT`, `BPMS:IN20:581:X`,
   `BPMS:IN20:581:Y`) to return finite values. This catches import errors, model
   init failures, and PVs that never come up.
3. **Push** to ghcr — only if the smoke test passed.

The same script backs the Kubernetes `kubernetes/test-pod.yaml` connectivity check
and the local self-check in step 1, so the gate matches what a real client sees.

To extend coverage, add PVs to the check via `SMOKE_PVS` in the workflow, or add
more assertions to `scripts/smoke_test.py`.
