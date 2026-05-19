# Base Image Pinning & Overlay Safety

## Current pins

| Image | Pinned to | Last upstream version we tested |
|-------|-----------|----------------------------------|
| `physical_ai_server/Dockerfile` | `robotis/physical-ai-server:amd64-0.8.2` | upstream now publishes `0.8.3` (2026-04-30) — bump on next test cycle |
| `open_manipulator/Dockerfile` | `robotis/open-manipulator:amd64-4.1.4` | latest amd64 tag as of 2026-03-18 |
| Modal training worker | `nvidia/cuda:12.1.1-devel-ubuntu22.04` (in `modal_app.py`) | Modal owns this image now; previously was `nettername/robotis-ai-training` |

## Why pinning matters

`:latest` is a mutable tag. ROBOTIS can retag it at any time to point to a
newer image with a different ROS 2 distro, different LeRobot version, different
Python version, or different file paths. If that happens, our build would
silently pick up incompatible code without any warning.

`:amd64-X.Y.Z` is an immutable version tag. ROBOTIS will not retag an old
version number. When they release a new version, `:latest` moves but
`:amd64-X.Y.Z` stays put.

## How to upgrade

When you intentionally want to use a newer ROBOTIS base image:

1. Check what's available on Docker Hub: `robotis/physical-ai-server`
2. Pull and test locally: `docker pull robotis/physical-ai-server:amd64-X.Y.Z`
3. Update the Dockerfile: `FROM robotis/physical-ai-server:amd64-X.Y.Z`
4. Push to a branch — `.github/workflows/docker-publish.yml`'s
   `base-digest-check` job will surface the drift; CI builds the new image.
5. Test the full pipeline (recording → training → inference) against the
   PR-build image before merging.
6. If everything works, merge and let CI publish.

The `bump-upstream-digests.sh` script (already in this directory) is wired
into `.github/workflows/docker-publish.yml` as a read-only diagnostic that
warns when upstream digests have moved since the last main build.

---

## M14: Overlay find assertions (fail-loud)

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
