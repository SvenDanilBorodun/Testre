#!/usr/bin/env bash
# image-source-parity — assert the SHIPPED image reflects the repo source at
# HEAD, with no left-outs. This is the durable guard against the v2.5.0/v2.5.1
# class of bug where a monorepo edit/deletion silently never reached the image
# (training_manager.py crash). Run per-arch in docker-publish.yml::smoke-test
# AFTER the image is pulled.
#
# Usage: image_source_parity.sh <kind> <image-ref>
#   kind = physical-ai-server   -> COPY-wholesale: image package must be
#          BYTE-IDENTICAL to the repo source-of-truth tree (exact diff -r).
#   kind = open-manipulator     -> overlay model: every overlay source file
#          must be present (by sha256) in the image install tree (it landed).
set -euo pipefail

KIND="${1:?usage: image_source_parity.sh <kind> <image-ref>}"
IMG="${2:?usage: image_source_parity.sh <kind> <image-ref>}"

# Extract a path from the image into a host dir (no execution; works any arch).
extract() {
    local src="$1" dst="$2" cid
    cid=$(docker create "$IMG")
    docker cp "$cid:$src" "$dst"
    docker rm "$cid" >/dev/null
}
strip_pyc() {
    find "$1" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    find "$1" -name '*.pyc' -delete 2>/dev/null || true
}

case "$KIND" in
  physical-ai-server)
    REPO="physical_ai_tools/physical_ai_server"
    rm -rf /tmp/parity_repo /tmp/parity_img
    mkdir -p /tmp/parity_repo
    # Mirror build-images.sh staging exclusions exactly (test/, the per-arch
    # Dockerfiles, CHANGELOG, bytecode are build inputs / dev-only, not shipped).
    cp -a "${REPO}/." /tmp/parity_repo/
    rm -rf /tmp/parity_repo/test \
           /tmp/parity_repo/Dockerfile.amd64 \
           /tmp/parity_repo/Dockerfile.arm64 \
           /tmp/parity_repo/Dockerfile.arm64cpu \
           /tmp/parity_repo/CHANGELOG.rst
    strip_pyc /tmp/parity_repo
    extract /root/ros2_ws/src/physical_ai_tools/physical_ai_server /tmp/parity_img
    strip_pyc /tmp/parity_img
    if diff -rq /tmp/parity_repo /tmp/parity_img; then
        echo "OK: physical-ai-server image package is byte-identical to repo HEAD"
    else
        echo "::error::physical-ai-server image package DIVERGES from repo HEAD (see diff above) — image does not reflect main"
        exit 1
    fi
    ;;

  open-manipulator)
    OVL="robotis_ai_setup/docker/open_manipulator/overlays"
    rm -rf /tmp/parity_src
    # Scan the SRC tree, not install/ — colcon --symlink-install makes install/
    # entries symlinks into build/, which `find -type f` skips. apply_overlay
    # writes the real overlaid bytes into src/ (install/ + build/ resolve to it).
    extract /root/ros2_ws/src /tmp/parity_src
    # Index every real file in the image src tree by sha256 once, into a temp
    # file (robust against -exec batching / set -e truncation). NUL-delimited
    # so paths with odd chars are safe.
    find /tmp/parity_src -type f -print0 2>/dev/null \
        | xargs -0 -r sha256sum 2>/dev/null \
        | awk '{print $1}' | sort -u > /tmp/parity_sums.txt
    fail=0
    for f in "${OVL}"/*; do
        [ -f "$f" ] || continue
        # apply_overlay() CR-strips before its sha; mirror that so a CRLF
        # checkout does not false-fail. (Linux CI checkout is LF anyway.)
        want=$(sed 's/\r$//' "$f" | sha256sum | cut -d' ' -f1)
        if grep -Fxq "$want" /tmp/parity_sums.txt; then
            echo "OK: $(basename "$f") present in image src tree"
        else
            echo "::error::open-manipulator overlay $(basename "$f") (sha $want) NOT found in image — overlay did not land / drifted from repo"
            fail=1
        fi
    done
    [ "$fail" = 0 ] || exit 1
    echo "OK: all open-manipulator overlays present in image"

    # edu6 plain COPYs (SECOND extraction root — they land in /usr/local/bin,
    # not the ros2_ws src tree; previously plain COPYs were verified by
    # NOTHING, the identify_arm.py precedent).
    rm -rf /tmp/parity_bin
    extract /usr/local/bin /tmp/parity_bin
    for f in edu6_arm_node.py feetech_bus.py identify_arm.py camera_ingest_node.py; do
        repo="robotis_ai_setup/docker/open_manipulator/$f"
        img="/tmp/parity_bin/$f"
        if [ ! -f "$img" ]; then
            echo "::error::plain COPY $f missing from image /usr/local/bin"
            fail=1
            continue
        fi
        want=$(sed 's/\r$//' "$repo" | sha256sum | cut -d' ' -f1)
        got=$(sed 's/\r$//' "$img" | sha256sum | cut -d' ' -f1)
        if [ "$want" = "$got" ]; then
            echo "OK: /usr/local/bin/$f byte-identical to repo"
        else
            echo "::error::plain COPY $f drifted (repo $want != image $got)"
            fail=1
        fi
    done
    [ "$fail" = 0 ] || exit 1
    echo "OK: all plain COPYs byte-identical"
    ;;

  *)
    echo "::error::unknown kind '$KIND' (expected physical-ai-server | open-manipulator)"
    exit 2
    ;;
esac
