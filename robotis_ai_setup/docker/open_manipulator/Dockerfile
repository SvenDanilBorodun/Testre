FROM robotis/open-manipulator:amd64-4.1.4

# Install Python dynamixel-sdk for identify_arm.py
# Install v4l-utils for camera device discovery (v4l2-ctl --info)
RUN apt-get update && apt-get install -y --no-install-recommends \
        v4l-utils python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir --break-system-packages dynamixel-sdk==4.0.3

# Copy entrypoint and identification script
COPY entrypoint_omx.sh /entrypoint_omx.sh
COPY identify_arm.py /usr/local/bin/identify_arm.py
# Strip Windows CRLF line endings (Git on Windows may convert LF→CRLF)
RUN sed -i 's/\r$//' /entrypoint_omx.sh /usr/local/bin/identify_arm.py \
    && chmod +x /entrypoint_omx.sh /usr/local/bin/identify_arm.py

# Overlay patched ros2_control configs (joint limits for safety envelope +
# aligned gripper current limit). Base image clones from ROBOTIS-GIT so
# these files are NOT in this repo — we overlay them onto the built image.
# sha256 check asserts the copy actually changed the target file so an
# upstream rename doesn't leave the image with stock (unsafe) configs.
COPY overlays/omx_f.ros2_control.xacro /tmp/overlays/omx_f.ros2_control.xacro
COPY overlays/omx_f_hardware_controller_manager.yaml /tmp/overlays/omx_f_hardware_controller_manager.yaml
RUN set -e && \
    apply_overlay() { \
        local name="$1" find_args="$2" src="$3" dest_name="$4"; \
        local targets; \
        targets=$(eval "find /root/ros2_ws -name '$name' $find_args 2>/dev/null") || true; \
        if [ -z "$targets" ]; then \
            echo "ERROR: $name not found in base image — overlay cannot be applied"; \
            return 1; \
        fi; \
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
    apply_overlay omx_f.ros2_control.xacro "-path */ros2_control/*" /tmp/overlays/omx_f.ros2_control.xacro && \
    apply_overlay hardware_controller_manager.yaml "-path */omx_f_follower_ai/*" /tmp/overlays/omx_f_hardware_controller_manager.yaml && \
    rm -rf /tmp/overlays

ENTRYPOINT ["/entrypoint_omx.sh"]
