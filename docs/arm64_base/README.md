# arm64 base images — one-time maintainer builds

> **Maintainer-only, run ONCE per base bump.** Students and teachers never
> run any of this. The base images are the immutable inputs that
> `build-images.sh` and CI (`docker-publish.yml`) build the thin production
> layers on top of. Everyday releases only rebuild the *thin* layers — the
> bases below already exist in the registry and are simply resolved by
> `docker manifest inspect`.

EduBotics ships one arm64 device flavor:

- **Orange Pi 5 Pro** (`-opi`) — the full student stack on a Rockchip
  RK3588S board (**no CUDA — ever**). CPU-only torch on a stock ROS base.
  See [`docs/ORANGE_PI_DEPLOY_PLAN.md`](../ORANGE_PI_DEPLOY_PLAN.md).

(The second one, the classroom Jetson `-jetson` with its GPU L4T base, was
retired on 2026-11-09; `Dockerfile.arm64` and `PLATFORM=arm64` are gone.)

## The base image set

All bases live on Docker Hub under the build `REGISTRY` (default
`nettername`; `build-images.sh` reads `${REGISTRY}`). The thin-layer builds
resolve them by `${REGISTRY}/<name>:<tag>` at build time. GHCR is the
student-facing *consumption* primary for the thin layers only — the bases
are build inputs and stay on Docker Hub.

| Base image | Built from | Flavor(s) that consume it | How to (re)build |
|---|---|---|---|
| `${REGISTRY}/open-manipulator-jetson-base:4.1.4` | `open_manipulator/docker/Dockerfile` (`FROM ros:jazzy-ros-base` — **CUDA/L4T-free**) | Orange Pi | `BUILD_BASE_OPI_OMX=1 PLATFORM=opi` |
| `${REGISTRY}/physical-ai-server-opi-base:0.8.2-opi2` | `physical_ai_tools/physical_ai_server/Dockerfile.arm64cpu` (`FROM ros:jazzy-ros-base` — stock, plain PyPI, **CPU torch 2.7.0**) | Orange Pi | `BUILD_BASE_OPI=1 PLATFORM=opi` |

### Why the Orange Pi's open_manipulator base is called `…-jetson-base`

The `open_manipulator` base is `FROM ros:jazzy-ros-base` (stock ROS 2 Jazzy
plus the Dynamixel + C++ `ros2_control` packages) — it carries **no CUDA and
no L4T**. It was first built for the classroom Jetson and the Orange Pi has
reused it verbatim ever since; when the Jetson was retired its build moved to
`BUILD_BASE_OPI_OMX=1 PLATFORM=opi`, but the repository kept its name:
renaming it would need a manual rebuild + push before any release could
resolve it. **Do not delete `open-manipulator-jetson-base` on Docker Hub** —
every Orange Pi build resolves it.

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

**Where to run it — native arm64 is strongly preferred.** Both base builds
emit `linux/arm64` images. On a native arm64 host (an
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

`build-images.sh` passes `--platform linux/arm64` on every opi buildx
call, so no further per-command flags are needed.

## Building the Orange Pi bases (one-time)

```bash
cd robotis_ai_setup/docker
# REQUIRED even though you only want the base: PLATFORM=opi builds the MANAGER
# FIRST (it is the only other flavor besides amd64 that ships one), and those
# three `${VAR:?}` gates abort the script long before the base build is reached.
# Omitting them does not "skip the manager" — it kills the run.
export SUPABASE_URL=https://xxx.supabase.co
export SUPABASE_ANON_KEY=...
export CLOUD_API_URL=https://scintillating-empathy-production-1068.up.railway.app

# Build order is: manager -> SERVER BASE (this) -> thin server (+flatten)
# -> OMX base resolve -> thin OMX -> push all three. The base is NOT first.
# It builds + pushes physical-ai-server-opi-base:<tag> from Dockerfile.arm64cpu
# (--platform linux/arm64 --push; context = physical_ai_tools/).
BUILD_BASE_OPI=1 PLATFORM=opi ./build-images.sh

# The open_manipulator base, only when it must be rebuilt (it exists; see
# above). Same script, same three variables, --push:
BUILD_BASE_OPI_OMX=1 PLATFORM=opi ./build-images.sh
```

The base tag is pinned at `build-images.sh`'s `opi` case (`PAS_BASE_IMAGE`).
**Bump it whenever you change `Dockerfile.arm64cpu`** and rebuild — nothing in
CI builds this base, so an unchanged tag means every later build silently
resolves the OLD published image and your edit ships nowhere.
`ci.yml::base-version-guard` fails the PR if a non-comment edit to the base
Dockerfile lands without the tag moving.

`Dockerfile.arm64cpu` is a stock `ros:jazzy-ros-base` build with plain PyPI:
it pins `torch==2.7.0` + `torchvision==0.22.0` (the aarch64 PyPI 2.7.0 wheel
is genuinely CPU-only — ~99 MB, zero `nvidia-*` deps) **before** LeRobot so
`lerobot==0.5.1`'s resolve can't drag in torch 2.10.x, then installs
`lerobot[pi,smolvla,peft]==0.5.1` under the same `numpy==1.26.4` /
`scipy>=1.14.0,<1.18` cross-arch floor as the amd64 base (Rule §5 — this
file is one of the five LeRobot pin sites). It has **no** CUDA or L4T
references.

Without `BUILD_BASE_OPI_OMX=1`, `PLATFORM=opi` only resolves the
open_manipulator base and aborts with a hint pointing back here if it is
missing.

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
  image). Target size **~5-6 GB**, gated at an **11 GB** ceiling in CI
  (`docker-publish.yml`'s opi-only size step). A real arm64 build measures
  **4.85 GB** uncompressed. (The 7 GB figure this doc used to quote was
  inherited from the original deploy plan and never matched the code — 7 GB
  left barely 1.2x headroom over the target, so the first honest build would
  have failed the release for being the size it was designed to be.)

## Verifying a pushed base (architecture / manifest)

After a base build, confirm the pushed image reports the expected
architecture before building thin layers on it:

```bash
# Should show `Platform: linux/arm64` for both opi bases.
docker buildx imagetools inspect ${REGISTRY}/physical-ai-server-opi-base:0.8.2-opi2
docker buildx imagetools inspect ${REGISTRY}/open-manipulator-jetson-base:4.1.4
```

The thin-layer builds and CI (`docker-publish.yml::smoke-test`) also assert
`uname -m == aarch64` and run `image_source_parity.sh` on the shipped
images, so an arch or source-drift mistake surfaces loudly at publish time
rather than on a bench Pi.

## After the bases exist

Everyday releases go through GitHub Actions (`docker-publish.yml`, W4 in the
golden order) — the `build`/`retag`/`smoke-test` matrix carries two
flavors (`amd64`, `opi`) and dual-pushes each thin image to GHCR
(primary) + Docker Hub (fallback). Local `build-images.sh` runs are
dev-only; never push production thin layers from a workstation (Rule §6).
