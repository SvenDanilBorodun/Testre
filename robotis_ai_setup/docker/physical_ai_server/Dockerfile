FROM robotis/physical-ai-server:amd64-0.8.2

# Patch upstream bug: ServerInference.__init__ never initializes self._endpoints,
# causing AttributeError on first register_endpoint() call.
# Ref: physical_ai_tools/physical_ai_server/physical_ai_server/inference/server_inference.py
#
# Also removes duplicate InferenceManager initialization (lines 71-75 repeat 60-64).
# Fix CRLF line endings in s6 service files.
# When building on Windows, Git converts LF to CRLF. s6-overlay rejects
# "longrun\r" as an invalid type. Strip \r from all affected files.
RUN find /etc/s6-overlay/s6-rc.d -type f -exec sed -i 's/\r$//' {} + && \
    find /usr/local/lib/s6-services -type f -name "*.sh" -exec sed -i 's/\r$//' {} + && \
    echo "Fixed CRLF in s6 service files"

# Patch upstream bug: ServerInference.__init__ never initializes self._endpoints,
# causing AttributeError on first register_endpoint() call.
# Also removes duplicate InferenceManager initialization (lines 71-75 repeat 60-64).
COPY patches/fix_server_inference.py /tmp/fix_server_inference.py
RUN TARGETS=$(find /root/ros2_ws -name "server_inference.py" -path "*/inference/*" 2>/dev/null) && \
    for f in $TARGETS; do \
        python3 /tmp/fix_server_inference.py "$f" && \
        echo "Patched: $f"; \
    done && \
    rm /tmp/fix_server_inference.py

# Overlay modified source files (inference validation, data sanitization, joint safety, dual camera config)
# Note: LeRobot itself is NOT overlaid — the base image clones the same version (989f3d05)
# that RunPod training pins. Only physical_ai_server files are patched here.
COPY overlays/inference_manager.py /tmp/overlays/inference_manager.py
COPY overlays/data_manager.py /tmp/overlays/data_manager.py
COPY overlays/data_converter.py /tmp/overlays/data_converter.py
COPY overlays/omx_f_config.yaml /tmp/overlays/omx_f_config.yaml
RUN TARGETS=$(find /root/ros2_ws -name "inference_manager.py" -path "*/inference/*" 2>/dev/null) && \
    [ -n "$TARGETS" ] || { echo "ERROR: inference_manager.py not found in base image — overlay cannot be applied"; exit 1; } && \
    for f in $TARGETS; do cp /tmp/overlays/inference_manager.py "$f" && echo "Overlaid: $f"; done && \
    TARGETS=$(find /root/ros2_ws -name "data_manager.py" -path "*/data_processing/*" 2>/dev/null) && \
    [ -n "$TARGETS" ] || { echo "ERROR: data_manager.py not found in base image — overlay cannot be applied"; exit 1; } && \
    for f in $TARGETS; do cp /tmp/overlays/data_manager.py "$f" && echo "Overlaid: $f"; done && \
    TARGETS=$(find /root/ros2_ws -name "data_converter.py" -path "*/data_processing/*" 2>/dev/null) && \
    [ -n "$TARGETS" ] || { echo "ERROR: data_converter.py not found in base image — overlay cannot be applied"; exit 1; } && \
    for f in $TARGETS; do cp /tmp/overlays/data_converter.py "$f" && echo "Overlaid: $f"; done && \
    TARGETS=$(find /root/ros2_ws -name "omx_f_config.yaml" 2>/dev/null) && \
    [ -n "$TARGETS" ] || { echo "ERROR: omx_f_config.yaml not found in base image — overlay cannot be applied"; exit 1; } && \
    for f in $TARGETS; do cp /tmp/overlays/omx_f_config.yaml "$f" && echo "Overlaid: $f"; done && \
    rm -rf /tmp/overlays
