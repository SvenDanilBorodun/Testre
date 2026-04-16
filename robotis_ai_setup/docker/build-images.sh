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
docker build \
    $BUILD_ARGS \
    -t "${REGISTRY}/physical-ai-manager:latest" \
    -f "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/Dockerfile" \
    "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/"
echo "   OK: physical-ai-manager built"

# ── Image 2a: physical_ai_server base (pull official ROBOTIS image) ──
echo ""
echo ">> Pulling physical_ai_server base from Docker Hub..."
if ! docker image inspect "robotis/physical-ai-server:latest" >/dev/null 2>&1; then
    docker pull "robotis/physical-ai-server:latest"
fi
echo "   OK: robotis/physical-ai-server:latest exists"

# ── Image 2b: physical_ai_server thin layer (patches upstream bugs) ──
echo ""
echo ">> Building physical_ai_server thin layer (patches)..."
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

# ── Image 5: robotis-ai-training (RunPod serverless worker) ──
echo ""
echo ">> Building robotis-ai-training (RunPod serverless worker)..."
docker build \
    -t "${REGISTRY}/robotis-ai-training:latest" \
    -f "${PROJECT_ROOT}/runpod_training/Dockerfile" \
    "${PROJECT_ROOT}/runpod_training/"
echo "   OK: robotis-ai-training built"

# ── Push all images ──
echo ""
echo ">> Pushing images to ${REGISTRY}..."
docker push "${REGISTRY}/physical-ai-manager:latest"
echo "   Pushed: physical-ai-manager"

docker push "${REGISTRY}/physical-ai-server:latest"
echo "   Pushed: physical-ai-server"

docker push "${REGISTRY}/open-manipulator:latest"
echo "   Pushed: open-manipulator"

docker push "${REGISTRY}/robotis-ai-training:latest"
echo "   Pushed: robotis-ai-training"

echo ""
echo "========================================"
echo "All images built and pushed!"
echo ""
echo "Images:"
echo "  ${REGISTRY}/open-manipulator:latest"
echo "  ${REGISTRY}/physical-ai-server:latest"
echo "  ${REGISTRY}/physical-ai-manager:latest"
echo "  ${REGISTRY}/robotis-ai-training:latest   (RunPod serverless — not pulled by students)"
echo "========================================"
