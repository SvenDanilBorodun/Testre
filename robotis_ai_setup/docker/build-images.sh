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
#
# ── --no-cache --pull on every buildx invocation ──
# Every `docker buildx build` below carries `--no-cache --pull`. This enforces
# the CLAUDE.md "Release & Deployment Pipeline" mandate: images must always
# contain EVERYTHING from the current working tree, never reuse a stale
# BuildKit cache layer, and always re-fetch the base image from the registry
# (so a locally-stale ROBOTIS base can't slip into the published image).
# The cost is ~3-5 extra minutes per build; the win is that every push is
# bit-for-bit reproducible from the current source state + the current
# registry-side base, with zero risk of cache-poisoned layers.

set -euo pipefail

REGISTRY=${REGISTRY:-nettername}
BUILD_BASE=${BUILD_BASE:-0}
# Platform selector: amd64 (default, current behaviour) or arm64 (classroom
# Jetson). Multi-arch (PLATFORM=both) deliberately not in v1 — invoke the
# script twice if you need both. See docs/arm64_base/README.md for the
# one-time arm64 base-image build steps.
PLATFORM=${PLATFORM:-amd64}
# When PLATFORM=arm64, pull our self-built arm64 bases instead of building
# them locally. Set BUILD_BASE_ARM64=1 to (re)build them — takes ~30-40 min
# each via QEMU on a Mac, much faster on a real arm64 host.
BUILD_BASE_ARM64=${BUILD_BASE_ARM64:-0}

case "$PLATFORM" in
    amd64)
        OMX_BASE_IMAGE="robotis/open-manipulator:amd64-4.1.4"
        # Must match the FROM tag in physical_ai_server/Dockerfile. Previously
        # this script pulled ":latest" independently, so the Dockerfile build
        # and the pre-pull could reference different content.
        PAS_BASE_IMAGE="robotis/physical-ai-server:amd64-0.8.2"
        # amd64 target tag suffix on the existing repos
        # (nettername/open-manipulator, nettername/physical-ai-server,
        # nettername/physical-ai-manager).
        IMAGE_TAG_SUFFIX="latest"
        # Output repo names for THIN-LAYER images (the things this script
        # publishes — bases above are immutable inputs). amd64 keeps the
        # legacy names so students' auto-pull keeps working.
        OMX_OUT_REPO="${REGISTRY}/open-manipulator"
        PAS_OUT_REPO="${REGISTRY}/physical-ai-server"
        DOCKER_BUILDX_ARGS=""
        ;;
    arm64)
        # v2.3.0 follow-up: separate repos for the Jetson arm64 images
        # so the "dont touch our EduBotics images" rule is honoured —
        # the classroom Jetson stack publishes to ${REGISTRY}/*-jetson*
        # while the student amd64 stack keeps publishing to
        # ${REGISTRY}/* (no -jetson suffix). Bumping the Jetson image
        # set can no longer regress amd64 students, and a docker push
        # to one set can never collide with the other.
        OMX_BASE_IMAGE="${REGISTRY}/open-manipulator-jetson-base:4.1.4"
        PAS_BASE_IMAGE="${REGISTRY}/physical-ai-server-jetson-base:0.8.2"
        # Single canonical tag — the repo name already indicates arm64/Jetson.
        IMAGE_TAG_SUFFIX="latest"
        # Output repo names for the thin layer Jetson images.
        OMX_OUT_REPO="${REGISTRY}/open-manipulator-jetson"
        PAS_OUT_REPO="${REGISTRY}/physical-ai-server-jetson"
        # buildx with --push bypasses Docker Desktop's dual-image-store gotcha
        # (CLAUDE.md §13.4.bis). On a Linux maintainer host with native arm64
        # this is also the only sane way to push the right manifest digests.
        DOCKER_BUILDX_ARGS="--platform linux/arm64 --push"
        ;;
    *)
        echo "ERROR: PLATFORM='${PLATFORM}' — expected 'amd64' or 'arm64'." >&2
        exit 1
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# OCI provenance — every buildx invocation below carries these labels so a
# live container can be traced back to a git commit without consulting the
# registry tag (which Railway / docker-publish re-tag on every deploy).
# BUILD_ID picks up the GHA SHA when running in CI; falls back to git HEAD.
_PROV_REV="${BUILD_ID:-$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}"
_PROV_CREATED="$(date -u +%FT%TZ)"
_PROV_SOURCE="https://github.com/SvenDanilBorodun/Testre"
OCI_LABELS=(
    --label "org.opencontainers.image.revision=${_PROV_REV}"
    --label "org.opencontainers.image.created=${_PROV_CREATED}"
    --label "org.opencontainers.image.source=${_PROV_SOURCE}"
)
# SOURCE_DATE_EPOCH backs the reproducible-builds claim set in docker-publish.yml.
# Honour an upstream value if set, else derive from the most recent commit time.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$PROJECT_ROOT" log -1 --pretty=%ct 2>/dev/null || echo 0)}"

# Expect repos to be siblings of robotis_ai_setup
OPEN_MANIPULATOR_DIR="${OPEN_MANIPULATOR_DIR:-$(dirname "$PROJECT_ROOT")/open_manipulator}"
PHYSICAL_AI_TOOLS_DIR="${PHYSICAL_AI_TOOLS_DIR:-$(dirname "$PROJECT_ROOT")/physical_ai_tools}"

echo "========================================"
echo "ROBOTIS AI — Docker Image Builder"
echo "Platform: ${PLATFORM}"
echo "Registry: ${REGISTRY}"
echo "Image tag suffix: ${IMAGE_TAG_SUFFIX}"
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

# ── Cleanup trap (registered BEFORE either platform branch) ──
# Both PLATFORM=amd64 (manager build + interfaces staging) and
# PLATFORM=arm64 (interfaces staging only) write transient files into
# the working tree. The trap MUST run on EITHER path — registering it
# inside the amd64 conditional, like the previous revision did, leaked
# physical_ai_server/interfaces/ into the source tree on every arm64
# build. Each cleanup branch is no-op when its variable is empty.
COCO_SNAPSHOT=""
INTERFACES_STAGING=""
PKG_STAGING=""
_build_cleanup() {
    if [ -n "${COCO_SNAPSHOT:-}" ]; then
        rm -f "${COCO_SNAPSHOT}"
    fi
    if [ -n "${INTERFACES_STAGING:-}" ]; then
        rm -rf "${INTERFACES_STAGING}"
    fi
    if [ -n "${PKG_STAGING:-}" ]; then
        rm -rf "${PKG_STAGING}"
    fi
}
trap _build_cleanup EXIT

# ── Image 1: physical_ai_manager (React + nginx) ──
# The React app is amd64-only by design: the student PC is always amd64
# Windows, and the Jetson does NOT run the manager (React stays on the
# student PC and connects to the Jetson rosbridge via the proxy). So
# arm64 builds skip this image entirely.
if [ "$PLATFORM" = "arm64" ]; then
    echo ""
    echo ">> PLATFORM=arm64 — skipping physical-ai-manager build (manager stays on amd64 student PCs)"
fi
if [ "$PLATFORM" = "amd64" ]; then
echo ""
echo ">> Building physical_ai_manager..."
# Cloud training env vars are baked into the React build. These MUST be set —
# without them the React bundle ships with `process.env.REACT_APP_SUPABASE_URL
# === undefined`, supabaseClient.js calls createClient(undefined, undefined),
# the supabase library throws "supabaseUrl is required" at module load (before
# React mounts), and every student gets a hard white screen with the error
# only visible in DevTools. This silently broke 4 published image tags
# (latest, rs-v1.1, roboter-studio-v1, 20260423-…) before we caught it.
# `${VAR:?msg}` aborts the script with the message if VAR is unset OR empty.
: "${SUPABASE_URL:?ERROR: export SUPABASE_URL before running this script (the Supabase project URL — e.g. https://xxx.supabase.co)}"
: "${SUPABASE_ANON_KEY:?ERROR: export SUPABASE_ANON_KEY before running this script (the project anon/publishable key)}"
: "${CLOUD_API_URL:?ERROR: export CLOUD_API_URL before running this script (the Railway-deployed FastAPI URL — e.g. https://xxx.up.railway.app)}"
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
# All three secret vars are required (verified above), so the conditional
# gate is gone — pass them unconditionally. The previous "if -n" gate is
# precisely how the white-screen bug shipped: a missing var caused the
# --build-arg to be omitted, the React bundle inlined undefined, and
# createClient threw at module load. Never gate these silently again.
BUILD_ARGS="--build-arg REACT_APP_SUPABASE_URL=${SUPABASE_URL}"
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY}"
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_CLOUD_API_URL=${CLOUD_API_URL}"
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_ALLOWED_POLICIES=${ALLOWED_POLICIES}"
BUILD_ARGS="$BUILD_ARGS --build-arg REACT_APP_BUILD_ID=${BUILD_ID}"

# Stage server-side coco_classes.py into the manager build context so
# the prebuild Jest hook (objectClasses.sync.test.js) can validate
# that the React dropdown matches the server allowlist. Without this
# the test would fail on ENOENT because physical_ai_server/ is a
# sibling repo, not part of physical_ai_manager/. The trap registered
# above the platform conditional ensures this file gets cleaned up on
# every exit path.
COCO_SNAPSHOT="${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/_coco_classes.py"
cp "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_server/physical_ai_server/workflow/coco_classes.py" \
   "${COCO_SNAPSHOT}"

# CLAUDE.md §13.4.bis: build to local daemon first with buildx --load so
# the post-build smoke grep can `docker run` against the image. A separate
# `docker push` follows once the smoke check passes. buildx --load writes
# into the daemon store (vs containerd-snapshotter) so the subsequent
# push reads the same content we just smoke-tested.
docker buildx build --platform linux/amd64 --load --no-cache --pull \
    "${OCI_LABELS[@]}" \
    $BUILD_ARGS \
    -t "${REGISTRY}/physical-ai-manager:latest" \
    -f "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/Dockerfile" \
    "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_manager/"

# Post-build smoke test: inspect the built React bundle and prove the
# Supabase + Cloud API URLs were actually inlined by Webpack. Without
# this check, an upstream regression in CRA env handling (or an
# accidentally-deleted ENV-promote line in the Dockerfile, etc.) would
# silently produce a broken image again — exactly how the white-screen
# bug went unnoticed across 4 previously-published tags.
#
# We grep for the literal URL string we passed in. If the bundler inlined
# `process.env.REACT_APP_SUPABASE_URL`, the URL is a string literal in the
# built JS. If it wasn't inlined, the only thing in the bundle is the
# library's defensive throw "supabaseUrl is required" — which is what
# the broken images showed.
#
# Path-agnostic: recursively grep the whole nginx html root rather than a
# fixed bundle filename. CRA emitted static/js/main.*.js; Vite (v2.6.x
# migration) emits static/js/index-*.js. `grep -r` on the html root means a
# future bundler/output rename never silently breaks this guard again.
echo "   Verifying secrets reached the bundle..."
_smoke_image="${REGISTRY}/physical-ai-manager:latest"
if ! docker run --rm --platform linux/amd64 --entrypoint sh "$_smoke_image" -c \
        "grep -q -r -F '${SUPABASE_URL}' /usr/share/nginx/html"; then
    echo ""
    echo "ERROR: SUPABASE_URL not found in the built bundle."
    echo "       The React build did NOT inline process.env.REACT_APP_SUPABASE_URL."
    echo "       Pushing this image would white-screen every student."
    echo "       Aborting before docker push."
    exit 1
fi
if ! docker run --rm --platform linux/amd64 --entrypoint sh "$_smoke_image" -c \
        "grep -q -r -F '${CLOUD_API_URL}' /usr/share/nginx/html"; then
    echo ""
    echo "ERROR: CLOUD_API_URL not found in the built bundle. Aborting."
    exit 1
fi
echo "   OK: physical-ai-manager built (smoke-tested)"
fi  # end PLATFORM=amd64 manager block

# ── Image 2a: physical_ai_server base ──
# amd64: ROBOTIS publishes the base on Docker Hub → just pull.
# arm64: ROBOTIS does NOT publish an arm64 variant. Either pull our own
#        previously-built nettername/physical-ai-server-base:arm64-0.8.2,
#        or build it now from physical_ai_tools/physical_ai_server/
#        Dockerfile.arm64 (~30-40 min on QEMU, faster on native arm64).
if [ "$PLATFORM" = "arm64" ] && [ "$BUILD_BASE_ARM64" = "1" ]; then
    echo ""
    echo ">> Building physical_ai_server arm64 base ${PAS_BASE_IMAGE} from upstream sources..."
    # CONTEXT MUST be physical_ai_tools/ (NOT physical_ai_server/).
    # The Dockerfile.arm64 COPYs from `docker/s6-agent/` and
    # `docker/s6-services/` which live one directory UP from the
    # Dockerfile itself. A context of physical_ai_server/ produces
    # "/docker/s6-services/common/ros2_service_finish.sh: not found".
    docker buildx build $DOCKER_BUILDX_ARGS --no-cache --pull \
        "${OCI_LABELS[@]}" \
        -t "${PAS_BASE_IMAGE}" \
        -f "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_server/Dockerfile.arm64" \
        "${PHYSICAL_AI_TOOLS_DIR}/"
    echo "   OK: ${PAS_BASE_IMAGE} built + pushed"
else
    echo ""
    echo ">> Resolving physical_ai_server base ${PAS_BASE_IMAGE}..."
    # docker manifest inspect queries the registry directly — works for
    # both amd64 (ROBOTIS Docker Hub) and arm64 (our registry) even when
    # the local daemon has no matching manifest cached.
    if ! docker manifest inspect "${PAS_BASE_IMAGE}" >/dev/null 2>&1; then
        echo "ERROR: ${PAS_BASE_IMAGE} not found in registry."
        if [ "$PLATFORM" = "arm64" ]; then
            echo "       Run BUILD_BASE_ARM64=1 PLATFORM=arm64 ${0##*/} to build + push it."
            echo "       See docs/arm64_base/README.md for the one-time setup."
        fi
        exit 1
    fi
    echo "   OK: ${PAS_BASE_IMAGE} resolves in registry"
fi

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
# ${COCO_SNAPSHOT}, ${INTERFACES_STAGING} and ${PKG_STAGING} on exit. We must
# NOT overwrite the trap here — that's the v1 bug — so this is a no-op.

# Stage the EduBotics physical_ai_server ROS package (the single source of
# truth) into the build context so the Dockerfile can COPY it wholesale over
# the upstream-cloned package. This replaces the historical apply_overlay model
# (which silently dropped any edit/deletion not in the hand-listed chain — the
# v2.5.0 training_manager.py crash). Excludes test/, the per-arch Dockerfiles,
# CHANGELOG.rst and __pycache__/*.pyc (build inputs / dev-only, not runtime).
# "Image == repo HEAD" is then true by construction; CI's image-source-parity
# job re-asserts byte equality after every build.
PKG_STAGING="${SCRIPT_DIR}/physical_ai_server/pkg_src"
echo ""
echo ">> Staging physical_ai_server package (COPY-wholesale source of truth)..."
rm -rf "${PKG_STAGING}"
mkdir -p "${PKG_STAGING}"
cp -a "${PHYSICAL_AI_TOOLS_DIR}/physical_ai_server/." "${PKG_STAGING}/"
rm -rf "${PKG_STAGING}/test" \
       "${PKG_STAGING}/Dockerfile.amd64" \
       "${PKG_STAGING}/Dockerfile.arm64" \
       "${PKG_STAGING}/CHANGELOG.rst"
find "${PKG_STAGING}" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "${PKG_STAGING}" -name '*.pyc' -delete 2>/dev/null || true
# Belt: the stripped safety guard (Rule §2) must never reach the staged tree.
if [ -e "${PKG_STAGING}/physical_ai_server/workflow/safety_envelope.py" ]; then
    echo "ERROR: safety_envelope.py present in staged package — stripped guard must not ship (Rule §2)" >&2
    exit 1
fi

echo ""
echo ">> Building physical_ai_server thin layer (patches + interface rebuild)..."
# Pass BASE_IMAGE so the parameterised FROM resolves to the right
# architecture (arm64 uses our self-built base; amd64 uses the ROBOTIS
# base via the Dockerfile default). For arm64, the buildx --push flag
# bypasses the Docker Desktop dual-store gotcha (CLAUDE.md §13.4.bis).
if [ "$PLATFORM" = "arm64" ]; then
    docker buildx build $DOCKER_BUILDX_ARGS --no-cache --pull \
        "${OCI_LABELS[@]}" \
        --build-arg BASE_IMAGE="${PAS_BASE_IMAGE}" \
        -t "${PAS_OUT_REPO}:${IMAGE_TAG_SUFFIX}" \
        -f "${SCRIPT_DIR}/physical_ai_server/Dockerfile" \
        "${SCRIPT_DIR}/physical_ai_server/"
else
    # CLAUDE.md §13.4.bis: build to local daemon first (--load), then push
    # in the per-image loop below. This bypasses Docker Desktop's dual-
    # image-store gotcha where `docker build` writes to containerd-
    # snapshotter and a subsequent `docker push` reads from the classic
    # daemon store (potentially uploading a stale `:latest`). With
    # --load, both the build output and the push source are the daemon
    # store. --platform linux/amd64 is still needed on Apple Silicon
    # hosts where the default builder would target arm64; harmless on
    # native Linux/amd64.
    docker buildx build --platform linux/amd64 --load --no-cache --pull \
        "${OCI_LABELS[@]}" \
        --build-arg BASE_IMAGE="${PAS_BASE_IMAGE}" \
        -t "${PAS_OUT_REPO}:${IMAGE_TAG_SUFFIX}" \
        -f "${SCRIPT_DIR}/physical_ai_server/Dockerfile" \
        "${SCRIPT_DIR}/physical_ai_server/"
fi
echo "   OK: physical-ai-server built (with patches)"

# ── Image 3: open_manipulator base ──
# amd64: ROBOTIS publishes amd64-4.1.4; pull from Docker Hub by default,
#        or set BUILD_BASE=1 to rebuild from upstream source (~40 min).
# arm64: ROBOTIS does NOT publish arm64. Either pull our previously-
#        built nettername/open-manipulator-base:arm64-4.1.4, or set
#        BUILD_BASE_ARM64=1 to build it now from upstream sources.
if [ "$PLATFORM" = "arm64" ] && [ "$BUILD_BASE_ARM64" = "1" ]; then
    echo ""
    echo ">> Building open_manipulator arm64 base ${OMX_BASE_IMAGE} from upstream sources..."
    docker buildx build $DOCKER_BUILDX_ARGS --no-cache --pull \
        "${OCI_LABELS[@]}" \
        -t "${OMX_BASE_IMAGE}" \
        -f "${OPEN_MANIPULATOR_DIR}/docker/Dockerfile" \
        "${OPEN_MANIPULATOR_DIR}/docker/"
    echo "   OK: ${OMX_BASE_IMAGE} built + pushed"
elif [ "$PLATFORM" = "amd64" ] && [ "$BUILD_BASE" = "1" ]; then
    echo ""
    echo ">> Building open_manipulator amd64 base from source (this takes ~40 min)..."
    # CLAUDE.md §13.4.bis: --load so subsequent `docker push` (if any)
    # reads from the daemon store rather than the containerd snapshotter.
    docker buildx build --platform linux/amd64 --load --no-cache --pull \
        "${OCI_LABELS[@]}" \
        -t "${OMX_BASE_IMAGE}" \
        -f "${OPEN_MANIPULATOR_DIR}/docker/Dockerfile" \
        "${OPEN_MANIPULATOR_DIR}/docker/"
    echo "   OK: open-manipulator base built from source"
else
    echo ""
    echo ">> Resolving open_manipulator base ${OMX_BASE_IMAGE}..."
    if ! docker manifest inspect "${OMX_BASE_IMAGE}" >/dev/null 2>&1; then
        echo "ERROR: ${OMX_BASE_IMAGE} not found in registry."
        if [ "$PLATFORM" = "arm64" ]; then
            echo "       Run BUILD_BASE_ARM64=1 PLATFORM=arm64 ${0##*/} to build + push it."
            echo "       See docs/arm64_base/README.md for the one-time setup."
        fi
        exit 1
    fi
    echo "   OK: ${OMX_BASE_IMAGE} resolves in registry"
fi

# ── Image 4: open_manipulator thin layer (entrypoint + identify_arm) ──
echo ""
echo ">> Building open_manipulator thin layer..."
if [ "$PLATFORM" = "arm64" ]; then
    docker buildx build $DOCKER_BUILDX_ARGS --no-cache --pull \
        "${OCI_LABELS[@]}" \
        --build-arg BASE_IMAGE="${OMX_BASE_IMAGE}" \
        -t "${OMX_OUT_REPO}:${IMAGE_TAG_SUFFIX}" \
        -f "${SCRIPT_DIR}/open_manipulator/Dockerfile" \
        "${SCRIPT_DIR}/open_manipulator/"
else
    # CLAUDE.md §13.4.bis: --load builds into the local daemon store so
    # the subsequent `docker push` below uploads the exact bits we just
    # built (avoiding the Docker Desktop dual-store gotcha where
    # `docker build` writes containerd and `docker push` reads daemon).
    # --platform linux/amd64 is still needed on Apple Silicon hosts.
    docker buildx build --platform linux/amd64 --load --no-cache --pull \
        "${OCI_LABELS[@]}" \
        --build-arg BASE_IMAGE="${OMX_BASE_IMAGE}" \
        -t "${OMX_OUT_REPO}:${IMAGE_TAG_SUFFIX}" \
        -f "${SCRIPT_DIR}/open_manipulator/Dockerfile" \
        "${SCRIPT_DIR}/open_manipulator/"
fi
echo "   OK: open-manipulator built"

# Cloud training image (formerly robotis-ai-training on Docker Hub) is now
# owned by Modal — see robotis_ai_setup/modal_training/. Deploy with:
#     modal deploy robotis_ai_setup/modal_training/modal_app.py

# ── Push all images ──
# `set -e` at the top already makes any single failed push abort the script,
# but we log per-image and emit a clear summary to avoid students pulling a
# half-updated image set (where e.g. the React bundle is new but the server
# still has last week's overlays).
#
# For PLATFORM=arm64, buildx --push already pushed each image as part of
# the build step, so this loop is a no-op. We still emit the summary.
if [ "$PLATFORM" = "amd64" ]; then
    echo ""
    echo ">> Pushing images to ${REGISTRY}..."
    pushed=()
    # amd64 keeps the legacy ${REGISTRY}/${img} naming.
    for img in physical-ai-manager physical-ai-server open-manipulator; do
        if docker push "${REGISTRY}/${img}:${IMAGE_TAG_SUFFIX}"; then
            pushed+=("$img")
            echo "   Pushed: $img"
        else
            echo ""
            echo "ERROR: push of ${REGISTRY}/${img}:${IMAGE_TAG_SUFFIX} failed."
            echo "Pushed so far: ${pushed[*]:-none}"
            echo "You now have a mismatched image set in the registry."
            echo "Fix the registry/auth issue and re-run this script."
            exit 1
        fi
    done
else
    echo ""
    echo ">> PLATFORM=arm64 — buildx --push already pushed all images."
fi

echo ""
echo "========================================"
echo "All images built and pushed!"
echo ""
echo "Images:"
echo "  ${OMX_OUT_REPO}:${IMAGE_TAG_SUFFIX}"
echo "  ${PAS_OUT_REPO}:${IMAGE_TAG_SUFFIX}"
if [ "$PLATFORM" = "amd64" ]; then
    echo "  ${REGISTRY}/physical-ai-manager:${IMAGE_TAG_SUFFIX}"
fi
echo ""
echo "Platform: ${PLATFORM}"
echo "Cloud training image is managed by Modal (no Docker Hub push)."
echo "========================================"
