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
# WHAT IT USED TO MISS (fixed): it parsed the two THIN-overlay
# `ARG BASE_IMAGE=` lines and nothing else — which is only the amd64 story. It
# was blind to:
#   - the THREE bases we build OURSELVES and pin in build-images.sh
#     (physical-ai-server-opi-base, physical-ai-server-jetson-base,
#     open-manipulator-jetson-base). Those are the pins most likely to be wrong,
#     because nothing in CI builds them: a maintainer hand-runs
#     `BUILD_BASE_*=1 ./build-images.sh` and pushes once. A pin naming a tag
#     that was never published fails the release, and this is the cheap place to
#     find out. Their tags are parsed OUT of build-images.sh rather than
#     duplicated here, so this script can never disagree with what builds.
#   - the upstream `FROM` of each base Dockerfile (`ros:jazzy-ros-base` for the
#     Orange Pi CPU base, `robotis/ros:...-cuda12.8.0` for the other two) —
#     mutable upstream tags that reach students through a rebuild, entirely
#     unreviewed.
#
# Behaviour:
#   - Exits non-zero if a Dockerfile is missing or unparseable.
#   - Exits non-zero if any pinned base image fails to resolve in the
#     registry (so a missing base image surfaces here instead of as a
#     confusing `docker buildx build` failure later). For a self-built base
#     that means "you have not pushed it yet" — see the closing notes.
#
# Usage:
#   ./bump-upstream-digests.sh
#   REGISTRY=nettername ./bump-upstream-digests.sh   # self-built base namespace

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

# Parse the upstream `FROM <ref>` of a base Dockerfile. Takes the FIRST FROM
# (these are single-stage builds with an `AS` alias, e.g.
# `FROM ros:jazzy-ros-base AS physical-ai-tools`), so $2 is the ref.
parse_from_image() {
    local dockerfile="$1"
    if [ ! -f "$dockerfile" ]; then
        echo "ERROR: Dockerfile not found: $dockerfile" >&2
        return 1
    fi
    local ref
    ref=$(awk '$1 == "FROM" { print $2; exit }' "$dockerfile")
    if [ -z "$ref" ]; then
        echo "ERROR: no FROM found in $dockerfile" >&2
        return 1
    fi
    echo "$ref"
}

# Parse a self-built base pin out of build-images.sh. The lines look like
#   PAS_BASE_IMAGE="${REGISTRY}/physical-ai-server-opi-base:0.8.2-opi2"
# so we match `<repo>:<tag>` and re-attach the registry ourselves. Reading the
# tag from build-images.sh (rather than hardcoding it here) is deliberate: that
# file is the single source of truth for what actually gets built against, and a
# second copy would be one more thing to forget.
parse_selfbuilt_base() {
    local repo="$1"
    local pin
    pin=$(grep -Eo "${repo}:[A-Za-z0-9._-]+" "$BUILD_SH" | head -n1)
    if [ -z "$pin" ]; then
        echo "ERROR: no ${repo}:<tag> pin found in $BUILD_SH" >&2
        return 1
    fi
    echo "${REGISTRY}/${pin}"
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

# Namespace the self-built bases live in. Mirrors build-images.sh's own default
# so an operator who overrides REGISTRY there overrides it identically here.
REGISTRY="${REGISTRY:-nettername}"

BUILD_SH="${SCRIPT_DIR}/build-images.sh"
PAS_DOCKERFILE="${SCRIPT_DIR}/physical_ai_server/Dockerfile"
OMX_DOCKERFILE="${SCRIPT_DIR}/open_manipulator/Dockerfile"
# The three server BASE Dockerfiles live in the physical_ai_tools tree, one
# level up from robotis_ai_setup/ (build-images.sh resolves them the same way).
PAT_DIR="${PHYSICAL_AI_TOOLS_DIR:-$(dirname "$(dirname "$SCRIPT_DIR")")/physical_ai_tools}"

# 1. Thin-overlay ARG BASE_IMAGE defaults (the amd64 pins we ship to students).
PAS_BASE=$(parse_base_image "$PAS_DOCKERFILE")
OMX_BASE=$(parse_base_image "$OMX_DOCKERFILE")

# 2. The bases we build ourselves, pinned in build-images.sh.
PAS_OPI_BASE=$(parse_selfbuilt_base "physical-ai-server-opi-base")
PAS_JETSON_BASE=$(parse_selfbuilt_base "physical-ai-server-jetson-base")
OMX_JETSON_BASE=$(parse_selfbuilt_base "open-manipulator-jetson-base")

# 3. The upstream FROMs those self-built bases are cut from.
FROM_AMD64=$(parse_from_image "${PAT_DIR}/physical_ai_server/Dockerfile.amd64")
FROM_ARM64=$(parse_from_image "${PAT_DIR}/physical_ai_server/Dockerfile.arm64")
FROM_ARM64CPU=$(parse_from_image "${PAT_DIR}/physical_ai_server/Dockerfile.arm64cpu")

echo "Pinned base images (parsed from ARG BASE_IMAGE defaults):"
echo "  physical_ai_server -> ${PAS_BASE}"
echo "  open_manipulator   -> ${OMX_BASE}"
echo ""
echo "Self-built bases (parsed from build-images.sh; NOT built by CI):"
echo "  physical_ai_server opi    -> ${PAS_OPI_BASE}"
echo "  physical_ai_server jetson -> ${PAS_JETSON_BASE}"
echo "  open_manipulator jetson   -> ${OMX_JETSON_BASE}"
echo ""
printf '%-44s | %-46s | %s | %s\n' \
    "Dockerfile" "Pinned tag" "Registry digest" "Last pushed"
printf '%-44s-+-%-46s-+-%s-+-%s\n' \
    "$(printf '%0.s-' {1..44})" \
    "$(printf '%0.s-' {1..46})" \
    "$(printf '%0.s-' {1..71})" \
    "$(printf '%0.s-' {1..11})"

errors=0
selfbuilt_missing=0

# Thin-overlay pins (pulled from ROBOTIS on every amd64 build).
report_row "$PAS_BASE" "docker/physical_ai_server/Dockerfile" || errors=$((errors + 1))
report_row "$OMX_BASE" "docker/open_manipulator/Dockerfile"   || errors=$((errors + 1))

# Self-built bases. A MISSING here is a different diagnosis from an upstream
# MISSING — it means the base Dockerfile was edited and nobody rebuilt/pushed
# it (exactly what ci.yml::base-version-guard fences at review time).
report_row "$PAS_OPI_BASE"    "build-images.sh (opi base, hand-built)"    || { errors=$((errors + 1)); selfbuilt_missing=1; }
report_row "$PAS_JETSON_BASE" "build-images.sh (jetson base, hand-built)" || { errors=$((errors + 1)); selfbuilt_missing=1; }
report_row "$OMX_JETSON_BASE" "build-images.sh (jetson OMX, hand-built)"  || { errors=$((errors + 1)); selfbuilt_missing=1; }

# Upstream FROMs of the self-built bases — mutable tags that reach a rig only
# via a base rebuild, so they are reviewed here rather than never.
report_row "$FROM_AMD64"    "Dockerfile.amd64 FROM"    || errors=$((errors + 1))
report_row "$FROM_ARM64"    "Dockerfile.arm64 FROM"    || errors=$((errors + 1))
report_row "$FROM_ARM64CPU" "Dockerfile.arm64cpu FROM" || errors=$((errors + 1))

echo ""
if [ "$errors" -gt 0 ]; then
    echo "ERROR: ${errors} base image(s) failed to resolve in the registry." >&2
    # NOTE: 'docker login' is single-quoted. It used to sit in BACKTICKS inside
    # a double-quoted string, which is command substitution — this error path
    # literally ran `docker login` and pasted its output into the message.
    echo "       Either the tag was retracted upstream, or 'docker login' is stale." >&2
    if [ "$selfbuilt_missing" = "1" ]; then
        echo "" >&2
        echo "       One of the MISSING rows is a base WE build. That is not an upstream" >&2
        echo "       problem: the tag pinned in build-images.sh has never been pushed." >&2
        echo "       Build + push it before tagging a release, e.g.:" >&2
        echo "         BUILD_BASE_OPI=1  PLATFORM=opi   ./build-images.sh   # opi CPU base" >&2
        echo "         BUILD_BASE_ARM64=1 PLATFORM=arm64 ./build-images.sh  # both Jetson bases" >&2
        echo "       Until then the matching build leg fails its manifest resolve — and for" >&2
        echo "       opi (continue-on-error in docker-publish.yml) it SKIPS silently." >&2
    fi
    exit 1
fi

cat <<EOF

To bump a pinned tag (review first!):

  1. Edit the ARG BASE_IMAGE=... default in the affected Dockerfile.
  2. Run a full test build:  REGISTRY=nettername ./build-images.sh
  3. Smoke-test the GUI flow + a training job before pushing.
  4. Commit the digest bump in its own commit.

To change a base WE build (Dockerfile.amd64 / .arm64 / .arm64cpu):

  1. Edit the base Dockerfile.
  2. Rebuild + push it:  BUILD_BASE_OPI=1 PLATFORM=opi ./build-images.sh
                    (or) BUILD_BASE_ARM64=1 PLATFORM=arm64 ./build-images.sh
  3. Bump the matching PAS_BASE_IMAGE tag in build-images.sh, SAME commit —
     ci.yml::base-version-guard enforces this. Nothing in CI builds these
     bases, so an un-bumped tag means the release silently ships the old one.

Modal training base (CUDA) is pinned inline in
modal_training/modal_app.py — bump that separately.
EOF
