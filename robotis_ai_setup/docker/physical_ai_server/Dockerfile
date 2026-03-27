ARG BASE_IMAGE=nettername/physical-ai-server-base:latest
FROM ${BASE_IMAGE}

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
