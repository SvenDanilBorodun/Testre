# arm64 base images — one-time maintainer builds

> **Maintainer-only, run ONCE per base bump.** Students and teachers never
> run any of this. The base images are the immutable inputs that
> `build-images.sh` and CI (`docker-publish.yml`) build the thin production
> layers on top of. Everyday releases only rebuild the *thin* layers — the
> bases below already exist in the registry and are simply resolved by
> `docker manifest inspect`.

EduBotics ships two arm64 device flavors, each with its own base set:

- **Jetson** (`-jetson`) — the classroom inference rig. GPU torch on the
  NVIDIA L4T/CUDA base. See [`docs/JETSON_DEPLOY.md`](../JETSON_DEPLOY.md).
- **Orange Pi 5 Pro** (`-opi`) — the full student stack on a Rockchip
  RK3588S board (**no CUDA — ever**). CPU-only torch on a stock ROS base.
  See [`docs/ORANGE_PI_DEPLOY_PLAN.md`](../ORANGE_PI_DEPLOY_PLAN.md).

## The base image set

All bases live on Docker Hub under the build `REGISTRY` (default
`nettername`; `build-images.sh` reads `${REGISTRY}`). The thin-layer builds
resolve them by `${REGISTRY}/<name>:<tag>` at build time. GHCR is the
student-facing *consumption* primary for the thin layers only — the bases
are build inputs and stay on Docker Hub.

| Base image | Built from | Flavor(s) that consume it | How to (re)build |
|---|---|---|---|
| `${REGISTRY}/open-manipulator-jetson-base:4.1.4` | `open_manipulator/docker/Dockerfile` (`FROM ros:jazzy-ros-base` — **CUDA/L4T-free**) | Jetson **and** Orange Pi | `BUILD_BASE_ARM64=1 PLATFORM=arm64` |
| `${REGISTRY}/physical-ai-server-jetson-base:0.8.2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64` (`FROM robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` — L4T, GPU torch) | Jetson only | `BUILD_BASE_ARM64=1 PLATFORM=arm64` |
| `${REGISTRY}/physical-ai-server-opi-base:0.8.2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu` (`FROM ros:jazzy-ros-base` — stock, plain PyPI, **CPU torch 2.7.0**) | Orange Pi only | `BUILD_BASE_OPI=1 PLATFORM=opi` |

### Why the Orange Pi has no dedicated open_manipulator base

The `open_manipulator` base is `FROM ros:jazzy-ros-base` (stock ROS 2 Jazzy
plus the Dynamixel + C++ `ros2_control` packages) — it carries **no CUDA and
no L4T**. So the Orange Pi flavor **REUSES** the Jetson OMX base
(`open-manipulator-jetson-base:4.1.4`) verbatim. There is no
`open-manipulator-opi-base`, and `PLATFORM=opi` never builds an OMX base —
it only *resolves* the Jetson one. The `-opi` divergence is entirely on the
**server** side, where the Jetson base's CUDA/L4T payload is unusable on
Rockchip, hence the separate CPU base above.

## Prerequisites

```bash
# 1. Docker with buildx (Docker 20.10+; buildx ships with modern Docker).
docker buildx version

# 2. Log in to the build registry (Docker Hub by default). The base builds
#    push here, and the thin layers resolve the bases from here.
docker login          # for REGISTRY=nettername

# 3. Repos cloned side by side (build-images.sh expects siblings of
#    robotis_ai_setup/):
#      open_manipulator/  physical_ai_tools/  robotis_ai_setup/
```

**Where to run it — native arm64 is strongly preferred.** All three base
builds emit `linux/arm64` images. On a native arm64 host (an
`ubuntu-24.04-arm` GitHub runner, an Ampere/Graviton box, or an Apple-Silicon
Linux VM) the build is native and fast. On an amd64 host you must emulate
via QEMU (see below) — correct but ~5-8× slower, with occasional
emulation-only edge cases; treat it as the fallback, not the default.

### QEMU setup (only when cross-building on a non-arm64 host, e.g. an Intel Mac)

```bash
# Register the qemu-user binfmt handlers so buildx can run aarch64 layers.
docker run --privileged --rm tonistiigi/binfmt --install arm64
# Create + use a buildx builder that can target linux/arm64.
docker buildx create --name edubotics-arm --use --bootstrap
```

`build-images.sh` passes `--platform linux/arm64` on every arm64/opi buildx
call, so no further per-command flags are needed.

## Building the Jetson bases (both, one-time)

```bash
cd robotis_ai_setup/docker
# Builds + pushes BOTH the open-manipulator-jetson-base and the
# physical-ai-server-jetson-base, then continues on to build the thin Jetson
# layers on top. ~30-40 min per base under QEMU on a Mac; much faster native.
BUILD_BASE_ARM64=1 PLATFORM=arm64 ./build-images.sh
```

`BUILD_BASE_ARM64=1` forces the two Jetson bases to build from upstream
sources (context = `physical_ai_tools/` for the server base so the
`docker/s6-*` COPYs resolve). Omit it on subsequent releases — the bases are
then resolved via `docker manifest inspect` and only the thin layers
rebuild:

```bash
PLATFORM=arm64 ./build-images.sh    # bases resolved, thin layers rebuilt + pushed
```

## Building the Orange Pi CPU server base (one-time)

```bash
cd robotis_ai_setup/docker
# Builds + pushes physical-ai-server-opi-base:0.8.2 from Dockerfile.arm64cpu
# (--platform linux/arm64 --push; context = physical_ai_tools/), then continues
# on to build the thin opi layers (server + open_manipulator + manager).
BUILD_BASE_OPI=1 PLATFORM=opi ./build-images.sh
```

`Dockerfile.arm64cpu` is a stock `ros:jazzy-ros-base` build with plain PyPI:
it pins `torch==2.7.0` + `torchvision==0.22.0` (the aarch64 PyPI 2.7.0 wheel
is genuinely CPU-only — ~99 MB, zero `nvidia-*` deps) **before** LeRobot so
`lerobot==0.5.1`'s resolve can't drag in torch 2.10.x, then installs
`lerobot[pi,smolvla,peft]==0.5.1` under the same `numpy==1.26.4` /
`scipy>=1.14.0,<1.18` cross-arch floor as the sibling bases (Rule §5 — this
file is the FOURTH LeRobot pin site). It has **no** CUDA or L4T references.

**The Orange Pi OMX base is NOT built here** — `PLATFORM=opi` reuses the
already-built `open-manipulator-jetson-base:4.1.4`. Build that once via the
Jetson path above (`BUILD_BASE_ARM64=1 PLATFORM=arm64`) if it does not yet
exist; `PLATFORM=opi` will otherwise abort with a hint pointing back here.

Subsequent opi releases resolve the base and only rebuild the thin layers:

```bash
PLATFORM=opi ./build-images.sh      # base resolved, thin layers rebuilt + pushed
```

## The flatten / slim story

`docker save` (and `docker push`) ship every layer, including whiteout'd
files hidden behind later `rm`s. Two flavors reclaim those bytes by
FLATTENING the thin server image (`flatten_image` in `build-images.sh` —
`docker export | docker import`, which collapses the layers so whiteouts
become real deletions and re-imports the rootfs under the correct arch
label):

- **amd64**: the thin Dockerfile's `SLIM_CUDA=1` step swaps torch+cu128 →
  torch+cpu and removes `/usr/local/cuda` + `nvidia-*` wheels (~16 GB →
  ~5-6 GB after flatten). The flatten is what makes the removal real.
- **opi**: `SLIM_CUDA=0` (a no-op — the CPU base never had CUDA to strip),
  but the flatten still collapses layer overhead **and, critically,
  re-imports the arm64 rootfs with the right `.Architecture` label**
  (`docker import` stamps arch from the `--platform` value, not the source
  image). Target size **~5-6 GB**, gated at a **~7 GB** ceiling in CI
  (`docker-publish.yml`'s opi-only size step).
- **arm64/Jetson**: NEVER flattened — it deliberately keeps GPU torch and
  the full CUDA/L4T payload.

## Verifying a pushed base (architecture / manifest)

After a base build, confirm the pushed image reports the expected
architecture before building thin layers on it:

```bash
# Should show `Platform: linux/arm64` for every arm64/opi base.
docker buildx imagetools inspect ${REGISTRY}/physical-ai-server-opi-base:0.8.2
docker buildx imagetools inspect ${REGISTRY}/physical-ai-server-jetson-base:0.8.2
docker buildx imagetools inspect ${REGISTRY}/open-manipulator-jetson-base:4.1.4
```

The thin-layer builds and CI (`docker-publish.yml::smoke-test`) also assert
`uname -m == aarch64` and run `image_source_parity.sh` on the shipped
images, so an arch or source-drift mistake surfaces loudly at publish time
rather than on a bench Pi/Jetson.

## After the bases exist

Everyday releases go through GitHub Actions (`docker-publish.yml`, W4 in the
golden order) — the `build`/`retag`/`smoke-test` matrix now carries three
flavors (`amd64`, `arm64`, `opi`) and dual-pushes each thin image to GHCR
(primary) + Docker Hub (fallback). Local `build-images.sh` runs are
dev-only; never push production thin layers from a workstation (Rule §6).
