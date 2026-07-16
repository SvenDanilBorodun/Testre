# Base Image Pinning & Overlay Safety

## Current pins

### Upstream bases we PULL (thin-overlay `ARG BASE_IMAGE` defaults)

| Image | Pinned to | Last upstream version we tested |
|-------|-----------|----------------------------------|
| `physical_ai_server/Dockerfile` | `robotis/physical-ai-server:amd64-0.8.2` | upstream now publishes `0.8.3` (2026-04-30) — bump on next test cycle |
| `open_manipulator/Dockerfile` | `robotis/open-manipulator:amd64-4.1.4` | latest amd64 tag as of 2026-03-18 |
| Modal training worker | `nvidia/cuda:12.1.1-devel-ubuntu22.04` (in `modal_app.py`) | Modal owns this image now; previously was `nettername/robotis-ai-training` |

### Bases we BUILD OURSELVES (pinned in `build-images.sh`, **not built by CI**)

These three were missing from this table entirely, which is backwards: they are
the pins most likely to be wrong, because **no workflow builds them**. A
maintainer hand-runs `BUILD_BASE_*=1 ./build-images.sh` once and pushes; every
CI build thereafter only *resolves* the tag below via `docker manifest inspect`.
Edit the source Dockerfile without bumping the tag and the release ships the
OLD base, green and silent (`ci.yml::base-version-guard` is the fence).

| Base image (tag pinned in `build-images.sh`) | Built from | Trigger |
|---|---|---|
| `${REGISTRY}/physical-ai-server-opi-base:0.8.2-opi2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu` | `BUILD_BASE_OPI=1 PLATFORM=opi` |
| `${REGISTRY}/physical-ai-server-jetson-base:0.8.2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64` | `BUILD_BASE_ARM64=1 PLATFORM=arm64` |
| `${REGISTRY}/open-manipulator-jetson-base:4.1.4` | `open_manipulator/docker/Dockerfile` | `BUILD_BASE_ARM64=1 PLATFORM=arm64` (opi REUSES this one — it is CUDA/L4T-free) |

### The upstream `FROM`s those self-built bases are cut from

| Base Dockerfile | `FROM` |
|---|---|
| `Dockerfile.amd64` | `robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` |
| `Dockerfile.arm64` | `robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` |
| `Dockerfile.arm64cpu` | `ros:jazzy-ros-base` (stock, Docker Official) |

`./bump-upstream-digests.sh` reports every row above (it parses the tags out of
`build-images.sh` and the `FROM`s out of the Dockerfiles, so it cannot drift
from what actually builds) and exits non-zero if any fails to resolve.

## Why pinning matters

`:latest` is a mutable tag. ROBOTIS can retag it at any time to point to a
newer image with a different ROS 2 distro, different LeRobot version, different
Python version, or different file paths. If that happens, our build would
silently pick up incompatible code without any warning.

`:amd64-X.Y.Z` is an immutable version tag. ROBOTIS will not retag an old
version number. When they release a new version, `:latest` moves but
`:amd64-X.Y.Z` stays put.

## How to upgrade a PULLED upstream base

When you intentionally want to use a newer ROBOTIS base image:

1. Check what's available on Docker Hub: `robotis/physical-ai-server`
2. Pull and test locally: `docker pull robotis/physical-ai-server:amd64-X.Y.Z`
3. Update the Dockerfile: `FROM robotis/physical-ai-server:amd64-X.Y.Z`
4. Push to a branch — `.github/workflows/docker-publish.yml`'s
   `base-digest-check` job will surface the drift; CI builds the new image.
5. Test the full pipeline (recording → training → inference) against the
   PR-build image before merging.
6. If everything works, merge and let CI publish.

## How to change a base we BUILD (`Dockerfile.amd64` / `.arm64` / `.arm64cpu`)

**Step 4 above does not apply here — "CI builds the new image" is FALSE for
these three.** Grep `.github/` for `BUILD_BASE_OPI` and you find nothing. If you
only push the source edit, CI resolves the previously-published tag, the build
succeeds, the smoke tests pass, and your change ships nowhere.

1. Edit the base Dockerfile.
2. Rebuild + push it from a workstation — the only path that exists:
   ```bash
   cd robotis_ai_setup/docker
   export SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=...   # required: the manager builds first
   BUILD_BASE_OPI=1   PLATFORM=opi   ./build-images.sh   # Dockerfile.arm64cpu
   BUILD_BASE_ARM64=1 PLATFORM=arm64 ./build-images.sh   # Dockerfile.arm64 (+ the OMX Jetson base)
   ```
3. Bump the matching `PAS_BASE_IMAGE` tag in `build-images.sh` **in the same
   commit** — `ci.yml::base-version-guard` enforces exactly this (comment-only
   edits to the base Dockerfile are exempt).
4. `./bump-upstream-digests.sh` to confirm the new tag actually resolves. The
   guard can only check that you bumped the tag; it cannot check that you
   pushed the image.

Note `Dockerfile.amd64` has no self-built base — its output is the ROBOTIS
pull. It is forward-compat scaffolding; keep its pins in lockstep anyway
(Rule §5, enforced by `tests/test_lerobot_pin_lockstep.py`).

The `bump-upstream-digests.sh` script (already in this directory) is wired
into `.github/workflows/docker-publish.yml` as a read-only diagnostic that
warns when upstream digests have moved since the last main build.

---

## M14: Overlay find assertions (fail-loud) — HISTORICAL, physical_ai_server no longer overlays

> **This section describes the RETIRED overlay model for physical_ai_server.**
> Kept for the reasoning; do not follow it. Since v2.5.2 the server package is
> **COPY-wholesale** (Rule §3): `build-images.sh` stages
> `physical_ai_tools/physical_ai_server/` as `pkg_src/`, the Dockerfile `rm`s the
> upstream clone and copies it verbatim, then re-`colcon`s. So "image == repo
> HEAD" holds by construction — edits, new files AND deletions all propagate,
> and none of the four files in the table below is overlaid any more.
> `image-source-parity` re-asserts byte-identity after every build.
>
> The fail-loud `find` assertion pattern is still live, but only for
> **open_manipulator**, which remains on the overlay model (it carries C++
> `ros2_control` packages where a full re-`colcon` is heavy/risky). Its chain is
> 7 files — see Rule §3 in `CLAUDE.md` for the current list. The lesson below is
> exactly why the server was converted: the overlay model silently dropped edits
> (the v2.5.0 `training_manager.py` regression → node crash on every rig).

### What changed
Every `find` command in the Dockerfile that locates overlay targets now has
an assertion that fails the build if no targets are found:

```dockerfile
TARGETS=$(find /root/ros2_ws -name "inference_manager.py" -path "*/inference/*") && \
    [ -n "$TARGETS" ] || { echo "ERROR: inference_manager.py not found"; exit 1; } && \
    for f in $TARGETS; do cp /tmp/overlays/inference_manager.py "$f" && echo "Overlaid: $f"; done
```

### Why
Previously, if ROBOTIS renamed or moved a file in their base image, the `find`
would return empty, the `for` loop would silently skip, and the image would
ship with zero overlays applied. Every safety feature we added (camera
validation, RAM warnings, timestamp gap detection, stale camera detection,
image resolution checks) would silently vanish.

With the assertion, the build **fails loudly** with a clear error message:
```
ERROR: inference_manager.py not found in base image — overlay cannot be applied
```

### The 4 overlays protected
| File | Path filter | Purpose |
|------|------------|---------|
| `inference_manager.py` | `*/inference/*` | Camera validation, resolution check, stale detection |
| `data_manager.py` | `*/data_processing/*` | RAM warnings, timestamp gap detection |
| `data_converter.py` | `*/data_processing/*` | Joint safety, trajectory time_from_start |
| `omx_f_config.yaml` | any match | Dual camera config |

### What triggers the assertion
- ROBOTIS renames the file
- ROBOTIS moves it to a different directory
- ROBOTIS removes it entirely
- The path filter no longer matches the new directory structure

In all cases, the build stops and tells you exactly which overlay failed.
