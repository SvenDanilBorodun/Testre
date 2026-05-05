#!/bin/bash
# build-images.sh — Build and push all Docker images
# Run by MAINTAINER on a Linux machine, NOT by students.
#
# Prerequisites:
#   - Docker installed and running
#   - Logged into registry: docker login
#   - Clone repos side by side:
#       /path/to/open_manipulator/
#       /path/to/physical_ai_tools/
#       /path/to/robotis_ai_setup/
#
# Usage:
#   REGISTRY=yourdockerhubuser ./build-images.sh
#   # or just:
#   ./build-images.sh   (uses default REGISTRY=nettername)
#
# The open_manipulator base image is the official ROBOTIS image from Docker Hub:
#   robotis/open-manipulator:amd64-4.1.4
# By default this script pulls it. To force a local rebuild from source instead:
#   BUILD_BASE=1 ./build-images.sh

set -euo pipefail

REGISTRY=${REGISTRY:-nettername}
BUILD_BASE=${BUILD_BASE:-0}
OMX_BASE_IMAGE="robotis/open-manipulator:amd64-4.1.4"
# Must match the FROM tag in physical_ai_server/Dockerfile. Previously this
# script pulled ":latest" independently, so the Dockerfile build and the
# pre-pull could reference different content.
PAS_BASE_IMAGE="robotis/physical-ai-server:amd64-0.8.2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Expect repos to be siblings of robotis_ai_setup
OPEN_MANIPULATOR_DIR="${OPEN_MANIPULATOR_DIR:-$(dirname "$PROJECT_ROOT")/open_manipulator}"
PHYSICAL_AI_TOOLS_DIR="${PHYSICAL_AI_TOOLS_DIR:-$(dirname "$PROJECT_ROOT")/physical_ai_tools}"

echo "========================================"
echo "ROBOTIS AI — Docker Image Builder"
echo "Registry: ${REGISTRY}"
echo "open_manipulator: ${OPEN_MANIPULATOR_DIR}"
echo "physical_ai_tools: ${PHYSICAL_AI_TOOLS_DIR}"
echo "========================================"

# Validate directories exist
for dir in "$OPEN_MANIPULATOR_DIR" "$PHYSICAL_AI_TOOLS_DIR"; do
    if [ ! -d "$dir" ]; then
        echo "ERROR: Directory not found: $dir"
        echo "Ensure open_manipulator and physical_ai_tools repos are cloned alongside robotis_ai_setup."
        exit 1
    fi
done

# ── Image 1: physical_ai_manager (React + nginx) ──
echo ""
echo ">> Building physical_ai_manager..."
# Cloud training env vars are baked into the React build
SUPABASE_URL=${SUPABASE_URL:-}
SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY:-}
CLOUD_API_URL=${CLOUD_API_URL:-}
# Student-facing policy allowlist. Defaults to 'act' so the student Docker
# build hides every other policy in the dropdown. Set ALLOWED_POLICIES in
# the environment (comma list) to override for an admin/dev build.
ALLOWED_POLICIES=${ALLOWED_POLICIES:-act}
# Build identifier — baked into the React bundle and written to
# build/version.json so the running app can detect a new image and self-reload.
# Format: <UTC timestamp>-<short git sha or "nogit">. Timestamp first guarantees
# every rebuild has a unique id even when the working tree is dirty or HEAD
# hasn't moved, which is exactly what the client-side version check needs.
_BUILD_TS=$(date -u +%Y%m%d-%H%M%S)
_BUILD_SHA=$(git -C "$(dirname "$PROJECT_ROOT")" rev-parse --short HEAD 2>/dev/null || true)
if [ -z "$_BUILD_SHA" ]; then
    # Fall back to an 8-byte random hex so two CI-without-git builds get
    # distinct BUILD_IDs — the old literal "nogit" fallback made every
    # non-git build collide on the same version id, so the React
    # self-reload check couldn't tell them apart.
    _BUILD_SHA=$(od -An -N4 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' | head -c 8)
    if [ -z "$_BUILD_SHA" ]; then
        _BUILD_SHA=$(date +%s | tail -c 8)
    fi
fi
BUILD_ID=${BUILD_ID:-${_BUILD_TS}-${_BUILD_SHA}}
echo "   BUILD_ID: ${BUILD_ID}"
BUILD_ARGS=""
if [ -n "$SUPABASE_URL" ]; then
    BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_SUPABASE_URL=${SUPABASE_URL}"
fi
if [ -n "$SUPABASE_ANON_KEY" ]; then
    BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY}"
fi
if [ -n "$CLOUD_API_URL" ]; then
    BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_CLOUD_API_URL=${CLOUD_API_URL}"
fi
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_ALLOWED_POLICIES=${ALLOWED_POLICIES}"
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_BUILD_ID=${BUILD_ID}"

# Stage server-side coco_classes.py into the manager build context so
# the prebuild Jest hook (objectClasses.sync.test.js) can validate
# that the React dropdown matches the server allowlist. Without this
# the test would fail on ENOENT because physical_ai_server/ is a
# sibling repo, not part of physical_ai_manager/.
COCO_SNAPSHOT="${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/_coco_classes.py"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_server/physical_ai_server/workflow/coco_classes.py" \
   "${COCO_SNAPSHOT}"
# Single combined cleanup. A previous version trapped the COCO_SNAPSHOT
# cleanup here and then trapped INTERFACES_STAGING again later — bash's
# trap is set-not-stack, so the second trap silently REPLACED this one
# and _coco_classes.py leaked into the source tree on every build. We
# now register both cleanups in one trap so neither path can be lost.
_build_cleanup() {
    rm -f "${COCO_SNAPSHOT}"
    rm -rf "${INTERFACES_STAGING:-/dev/null}"
}
trap _build_cleanup EXIT

docker build \
    $BUILD_ARGS \
    -t "${REGISTRY}/physical-ai-manager:latest" \
    -f "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/Dockerfile" \
    "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/"
echo "   OK: physical-ai-manager built"

# ── Image 2a: physical_ai_server base (pull official ROBOTIS image) ──
echo ""
echo ">> Pulling physical_ai_server base ${PAS_BASE_IMAGE}..."
if ! docker image inspect "${PAS_BASE_IMAGE}" >/dev/null 2>&1; then
    docker pull "${PAS_BASE_IMAGE}"
fi
echo "   OK: ${PAS_BASE_IMAGE} exists"

# ── Image 2b: physical_ai_server thin layer (patches upstream bugs) ──
#
# Stage physical_ai_interfaces source into the build context so the
# Dockerfile can re-build it inside the image. The base image lacks
# the Roboter Studio service / message types that landed in
# d408378, so without this rebuild physical_ai_server.py would crash
# on import. We copy the whole package directory (msg/, srv/,
# CMakeLists.txt, package.xml) as `interfaces/` and clean up after.
INTERFACES_STAGING="${SCRIPT_DIR}/physical_ai_server/interfaces"
echo ""
echo ">> Staging physical_ai_interfaces source for in-image rebuild..."
rm -rf "${INTERFACES_STAGING}"
mkdir -p "${INTERFACES_STAGING}/msg" "${INTERFACES_STAGING}/srv"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_interfaces/CMakeLists.txt" "${INTERFACES_STAGING}/"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_interfaces/package.xml"    "${INTERFACES_STAGING}/"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_interfaces/msg/"*.msg      "${INTERFACES_STAGING}/msg/"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_interfaces/srv/"*.srv      "${INTERFACES_STAGING}/srv/"

# Cleanup is already registered above via _build_cleanup, which removes
# both ${COCO_SNAPSHOT} and ${INTERFACES_STAGING} on exit. We must NOT
# overwrite the trap here — that's the v1 bug — so this is a no-op.

echo ""
echo ">> Building physical_ai_server thin layer (patches + interface rebuild)..."
docker build \
    -t "${REGISTRY}/physical-ai-server:latest" \
    -f "${SCRIPT_DIR}/physical_ai_server/Dockerfile" \
    "${SCRIPT_DIR}/physical_ai_server/"
echo "   OK: physical-ai-server built (with patches)"

# ── Image 3: open_manipulator base ──
if [ "$BUILD_BASE" = "1" ]; then
    echo ""
    echo ">> Building open_manipulator base from source (this takes ~40 min)..."
    docker build \
        -t "${OMX_BASE_IMAGE}" \
        -f "${OPEN_MANIPULATOR_DIR}/docker/Dockerfile" \
        "${OPEN_MANIPULATOR_DIR}/docker/"
    echo "   OK: open-manipulator base built from source"
else
    echo ""
    echo ">> Pulling open_manipulator base from Docker Hub..."
    if ! docker image inspect "${OMX_BASE_IMAGE}" >/dev/null 2>&1; then
        docker pull "${OMX_BASE_IMAGE}"
    fi
    echo "   OK: ${OMX_BASE_IMAGE} exists"
fi

# ── Image 4: open_manipulator thin layer (entrypoint + identify_arm) ──
echo ""
echo ">> Building open_manipulator thin layer..."
docker build \
    -t "${REGISTRY}/open-manipulator:latest" \
    -f "${SCRIPT_DIR}/open_manipulator/Dockerfile" \
    "${SCRIPT_DIR}/open_manipulator/"
echo "   OK: open-manipulator built"

# Cloud training image (formerly robotis-ai-training on Docker Hub) is now
# owned by Modal — see robotis_ai_setup/modal_training/. Deploy with:
#     modal deploy robotis_ai_setup/modal_training/modal_app.py

# ── Push all images ──
# `set -e` at the top already makes any single failed push abort the script,
# but we log per-image and emit a clear summary to avoid students pulling a
# half-updated image set (where e.g. the React bundle is new but the server
# still has last week's overlays).
echo ""
echo ">> Pushing images to ${REGISTRY}..."
pushed=()
for img in physical-ai-manager physical-ai-server open-manipulator; do
    if docker push "${REGISTRY}/${img}:latest"; then
        pushed+=("$img")
        echo "   Pushed: $img"
    else
        echo ""
        echo "ERROR: push of ${REGISTRY}/${img}:latest failed."
        echo "Pushed so far: ${pushed[*]:-none}"
        echo "You now have a mismatched image set in the registry."
        echo "Fix the registry/auth issue and re-run this script."
        exit 1
    fi
done

echo ""
echo "========================================"
echo "All images built and pushed!"
echo ""
echo "Images:"
echo "  ${REGISTRY}/open-manipulator:latest"
echo "  ${REGISTRY}/physical-ai-server:latest"
echo "  ${REGISTRY}/physical-ai-manager:latest"
echo ""
echo "Cloud training image is managed by Modal (no Docker Hub push)."
echo "========================================"
