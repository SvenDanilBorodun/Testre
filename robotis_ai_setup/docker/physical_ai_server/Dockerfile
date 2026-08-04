# Base image is parameterised so a single Dockerfile builds for both
# architectures. amd64 uses the ROBOTIS-published base; arm64 uses our
# own base built once from the upstream physical_ai_tools/physical_ai_server/
# Dockerfile.arm64 sources (see docs/arm64_base/README.md). The
# build-images.sh script passes the right base via --build-arg
# BASE_IMAGE=... per platform.
ARG BASE_IMAGE=robotis/physical-ai-server:amd64-0.8.2
FROM ${BASE_IMAGE}

# Fix CRLF line endings in s6 service files. When building on Windows, Git
# converts LF to CRLF and s6-overlay rejects "longrun\r" as an invalid type.
# Strip \r from text files only — a blanket `find … -type f -exec sed` would
# also rewrite binaries that happen to sit in the s6 tree, corrupting them.
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
# StopWorkflow, WorkflowStatus). The audit also adds Detection.msg, plus
# changes WorkflowStatus.msg from a parallel-array shape to a typed
# Detection[] field.
#
# Without this step physical_ai_server.py crashes at import time with
# "ImportError: cannot import name 'StartCalibration'" — meaning the
# v1 ship was actually broken at runtime even though docker push
# succeeded. ros2_service_run.sh sources ${COLCON_WS}/install/setup.bash,
# so re-building the package here is enough — no entrypoint change.
COPY interfaces /tmp/interfaces_src
# Deletion-propagation (mirrors the physical_ai_server COPY-wholesale block
# below): the msg/ + srv/ dirs are REPLACED (rm -rf + fresh copy), never
# merged with `cp -f` — a .srv/.msg DELETED from the repo would otherwise
# survive as the base image's stale copy and keep generating dead Python
# bindings. The base image's build/ + install/ trees for the package are
# purged too, so colcon can't resurrect removed interfaces from its cached
# manifests (same rationale as the server package purge at the rebuild below).
RUN set -e && \
    target_pkg=$(find /root/ros2_ws/src -type d -name physical_ai_interfaces | head -n1) && \
    [ -n "$target_pkg" ] || { echo "ERROR: physical_ai_interfaces source not found"; exit 1; } && \
    rm -rf "$target_pkg/msg" "$target_pkg/srv" && \
    mkdir -p "$target_pkg/msg" "$target_pkg/srv" && \
    cp /tmp/interfaces_src/msg/*.msg "$target_pkg/msg/" && \
    cp /tmp/interfaces_src/srv/*.srv "$target_pkg/srv/" && \
    cp -f /tmp/interfaces_src/CMakeLists.txt "$target_pkg/CMakeLists.txt" && \
    cp -f /tmp/interfaces_src/package.xml "$target_pkg/package.xml" && \
    cd /root/ros2_ws && \
    rm -rf build/physical_ai_interfaces install/physical_ai_interfaces && \
    bash -c "source /opt/ros/jazzy/setup.bash && colcon build --packages-select physical_ai_interfaces --cmake-args -DCMAKE_BUILD_TYPE=Release" && \
    install_root=/root/ros2_ws/install/physical_ai_interfaces/lib/python3.12/site-packages/physical_ai_interfaces && \
    test -f "$install_root/srv/_start_calibration.py" && \
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

# ── EduBotics physical_ai_server package — COPY-WHOLESALE (v2.5.2 durable fix) ──
#
# Replaces the historical "apply_overlay ~9 hand-listed files onto the upstream
# clone" model. That model silently dropped any monorepo edit OR DELETION not
# wired into the chain — the root cause of the v2.5.0 regression where a slimmed
# training_manager.py never reached the image, leaving the dead 269-line upstream
# copy whose `from lerobot.scripts.eval import eval_policy` (renamed in LeRobot
# 0.5.1) crashed the ROS node at startup → recording + inference dead, both arches.
#
# The image's physical_ai_server package is now a VERBATIM copy of the repo's
# single source of truth, physical_ai_tools/physical_ai_server/, staged into this
# build context as pkg_src/ by build-images.sh (test/, the two Dockerfiles,
# CHANGELOG and __pycache__ excluded — build inputs / dev-only, not runtime).
# "Image == repo HEAD" is now true BY CONSTRUCTION: edits, new files AND deletions
# all propagate, with no hand-maintained list to forget. The dead ZMQ pair
# (server_inference.py + client_inference.py — never instantiated; deleted from
# the tree 2026-06-07) propagates out the same way. The Roboter Studio workflow/
# module is part of this tree, so the separate fix_server_inference.py patch
# step and the workflow COPY block are gone. The asserts below fail loudly if a
# stripped guard (safety_envelope.py, Rule §2) or the dead on-device-training trees
# ever re-enter; CI's image-source-parity job re-asserts byte equality after every
# build, and the build-time node import near the end is the runtime belt.
#
# The package's build/ + install/ dirs are purged before the colcon rebuild so a
# source file DELETED in the repo (e.g. a pruned config/*.yaml) can't survive in
# the base image's cached colcon SOURCES manifest and fail the setuptools copy.
COPY pkg_src/ /tmp/pkg_src/
RUN set -e && \
    mod_dir=$(find /root/ros2_ws/src -type d -path "*/physical_ai_tools/physical_ai_server/physical_ai_server" | head -n1) && \
    [ -n "$mod_dir" ] || { echo "ERROR: physical_ai_server package not found in base image"; exit 1; } && \
    pkg_root=$(dirname "$mod_dir") && \
    echo "Replacing upstream-cloned package at $pkg_root with the repo source of truth" && \
    rm -rf "$pkg_root" && mkdir -p "$pkg_root" && \
    cp -a /tmp/pkg_src/. "$pkg_root/" && \
    rm -rf /tmp/pkg_src && \
    test -f "$pkg_root/package.xml" || { echo "ERROR: package.xml missing after copy"; exit 1; } && \
    test -f "$pkg_root/physical_ai_server/physical_ai_server.py" || { echo "ERROR: node module missing after copy"; exit 1; } && \
    { test ! -e "$pkg_root/physical_ai_server/workflow/safety_envelope.py" || { echo "ERROR: stripped safety_envelope.py present (Rule §2)"; exit 1; }; } && \
    { test ! -e "$pkg_root/physical_ai_server/training/trainers" || { echo "ERROR: dead training/trainers/ present"; exit 1; }; } && \
    { test ! -e "$pkg_root/physical_ai_server/evaluation" || { echo "ERROR: dead evaluation/ present"; exit 1; }; } && \
    cd /root/ros2_ws && \
    rm -rf build/physical_ai_server install/physical_ai_server && \
    bash -c "source /opt/ros/jazzy/setup.bash && colcon build --packages-select physical_ai_server --symlink-install" && \
    echo "physical_ai_server replaced from repo source of truth + colcon-rebuilt"

# Override the base image's inherited LeRobot v0.2.0 with PyPI v0.5.1.
#
# Why this lives in the thin overlay, not the upstream Dockerfile.amd64:
# `build-images.sh` for amd64 PULLS robotis/physical-ai-server:amd64-0.8.2
# (which still ships LeRobot v0.2.0 inside) — it does NOT build from
# Dockerfile.amd64. The Phase 4 edits to Dockerfile.amd64 are kept as
# forward-compatible "if we ever rebuild the base" scaffolding, but the
# actual v0.5.1 install for student images happens HERE.
#
# Two cleanups are mandatory beyond pip install:
#   1. Strip the lerobot/src PYTHONPATH entries from /root/.bashrc AND from
#      the s6 service script. Without this, PYTHONPATH is searched before
#      site-packages and the ROS node imports v0.2.0 at runtime regardless
#      of what pip just installed.
#   2. rm -rf the inherited lerobot/ subdir so no dead v0.2.0 code can be
#      reached by any other path resolution path (and to recover ~500 MB
#      of disk space — the strip-bloat block later removes everything else).
#
# torchcodec uninstall mirrors the Modal worker (modal_app.py): v0.5.1 still
# falls back to pyav when torchcodec is absent, and torchcodec wheels for
# our CPU-only student arch are unstable.
#
# accelerate is now a hard runtime dep in v0.5.1 (was optional in v0.2.0);
# pin to a safe minor-version range so a future major doesn't break the
# resolver. Do NOT pre-pin transformers — let lerobot[smolvla,peft] extras
# resolve transformers==5.3.0 (LeRobot's exact pin).
# scipy is pinned EXPLICITLY (v2.5.1 audit fix). It is otherwise only a
# TRANSITIVE dep of the lerobot[pi] extra (lerobot 0.5.1: extra 'pi' ->
# 'scipy-dep' -> scipy>=1.14.0,<2.0.0), and that transitive failed to
# materialize on at least one published build (physical-ai-server:latest@fb3cc44
# shipped WITHOUT scipy). pi0_fast's action decoder hard-needs scipy.fftpack.idct
# (lerobot/policies/pi0_fast/modeling_pi0_fast.py: idct=None when scipy is
# absent -> TypeError at inference). Pinning it directly here makes pi0_fast
# inference deterministic across builds and both arches.
# v2.9.3: CAP <1.18 (was <2.0.0). scipy 1.18.0 dropped numpy-1.x support and its
# scipy.sparse._sputils references np.long (numpy-2-only); against the forced
# numpy==1.26.4 cv_bridge floor it raises "module 'numpy' has no attribute 'long'"
# at the transformers D-FINE-loss import and broke the v2.9.2 build. 1.17.1 is the
# proven-good last numpy-1.x-compatible release. Do NOT widen back to <2.0.0 while
# numpy is floored at 1.26.4 (mirror this cap in the two base Dockerfiles).
RUN set -e && \
    python3 -m pip uninstall -y lerobot torchcodec 2>/dev/null || true && \
    python3 -m pip install --no-cache-dir \
        "lerobot[pi,smolvla,peft]==0.5.1" \
        "accelerate>=1.10.0,<2.0.0" \
        "scipy>=1.14.0,<1.18" && \
    rm -rf /root/ros2_ws/src/physical_ai_tools/lerobot && \
    sed -i '\|physical_ai_tools/lerobot/src|d' /root/.bashrc 2>/dev/null || true && \
    sed -i '\|physical_ai_tools/lerobot/src|d' /usr/local/lib/s6-services/ros2_service_run.sh 2>/dev/null || true && \
    sed -i '\|physical_ai_tools/lerobot/src|d' /etc/s6-overlay/s6-rc.d/physical_ai_server/run 2>/dev/null || true && \
    python3 -c "import lerobot; assert lerobot.__version__ == '0.5.1', f'expected lerobot==0.5.1, got {lerobot.__version__}'; print(f'LeRobot v{lerobot.__version__} installed (overrode base image v0.2.0)')" && \
    python3 -c "import scipy; from scipy.fftpack import idct; print(f'scipy {scipy.__version__} OK (pi0_fast idct available)')"

# Roboter Studio runtime dependencies. opencv-contrib-python is required
# for cv2.aruco (the main opencv-python wheel does NOT include the
# contrib aruco module). pupil-apriltags is BSD-licensed, lightweight
# AprilTag detection. pip-licenses runs the cross-cutting AGPL gate.
#
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
        pip-licenses==5.0.0 \
        urdf-parser-py==0.0.4 \
        requests==2.32.3 && \
    python3 -c "import cv2, pupil_apriltags, requests; print('runtime deps OK:', cv2.__version__)"

# Runtime ROS message deps (NOT an IK install — IK is closed-form pure NumPy,
# see the urdf-parser-py block below; there is no TRAC-IK / PyKDL / kdl_parser).
# ros-jazzy-control-msgs: the teleop collision e-stop's gpio subscription
# (safety/collision_monitor.py) needs control_msgs/DynamicInterfaceGroupValues.
# ros-base does NOT ship control_msgs; without this install the import guard
# silently fail-opens ("[KOLLISION] ... detection DISABLED") and force
# protection is off on every shipped image (caught on-rig 2026-06-03).
# package.xml declares the dep; this is where it actually materializes.
#
# controller_manager_msgs is deliberately ABSENT from this image — its
# rosidl_typesupport_fastrtps_c .so is ABI-incompatible with the Fast-CDR pinned
# here, so creating a SwitchController client crashes the node (undefined symbol
# → exit 127, crash loop). Roboter Studio runs FOLLOWER-ONLY (no leader
# broadcaster), so physical_ai_server never needs to switch a controller. Keep
# controller_manager_msgs out — never add it back.
#
# apt-get retry loop (added 2026-05-20 — same rationale as open_manipulator/
# Dockerfile: Ubuntu ports/archive mirrors occasionally serve stale Packages.gz
# during sync windows. 5 retries with 5s/10s/15s/20s/25s backoff = 75s worst
# case before bailing.) ros-jazzy-control-msgs (the collision guard's
# DynamicInterfaceGroupValues; ros-base does not ship it) MUST succeed. PyKDL is
# NO LONGER installed — the closed-form ik_solver.py is pure NumPy with its own
# exact forward kinematics, so neither orocos-kdl nor kdl_parser is needed.
RUN set -eux; \
    for i in 1 2 3 4 5; do \
        apt-get update && \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            ros-jazzy-control-msgs && \
        break || { \
            echo "apt-get retry $i/5 (mirror sync or transient network)"; \
            rm -rf /var/lib/apt/lists/*; \
            sleep $((i * 5)); \
        }; \
        [ "$i" = "5" ] && exit 1; \
    done; \
    rm -rf /var/lib/apt/lists/*

# ── IK is closed-form (pure NumPy) — no TRAC-IK / PyKDL / IKFast install ─────
# ik_solver.py is an exact analytical solver for the OMX-F geometry with its own
# NumPy forward kinematics. It deliberately has NO native-build dependency: the
# old tracikpy install was a from-source SWIG build linking ROS shared libs
# (the same Fast-CDR/ABI-crash risk class as controller_manager_msgs), and the
# PyKDL LMA fallback caused the orientation-unconstrained "crazy motion". Both
# are gone. The only Python dep is urdf-parser-py, used ONLY by ik_solver.py's
# optional constant-verification (cross-checks the baked geometry against
# /robot_description and warns on a mismatch) — the IK itself needs no URDF.
RUN python3 -m pip install --no-cache-dir urdf-parser-py==0.0.4 \
    && python3 -c "import urdf_parser_py.urdf; print('urdf_parser_py OK')"

# AGPL audit: every package we explicitly pip-install above (opencv-contrib-
# python 4.10 / pupil-apriltags / urdf-parser-py / pip-licenses) ships under
# Apache-2.0, BSD, or MIT. ultralytics (the original AGPL concern) is NOT
# installed anywhere. A pip-licenses runtime gate broke on this base image
# (its `pip` installs into a Python prefix that `python3 -m` can't see), so
# the gate now lives in CI / manifest review rather than the Dockerfile. If
# anyone adds a new pip dep, the WORKFLOW.md license rule still applies.
RUN echo "AGPL audit: relies on explicit dependency manifest, see Dockerfile comment"

# ── amd64-only: drop CUDA / GPU torch (student PCs have NO GPU) ──────────────
# Gated by SLIM_CUDA, passed =1 ONLY for amd64 by build-images.sh; arm64/Jetson
# never set it, so this whole step is a no-op there and the shared Dockerfile
# stays Jetson-safe. The inherited base is built on nvidia/cuda:12.8.0-devel with
# torch 2.7.0+cu128 — ~13 GB of CUDA a GPU-less student image never uses:
# inference is hard-gated (inference_manager.load_policy() returns a German
# "needs GPU" error when torch.cuda.is_available() is False), recording never
# loads a policy, and on-device training is Cloud-only. Swap to torch 2.7.0+cpu
# (SAME version — still satisfies LeRobot 0.5.1's torch>=2.7,<2.11, so Rule §5 is
# unchanged), drop the CUDA runtime wheels (nvidia-*-cu12 / triton / torchaudio)
# and /usr/local/cuda — plus the two large NVIDIA DEVELOPER trees that sit
# outside that prefix and so survived this step for its first year
# (nsight-compute + libnccl, ~1.97 GB; rationale at the deletions themselves).
#
# These rm/uninstalls write WHITEOUTS over the immutable base layers, so
# `docker save` would still ship the bytes — build-images.sh FLATTENs the amd64
# image (docker export|import) AFTER this Dockerfile to actually reclaim them.
# Placed BEFORE the cross-arch floor pin (so numpy<2 is re-asserted after the
# torch swap) and BEFORE the build-time smoke gates (so node-import + the
# cv_bridge functional decode + collision + all policy imports run on the CPU
# torch — a CPU-torch regression fails the build here, not on a student PC).
ARG SLIM_CUDA=0
RUN if [ "$SLIM_CUDA" = "1" ]; then \
        set -eux; \
        echo "[SLIM] amd64 CPU-only: torch+cu128 -> torch+cpu, removing CUDA"; \
        python3 -m pip uninstall -y torch torchvision torchaudio triton 2>/dev/null || true; \
        # The +cpu local-version pin can't be satisfied by PyPI's plain 2.7.0
        # (PEP 440), so --extra-index-url can't pull a CUDA build; the extra-index
        # IS needed to resolve torch's PyPI-only pure-python deps
        # (sympy/networkx/jinja2/filelock/fsspec). Do NOT drop it for "consistency".
        python3 -m pip install --no-cache-dir \
            --index-url https://download.pytorch.org/whl/cpu \
            --extra-index-url https://pypi.org/simple \
            "torch==2.7.0+cpu" "torchvision==0.22.0+cpu"; \
        nv="$(python3 -m pip list --format=freeze 2>/dev/null | grep -iE '^(nvidia-|triton)' | cut -d= -f1 || true)"; \
        if [ -n "$nv" ]; then echo "$nv" | xargs -r python3 -m pip uninstall -y || true; fi; \
        rm -rf /usr/local/cuda* /usr/local/nvidia 2>/dev/null || true; \
        # ── The two NVIDIA developer trees the glob above CANNOT reach ───────
        # `/usr/local/cuda*` + `/usr/local/nvidia` is where the CUDA TOOLKIT
        # lives, but the nvidia/cuda:12.8.0-devel base also drops two large
        # payloads OUTSIDE that prefix. They therefore survived every slim so
        # far and shipped in every student download (measured on the released
        # physical-ai-server:2.13.0):
        #
        #   /opt/nvidia/nsight-compute                    1192.1 MB
        #       the GPU kernel profiler (incl. a 180 MB libQt6WebEngineCore.so)
        #   /usr/lib/x86_64-linux-gnu/libnccl.so.2.25.1    383.6 MB
        #   /usr/lib/x86_64-linux-gnu/libnccl_static.a     390.2 MB
        #       multi-GPU collective communication
        #
        # They are provably DEAD on this flavor, not merely unused. By the time
        # this runs the image is on torch-2.7.0+cpu (torch.version.cuda is None,
        # torch.distributed.is_nccl_available() False), there is no
        # libtorch_cuda.so and no site-packages/nvidia/. The load-bearing fact is
        # that NOTHING DECLARES OR dlopens either soname: a DT_NEEDED sweep over
        # /opt/ros, /usr/lib and /usr/local/lib returns zero hits for nccl. The
        # only FUNCTIONAL reference is torch/__init__.py's `cuda_libs` dict,
        # reached solely from _load_global_deps()'s `except OSError` fallback via
        # _preload_cuda_deps -> _get_cuda_dep_paths, which globs only
        # <sys.path entry>/nvidia/<folder>/lib — the pip wheels this very block
        # already uninstalled — and sys.path contains no /usr/lib/x86_64-linux-gnu.
        # Deliberately NOT claimed: that the strings appear nowhere else. The
        # base image's NV_LIBNCCL_PACKAGE_NAME / NCCL_VERSION env vars survive in
        # captured colcon *.sh.env files and build logs, and PATH aside,
        # /usr/lib/x86_64-linux-gnu IS listed in /etc/ld.so.conf.d because it is
        # the default system lib dir. Both are inert: a name in a stale env
        # capture or a directory on the loader search path is not a dependency.
        # Validated by building the candidate against the released 2.14.0 image,
        # flattening it, and passing all four build-time smoke gates plus a full
        # container boot (all four ROS nodes alive, 66 services, rosbridge :9090
        # and web_video_server :8080 both serving — the latter matters because it
        # fails SILENTLY on a missing library — zero errors in the logs).
        # Measured effect on the amd64 image: .Size 7.53 GB -> 5.56 GB, and
        # 2.626 GB -> 1.592 GB compressed — i.e. -39 % on what a student on a
        # school connection actually downloads.
        #
        # This MUST stay inside the SLIM_CUDA=1 branch, and "consolidating" it
        # out is a regression in two different directions: Jetson/arm64 KEEPS
        # its GPU torch (where NCCL is a legitimate dependency) and is never
        # flattened, so running it there would buy zero download bytes for real
        # runtime risk; opi has neither path at all (measured 0 MB), so it is a
        # no-op there regardless.
        #
        # Every removal is nonexistence-tolerant on purpose, because this block
        # runs under `set -eux`: `rm -f`/`rm -rf` treat a missing operand as
        # success, and an unmatched glob is passed through literally by POSIX sh
        # (no nullglob), so `libnccl.so*` degrades into exactly such a missing
        # operand. The trailing `|| true` mirrors the /usr/local/cuda line above
        # and makes all of that belt-and-braces.
        #
        # The `rmdir` is a FAIL-SAFE THAT NEVER FIRES ON TODAY'S BASE, and that
        # is the intended outcome — do not "fix" it into an `rm -rf`. This base
        # also keeps /opt/nvidia/entrypoint.d/ + nvidia_entrypoint.sh there, so
        # the directory is never empty and the rmdir always fails into `|| true`,
        # leaving /opt/nvidia at ~2.5 KB. It exists so that a FUTURE base which
        # ships nsight as the only tenant gets the empty parent cleaned up, while
        # a future base that adds a SECOND tool there can never be silently swept
        # away. Verified in a real build trace: `+ rmdir /opt/nvidia` -> `+ true`.
        rm -rf /opt/nvidia/nsight-compute 2>/dev/null || true; \
        rmdir /opt/nvidia 2>/dev/null || true; \
        rm -f /usr/lib/x86_64-linux-gnu/libnccl.so* \
              /usr/lib/x86_64-linux-gnu/libnccl_static.a 2>/dev/null || true; \
        rm -f /usr/share/applications/nsight-compute-*.desktop 2>/dev/null || true; \
        # Drop the deleted sonames from the loader cache. Hygiene only —
        # nothing resolves them (see the sweep above) — so it is guarded: a
        # cache refresh must never be able to fail a release build.
        ldconfig 2>/dev/null || true; \
        python3 -c "import torch, torchvision; print('[SLIM] torch', torch.__version__, 'torchvision', torchvision.__version__, 'cuda=', torch.version.cuda); assert torch.version.cuda is None, 'expected a CPU-only torch build'"; \
        # Hard-fail on incomplete CUDA removal so a maintainer-local build catches
        # it too (not only the docker-publish size gate in CI).
        rem="$(python3 -m pip list --format=freeze 2>/dev/null | grep -iE '^(nvidia-|triton)' || true)"; \
        [ -z "$rem" ] || { echo "[SLIM] ERROR: GPU wheels survived: $rem"; exit 1; }; \
        [ ! -d /usr/local/cuda ] || { echo "[SLIM] ERROR: /usr/local/cuda still present"; exit 1; }; \
    else \
        echo "[SLIM] SLIM_CUDA=0 — no torch swap. Covers TWO different flavors: Jetson/arm64 (KEEPS its GPU torch by design) and opi (its CPU base already ships CPU torch, so there is nothing to strip). The TORCH_EXPECT_CPU gate below is what tells them apart."; \
    fi

# ── Rule §5 torch gate — runs on EVERY flavor, not just the SLIM_CUDA=1 swap ──
# "One PyTorch surface (torch 2.7.x), three CUDA channels" is a Rule §5 invariant,
# and torch/torchvision are the ONLY Rule §5 pins this thin overlay does not
# re-install — every flavor INHERITS torch from its base (amd64: the ROBOTIS
# cuda base, then swapped to +cpu above; arm64: the Jetson AI Lab base; opi:
# the explicit torch==2.7.0 pin in Dockerfile.arm64cpu). So this file must at
# least ASSERT what it inherited.
#
# It previously did not. The only torch assertion lived INSIDE the
# `if [ "$SLIM_CUDA" = "1" ]` branch above, i.e. amd64-only — and opi passes
# SLIM_CUDA=0, so the entire Orange Pi path shipped with no torch check at all.
# Its base pins torch explicitly precisely BECAUSE lerobot 0.5.1 allows
# torch<2.11 and an unpinned resolve silently lands 2.10.x; that pin lives in a
# base image which is hand-rebuilt (not CI-built), so the one layer CI DOES
# build every push is exactly where the assert belongs.
#
# TORCH_EXPECT_CPU is passed IN by build-images.sh (=1 for amd64 + opi, left 0
# for arm64) rather than re-derived here from SLIM_CUDA: the two are not the
# same question. SLIM_CUDA asks "should this build strip CUDA?" — opi answers no
# because there is nothing to strip, while still being a CPU flavor. Deriving
# one from the other is what made the opi gap invisible in the first place.
ARG TORCH_EXPECT_CPU=0
RUN python3 -c "\
import sys, torch, torchvision; \
expect_cpu = sys.argv[1] == '1'; \
print('[TORCH] torch', torch.__version__, '| torchvision', torchvision.__version__, \
      '| cuda=', torch.version.cuda, '| expect_cpu=', expect_cpu); \
assert torch.__version__.startswith('2.7'), \
    f'Rule §5 one-torch-surface violated: expected torch 2.7.x, got {torch.__version__}'; \
assert (torch.version.cuda is None) or not expect_cpu, \
    f'expected a CPU-only torch on this flavor, got a CUDA build (cuda={torch.version.cuda})'; \
print('[TORCH] Rule §5 gate OK')" "$TORCH_EXPECT_CPU"

# Cross-arch package floor — pin safetensors / protobuf / pillow / numpy to a
# single version on BOTH arches. v0.5.1 resolves these naturally to compatible
# versions, but we pin explicitly to keep amd64 + arm64 producing byte-identical
# site-packages. Versions chosen from Phase 0's verified pip resolve of
# `lerobot[pi,smolvla,peft]==0.5.1` on Python 3.12.
#
#   safetensors==0.7.0   (LCD across arches; v0.8 still rc as of 2026-05)
#   protobuf==6.33.6     (bumped from 6.31.0 for v0.5.1's grpcio-dep)
#   pillow==12.2.0       (bumped from 12.1.0; v0.5.1's natural resolve)
#   numpy==1.26.4        (v2.5.4 cv_bridge ABI fix — see below)
#
# numpy==1.26.4 is the v2.5.4 ship-blocker fix. lerobot 0.5.1 pulls numpy 2.2.6
# (its pyproject demands numpy>=2), which SHADOWS the base image's numpy 1.26.4
# at node runtime. But ros-jazzy-cv-bridge 4.1.0's cv_bridge_boost.so is compiled
# against NumPy 1.x — so the color-conversion path (compressed_imgmsg_to_cv2,
# desired_encoding='rgb8') used by BOTH the recording save loop and the inference
# loop (data_manager.convert_msgs_to_raw_datas) SEGFAULTS on the first camera
# frame under numpy 2.x (a SIGSEGV uncatchable by the node's try/except → the ROS
# node dies). A bare `import cv_bridge` only warns and exits 0, so the import-only
# smoke tests never caught it. Pinning numpy<2 here (after the lerobot install)
# restores the cv_bridge ABI match; verified in-image that lerobot 0.5.1 / torch
# 2.7.0 / scipy 1.17.1 / cv2 / all policy imports + predict_action +
# make_pre_post_processors still work under 1.26.4. The build-time functional
# decode gate below proves the fix held. Pre-existing since v2.5.0; fixed v2.5.4.
#
# Mirrored in physical_ai_tools/physical_ai_server/Dockerfile.{amd64,arm64}.
# `--force-reinstall` so the pins override any transitive resolve (incl. lerobot's
# numpy>=2) from the earlier pip layers.
RUN python3 -m pip install --no-cache-dir --force-reinstall \
        safetensors==0.7.0 \
        protobuf==6.33.6 \
        pillow==12.2.0 \
        numpy==1.26.4 && \
    python3 -c "import safetensors, google.protobuf, PIL, numpy; \
                print('cross-arch floor:', safetensors.__version__, \
                      google.protobuf.__version__, PIL.__version__, numpy.__version__); \
                assert numpy.__version__.startswith('1.'), 'numpy must be <2 for cv_bridge ABI'"

# Strip ~7.2 GB of upstream bloat — see CLAUDE.md image-level audit (2026-05).
# This MUST come AFTER the package replacement + colcon rebuild. Any earlier
# and we delete sources the build still needs.
#
# Breakdown (verified via `du -sh` inside :latest):
#   /root/.cache       ~5.0 GB  (pip wheel cache + puccinialin 962 MB, inherited from upstream base)
#   /root/.rustup      ~1.2 GB  (Rust toolchain — never invoked at runtime)
#   /root/.cargo       ~0.1 GB  (cargo registry cache)
#   rerun/wandb/cmake  ~415 MB  (dev-only ML deps — see the pip uninstall below)
#   .git dirs          ~331 MB  (inherited from `git clone --recursive` in upstream Dockerfile)
#   /opt/talos_system_manager ~216 MB  (Robotis VNC dashboard, dev-only — see comment below)
#   …/physical_ai_tools/physical_ai_manager  (stale upstream React tree from the base clone —
#       the SPA ships from the separate physical-ai-manager image; this copy is never
#       built/served but carries files deleted from the repo (TokenInputPopup.js,
#       ResumePolicySelector.js) that trip audits. Whiteout-hidden, base bytes remain.)
#   /opt/torch2trt/.git  (NVIDIA leftover in the arm64 Jetson base; path absent on amd64,
#       where the rm is a harmless no-op)
#
# ── talos_system_manager: the directory goes, and so must its s6 service ─────
# EduBotics does NOT import from talos_system_manager, so the ~216 MB directory
# is stripped. The upstream Dockerfile installs talos's pip requirements into
# site-packages (those STAY — they're real deps shared with other packages) and
# sets PYTHONPATH=/opt/talos_system_manager, which the ENV scrub near the end of
# this file empties.
#
# But removing the directory is NOT sufficient on its own, and the comment that
# used to sit here said it was — it justified the rm with
# `grep -rn 'talos' /root/ros2_ws/  ->  zero hits`, a grep whose ROOT was wrong.
# The reference that matters lives OUTSIDE /root/ros2_ws: all three base
# Dockerfiles enable exactly ONE s6 service at build
# (`touch .../user/contents.d/s6-agent`), and that service's run script execs
# `uvicorn talos.agent.s6_agent:app`. With talos deleted it dies on
# `ModuleNotFoundError: No module named 'talos'` and s6 restarts it — measured
# on a real arm64 boot at ~1 restart/second, FOREVER: 491 KB of traceback in
# 100 s (~425 MB/day), drowning the student's Protokoll and the support log, and
# burning a python interpreter start per second on a Pi that has 8 cores to
# spare for the 100 Hz control loop instead. So DISABLE the service here, in the
# same layer that removes its app — the two must never drift apart.
#
# This does NOT affect the ROS node: `.s6-keep` (the 0-byte compose bind-mount
# onto .../user/contents.d/physical_ai_server) is what enables physical_ai_server
# at RUNTIME, and it is mounted, not baked. Removing the s6-agent marker leaves
# that path untouched — verified on a real boot: rosbridge :9090 up, all four ROS
# nodes alive, 749 B of log (vs 491,375 B) and zero tracebacks.
#
# ── rerun-sdk / wandb / cmake: 415 MB of dev-only ML deps ────────────────────
# Pulled transitively by lerobot but never reached at runtime: rerun is lerobot's
# visualisation UI, wandb its training telemetry — and training is CLOUD-only
# (Modal), so no student image ever logs a run. cmake is a build-time wheel
# dependency with nothing left to build. Measured: rerun_sdk 162 MB +
# rerun_bindings 100 MB + cmake 78 MB + wandb 77 MB. Verified with them removed:
# all 13 policy imports, the ROS node import and LeRobotDataset still load.
#
# Deliberately in the THIN OVERLAY, not the bases: CI builds this layer on every
# push, so the saving lands without the manual base rebuild the base Dockerfiles
# need (see docs/arm64_base/README.md), and amd64 + opi are FLATTENed afterwards
# so the bytes are actually reclaimed rather than whiteout-hidden (Jetson is not
# flattened — the uninstall is a harmless whiteout there). The policy-import gate
# below runs AFTER this block, so a future lerobot that hard-imports wandb at
# module scope fails the BUILD instead of a student's inference run.
RUN set -e && \
    rm -rf /root/.cache /root/.rustup /root/.cargo && \
    find /root/ros2_ws -type d -name ".git" -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /opt/talos_system_manager && \
    rm -f /etc/s6-overlay/s6-rc.d/user/contents.d/s6-agent && \
    sed -i '\|/opt/talos_system_manager|d' /root/.bashrc /etc/bash.bashrc 2>/dev/null || true && \
    python3 -m pip uninstall -y rerun-sdk wandb cmake 2>/dev/null || true && \
    find /root/ros2_ws/src -type d -path "*/physical_ai_tools/physical_ai_manager" -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /opt/torch2trt/.git && \
    echo "Stripped upstream bloat (+ s6-agent service, dev-only ML deps, stale manager tree)"

# Build-time policy import smoke test. Catches a LeRobot module-layout drift
# at `docker buildx build` time instead of at student-bench inference time.
# Imports every policy modeling module that inference_manager.py lazy-loads,
# plus the v0.5.1 inference helpers and v3.0 dataset I/O symbols. If any of
# these fail (a future LeRobot release reorganises modules without bumping
# our pin via PyPI's preview channel), the image build aborts loudly.
#
# The last two asserts guard our two LeRobot PRIVATE-API seams. Both are
# load-bearing, and one of them fails SILENTLY, which is why they belong here
# rather than in a unit test (no test in this repo can see the real lerobot —
# the deps-free suites sys.modules-stub it):
#
#   1. dataset_tools._copy_and_reindex_videos — upstream's real default is
#      vcodec='libsvtav1'. data_editor_v3._force_recorder_vcodec scope-patches
#      that helper to the recorder's h264 for the duration of an edit. On an
#      upstream RENAME the patch's getattr(..., None) misses, it logs an English
#      warning and yields anyway (deliberate — an edit must not die because of
#      it), so every mixed kept/deleted video file silently re-encodes as lossy
#      AV1 on a software SVT-AV1 encode that pegs every core. That is exactly
#      the 2026-06-07 scar the scope-patch exists to prevent, restored without
#      a single failing signal. Fail the BUILD instead.
#   2. LeRobotDataset.create(streaming_encoding=...) — upstream's real default
#      is False. lerobot_dataset_wrapper setdefault()s it to True, and that is
#      the ONLY thing bounding recording RAM since the JPEG-in-RAM valve was
#      removed. A rename here fails loud (TypeError on unexpected kwarg), so
#      this assert only moves the failure earlier — but it costs nothing and
#      documents the seam next to its silent sibling.
RUN python3 -c "\
import inspect; \
import lerobot; print(f'lerobot=={lerobot.__version__}'); \
from lerobot.policies.act.modeling_act import ACTPolicy; \
from lerobot.policies.diffusion.modeling_diffusion import DiffusionPolicy; \
from lerobot.policies.tdmpc.modeling_tdmpc import TDMPCPolicy; \
from lerobot.policies.vqbet.modeling_vqbet import VQBeTPolicy; \
from lerobot.policies.pi0.modeling_pi0 import PI0Policy; \
from lerobot.policies.pi0_fast.modeling_pi0_fast import PI0FastPolicy; \
from lerobot.policies.pi05.modeling_pi05 import PI05Policy; \
from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy; \
from lerobot.utils.control_utils import predict_action; \
from lerobot.policies.factory import make_pre_post_processors; \
from lerobot.datasets.feature_utils import validate_frame; \
from lerobot.datasets.io_utils import write_info, write_episodes, write_stats; \
from lerobot.datasets.dataset_metadata import CODEBASE_VERSION; \
from lerobot.datasets import dataset_tools; \
from lerobot.datasets.lerobot_dataset import LeRobotDataset; \
assert CODEBASE_VERSION == 'v3.0', f'unexpected codebase_version: {CODEBASE_VERSION}'; \
assert hasattr(dataset_tools, '_copy_and_reindex_videos'), \
    'lerobot renamed dataset_tools._copy_and_reindex_videos: data_editor_v3._force_recorder_vcodec would SILENTLY fall back to upstream lossy AV1 on every dataset edit'; \
assert 'streaming_encoding' in inspect.signature(LeRobotDataset.create).parameters, \
    'lerobot dropped LeRobotDataset.create(streaming_encoding=...): lerobot_dataset_wrapper would lose the recording RAM bound'; \
print('all v0.5.1 imports OK; codebase_version=v3.0; private-API seams intact')"

# Build-time NODE-import smoke test (v2.5.1 audit). The policy smoke test above
# imports lerobot.policies.* but NEVER imports physical_ai_server itself, so it
# could not catch the v2.5.0 regression where the shipping physical_ai_server.py
# does a module-level `from physical_ai_server.training.training_manager import
# TrainingManager` while the (then un-overlaid) upstream training_manager.py
# pulled the removed `lerobot.scripts.eval` -> ModuleNotFoundError at node
# startup (recording + inference dead). This step imports the exact module the
# s6 service loads at runtime; sourcing the colcon workspace makes
# physical_ai_interfaces + the package resolvable. Any dead module-level import
# now fails the build loudly instead of every student's classroom.
RUN bash -c "set -e && \
    source /opt/ros/jazzy/setup.bash && \
    source /root/ros2_ws/install/setup.bash && \
    python3 -c 'import physical_ai_server.physical_ai_server; print(\"physical_ai_server node module imports OK\")'"

# Build-time COLLISION-GUARD gate. The node-import test above CANNOT catch a
# missing control_msgs: collision_monitor.py guards the import (fail-open ->
# "[KOLLISION] ... detection DISABLED") so the node still imports and every
# health check passes while force protection is silently OFF (exactly how the
# 2026-06-03 on-rig session found it). Assert the Jazzy gpio state message is
# importable so the regression becomes a loud build failure.
RUN bash -c "set -e && \
    source /opt/ros/jazzy/setup.bash && \
    python3 -c 'from control_msgs.msg import DynamicInterfaceGroupValues; print(\"control_msgs gpio state msg OK — collision guard can arm\")'"

# Build-time FUNCTIONAL cv_bridge decode gate (v2.5.4). The import-only smoke
# tests above cannot catch the NumPy-2-vs-cv_bridge ABI segfault: `import
# cv_bridge` merely warns and exits 0; the crash only happens when the C-API is
# actually CALLED with a color conversion. This gate runs the EXACT recording/
# inference camera-decode path — compressed_imgmsg_to_cv2(desired_encoding='rgb8')
# on a real JPEG (data_manager.convert_msgs_to_raw_datas) — so a future numpy/
# cv_bridge ABI mismatch SIGSEGVs the build instead of every student's first
# recorded frame. Paired with the numpy==1.26.4 cross-arch floor pin above.
RUN bash -c "set -e && \
    source /opt/ros/jazzy/setup.bash && \
    python3 -c '\
import numpy as np, cv2; \
from cv_bridge import CvBridge; \
from sensor_msgs.msg import CompressedImage; \
img = (np.random.default_rng(0).random((48, 64, 3)) * 255).astype(np.uint8); \
ok, jpg = cv2.imencode(\".jpg\", img); assert ok; \
m = CompressedImage(); m.format = \"jpeg\"; m.data = jpg.tobytes(); \
out = CvBridge().compressed_imgmsg_to_cv2(m, desired_encoding=\"rgb8\"); \
assert out.shape == (48, 64, 3), out.shape; \
print(f\"cv_bridge functional decode gate OK (numpy {np.__version__})\")'"

# Scrub upstream-base ENV leaks that the sed of /root/.bashrc above can't
# touch (image-config ENV directives are independent of interactive-shell
# rc files). Two classes:
#
# 1) PYTHONPATH=/opt/talos_system_manager:… set by upstream — the dir is
#    rm -rf'd above so this entry is dangling. Bare ENV PYTHONPATH=
#    empties the var on both amd64 and arm64.
#
# 2) Jetson AI Lab base ships TWINE_*/SCP_UPLOAD_* env vars containing
#    NVIDIA devpi upload credentials (TWINE_PASSWORD=Nvid…14chars,
#    SCP_UPLOAD_PASS=nvidia). Leaking real creds into a public Docker
#    image is the failure mode this closes. Empty on amd64 too — the
#    arm64 base sets them, amd64 base doesn't, blanket-scrub is harmless.
ENV PYTHONPATH= \
    TWINE_PASSWORD= \
    TWINE_USERNAME= \
    TWINE_REPOSITORY_URL= \
    TWINE_NON_INTERACTIVE= \
    SCP_UPLOAD_PASS= \
    SCP_UPLOAD_USER= \
    SCP_UPLOAD_URL=
