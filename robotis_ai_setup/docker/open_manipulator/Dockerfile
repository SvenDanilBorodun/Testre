# Base image is parameterised so a single Dockerfile builds for both
# architectures. amd64 uses the ROBOTIS-published base; arm64 uses our
# own base built once from the upstream open_manipulator/docker/
# Dockerfile sources (see docs/arm64_base/README.md). The build-images.sh
# script passes the right base via --build-arg BASE_IMAGE=... per platform.
ARG BASE_IMAGE=robotis/open-manipulator:amd64-4.1.4
FROM ${BASE_IMAGE}

# Install Python dynamixel-sdk for identify_arm.py
# Install v4l-utils for camera device discovery (v4l2-ctl --info)
#
# apt-get retry loop (added 2026-05-20 after docker-publish run #26166193986
# failed on arm64 with "ports.ubuntu.com noble-security/main: Mirror sync in
# progress, File has unexpected size"). The Ubuntu ports mirror (used by
# arm64 builds) intermittently serves stale index files for a few minutes
# while it re-syncs. Up to 5 retries with exponential-ish backoff (5, 10,
# 15, 20, 25 seconds = 75s total worst case) absorbs typical mirror sync
# windows without artificially extending green build times.
RUN set -eux; \
    for i in 1 2 3 4 5; do \
        apt-get update && \
        apt-get install -y --no-install-recommends v4l-utils python3-pip && \
        break || { \
            echo "apt-get retry $i/5 (mirror sync or transient network)"; \
            rm -rf /var/lib/apt/lists/*; \
            sleep $((i * 5)); \
        }; \
        [ "$i" = "5" ] && exit 1; \
    done; \
    rm -rf /var/lib/apt/lists/*; \
    pip3 install --no-cache-dir --break-system-packages dynamixel-sdk==4.0.3

# Copy entrypoint and identification script
COPY entrypoint_omx.sh /entrypoint_omx.sh
COPY identify_arm.py /usr/local/bin/identify_arm.py
# Native-camera ingest node (WSL2/Windows student path). NOT an apply_overlay
# target — it's a brand-new standalone rclpy script with no upstream file to
# replace, so it's COPY'd like identify_arm.py. The entrypoint runs it (instead
# of usb_cam) when EDUBOTICS_CAMERA_SOURCE=native_bridge. See the script header
# and CLAUDE.md "native camera capture bridge" for the full rationale.
COPY camera_ingest_node.py /usr/local/bin/camera_ingest_node.py
# Teleop collision-threshold calibration helper (operator runs it once per rig). Standalone
# rclpy script, COPY'd like identify_arm.py / camera_ingest_node.py — NOT an apply_overlay target.
COPY calibrate_collision_currents.py /usr/local/bin/calibrate_collision_currents.py
# edu6_studio Feetech driver (docs/plans/edu6-studio-arm.md §3): the clean-room
# STS3215 bus + the rclpy driver node the entrypoint branches into on
# EDUBOTICS_ROBOT_TYPE=edu6_studio. Plain COPYs like identify_arm.py (NOT
# apply_overlay targets — there is no upstream file to overlay); byte-verified
# by image_source_parity.sh's /usr/local/bin root.
COPY feetech_bus.py /usr/local/bin/feetech_bus.py
COPY edu6_arm_node.py /usr/local/bin/edu6_arm_node.py
# The driver's whole-link box model (table-floor pre-check for boot-home).
# Plain COPY like its two siblings, byte-verified by image_source_parity.sh.
COPY edu6_geometry.py /usr/local/bin/edu6_geometry.py
# Strip Windows CRLF line endings (Git on Windows may convert LF→CRLF)
RUN sed -i 's/\r$//' /entrypoint_omx.sh /usr/local/bin/identify_arm.py /usr/local/bin/camera_ingest_node.py /usr/local/bin/calibrate_collision_currents.py /usr/local/bin/feetech_bus.py /usr/local/bin/edu6_arm_node.py /usr/local/bin/edu6_geometry.py \
    && chmod +x /entrypoint_omx.sh /usr/local/bin/identify_arm.py /usr/local/bin/camera_ingest_node.py /usr/local/bin/calibrate_collision_currents.py /usr/local/bin/feetech_bus.py /usr/local/bin/edu6_arm_node.py /usr/local/bin/edu6_geometry.py

# Overlay patched ros2_control configs:
#   - follower (omx_f): joint limits + aligned gripper current limit
#   - leader   (omx_l): enable gravity compensation on joints 1-5
#                       (effort/Goal Current command interfaces, controller
#                       spawned first in launch). Mirrors OMY-L100 wiring.
# Base image clones from ROBOTIS-GIT so these files are NOT in this repo —
# we overlay them onto the built image. sha256 check asserts the copy
# actually changed the target file so an upstream rename doesn't leave the
# image with stock (unsafe) configs.
COPY overlays/omx_f.ros2_control.xacro /tmp/overlays/omx_f.ros2_control.xacro
COPY overlays/omx_f_follower_ai.launch.py /tmp/overlays/omx_f_follower_ai.launch.py
COPY overlays/omx_f_hardware_controller_manager.yaml /tmp/overlays/omx_f_hardware_controller_manager.yaml
COPY overlays/omx_l.ros2_control.xacro /tmp/overlays/omx_l.ros2_control.xacro
COPY overlays/omx_l_leader_ai.launch.py /tmp/overlays/omx_l_leader_ai.launch.py
COPY overlays/omx_l_leader_ai_hardware_controller_manager.yaml /tmp/overlays/omx_l_leader_ai_hardware_controller_manager.yaml
COPY overlays/camera_usb_cam.launch.py /tmp/overlays/camera_usb_cam.launch.py
RUN set -e && \
    apply_overlay() { \
        local name="$1" path_filter="$2" src="$3"; \
        local targets; \
        if [ -n "$path_filter" ]; then \
            targets=$(find /root/ros2_ws -name "$name" -path "$path_filter" 2>/dev/null); \
        else \
            targets=$(find /root/ros2_ws -name "$name" 2>/dev/null); \
        fi; \
        if [ -z "$targets" ]; then \
            echo "ERROR: $name not found in base image — overlay cannot be applied"; \
            return 1; \
        fi; \
        # Audit E4: defensive CRLF strip so Windows checkouts with stale
        # core.autocrlf settings still produce byte-identical overlays.
        # See physical_ai_server/Dockerfile for the full rationale.
        sed -i 's/\r$//' "$src"; \
        local expected; expected=$(sha256sum "$src" | cut -d' ' -f1); \
        for f in $targets; do \
            before=$(sha256sum "$f" | cut -d' ' -f1); \
            if [ "$before" = "$expected" ]; then echo "Overlay already in place: $f"; continue; fi; \
            cp "$src" "$f"; \
            after=$(sha256sum "$f" | cut -d' ' -f1); \
            if [ "$after" != "$expected" ]; then echo "ERROR: overlay cp failed on $f"; return 1; fi; \
            echo "Overlaid: $f ($before -> $after)"; \
        done; \
    } && \
    apply_overlay omx_f.ros2_control.xacro "*/ros2_control/*" /tmp/overlays/omx_f.ros2_control.xacro && \
    apply_overlay omx_f_follower_ai.launch.py "*/launch/*" /tmp/overlays/omx_f_follower_ai.launch.py && \
    apply_overlay hardware_controller_manager.yaml "*/omx_f_follower_ai/*" /tmp/overlays/omx_f_hardware_controller_manager.yaml && \
    apply_overlay omx_l.ros2_control.xacro "*/ros2_control/*" /tmp/overlays/omx_l.ros2_control.xacro && \
    apply_overlay omx_l_leader_ai.launch.py "*/launch/*" /tmp/overlays/omx_l_leader_ai.launch.py && \
    apply_overlay hardware_controller_manager.yaml "*/omx_l_leader_ai/*" /tmp/overlays/omx_l_leader_ai_hardware_controller_manager.yaml && \
    apply_overlay camera_usb_cam.launch.py "*/launch/*" /tmp/overlays/camera_usb_cam.launch.py && \
    rm -rf /tmp/overlays

# Audit hygiene: hide base-image-inherited VCS/build cruft — the .git trees
# left by the upstream base's `git clone` steps (open_manipulator,
# realsense-ros, DynamixelSDK, rmw_zenoh, …: ~43 MB amd64 / ~145 MB arm64)
# plus the zenoh cargo/build caches. These bytes live in BASE layers, so this
# RUN only adds a whiteout: the files vanish from the running container (and
# from container audits), but registry/pull size does NOT change. Mirrors the
# physical-ai-server strip in ../physical_ai_server/Dockerfile. The overlay
# sources under /root/ros2_ws/src are untouched (image-source-parity checks
# overlay file sha256s, not .git dirs).
RUN set -e && \
    find /root/ros2_ws -type d -name ".git" -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /root/.cargo /root/.cache && \
    echo "Whiteout-stripped base-inherited .git dirs + cargo/build caches"

ENTRYPOINT ["/entrypoint_omx.sh"]
