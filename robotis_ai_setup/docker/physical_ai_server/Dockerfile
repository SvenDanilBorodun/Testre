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

# Re-build physical_ai_interfaces with the v1 + audit additions.
#
# Why this exists: the upstream base image (robotis/physical-ai-server:amd64-0.8.2)
# was built BEFORE commit d408378 added the Roboter Studio service /
# message types (StartCalibration, CalibrationCaptureFrame, CalibrationSolve,
# AutoPoseSuggest, ExecuteCalibrationPose, MarkDestination, StartWorkflow,
# StopWorkflow, WorkflowStatus). The audit also adds Detection.msg and
# CalibrationCaptureColor.srv, plus changes WorkflowStatus.msg from a
# parallel-array shape to a typed Detection[] field.
#
# Without this step physical_ai_server.py crashes at import time with
# "ImportError: cannot import name 'StartCalibration'" — meaning the
# v1 ship was actually broken at runtime even though docker push
# succeeded. ros2_service_run.sh sources ${COLCON_WS}/install/setup.bash,
# so re-building the package here is enough — no entrypoint change.
COPY interfaces /tmp/interfaces_src
RUN set -e && \
    target_pkg=$(find /root/ros2_ws/src -type d -name physical_ai_interfaces | head -n1) && \
    [ -n "$target_pkg" ] || { echo "ERROR: physical_ai_interfaces source not found"; exit 1; } && \
    cp -f /tmp/interfaces_src/msg/*.msg "$target_pkg/msg/" && \
    cp -f /tmp/interfaces_src/srv/*.srv "$target_pkg/srv/" && \
    cp -f /tmp/interfaces_src/CMakeLists.txt "$target_pkg/CMakeLists.txt" && \
    cp -f /tmp/interfaces_src/package.xml "$target_pkg/package.xml" && \
    cd /root/ros2_ws && \
    bash -c "source /opt/ros/jazzy/setup.bash && colcon build --packages-select physical_ai_interfaces --cmake-args -DCMAKE_BUILD_TYPE=Release" && \
    install_root=/root/ros2_ws/install/physical_ai_interfaces/lib/python3.12/site-packages/physical_ai_interfaces && \
    test -f "$install_root/srv/_start_calibration.py" && \
    test -f "$install_root/srv/_calibration_capture_color.py" && \
    test -f "$install_root/msg/_detection.py" && \
    test -f "$install_root/msg/_workflow_status.py" && \
    # Phase-2/3 (audit round-3 §F/§Y): assert every new generated
    # srv/msg so a future colcon regression that silently skips one
    # surfaces here at build time instead of as an ImportError on
    # first /workflow/pause call in a student's classroom.
    test -f "$install_root/srv/_calibration_capture_frame.py" && \
    test -f "$install_root/srv/_calibration_history.py" && \
    test -f "$install_root/srv/_calibration_preview.py" && \
    test -f "$install_root/srv/_verify_calibration.py" && \
    test -f "$install_root/srv/_workflow_continue.py" && \
    test -f "$install_root/srv/_workflow_pause.py" && \
    test -f "$install_root/srv/_workflow_set_breakpoints.py" && \
    test -f "$install_root/srv/_workflow_step.py" && \
    test -f "$install_root/msg/_sensor_snapshot.py" && \
    rm -rf /tmp/interfaces_src && \
    echo "physical_ai_interfaces re-built with audit additions"

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
# physical_ai_server.py overlay keeps the bearer-token log scrubber,
# the workflow-detection cache that feeds the React debugger, and all
# Roboter Studio service handlers. The inference-time safety envelope
# was removed in the safety stripdown.
COPY overlays/physical_ai_server.py /tmp/overlays/physical_ai_server.py
# communicator.py adds get_latest_bgr_frame + get_latest_follower_joints
# (decode CompressedImage + return JointState position) so the
# Roboter Studio calibration manager can actually collect frames + EEF
# poses on real hardware. Without this overlay the v1.2 provider hooks
# in physical_ai_server.py find no methods to call and the wizard
# returns "Kein Kamerabild verfügbar." on every capture press.
COPY overlays/communicator.py /tmp/overlays/communicator.py
# Audit F62: lerobot_dataset_wrapper.py was previously NOT overlaid, so
# every prior build shipped the upstream baseline's `preset='ultrafast',
# crf=28` mp4 encoder defaults — the F62 source change in
# physical_ai_tools/... never reached student installs. Adding it here
# as an overlay (path filter `*/data_processing/*` matches the same
# directory as data_manager.py / data_converter.py) so the cleaner
# `preset='fast', crf=23` defaults actually ship.
COPY overlays/lerobot_dataset_wrapper.py /tmp/overlays/lerobot_dataset_wrapper.py
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
        # Audit E4: scrub CR characters from the staged overlay BEFORE
        # the sha256 so a Windows checkout with core.autocrlf=true (which
        # rewrote our LF files to CRLF) still produces a byte-identical
        # copy. Without this, the sha256 post-check would still pass IF
        # both the staged file AND the in-image file ended up CRLF (silent
        # content-equivalence), but xacro/yaml parsers in the base image
        # would then fail with confusing line-ending errors at runtime.
        # The .gitattributes LF rule landed at the same time defends new
        # clones; this is the build-side belt for stale working trees.
        sed -i 's/\r$//' "$src"; \
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
    apply_overlay communicator.py "*/communication/communicator.py" && \
    apply_overlay lerobot_dataset_wrapper.py "*/data_processing/*" && \
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
# pupil-apriltags 1.0.4 ships an upstream CMakeLists with
# `cmake_minimum_required(VERSION 3.0)`, which CMake 4 (the version
# bundled in the wheel build env) refuses to accept. The
# CMAKE_POLICY_VERSION_MINIMUM env var asks CMake to behave as if 3.5
# were the floor — this is the upstream-recommended workaround until
# pupil-apriltags ships an updated cmake_minimum_required.
#
# Without this the v1 build silently fell back to the cache from an
# earlier state that never actually had the package — symptom: at
# runtime, `import pupil_apriltags` raised ModuleNotFoundError and
# the AprilTag perception path was permanently disabled.
ENV CMAKE_POLICY_VERSION_MINIMUM=3.5
RUN python3 -m pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true && \
    python3 -m pip install --no-cache-dir \
        opencv-contrib-python==4.10.0.84 \
        pupil-apriltags==1.0.4 \
        onnxruntime==1.20.1 \
        pip-licenses==5.0.0 \
        urdf-parser-py==0.0.4 \
        requests==2.32.3 && \
    python3 -c "import cv2, onnxruntime, pupil_apriltags, requests; print('runtime deps OK:', cv2.__version__, onnxruntime.__version__)"

# YOLOX-tiny ONNX (Megvii, Apache-2.0). Pinned release URL + SHA-256
# so the binary is stable across builds and a future GitHub release
# re-upload can't silently swap weights past us. If you ever bump the
# YOLOX version, recompute the digest with:
#     sha256sum /opt/edubotics/yolox_tiny.onnx
#
# Image-size impact: ~19.3 MiB (well under the 100 MiB plan budget).
RUN set -e && \
    mkdir -p /opt/edubotics && \
    curl -fL --retry 3 --retry-delay 5 -o /opt/edubotics/yolox_tiny.onnx \
        https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx && \
    echo '427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7  /opt/edubotics/yolox_tiny.onnx' \
        | sha256sum -c -

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
