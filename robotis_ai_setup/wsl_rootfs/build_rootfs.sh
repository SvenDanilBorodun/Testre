#!/usr/bin/env bash
# Build the EduBotics WSL2 rootfs tarball.
#
# Runs on Linux, WSL2, or macOS — anywhere Docker is available. Output
# is installer/assets/edubotics-rootfs.tar.gz which the Inno Setup
# installer picks up and ships.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
OUT_DIR="${SCRIPT_DIR}/../installer/assets"
OUT_FILE="${OUT_DIR}/edubotics-rootfs.tar.gz"
IMAGE_TAG="edubotics-rootfs:latest"

# Rootfs generation stamp, baked into /etc/edubotics-rootfs-version inside
# the image. The installer's upgrade gate compares it against the shipped
# wsl_rootfs/ROOTFS_VERSION file to decide whether an upgrade must
# unregister + re-import the distro (which destroys the student's Docker
# volumes). The tarball bytes are NOT reproducible across CI builds (apt
# timestamps), so this explicit stamp — not the tarball sha — is the gate.
# Bump wsl_rootfs/ROOTFS_VERSION whenever any rootfs build input changes
# (Dockerfile, wsl.conf, daemon.json, start-dockerd.sh, 99-edubotics.rules).
if [ ! -f "${SCRIPT_DIR}/ROOTFS_VERSION" ]; then
    echo "ERROR: ${SCRIPT_DIR}/ROOTFS_VERSION is missing." >&2
    exit 1
fi
ROOTFS_VERSION="$(tr -d '[:space:]' < "${SCRIPT_DIR}/ROOTFS_VERSION")"
if [ -z "${ROOTFS_VERSION}" ]; then
    echo "ERROR: ${SCRIPT_DIR}/ROOTFS_VERSION is empty." >&2
    exit 1
fi

# WSL2 on Windows always runs amd64. The Dockerfile hardcodes
# `arch=amd64` in /etc/apt/sources.list.d/docker.list, so building this
# image as anything other than linux/amd64 breaks at the docker-ce apt
# install step. On Apple Silicon Macs `docker build` defaults to the
# host's linux/arm64, so pin --platform explicitly here. On Linux/x86_64
# build hosts this is a no-op.
echo ">> Building image ${IMAGE_TAG} for linux/amd64 (rootfs version ${ROOTFS_VERSION})"
docker build --pull --platform linux/amd64 \
    --build-arg EDUBOTICS_ROOTFS_VERSION="${ROOTFS_VERSION}" \
    -t "${IMAGE_TAG}" "${SCRIPT_DIR}"

echo ">> Creating temporary container"
CID="$(docker create --platform linux/amd64 "${IMAGE_TAG}" true)"
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT

mkdir -p "${OUT_DIR}"
echo ">> Exporting rootfs to ${OUT_FILE}"
docker export "${CID}" | gzip -9 > "${OUT_FILE}"

# Portable file size: `stat -c%s` is GNU-only; `wc -c < file` works on
# both GNU coreutils (Linux/WSL) and BSD (macOS) without needing
# `brew install coreutils`.
SIZE_BYTES=$(wc -c < "${OUT_FILE}" | tr -d ' ')
SIZE_MB=$(( SIZE_BYTES / 1024 / 1024 ))
echo ">> Done. Rootfs size: ${SIZE_MB} MB"
echo "   -> ${OUT_FILE}"

# Publish a matching SHA256 so the installer (and any out-of-band
# distribution channel) can verify the tar before importing it. Without
# this, a corrupted download or swapped tar would only fail at
# `wsl --import` time with a cryptic error.
#
# `sha256sum` is GNU coreutils (Linux/WSL); macOS ships `shasum` instead.
if command -v sha256sum >/dev/null 2>&1; then
    SHA256=$(sha256sum "${OUT_FILE}" | awk '{print $1}')
else
    SHA256=$(shasum -a 256 "${OUT_FILE}" | awk '{print $1}')
fi
printf '%s  %s\n' "${SHA256}" "$(basename "${OUT_FILE}")" > "${OUT_FILE}.sha256"
echo ">> SHA256: ${SHA256}"
echo "   -> ${OUT_FILE}.sha256"
