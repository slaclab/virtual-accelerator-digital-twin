#!/usr/bin/env bash
#
# update-sif.sh - OPTIONAL example server-side auto-updater for the
# virtual accelerator digital twin.
#
# It checks ghcr.io for a newer image, and if the digest changed, re-pulls the
# image into a fresh .sif and (re)starts the Apptainer instance. Run it by hand,
# from cron, or from a systemd timer (see docs/server-deployment.md).
#
# This is a starting point to ADAPT to your server - review the paths, the
# instance name, and the restart logic before enabling it anywhere.
#
# Config (override via environment):
#   VA_IMAGE     Image ref to track.  Default: ghcr.io/slaclab/virtual-accelerator-digital-twin:latest
#   VA_SIF       Path to the .sif in use. Default: /sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif
#   VA_INSTANCE  Apptainer instance name.  Default: va-dt
#
# Exit codes: 0 = up to date or successfully updated, non-zero = error.

set -euo pipefail

VA_IMAGE="${VA_IMAGE:-ghcr.io/slaclab/virtual-accelerator-digital-twin:latest}"
VA_SIF="${VA_SIF:-/sdf/group/cds/sw/epics/users/gopikab/va-dt/virtual-accelerator.sif}"
VA_INSTANCE="${VA_INSTANCE:-va-dt}"

DIGEST_FILE="${VA_SIF}.digest"   # records the digest of the currently-deployed SIF
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Resolve the digest the :latest tag currently points at, without pulling.
# `apptainer` bundles the registry client; `oras` would also work if available.
remote_digest() {
  apptainer remote status >/dev/null 2>&1 || true
  # `docker://` inspect via apptainer is not universally available, so fall back
  # to `skopeo` if present, otherwise pull-and-read below.
  if command -v skopeo >/dev/null 2>&1; then
    skopeo inspect "docker://${VA_IMAGE}" --format '{{.Digest}}' 2>/dev/null || true
  fi
}

restart_instance() {
  log "Restarting apptainer instance '${VA_INSTANCE}'"
  apptainer instance stop "${VA_INSTANCE}" >/dev/null 2>&1 || true
  apptainer instance start --writable-tmpfs "${VA_SIF}" "${VA_INSTANCE}"
  # The container's CMD (python run.py) starts the PVA server inside the instance.
}

main() {
  local current_digest new_digest tmp_sif
  current_digest="$(cat "${DIGEST_FILE}" 2>/dev/null || echo "none")"
  new_digest="$(remote_digest)"

  if [[ -n "${new_digest}" && "${new_digest}" == "${current_digest}" ]]; then
    log "Already up to date (${current_digest}); nothing to do."
    return 0
  fi

  log "Update available (have=${current_digest} remote=${new_digest:-unknown}); pulling ${VA_IMAGE}"
  tmp_sif="$(mktemp "${VA_SIF}.new.XXXXXX")"
  # --force overwrites the temp target; pull converts the OCI image to a SIF.
  apptainer pull --force "${tmp_sif}" "docker://${VA_IMAGE}"

  # Atomically swap the new SIF into place, then record its digest.
  mv -f "${tmp_sif}" "${VA_SIF}"
  if [[ -z "${new_digest}" ]] && command -v skopeo >/dev/null 2>&1; then
    new_digest="$(skopeo inspect "docker://${VA_IMAGE}" --format '{{.Digest}}' 2>/dev/null || echo pulled)"
  fi
  echo "${new_digest:-pulled}" > "${DIGEST_FILE}"

  restart_instance
  log "Update complete (now ${new_digest:-pulled})."
}

main "$@"
