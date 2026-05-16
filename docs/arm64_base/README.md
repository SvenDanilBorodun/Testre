# arm64 base images — one-time build for the classroom Jetson

The classroom Jetson Orin Nano runs the same Docker stack as a student PC,
but on `linux/arm64` instead of `linux/amd64`. ROBOTIS publishes only amd64
variants of their base images, so we maintain two arm64 base images of our
own:

| Our tag | Upstream Dockerfile | One-time build cost |
|---|---|---|
| `nettername/physical-ai-server-base:arm64-0.8.2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64` | ~30-40 min on QEMU, ~10 min on native arm64 |
| `nettername/open-manipulator-base:arm64-4.1.4` | `open_manipulator/docker/Dockerfile` | ~30-40 min on QEMU, ~10 min on native arm64 |

Both upstream Dockerfiles' base layers (`robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0`,
`ros:jazzy-ros-base`, `robotis/ros:jazzy-ros-base-librealsense`) are multi-arch,
so the build "just works" with `docker buildx --platform linux/arm64`. No
source-code changes are needed in the upstream Dockerfiles themselves.

## When you need to rebuild

Only when one of these is bumped:
- LeRobot pinning (CLAUDE.md §1.5 — 5-place change)
- ROS Jazzy major version
- The pinned ROBOTIS-GIT commits inside `open_manipulator/docker/Dockerfile`
- A torch/cuda revision in the physical_ai_server base

In normal day-to-day work (overlay edits, patches, new Cloud API endpoints,
React tab changes) you do NOT rebuild the bases — `build-images.sh PLATFORM=arm64`
pulls the pre-built bases from the registry and only rebuilds the thin overlay
layers.

## Prerequisites

- `docker buildx` available (Docker Desktop on macOS / Linux Docker Engine)
- Logged into the Docker Hub registry: `docker login`
- For QEMU emulation on a Mac (the common case):
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install arm64
  ```
- For native arm64 builds: a Linux box with ARM CPUs (Jetson Orin itself works,
  or any cloud arm64 instance — AWS Graviton, Hetzner ARM, Scaleway A1).

## One-time build commands

Run these from the repo root:

```bash
# 1. physical_ai_server arm64 base — uses upstream Dockerfile.arm64.
# NOTE: build context is physical_ai_tools/ (NOT physical_ai_server/) —
# the Dockerfile COPYs from `docker/s6-agent/` and `docker/s6-services/`
# which live one directory UP from the Dockerfile.
docker buildx build \
    --platform linux/arm64 \
    --push \
    -t nettername/physical-ai-server-base:arm64-0.8.2 \
    -f physical_ai_tools/physical_ai_server/Dockerfile.arm64 \
    physical_ai_tools/

# 2. open_manipulator arm64 base — uses upstream open_manipulator/docker/Dockerfile
docker buildx build \
    --platform linux/arm64 \
    --push \
    -t nettername/open-manipulator-base:arm64-4.1.4 \
    -f open_manipulator/docker/Dockerfile \
    open_manipulator/docker/
```

Each push takes 5-20 GB of registry bandwidth. The Dockerfiles use HF +
PyPI mirrors aggressively so the builds are reproducible — but they are
also CUDA + torch + LeRobot, which is what makes them large.

## Or use the helper in build-images.sh

```bash
BUILD_BASE_ARM64=1 PLATFORM=arm64 ./robotis_ai_setup/docker/build-images.sh
```

This invokes the same `docker buildx build --platform linux/arm64 --push`
commands above, then continues into the thin-overlay build for both images,
and exits when everything is pushed.

## Verification

After build:

```bash
# Confirm the manifest is linux/arm64
docker buildx imagetools inspect nettername/physical-ai-server-base:arm64-0.8.2
docker buildx imagetools inspect nettername/open-manipulator-base:arm64-4.1.4

# Both outputs should show a single `Platform: linux/arm64` entry (plus
# the standard `unknown/unknown` attestation manifest from buildx).
```

## What's NOT arm64-built

- `physical_ai_manager` (the React app). The Jetson does NOT run the manager —
  the student's browser on their school PC (amd64 Windows) reaches the Jetson's
  rosbridge proxy directly. Therefore `build-images.sh PLATFORM=arm64` skips
  the manager build entirely.
- Modal training/vision images. Those are managed by `modal deploy` and live
  outside Docker Hub.

## Why not multi-arch single tags?

We tag separately (`:latest` for amd64, `:arm64-latest` for arm64) instead
of producing a single multi-arch manifest because:
- Multi-arch single tags require BOTH archs to be pushed in lockstep — a
  forgot-to-rebuild-arm64 ship would silently keep the old arm64 manifest.
- The Jetson agent docker-compose pulls explicit `:arm64-latest`, so confusion
  about "which arch did I just pull?" is eliminated.
- Existing student PCs continue to pull `:latest` unmodified.

If we ever need multi-arch single tags in the future, the right tool is
`docker manifest create + push` or buildx with `--platform linux/amd64,linux/arm64`.
