#!/bin/bash
# bump-upstream-digests.sh — Refresh digest pins for upstream base images.
#
# Why this exists:
#   open_manipulator/Dockerfile pins its upstream base image by sha256 digest,
#   not :latest, so our build is reproducible and a surprise upstream retag
#   cannot inject changes into student installs. The trade-off is that we
#   don't pick up upstream improvements automatically.
#
#   The CUDA base for cloud training now lives in modal_training/modal_app.py
#   (not a Dockerfile). Modal builds and caches that image server-side; the
#   tag pin there can be bumped manually without any digest lookup here.
#
# What this script does:
#   Looks up the current top-level digest for each upstream image and prints
#   the exact `sed` commands needed to update the Dockerfiles. Review the
#   changes manually before committing — bumping a base image without testing
#   is exactly the kind of surprise we are trying to prevent.
#
# Usage:
#   ./bump-upstream-digests.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

lookup() {
    local image="$1"
    local digest
    digest=$(docker buildx imagetools inspect "$image" 2>&1 | awk '/^Digest:/ {print $2; exit}')
    if [ -z "$digest" ]; then
        echo "ERROR: could not resolve digest for $image" >&2
        return 1
    fi
    echo "$digest"
}

echo "==> robotis/open-manipulator:latest"
ROBOTIS_DIGEST=$(lookup robotis/open-manipulator:latest)
echo "    current pin: $(grep -oE 'sha256:[a-f0-9]+' "${SCRIPT_DIR}/open_manipulator/Dockerfile" || echo 'NONE')"
echo "    new digest:  ${ROBOTIS_DIGEST}"

cat <<EOF

To apply the bump (review first!):

    sed -i 's|sha256:[a-f0-9]\\+|${ROBOTIS_DIGEST}|' \\
        "${SCRIPT_DIR}/open_manipulator/Dockerfile"

After applying:
  1. Run a full test build:  REGISTRY=nettername ${SCRIPT_DIR}/build-images.sh
  2. Smoke-test the GUI flow + a training job
  3. Commit the digest bump in its own commit
EOF
