# Base Image Pinning & Overlay Safety

## M13: Base image pinned to version tag

### What changed
`physical_ai_server/Dockerfile` now uses:
```dockerfile
FROM robotis/physical-ai-server:amd64-0.8.2
```
instead of the previous:
```dockerfile
FROM robotis/physical-ai-server:latest
```

### Why
`:latest` is a mutable tag. ROBOTIS can retag it at any time to point to a
newer image with a different ROS2 distro, different LeRobot version, different
Python version, or different file paths. If that happens, our build would
silently pick up incompatible code without any warning.

`:amd64-0.8.2` is an immutable version tag. ROBOTIS will not retag an old
version number. When they release 0.9.0, `:latest` moves but `:amd64-0.8.2`
stays put.

### How to upgrade
When you intentionally want to use a newer ROBOTIS base image:

1. Check what's available: `docker search robotis/physical-ai-server`
2. Pull and test: `docker pull robotis/physical-ai-server:amd64-X.Y.Z`
3. Update the Dockerfile: `FROM robotis/physical-ai-server:amd64-X.Y.Z`
4. Rebuild: `./build-images.sh`
5. Test: run the full pipeline (recording, training, inference)
6. If everything works, commit the Dockerfile change

The `bump-upstream-digests.sh` script can also help by showing the current
digests of upstream images.

### Other base images (for reference)
- `open-manipulator`: already pinned to `robotis/open-manipulator:amd64-4.1.4`
- `robotis-ai-training`: already pinned to `nvidia/cuda:12.1.1-devel-ubuntu22.04`

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
