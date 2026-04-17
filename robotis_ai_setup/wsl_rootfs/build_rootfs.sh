#!/usr/bin/env bash
# Build the EduBotics WSL2 rootfs tarball.
#
# Run on WSL2 or Linux with Docker. Output is installer/assets/edubotics-rootfs.tar.gz
# which the Inno Setup installer picks up and ships.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
OUT_DIR="${SCRIPT_DIR}/../installer/assets"
OUT_FILE="${OUT_DIR}/edubotics-rootfs.tar.gz"
IMAGE_TAG="edubotics-rootfs:latest"

echo ">> Building image ${IMAGE_TAG}"
docker build --pull -t "${IMAGE_TAG}" "${SCRIPT_DIR}"

echo ">> Creating temporary container"
CID="$(docker create "${IMAGE_TAG}" true)"
trap 'docker rm -f "${CID}" >/dev/null 2>&1 || true' EXIT

mkdir -p "${OUT_DIR}"
echo ">> Exporting rootfs to ${OUT_FILE}"
docker export "${CID}" | gzip -9 > "${OUT_FILE}"

SIZE_MB=$(( $(stat -c%s "${OUT_FILE}") / 1024 / 1024 ))
echo ">> Done. Rootfs size: ${SIZE_MB} MB"
echo "   -> ${OUT_FILE}"
