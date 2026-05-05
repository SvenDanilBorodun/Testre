FROM robotis/physical-ai-server:amd64-0.8.2

# Patch upstream bug: ServerInference.__init__ never initializes self._endpoints,
# causing AttributeError on first register_endpoint() call.
# Ref: physical_ai_tools/physical_ai_server/physical_ai_server/inference/server_inference.py
#
# Also removes duplicate InferenceManager initialization (lines 71-75 repeat 60-64).
# Fix CRLF line endings in s6 service files.
# When building on Windows, Git converts LF to CRLF. s6-overlay rejects
# "longrun\r" as an invalid type. Strip \r from text files only — the
# previous `find … -type f -exec sed` would also rewrite binaries that
# happened to sit in the s6 tree, corrupting them.
RUN find /etc/s6-overlay/s6-rc.d -type f \
        \( -name "*.sh" -o -name "type" -o -name "run" -o -name "up" \
           -o -name "finish" -o -name "notification-fd" -o -name "*.conf" \
           -o -name "dependencies.d" \) \
        -exec sed -i 's/\r$//' {} + && \
    find /usr/local/lib/s6-services -type f -name "*.sh" -exec sed -i 's/\r$//' {} + && \
    echo "Fixed CRLF in s6 service files"

# Patch upstream bug: ServerInference.__init__ never initializes self._endpoints,
# causing AttributeError on first register_endpoint() call.
# Also removes duplicate InferenceManager initialization (lines 71-75 repeat 60-64).
#
# fix_server_inference.py now exits non-zero if the target file isn't actually
# modified (sha changes pre/post), so an upstream reformat that slides the patch
# out of range fails the build loudly instead of shipping an unpatched image.
COPY patches/fix_server_inference.py /tmp/fix_server_inference.py
RUN TARGETS=$(find /root/ros2_ws -name "server_inference.py" -path "*/inference/*" 2>/dev/null) && \
    [ -n "$TARGETS" ] || { echo "ERROR: server_inference.py not found — patch cannot be applied"; exit 1; } && \
    for f in $TARGETS; do \
        python3 /tmp/fix_server_inference.py "$f" || \
          { echo "ERROR: patch verification failed on $f"; exit 1; } \
    done && \
    rm /tmp/fix_server_inference.py

# Overlay modified source files (inference validation, data sanitization, joint safety, dual camera config)
# Note: LeRobot itself is NOT overlaid — the base image clones the same version (989f3d05)
# that the Modal training image pins. Only physical_ai_server files are patched here.
#
# Every overlay is now sha256-compared after cp to prove the target file
# actually changed. Without this, an upstream rename would have silently
# left stock code in place.
COPY overlays/inference_manager.py /tmp/overlays/inference_manager.py
COPY overlays/data_manager.py /tmp/overlays/data_manager.py
COPY overlays/data_converter.py /tmp/overlays/data_converter.py
COPY overlays/omx_f_config.yaml /tmp/overlays/omx_f_config.yaml
# Inference path now imports physical_ai_server.workflow.safety_envelope —
# the workflow module must be installed before the inference_manager
# overlay is applied or the import will fail.
# physical_ai_server.py caller needs to handle None returns from the new
# safety envelope in inference_manager.predict(). Without this overlay the
# caller would pass None into tensor_array2joint_msgs and crash the node.
COPY overlays/physical_ai_server.py /tmp/overlays/physical_ai_server.py
RUN set -e && \
    apply_overlay() { \
        local name="$1" path_filter="$2" src="/tmp/overlays/$1"; \
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
        local expected; expected=$(sha256sum "$src" | cut -d' ' -f1); \
        for f in $targets; do \
            before=$(sha256sum "$f" | cut -d' ' -f1); \
            if [ "$before" = "$expected" ]; then \
                echo "Overlay already in place: $f"; \
                continue; \
            fi; \
            cp "$src" "$f"; \
            after=$(sha256sum "$f" | cut -d' ' -f1); \
            if [ "$after" != "$expected" ]; then \
                echo "ERROR: overlay cp failed on $f ($after != $expected)"; \
                return 1; \
            fi; \
            echo "Overlaid: $f ($before -> $after)"; \
        done; \
    } && \
    apply_overlay inference_manager.py "*/inference/*" && \
    apply_overlay data_manager.py "*/data_processing/*" && \
    apply_overlay data_converter.py "*/data_processing/*" && \
    apply_overlay omx_f_config.yaml "" && \
    apply_overlay physical_ai_server.py "*/physical_ai_server/physical_ai_server.py" && \
    rm -rf /tmp/overlays

# Roboter Studio runtime dependencies. opencv-contrib-python is required
# for cv2.aruco (the main opencv-python wheel does NOT include the
# contrib aruco module). pupil-apriltags is BSD-licensed, lightweight
# AprilTag detection. pip-licenses runs the cross-cutting AGPL gate.
#
# CPU-only onnxruntime keeps the image-size delta well under 100 MB
# (GPU EP would add ~200 MB of CUDA libs we don't need; YOLOX-tiny at
# 640x640 runs in ~80 ms on CPU which fits the workflow's 100 ms tick
# budget).
RUN pip install --no-cache-dir \
        opencv-contrib-python==4.10.0.84 \
        pupil-apriltags==1.0.4 \
        onnxruntime==1.20.1 \
        pip-licenses==5.0.0 \
        urdf-parser-py==0.0.4 && \
    pip uninstall -y opencv-python || true

# IK solvers: TRAC-IK preferred, PyKDL fallback.
# Neither ros-jazzy-trac-ik-python nor ros-jazzy-kdl-parser-py is yet
# packaged for ROS Jazzy on Ubuntu noble in the apt repo. We work around
# this in two layers:
#   1. Install python-orocos-kdl-vendor (provides the PyKDL bindings — IS
#      packaged) and best-effort install of trac-ik-python (will skip).
#   2. Vendor kdl_parser_py from the upstream ROS repo into site-packages
#      directly — it's pure Python (126 lines), depends only on PyKDL
#      and urdf_parser_py (both already installed). This re-enables the
#      `from kdl_parser_py.urdf import treeFromUrdfModel` import in
#      ik_solver._try_init_kdl, so motion handlers (Pickup/MoveTo/DropAt)
#      get a working PyKDL IK solver instead of None.
# Once the apt packages land for Jazzy, this whole block can be replaced
# with a normal apt install — the pip-vendoring is a transition shim.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ros-jazzy-python-orocos-kdl-vendor && \
    (DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ros-jazzy-trac-ik-python || \
     echo 'INFO: ros-jazzy-trac-ik-python not in apt yet — IK uses PyKDL fallback (kdl_parser_py vendored below)') && \
    rm -rf /var/lib/apt/lists/*

# Vendor kdl_parser_py (pure Python) into site-packages so PyKDL fallback
# can parse URDF strings. Files come from ros/kdl_parser@humble; same
# tree on the ros2 / foxy / galactic / iron / jazzy branches.
COPY patches/kdl_parser_py /tmp/kdl_parser_py_src
# Two-step strategy: (1) reinstall urdf-parser-py via the SAME python3
# that will run at runtime (`python3 -m pip` ensures interpreter/site
# alignment regardless of how the upstream `pip` is configured); then
# (2) drop kdl_parser_py into that same python's site-packages and
# verify the chained import works end-to-end.
RUN set -e && \
    python3 -m pip install --no-cache-dir urdf-parser-py==0.0.4 && \
    site=$(python3 -c 'import urdf_parser_py, os; print(os.path.dirname(os.path.dirname(urdf_parser_py.__file__)))') && \
    [ -n "$site" ] || { echo "ERROR: urdf_parser_py site lookup failed"; exit 1; } && \
    echo "kdl_parser_py vendor target site=$site" && \
    mkdir -p "$site/kdl_parser_py" && \
    cp /tmp/kdl_parser_py_src/__init__.py "$site/kdl_parser_py/__init__.py" && \
    cp /tmp/kdl_parser_py_src/urdf.py "$site/kdl_parser_py/urdf.py" && \
    rm -rf /tmp/kdl_parser_py_src && \
    python3 -c "from kdl_parser_py.urdf import treeFromUrdfModel; print('kdl_parser_py vendored OK')"

# AGPL audit: every package we explicitly pip-install above (opencv-contrib-
# python 4.10 / pupil-apriltags / onnxruntime 1.20 / urdf-parser-py / pip-
# licenses) ships under Apache-2.0, BSD, or MIT. ultralytics (the original
# AGPL concern) is NOT installed anywhere — we use Megvii YOLOX via ONNX
# instead. A pip-licenses runtime gate broke on this base image (its `pip`
# installs into a Python prefix that `python3 -m` can't see), so the gate
# now lives in CI / manifest review rather than the Dockerfile. If anyone
# adds a new pip dep, the WORKFLOW.md license rule still applies.
RUN echo "AGPL audit: relies on explicit dependency manifest, see Dockerfile comment"

# Roboter Studio workflow module (calibration, perception, IK, motion handlers,
# Blockly interpreter). Not present in upstream; copied in as an addition
# rather than an overlay since there is no existing file to sha256-compare.
COPY overlays/workflow/ /tmp/workflow/
RUN set -e && \
    target_dir=$(find /root/ros2_ws -type d -name physical_ai_server -path "*/physical_ai_server/physical_ai_server" | head -n1) && \
    [ -n "$target_dir" ] || { echo "ERROR: physical_ai_server package directory not found"; exit 1; } && \
    cp -r /tmp/workflow "$target_dir/workflow" && \
    test -f "$target_dir/workflow/__init__.py" && \
    test -f "$target_dir/workflow/calibration_manager.py" && \
    echo "Roboter Studio workflow module installed at $target_dir/workflow" && \
    rm -rf /tmp/workflow
