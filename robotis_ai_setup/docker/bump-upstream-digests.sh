#!/bin/bash
# bump-upstream-digests.sh — Look up current registry digests for the
# upstream base images pinned in the EduBotics Dockerfiles.
#
# Why this exists:
#   physical_ai_server/Dockerfile pins `robotis/physical-ai-server:amd64-0.8.2`
#   and open_manipulator/Dockerfile pins `robotis/open-manipulator:amd64-4.1.4`
#   via the `ARG BASE_IMAGE=...` defaults. Both tags are mutable in
#   theory — ROBOTIS can repush the same tag with new content. Pinning
#   by tag in source + reviewing digests separately is the policy.
#
#   This script reads the exact ARG default from each Dockerfile (so
#   it can never drift from what we actually build against), queries
#   the registry for the current top-level digest via
#   `docker buildx imagetools inspect`, and prints a concise summary
#   table you can eyeball before deciding whether to bump.
#
#   Modal training base image lives in modal_training/modal_app.py
#   (a CUDA base tag, not a Dockerfile pin); bump that manually.
#
# Behaviour:
#   - Exits non-zero if either Dockerfile is missing or unparseable.
#   - Exits non-zero if any pinned base image fails to resolve in the
#     registry (so a missing base image surfaces here instead of as a
#     confusing `docker buildx build` failure later).
#
# Usage:
#   ./bump-upstream-digests.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse the `ARG BASE_IMAGE=<ref>` default from a Dockerfile. The build
# script overrides this via --build-arg per platform, but the default
# IS the amd64 pin we ship to students.
parse_base_image() {
    local dockerfile="$1"
    if [ ! -f "$dockerfile" ]; then
        echo "ERROR: Dockerfile not found: $dockerfile" >&2
        return 1
    fi
    local ref
    ref=$(awk -F= '
        $1 ~ /^ARG[[:space:]]+BASE_IMAGE$/ {
            # `ARG BASE_IMAGE=robotis/...` — the value is field 2 onward.
            sub(/^[[:space:]]+/, "", $2)
            print $2
            exit
        }' "$dockerfile")
    if [ -z "$ref" ]; then
        echo "ERROR: no ARG BASE_IMAGE=... default found in $dockerfile" >&2
        return 1
    fi
    echo "$ref"
}

# Resolve the registry-side top-level digest for an image ref. Uses
# `docker buildx imagetools inspect` which works for both single-arch
# and multi-arch manifests without needing the local daemon to have
# the image cached.
inspect_image() {
    local image="$1"
    local raw digest pushed
    if ! raw=$(docker buildx imagetools inspect "$image" 2>&1); then
        return 1
    fi
    digest=$(printf '%s\n' "$raw" | awk '/^Digest:/ {print $2; exit}')
    # `imagetools inspect` doesn't surface push timestamp; record "n/a"
    # rather than fabricating a value the operator might trust.
    pushed="n/a"
    if [ -z "$digest" ]; then
        return 1
    fi
    printf '%s|%s' "$digest" "$pushed"
}

# (image_ref, dockerfile_label) -> single row of the summary table.
report_row() {
    local image="$1"
    local label="$2"
    local result
    if result=$(inspect_image "$image"); then
        local digest pushed
        digest=${result%|*}
        pushed=${result#*|}
        printf '%-44s | %-46s | %s | %s\n' \
            "$label" "$image" "$digest" "$pushed"
        return 0
    fi
    printf '%-44s | %-46s | %s | %s\n' \
        "$label" "$image" "MISSING" "n/a"
    return 1
}

PAS_DOCKERFILE="${SCRIPT_DIR}/physical_ai_server/Dockerfile"
OMX_DOCKERFILE="${SCRIPT_DIR}/open_manipulator/Dockerfile"

PAS_BASE=$(parse_base_image "$PAS_DOCKERFILE")
OMX_BASE=$(parse_base_image "$OMX_DOCKERFILE")

echo "Pinned base images (parsed from ARG BASE_IMAGE defaults):"
echo "  physical_ai_server -> ${PAS_BASE}"
echo "  open_manipulator   -> ${OMX_BASE}"
echo ""
printf '%-44s | %-46s | %s | %s\n' \
    "Dockerfile" "Pinned tag" "Registry digest" "Last pushed"
printf '%-44s-+-%-46s-+-%s-+-%s\n' \
    "$(printf '%0.s-' {1..44})" \
    "$(printf '%0.s-' {1..46})" \
    "$(printf '%0.s-' {1..71})" \
    "$(printf '%0.s-' {1..11})"

errors=0
report_row "$PAS_BASE" "docker/physical_ai_server/Dockerfile" || errors=$((errors + 1))
report_row "$OMX_BASE" "docker/open_manipulator/Dockerfile"   || errors=$((errors + 1))

echo ""
if [ "$errors" -gt 0 ]; then
    echo "ERROR: ${errors} base image(s) failed to resolve in the registry." >&2
    echo "       Either the tag was retracted upstream or `docker login` is stale." >&2
    exit 1
fi

cat <<EOF

To bump a pinned tag (review first!):

  1. Edit the ARG BASE_IMAGE=... default in the affected Dockerfile.
  2. Run a full test build:  REGISTRY=nettername ./build-images.sh
  3. Smoke-test the GUI flow + a training job before pushing.
  4. Commit the digest bump in its own commit.

Modal training base (CUDA) is pinned inline in
modal_training/modal_app.py — bump that separately.
EOF
