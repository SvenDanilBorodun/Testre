#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Camera + colour-profile calibration for Roboter Studio (SCENE-cam only).

The pipeline is a 3-step state machine driven by the React wizard:
    intrinsic (scene) -> extrinsic (scene, board-on-table) -> colour profile

WS4 (2026-06-17) — the GRIPPER camera was DROPPED from calibration. The
gripper camera rides on a 3D-printed eye-in-hand holder that is NOT modelled
in the ``omx_f`` URDF (the URDF has zero camera links), so the software
cannot know where the gripper camera is and cannot aim it. Its eye-in-hand
hand-eye step was therefore unfinishable AND unused at runtime
(``_load_workflow_calibration`` loads scene-only). The whole pick-and-place
path runs on the SCENE camera's table-plane projection
(``projection.project_pixel_to_table``), so calibration is now scene-only.

The SCENE extrinsic is a **single-shot board-on-table** measurement (no arm
motion — the prior eye-to-base flow needed precise IK to drive a
gripper-mounted ChArUco through 14 auto-poses, which the deployed crude IK
cannot do). The student lays the ChArUco board FLAT on the table at a marked
reference position; the static scene cam sees it ONCE; ``solvePnP`` gives
``T_board_to_cam``; combined with the known board-on-table placement
``T_board_to_base`` (see ``_board_to_base_transform``) it yields the
``T_cam_to_base`` extrinsic + ``z_table`` projection plane in one capture.

ChArUco board: 7x5 squares, 30 mm square, 22 mm marker, DICT_5X5_250.

Calibration state is persisted to per-camera YAML files under
CALIB_DIR (a docker named volume mount inside the physical_ai_server
container). Re-running a step overwrites the corresponding YAML.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np


def _safe_float_env(name: str, default: float) -> float:
    """Parse a numeric env override, falling back to ``default`` on a malformed
    value. A bad ``EDUBOTICS_*`` value (an operator typo) must NEVER crash module
    import / manager construction — it degrades to the default with a warning."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        print(
            f'[WARNUNG] {name}={raw!r} ist keine gültige Zahl — '
            f'Standardwert {default} wird verwendet.',
            file=sys.stderr, flush=True,
        )
        return default


def _safe_int_env(name: str, default: int) -> int:
    """Integer counterpart of :func:`_safe_float_env`."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(
            f'[WARNUNG] {name}={raw!r} ist keine gültige Ganzzahl — '
            f'Standardwert {default} wird verwendet.',
            file=sys.stderr, flush=True,
        )
        return default


CALIB_DIR = Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))
INTRINSIC_FRAMES_REQUIRED = 12
# WS4 (2026-06-17): the SCENE extrinsic is now a SINGLE-SHOT board-on-table
# measurement — one capture is enough. (The legacy multi-pose eye-to-base
# constant ``HANDEYE_FRAMES_REQUIRED`` is gone with the arm-motion flow.)
SCENE_EXTRINSIC_FRAMES_REQUIRED = 1

CHARUCO_SQUARES_X = 7
CHARUCO_SQUARES_Y = 5
CHARUCO_SQUARE_LENGTH_M = 0.030
CHARUCO_MARKER_LENGTH_M = 0.022

# ── Board-on-table placement convention (base frame, metres) ────────────────
# The student lays the ChArUco board FLAT on the table, PRINTED SIDE UP, with:
#   * the LONG (7-square, 21 cm) edge running FRONT-BACK — i.e. pointing AWAY
#     from the robot along base +X (NOT left-right),
#   * the SHORT (5-square, 15 cm) edge running LEFT-RIGHT,
#   * the board CENTRED on the robot's forward centre-line,
#   * the ORIGIN corner (the marker-0 / OpenCV (0,0) corner) at the NEAR-LEFT
#     (closest to the robot, on its left).
# These offsets place that OpenCV origin corner in the base frame.
#
# IMPORTANT (2026-06-18, rig-found): OpenCV's ChArUco board frame has its +Z
# axis exiting the BACK of the board. With the board printed-side-UP on the
# table, that +Z therefore points DOWN, so the board orientation in base is a
# 180° flip about board +X (see _board_to_base_transform), NOT identity. With
# the flip + this origin the scene camera resolves correctly ABOVE the table
# and the board lands centred in front. (The old identity convention + a
# 21-cm-wide / origin-on-the-right comment was never rig-validated and made the
# camera resolve BELOW the table → the "Kamera nicht über dem Tisch" failure.)
# z = 0.0 = table surface coincides with the OMX base mounting plane (link0).
# All overridable per-classroom via env (forwarded by compose); a jig that
# holds the board at a different yaw sets BOARD_YAW_DEG once.
BOARD_ORIGIN_X_M = _safe_float_env('EDUBOTICS_BOARD_ORIGIN_X_M', 0.18)
BOARD_ORIGIN_Y_M = _safe_float_env('EDUBOTICS_BOARD_ORIGIN_Y_M', 0.075)
BOARD_TABLE_Z_M = _safe_float_env('EDUBOTICS_BOARD_TABLE_Z_M', 0.0)
# Yaw (deg) of the board's OpenCV frame about base +Z. With the 3D-printed
# L-jig the board butts square to the base, so the default 0 (board axes ==
# base axes) is mechanically guaranteed. A classroom whose jig holds the board
# at 90°/180° sets this once (forwarded by compose) — no code change.
BOARD_YAW_DEG = _safe_float_env('EDUBOTICS_BOARD_YAW_DEG', 0.0)

# Extrinsic solve quality gates (no silent acceptance of a weak/wrong solve):
#   min ChArUco corners for a trustworthy single-shot PnP, and the max mean
#   reprojection error (px) of that PnP.
SCENE_EXTRINSIC_MIN_CORNERS = 6
SCENE_EXTRINSIC_MAX_REPROJ_PX = 1.5

# ── Orientation sanity gate for the single-shot scene extrinsic ──────────────
# The camera-above-table gate (below, in _solve_scene_extrinsic) catches a board
# laid UPSIDE-DOWN (camera resolves below the table). On top of that, two
# ROLL-INDEPENDENT checks catch a GROSSLY misplaced board (both computed purely
# from the recovered T_cam_to_base, no ground-truth point needed):
#
#  (1) WORKSPACE-REGION: the camera's optical axis (cam +Z) must intersect the
#      table inside the expected workspace band — forward of the robot
#      (x in [REGION_X_MIN, REGION_X_MAX], around the board's 0.18–0.39 m span)
#      and roughly centred laterally (|y| <= REGION_Y_ABS). Bounds are GENEROUS
#      so a tilted/off-centre (but correctly placed) mount still passes.
#  (2) OPTICAL-AXIS-DOWNWARD: cam +Z in base must point clearly DOWN (negative
#      z) — the camera must look at the table, not sideways/up.
#
# HONEST SCOPE — what this CANNOT reliably catch (rig-found 2026-06-18; verified
# 2026-06-18 second pass). The ONLY misplacement it reliably rejects is a board
# whose look-point leaves the region (camera not aimed at the workspace) or a
# camera not looking down. It does NOT reliably catch:
#  * a 90°/-90° IN-PLANE board rotation,
#  * a 180° rotation (whether its look-point leaves the band is CAMERA-POSITION
#    dependent — a board centred under a near-overhead cam can be accepted), and
#  * a left/right MIRROR (origin corner at near-RIGHT instead of near-LEFT).
# A scene camera may be mounted at ANY roll (image-"up" toward OR away from the
# robot — both valid), so the camera's image +X direction is NOT a reliable
# "board points forward" signal: a 90° board yaw is geometrically
# indistinguishable from a 90° camera roll from one top-down view. An earlier
# forward-axis (cam +X) check tried to catch 90° but FALSE-REJECTED a correctly
# placed board under a 180°-rolled real camera, so it was removed. The residual
# 90°/180°/mirror is resolved by the rig GROUND-TRUTH check (place an object at a
# measured (x,y) and confirm the detected position), not by this single-shot
# gate — which is why that ground-truth step is MANDATORY before grasping.
SCENE_EXTRINSIC_REGION_X_MIN = _safe_float_env('EDUBOTICS_EXTRINSIC_REGION_X_MIN', 0.08)
SCENE_EXTRINSIC_REGION_X_MAX = _safe_float_env('EDUBOTICS_EXTRINSIC_REGION_X_MAX', 0.46)
SCENE_EXTRINSIC_REGION_Y_ABS = _safe_float_env('EDUBOTICS_EXTRINSIC_REGION_Y_ABS', 0.18)

# Table touch-off ("Tisch vermessen"): hand-guide the torque-off follower to
# tap the table at >= N spread points; FK → least-squares plane → z_table (+
# tilt). Measures the table height directly in the IK/end-effector frame, the
# quantity the grasp descends to. The plane-fit residual + xy-spread gates
# reject a sloppy or near-collinear capture.
TABLE_TOUCH_POINTS_REQUIRED = 3
TABLE_TOUCH_MAX_RESIDUAL_M = 0.008      # 8 mm — points must lie on one plane
TABLE_TOUCH_MIN_SPREAD_M = 0.06         # the xy points must span >= 6 cm (radius)
# The points must also span a 2D AREA, not a line: near-collinear points pass a
# radial-spread test but leave the table TILT in the perpendicular direction
# unconstrained (lstsq returns 0 residual on the rank-deficient fit). Gate on
# the minor-axis (perpendicular) rms spread.
TABLE_TOUCH_MIN_MINOR_M = 0.015         # >= 1.5 cm spread off the dominant line
# Loose cross-check vs the camera extrinsic's board height: the touch plane
# (end-effector frame, a finger-length above the table) and the camera board
# height differ by the finger length, so only a GROSS disagreement (wrong sign
# / placement) is rejected.
TABLE_TOUCH_VS_CAMERA_MAX_M = 0.12
# Verticality gate for each touch-off tap. The grasp ALWAYS descends with the
# gripper straight down (the closed-form IK is strict-vertical, approach axis
# == base −z exactly), and we record the FK ``end_effector_link`` z as the
# table height. That is only self-consistent if the student taps with the
# gripper ALSO straight down: a tilted tap places the EE-link origin at a
# different height than a vertical grasp would reach (a 15° tilt biases z by
# ~4 mm and grows with arm extension), and the fingertip↔EE-link offset stops
# being purely vertical. So reject a tap whose gripper approach axis deviates
# more than this from straight-down. The approach axis is the EE-link local +x
# (the forearm/tool pointing direction) in base frame = FK ``R[:, 0]``.
TABLE_TOUCH_MAX_TILT_DEG = 12.0


def _build_charuco_board() -> tuple[cv2.aruco.CharucoBoard, cv2.aruco.Dictionary]:
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
    board = cv2.aruco.CharucoBoard(
        (CHARUCO_SQUARES_X, CHARUCO_SQUARES_Y),
        CHARUCO_SQUARE_LENGTH_M,
        CHARUCO_MARKER_LENGTH_M,
        aruco_dict,
    )
    return board, aruco_dict


@dataclass
class IntrinsicCaptureBuffer:
    """Charuco corners + ids for each captured frame, plus the source image
    sizes used by `calibrateCameraCharucoExtended`."""

    all_corners: list[np.ndarray] = field(default_factory=list)
    all_ids: list[np.ndarray] = field(default_factory=list)
    image_size: tuple[int, int] | None = None
    last_view_rms: float | None = None


@dataclass
class HandEyeCaptureBuffer:
    """Scene-extrinsic capture buffer (WS4 single-shot). Holds the latest
    ``(R_target2cam, t_target2cam)`` from the board detection (solvePnP of the
    flat-on-table ChArUco). The ``R/t_gripper2base`` lists are retained for
    back-compat but are unused by the single-shot solve (kept empty); the
    old multi-pose eye-to-base flow that filled them was removed."""

    R_target2cam: list[np.ndarray] = field(default_factory=list)
    t_target2cam: list[np.ndarray] = field(default_factory=list)
    R_gripper2base: list[np.ndarray] = field(default_factory=list)
    t_gripper2base: list[np.ndarray] = field(default_factory=list)


@dataclass
class TableTouchBuffer:
    """Touch-off table-measurement buffer. Each ``points`` entry is a
    base-frame end-effector (x, y, z) read from FK when the student tapped the
    gripper tip on the table; the least-squares plane ``z = a·x + b·y + c`` is
    stored after solving."""

    points: list[tuple[float, float, float]] = field(default_factory=list)
    plane: tuple[float, float, float] | None = None


class CalibrationManager:
    """State machine + ChArUco math for Roboter Studio SCENE-camera setup.

    WS4 (2026-06-17): scene-cam only (intrinsic + single-shot board-on-table
    extrinsic + colour profile). The gripper camera is no longer calibrated.

    Designed for single-threaded interaction from the ROS service callbacks
    (the manager's lock serialises capture/solve calls). Image acquisition is
    delegated to provider callables so the manager stays unit-testable with
    synthetic frames. ``get_gripper_pose`` is retained (the node still wires
    its FK provider for ``destination_current``) but is no longer used by the
    capture path."""

    def __init__(
        self,
        get_frame: Callable[[str], np.ndarray | None] | None = None,
        get_gripper_pose: Callable[[], tuple[np.ndarray, np.ndarray] | None] | None = None,
    ) -> None:
        self._get_frame = get_frame
        self._get_gripper_pose = get_gripper_pose
        self._lock = threading.Lock()
        self._board, self._dict = _build_charuco_board()
        self._detector_params = cv2.aruco.DetectorParameters()
        self._charuco_detector = None
        self._intrinsic_buffers: dict[str, IntrinsicCaptureBuffer] = {}
        self._handeye_buffers: dict[str, HandEyeCaptureBuffer] = {}
        self._table_touch: TableTouchBuffer | None = None
        self._intrinsics: dict[str, dict] = {}
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        self._load_persisted_intrinsics()
        self._seed_factory_intrinsics()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _intrinsic_path(self, camera: str) -> Path:
        return CALIB_DIR / f'{camera}_intrinsics.yaml'

    def _handeye_path(self, camera: str) -> Path:
        return CALIB_DIR / f'{camera}_handeye.yaml'

    def _color_profile_path(self) -> Path:
        return CALIB_DIR / 'color_profile.yaml'

    def _load_persisted_intrinsics(self) -> None:
        for camera in ('gripper', 'scene'):
            path = self._intrinsic_path(camera)
            if not path.exists():
                continue
            try:
                fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
                K = fs.getNode('camera_matrix').mat()
                dist = fs.getNode('distortion_coefficients').mat()
                fs.release()
                if K is not None and dist is not None:
                    self._intrinsics[camera] = {'K': K, 'dist': dist}
            except Exception as e:
                # Audit F61: corrupt YAML is recoverable by re-running
                # calibration, but logging beats silent. Without this
                # log a YAML that fails to parse on every node restart
                # leaves the operator wondering why calibration always
                # appears uninitialised.
                import sys
                print(
                    f'[WARNUNG] {camera}-Kalibrierdatei beschädigt '
                    f'({path}): {e}. Bitte Kalibrierung neu starten.',
                    file=sys.stderr, flush=True,
                )

    def _seed_factory_intrinsics(self) -> None:
        """Seed shipped factory-default scene-camera intrinsics when no per-rig
        calibration exists, so students can SKIP the fragile 12-frame ChArUco
        intrinsic step. A per-rig intrinsic calibration (optional refine)
        overwrites the YAML and supersedes this.

        Env-read at call time (testable). Set ``EDUBOTICS_FACTORY_INTRINSICS=0``
        to require the per-rig step instead.

        HONEST NOTE: the default fx/fy below are an ESTIMATE for the Innomaker
        scene camera at 640×480. The touch-off fixes grasp HEIGHT independently,
        but the LATERAL scale depends on fx/fy — for best accuracy a teacher
        calibrates once per camera model (or the real measured values are baked
        here in P6). A wrong focal length scales every projected XY.
        """
        if os.environ.get('EDUBOTICS_FACTORY_INTRINSICS', '1') == '0':
            return
        if 'scene' in self._intrinsics:
            return
        fx = _safe_float_env('EDUBOTICS_FACTORY_FX', 600.0)
        fy = _safe_float_env('EDUBOTICS_FACTORY_FY', 600.0)
        cx = _safe_float_env('EDUBOTICS_FACTORY_CX', 320.0)
        cy = _safe_float_env('EDUBOTICS_FACTORY_CY', 240.0)
        w = _safe_int_env('EDUBOTICS_FACTORY_IMG_W', 640)
        h = _safe_int_env('EDUBOTICS_FACTORY_IMG_H', 480)
        K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
                     dtype=np.float64)
        dist = np.zeros((5, 1), dtype=np.float64)
        self._intrinsics['scene'] = {'K': K, 'dist': dist}
        # Persist so the runtime loader (_load_workflow_calibration, which reads
        # the YAML directly) sees it too. source=factory_default marks it so a
        # real calibration is distinguishable.
        try:
            path = self._intrinsic_path('scene')
            if not path.exists():
                fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
                fs.write('camera_matrix', K)
                fs.write('distortion_coefficients', dist)
                fs.write('image_width', w)
                fs.write('image_height', h)
                fs.write('reprojection_error', -1.0)
                fs.write('source', 'factory_default')
                fs.release()
        except Exception:  # noqa: BLE001 — seeding is best-effort
            pass

    def has_intrinsics(self, camera: str) -> bool:
        return camera in self._intrinsics

    def has_handeye(self, camera: str) -> bool:
        # Back-compat name; the scene-cam extrinsic is persisted to the same
        # ``<camera>_handeye.yaml`` file so ``_load_workflow_calibration``
        # keeps reading it unchanged. WS4 renamed the step ('extrinsic') but
        # NOT the on-disk artifact.
        return self._handeye_path(camera).exists()

    def has_extrinsic(self, camera: str) -> bool:
        """WS4 alias for ``has_handeye`` — the scene-cam extrinsic
        ('camera->base' transform). Same YAML on disk."""
        return self.has_handeye(camera)

    def has_table_plane(self, camera: str = 'scene') -> bool:
        """True when the touch-off table measurement has been solved (the scene
        extrinsic YAML carries a measured ``table_plane``)."""
        path = self._handeye_path(camera)
        if not path.exists():
            return False
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
            present = not fs.getNode('table_plane').empty()
            fs.release()
            return bool(present)
        except Exception:
            return False

    def has_color_profile(self) -> bool:
        return self._color_profile_path().exists()

    # ------------------------------------------------------------------
    # Step lifecycle
    # ------------------------------------------------------------------
    def start_step(self, camera: str, step: str) -> tuple[bool, str]:
        with self._lock:
            # WS4: the gripper camera is no longer calibrated (not modelled
            # in the URDF; unused at runtime). Reject any gripper step so a
            # stale frontend can't re-introduce the unfinishable flow.
            if camera == 'gripper':
                return False, (
                    'Die Greifer-Kamera wird nicht mehr kalibriert — nur die '
                    'Szenen-Kamera ist nötig. Bitte die Szenen-Kamera wählen.'
                )
            if step == 'intrinsic':
                # Drop any in-flight extrinsic buffer for this camera so
                # capture_frame doesn't route to the wrong solver after the
                # student switches steps.
                self._handeye_buffers.pop(camera, None)
                self._intrinsic_buffers[camera] = IntrinsicCaptureBuffer()
                return True, f'Intrinsische Kalibrierung für {camera} gestartet.'
            # 'extrinsic' is the WS4 name; 'handeye' is accepted as a
            # synonym so an in-flight older frontend build keeps working.
            if step in ('extrinsic', 'handeye'):
                if camera != 'scene':
                    return False, (
                        'Die Extrinsik wird nur für die Szenen-Kamera '
                        'kalibriert.'
                    )
                if not self.has_intrinsics(camera):
                    return False, (
                        f'Bitte erst die intrinsische Kalibrierung der '
                        f'{camera}-Kamera abschließen.'
                    )
                # Drop any leftover intrinsic buffer for this camera —
                # without this, capture_frame's "if camera in
                # _intrinsic_buffers" precedence routes extrinsic captures
                # into the intrinsic buffer and the student never collects
                # a single extrinsic sample.
                self._intrinsic_buffers.pop(camera, None)
                self._handeye_buffers[camera] = HandEyeCaptureBuffer()
                return True, (
                    'Extrinsik der Szenen-Kamera gestartet — bitte die Tafel '
                    'flach auf den markierten Punkt legen und ein Bild '
                    'erfassen.'
                )
            # The 'color_profile' step has no per-step start; capture is
            # gated by the prerequisite check inside
            # calibration_capture_color_callback. Anything other than
            # 'intrinsic' / 'handeye' is rejected as unknown so a typo'd
            # frontend call surfaces clearly.
            return False, f'Unbekannter Kalibrier-Schritt: {step}'

    def cancel_step(self, camera: str | None = None) -> tuple[bool, str]:
        """Drop all in-flight capture buffers and forget the active step.
        When ``camera`` is None, cancels every camera; otherwise narrows
        to the one camera. Idempotent — safe to call when no step is
        active. Used by /calibration/cancel so a closed wizard doesn't
        leave the on_calibration mutex stuck."""
        with self._lock:
            if camera is None:
                self._intrinsic_buffers.clear()
                self._handeye_buffers.clear()
                self._table_touch = None
                return True, 'Alle Kalibrier-Schritte abgebrochen.'
            self._intrinsic_buffers.pop(camera, None)
            self._handeye_buffers.pop(camera, None)
            self._table_touch = None
            return True, f'Kalibrier-Schritt für {camera} abgebrochen.'

    # ------------------------------------------------------------------
    # Frame capture
    # ------------------------------------------------------------------
    def _detect_charuco(self, gray: np.ndarray) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Detect ChArUco corners + ids; returns (None, None) on no detection."""
        marker_corners, marker_ids, _ = cv2.aruco.detectMarkers(
            gray, self._dict, parameters=self._detector_params,
        )
        if marker_ids is None or len(marker_ids) == 0:
            return None, None
        ret, ch_corners, ch_ids = cv2.aruco.interpolateCornersCharuco(
            marker_corners, marker_ids, gray, self._board,
        )
        if ret is None or ret < 4 or ch_corners is None:
            return None, None
        return ch_corners, ch_ids

    def capture_frame(
        self,
        camera: str,
        bgr: np.ndarray | None = None,
    ) -> tuple[bool, int, int, float, str]:
        """Capture a single calibration frame for the active step.

        Returns (success, frames_captured, frames_required, last_view_rms, message).
        """
        with self._lock:
            if camera not in self._intrinsic_buffers and camera not in self._handeye_buffers:
                return False, 0, 0, 0.0, (
                    f'Kein aktiver Kalibrier-Schritt für {camera}. Bitte zuerst starten.'
                )

            frame = bgr if bgr is not None else (self._get_frame(camera) if self._get_frame else None)
            if frame is None:
                return False, 0, 0, 0.0, 'Kein Kamerabild verfügbar.'

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            corners, ids = self._detect_charuco(gray)
            if corners is None:
                return False, 0, 0, 0.0, 'ChArUco-Tafel nicht erkannt — bitte Position anpassen.'

            if camera in self._intrinsic_buffers:
                buf = self._intrinsic_buffers[camera]
                # Image-size guard: cv2.calibrateCameraCharucoExtended takes
                # a single image_size, so a mixed-resolution capture set
                # produces nonsense intrinsics with no error. Reject any
                # frame whose dimensions disagree with the first stored
                # frame; the student gets a clear German message and the
                # buffer stays clean for a retry.
                this_size = gray.shape[::-1]  # (W, H)
                if buf.image_size is not None and buf.image_size != this_size:
                    return False, len(buf.all_corners), INTRINSIC_FRAMES_REQUIRED, float(buf.last_view_rms or 0.0), (
                        'Kamera-Auflösung hat sich geändert. Bitte Aufnahme '
                        'neu starten und mit einer einzigen Auflösung erfassen.'
                    )
                buf.all_corners.append(corners)
                buf.all_ids.append(ids)
                buf.image_size = this_size
                rms = self._estimate_view_rms(buf)
                buf.last_view_rms = rms
                return True, len(buf.all_corners), INTRINSIC_FRAMES_REQUIRED, float(rms or 0.0), (
                    f'Bild {len(buf.all_corners)}/{INTRINSIC_FRAMES_REQUIRED} erfasst.'
                )

            # Scene extrinsic step (WS4 single-shot board-on-table).
            # NO arm motion, NO gripper pose: the board lies flat on the
            # table, the static scene cam sees it once, solvePnP gives the
            # board->camera transform, and the solve combines it with the
            # known board-on-table placement (T_board_to_base) to recover
            # T_camera_to_base. Each capture is latest-wins (re-pressing
            # "Bild erfassen" overwrites), so the buffer holds at most one.
            if not self.has_intrinsics(camera):
                return False, 0, 0, 0.0, (
                    f'Intrinsische Daten für {camera} fehlen.'
                )
            K = self._intrinsics[camera]['K']
            dist = self._intrinsics[camera]['dist']
            # Audit F60: cv2.aruco.getBoardObjectAndImagePoints is
            # deprecated in OpenCV 4.10+; switch to the
            # CharucoBoard.matchImagePoints API (4.7+). Fall back to
            # the legacy call if the new API isn't available so the
            # overlay still applies cleanly on older OpenCV builds.
            match_fn = getattr(self._board, 'matchImagePoints', None)
            if callable(match_fn):
                object_points, image_points = match_fn(corners, ids)
            else:
                object_points, image_points = cv2.aruco.getBoardObjectAndImagePoints(
                    self._board, corners, ids,
                )
            if object_points is None or len(object_points) < SCENE_EXTRINSIC_MIN_CORNERS:
                return False, 0, 0, 0.0, (
                    'Zu wenige Tafel-Punkte erkannt — bitte die Tafel näher / '
                    'vollständig ins Bild legen.'
                )
            ok, rvec, tvec = cv2.solvePnP(object_points, image_points, K, dist)
            if not ok:
                return False, 0, 0, 0.0, 'Pose der Tafel konnte nicht bestimmt werden.'
            # Reprojection-error gate: a weak/ambiguous planar solve (grazing
            # angle, partial board) would silently mis-place the WHOLE world
            # frame. Reject above the threshold instead of saving garbage.
            reproj = self._reprojection_error_px(
                object_points, image_points, rvec, tvec, K, dist)
            if not math.isfinite(reproj) or reproj > SCENE_EXTRINSIC_MAX_REPROJ_PX:
                shown = reproj if math.isfinite(reproj) else 0.0
                return False, 0, 0, float(shown), (
                    f'Tafel-Pose ungenau (Reprojektionsfehler {shown:.1f} px) — '
                    'bitte die Tafel flach und vollständig ins Bild legen und '
                    'erneut erfassen.'
                )
            R_t2c, _ = cv2.Rodrigues(rvec)
            t_t2c = tvec.reshape(3, 1)

            buf = self._handeye_buffers[camera]
            # Latest-wins: replace any prior single-shot sample so a
            # re-capture (board nudged) supersedes the old one cleanly.
            buf.R_target2cam = [R_t2c]
            buf.t_target2cam = [t_t2c]
            buf.R_gripper2base = []
            buf.t_gripper2base = []

            return True, len(buf.R_target2cam), SCENE_EXTRINSIC_FRAMES_REQUIRED, float(reproj), (
                f'Bild {len(buf.R_target2cam)}/{SCENE_EXTRINSIC_FRAMES_REQUIRED} '
                f'erfasst (Reprojektionsfehler {reproj:.1f} px) — bitte berechnen '
                '& speichern.'
            )

    def _estimate_view_rms(self, buf: IntrinsicCaptureBuffer) -> float | None:
        """Run a quick calibration on the current set to estimate quality. Cheap
        enough at <=12 frames to surface live RMS feedback in the wizard."""
        if len(buf.all_corners) < 4 or buf.image_size is None:
            return None
        try:
            ret, _, _, _, _ = cv2.aruco.calibrateCameraCharuco(
                buf.all_corners, buf.all_ids, self._board, buf.image_size, None, None,
            )
            return float(ret)
        except cv2.error:
            return None

    @staticmethod
    def _reprojection_error_px(object_points, image_points, rvec, tvec, K, dist) -> float:
        """Mean reprojection error (px) of a solvePnP result — the quality
        signal for the single-shot extrinsic. Returns inf on an OpenCV error."""
        try:
            proj, _ = cv2.projectPoints(object_points, rvec, tvec, K, dist)
            proj = np.asarray(proj, dtype=np.float64).reshape(-1, 2)
            img = np.asarray(image_points, dtype=np.float64).reshape(-1, 2)
            return float(np.mean(np.linalg.norm(proj - img, axis=1)))
        except cv2.error:
            return float('inf')

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    def solve(self, camera: str, step: str) -> tuple[bool, float, float, str]:
        """Solve the named step. Returns (success, reprojection_error,
        method_disagreement_deg, message)."""
        with self._lock:
            if step == 'intrinsic':
                return self._solve_intrinsic(camera)
            # 'extrinsic' is the WS4 name; 'handeye' is accepted as a
            # synonym so an in-flight older frontend build keeps working.
            if step in ('extrinsic', 'handeye'):
                return self._solve_scene_extrinsic(camera)
            return False, 0.0, 0.0, f'Unbekannter Solve-Schritt: {step}'

    def _solve_intrinsic(self, camera: str) -> tuple[bool, float, float, str]:
        buf = self._intrinsic_buffers.get(camera)
        if buf is None or len(buf.all_corners) < INTRINSIC_FRAMES_REQUIRED:
            need = INTRINSIC_FRAMES_REQUIRED - len(buf.all_corners) if buf else INTRINSIC_FRAMES_REQUIRED
            return False, 0.0, 0.0, f'Es fehlen noch {need} Bilder.'
        try:
            ret, K, dist, _, _, _, _, _ = cv2.aruco.calibrateCameraCharucoExtended(
                buf.all_corners, buf.all_ids, self._board, buf.image_size,
                None, None,
            )
        except cv2.error as e:
            return False, 0.0, 0.0, f'OpenCV-Solver-Fehler: {e}'

        # Audit fix #18: refuse to persist a degenerate solve. NaN/Inf
        # comes out of calibrateCameraCharucoExtended when the captured
        # set is collinear or otherwise degenerate; > 5 px reprojection
        # error is a noisy/blurry set. Either way the resulting K/dist
        # would calibrate the perception pipeline against garbage and
        # silently mis-project every pixel click. Force a re-capture.
        if not math.isfinite(ret) or ret > 5.0:
            return False, float(ret) if math.isfinite(ret) else 0.0, 0.0, (
                'Reprojektionsfehler zu hoch — bitte mit unterschiedlichen '
                'Posen neu kalibrieren.'
            )

        self._intrinsics[camera] = {'K': K, 'dist': dist}
        path = self._intrinsic_path(camera)
        fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
        fs.write('camera_matrix', K)
        fs.write('distortion_coefficients', dist)
        fs.write('image_width', int(buf.image_size[0]))
        fs.write('image_height', int(buf.image_size[1]))
        fs.write('reprojection_error', float(ret))
        fs.write('captured_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
        fs.release()
        return True, float(ret), 0.0, (
            f'Intrinsische Kalibrierung gespeichert (RMS {ret:.2f} px).'
        )

    @staticmethod
    def _board_to_base_transform() -> np.ndarray:
        """T_board_to_base — the known placement of the ChArUco board on the
        table, in base frame.

        Single-shot convention: the board lies FLAT on the table, printed-side
        up, so its OpenCV origin corner sits at ``(BOARD_ORIGIN_X_M,
        BOARD_ORIGIN_Y_M, BOARD_TABLE_Z_M)``.

        Rotation = Rz(BOARD_YAW_DEG) @ Rx(180°). The Rx(180°) flip is REQUIRED,
        not cosmetic: OpenCV's ChArUco board frame has +Z exiting the BACK of
        the board, so with the board printed-side-up on the table that +Z points
        DOWN. Without the flip, ``T_cam_to_base = T_board_to_base @
        inv(T_board_to_cam)`` resolves the scene camera to the MIRROR position
        BELOW the table (rig-found 2026-06-18 — the "Kamera nicht über dem
        Tisch" failure; verified: identity → cam_z −0.35 m, Rx(180°) → +0.35 m).
        The flip maps the board normal to base −Z and, with the right-hand rule
        (board +X stays = base +X forward, since the board is physically in
        FRONT of the robot), board +Y → base −Y. det = +1 (proper rotation).
        BOARD_YAW_DEG adds an in-plane yaw on top (0 = board square to base).
        """
        yaw = math.radians(BOARD_YAW_DEG)
        c, s = math.cos(yaw), math.sin(yaw)
        r_yaw = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]],
                         dtype=np.float64)
        # Rx(180°): OpenCV board +Z exits the back -> flip so the board normal
        # points base -Z (down) and the camera resolves ABOVE the table.
        r_flip = np.diag([1.0, -1.0, -1.0]).astype(np.float64)
        T = np.eye(4, dtype=np.float64)
        T[:3, :3] = r_yaw @ r_flip
        T[0, 3] = BOARD_ORIGIN_X_M
        T[1, 3] = BOARD_ORIGIN_Y_M
        T[2, 3] = BOARD_TABLE_Z_M
        return T

    @staticmethod
    def _optical_axis_table_hit(T_cam_to_base: np.ndarray) -> np.ndarray | None:
        """Where the camera's optical axis (cam +Z) hits the table plane
        ``z = BOARD_TABLE_Z_M`` in base frame. Returns the (x, y, z) point, or
        ``None`` when the optical axis is parallel to the table (no
        intersection) or behind the camera."""
        try:
            origin = np.asarray(T_cam_to_base, dtype=np.float64)[:3, 3]
            direction = np.asarray(T_cam_to_base, dtype=np.float64)[:3, 2]
        except (ValueError, TypeError):
            return None
        dz = float(direction[2])
        if not math.isfinite(dz) or abs(dz) < 1e-9:
            return None
        s = (BOARD_TABLE_Z_M - float(origin[2])) / dz
        if not math.isfinite(s) or s <= 0:
            # Table is behind the camera along the optical axis — the camera is
            # not looking at the table at all.
            return None
        return origin + s * direction

    def _check_extrinsic_orientation(
        self, T_cam_to_base: np.ndarray
    ) -> tuple[bool, str]:
        """Roll-independent orientation sanity gate for the single-shot scene
        extrinsic.

        Catches a GROSSLY misplaced board (180° / wrong region / camera not
        looking down) which would silently rotate the world frame while the
        solve still "succeeds". Does NOT catch a 90° in-plane rotation or a
        left/right mirror — those are resolved by the rig ground-truth check.
        See the SCENE_EXTRINSIC_REGION_* constant block for the full rationale.

        Returns ``(True, '')`` when the recovered pose is plausible, else
        ``(False, german_message)``."""
        # (1) Workspace-region: the camera must LOOK at the workspace in front
        # of the robot, not off to the side / behind it.
        hit = self._optical_axis_table_hit(T_cam_to_base)
        if hit is None:
            return False, (
                'Die Kamera schaut nicht auf die Arbeitsfläche — bitte prüfen, '
                'dass die ChArUco-Tafel flach auf dem markierten Punkt vor dem '
                'Roboter liegt, und erneut erfassen.'
            )
        hit_x, hit_y = float(hit[0]), float(hit[1])
        if not (SCENE_EXTRINSIC_REGION_X_MIN <= hit_x <= SCENE_EXTRINSIC_REGION_X_MAX
                and abs(hit_y) <= SCENE_EXTRINSIC_REGION_Y_ABS):
            return False, (
                'Die Tafel scheint verdreht oder falsch platziert zu sein '
                f'(Kamera-Blickpunkt x={hit_x * 100:.0f} cm, y={hit_y * 100:.0f} '
                'cm). Bitte die Tafel mittig vor dem Roboter platzieren — die '
                'lange Kante zeigt nach vorne (vom Roboter weg), die '
                'Ursprungs-Ecke vorne links — und erneut erfassen.'
            )
        # (2) Optical-axis-downward (roll-INDEPENDENT): the camera must look DOWN
        # at the table, not sideways/up. cam +Z in base must have a clearly
        # negative z component. This (together with the camera-above-table gate
        # in the caller and the region check above) is the strongest sanity we
        # can apply WITHOUT knowing the camera's roll.
        #
        # NOTE — what this deliberately does NOT do: catch a 90° in-plane board
        # rotation or a left/right mirror. A scene camera may be mounted at ANY
        # roll (e.g. image-"up" toward OR away from the robot — both are valid),
        # so the camera's image +X direction in base is NOT a reliable "board is
        # forward" signal: a 90° board yaw is indistinguishable from a 90° camera
        # roll from a single top-down view. (An earlier forward-axis check using
        # cam +X false-rejected a correctly-placed board under a 180°-rolled
        # camera — rig-found 2026-06-18.) The residual 90°/mirror is resolved by
        # the rig ground-truth check (place an object at a measured (x,y) and
        # confirm the detected position), not by this single-shot gate.
        cam_z_axis = np.asarray(T_cam_to_base, dtype=np.float64)[:3, 2]
        if not math.isfinite(float(cam_z_axis[2])) or float(cam_z_axis[2]) > -0.30:
            return False, (
                'Die Kamera schaut nicht nach unten auf den Tisch — bitte prüfen, '
                'dass die Szenen-Kamera von oben auf die Arbeitsfläche zeigt und '
                'die Tafel flach auf dem markierten Punkt liegt, und erneut '
                'erfassen.'
            )
        return True, ''

    def _solve_scene_extrinsic(self, camera: str) -> tuple[bool, float, float, str]:
        """Single-shot board-on-table scene extrinsic (WS4).

        Recovers ``T_cam_to_base`` from ONE capture and the known board
        placement — NO arm motion, NO 14-pose hand-eye, NO PARK/TSAI. The
        gripper camera is no longer calibrated, so this is scene-only.

        Math:
            T_board_to_cam  = [R_t2c | t_t2c]      (from solvePnP at capture)
            T_cam_to_board  = inverse(T_board_to_cam)
            T_cam_to_base   = T_board_to_base @ T_cam_to_board
            z_table         = BOARD_TABLE_Z_M      (board lies flat at table z)
        """
        if camera != 'scene':
            return False, 0.0, 0.0, (
                'Die Extrinsik wird nur für die Szenen-Kamera kalibriert.'
            )
        buf = self._handeye_buffers.get(camera)
        if buf is None or len(buf.R_target2cam) < SCENE_EXTRINSIC_FRAMES_REQUIRED:
            return False, 0.0, 0.0, (
                'Es fehlt noch ein Bild der flach liegenden Tafel — bitte die '
                'Tafel auf den markierten Punkt legen und "Bild erfassen" '
                'drücken.'
            )

        R_t2c = buf.R_target2cam[-1]
        t_t2c = buf.t_target2cam[-1]

        # T_board_to_cam, then invert to T_cam_to_board.
        T_board_to_cam = np.eye(4, dtype=np.float64)
        T_board_to_cam[:3, :3] = R_t2c
        T_board_to_cam[:3, 3] = t_t2c.reshape(3)
        try:
            T_cam_to_board = np.linalg.inv(T_board_to_cam)
        except np.linalg.LinAlgError:
            return False, 0.0, 0.0, (
                'Tafel-Pose ist entartet — bitte die Tafel flacher legen und '
                'das Bild erneut erfassen.'
            )

        T_board_to_base = self._board_to_base_transform()
        T_cam_to_base = T_board_to_base @ T_cam_to_board

        # Sanity guard: the camera must be ABOVE the table (looking down at
        # it). A camera origin below the table plane means the board was laid
        # upside-down or the placement convention was not followed — refuse so
        # projection never silently inverts.
        cam_origin_z = float(T_cam_to_base[2, 3])
        if cam_origin_z <= BOARD_TABLE_Z_M + 0.02:
            self._handeye_buffers.pop(camera, None)
            return False, 0.0, 0.0, (
                'Die Kamera scheint nicht über dem Tisch zu liegen — bitte '
                'prüfen, dass die ChArUco-Tafel mit der bedruckten Seite nach '
                'oben flach auf dem markierten Punkt liegt, und erneut '
                'erfassen.'
            )

        # Orientation sanity guard: the camera-above gate above only catches an
        # upside-down board. A board rotated 90°/180° in-plane (or placed in the
        # wrong region) still resolves the camera ABOVE the table but silently
        # rotates/mirrors the whole world frame → every grasp goes to the wrong
        # place. Reject a grossly-misoriented placement (see
        # _check_extrinsic_orientation for what it can / cannot catch).
        ok_orient, orient_msg = self._check_extrinsic_orientation(T_cam_to_base)
        if not ok_orient:
            self._handeye_buffers.pop(camera, None)
            return False, 0.0, 0.0, orient_msg

        # The board's supplied surface height. Written as ``board_table_z`` —
        # NOT ``z_table`` — so the runtime grasp height (``z_table``) is the
        # MEASURED touch-off value only. Until the touch-off runs, there is no
        # ``z_table`` and projection/grasp correctly refuse (no silent grasp
        # against the supplied/guessed surface in the wrong frame).
        board_table_z = float(BOARD_TABLE_Z_M)

        path = self._handeye_path(camera)
        fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
        fs.write('transform', T_cam_to_base)
        fs.write('method', 'BOARD_ON_TABLE')
        fs.write('board_table_z', board_table_z)
        fs.write('board_origin_x', float(BOARD_ORIGIN_X_M))
        fs.write('board_origin_y', float(BOARD_ORIGIN_Y_M))
        fs.write('captured_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
        fs.release()

        # method_disagreement (3rd return) is 0.0 — there is no PARK/TSAI
        # disagreement in the single-shot path. The frontend hides that field
        # for the scene extrinsic.
        return True, 0.0, 0.0, (
            f'Szenen-Extrinsik gespeichert (Kamera {cam_origin_z * 100:.0f} cm '
            'über dem Tisch).'
        )

    # ------------------------------------------------------------------
    # Table touch-off ("Tisch vermessen") — measured z_table (+ tilt)
    # ------------------------------------------------------------------
    def start_table_touch(self) -> tuple[bool, str]:
        """Begin a table-measurement session. Requires the scene extrinsic
        (the camera→base transform) to already be calibrated."""
        with self._lock:
            if not self.has_extrinsic('scene'):
                return False, (
                    'Bitte zuerst die Szenen-Kamera ausrichten (Extrinsik), '
                    'bevor du den Tisch vermisst.'
                )
            self._table_touch = TableTouchBuffer()
            return True, (
                'Tischvermessung gestartet — führe den Greifer von Hand, bis die '
                'Spitze den Tisch berührt, dann drücke Erfassen.'
            )

    def capture_touch_point(self) -> tuple[bool, int, int, str]:
        """Record one table-contact point: read the follower joints → FK →
        base-frame end-effector (x, y, z). Returns (ok, count, required, msg)."""
        with self._lock:
            buf = self._table_touch
            if buf is None:
                return False, 0, TABLE_TOUCH_POINTS_REQUIRED, (
                    'Tischvermessung nicht gestartet.'
                )
            if self._get_gripper_pose is None:
                return False, len(buf.points), TABLE_TOUCH_POINTS_REQUIRED, (
                    'Greiferpose (FK) nicht verfügbar.'
                )
            pose = self._get_gripper_pose()
            if pose is None:
                return False, len(buf.points), TABLE_TOUCH_POINTS_REQUIRED, (
                    'Keine Gelenkdaten — bitte kurz warten und erneut erfassen.'
                )
            R, t = pose
            # Verticality gate: the grasp descends strictly vertically and we
            # record the EE-link z as the table height, so a tilted tap would
            # bias z_table. The approach axis is the EE-link local +x (forearm/
            # tool pointing direction) in base frame = R[:, 0]; reject taps that
            # deviate more than TABLE_TOUCH_MAX_TILT_DEG from straight-down.
            tilt_deg = self._approach_tilt_deg(R)
            if tilt_deg is None:
                return False, len(buf.points), TABLE_TOUCH_POINTS_REQUIRED, (
                    'Greiferausrichtung unbekannt — bitte erneut erfassen.'
                )
            if tilt_deg > TABLE_TOUCH_MAX_TILT_DEG:
                return False, len(buf.points), TABLE_TOUCH_POINTS_REQUIRED, (
                    f'Greifer steht schräg ({tilt_deg:.0f}°) — bitte den Greifer '
                    'senkrecht nach unten halten und die Spitze gerade auf den '
                    'Tisch tippen, dann erneut erfassen.'
                )
            buf.points.append((float(t[0]), float(t[1]), float(t[2])))
            n = len(buf.points)
            return True, n, TABLE_TOUCH_POINTS_REQUIRED, (
                f'Punkt {n}/{TABLE_TOUCH_POINTS_REQUIRED} erfasst.'
            )

    @staticmethod
    def _approach_tilt_deg(R: np.ndarray) -> float | None:
        """Angle (deg) between the gripper approach axis and straight-down.

        The approach axis is the EE-link local +x (forearm/tool pointing
        direction) expressed in base frame, i.e. the first column of the FK
        rotation ``R``. Straight-down is base ``(0, 0, -1)``. Returns ``None``
        when ``R`` is malformed / the axis is degenerate."""
        try:
            axis = np.asarray(R, dtype=np.float64).reshape(3, 3)[:, 0]
        except (ValueError, TypeError):
            return None
        norm = float(np.linalg.norm(axis))
        if not math.isfinite(norm) or norm < 1e-9:
            return None
        cos_a = float(np.dot(axis / norm, np.array([0.0, 0.0, -1.0])))
        cos_a = max(-1.0, min(1.0, cos_a))
        return math.degrees(math.acos(cos_a))

    @staticmethod
    def _fit_plane(points: np.ndarray) -> tuple[tuple[float, float, float], float]:
        """Least-squares plane ``z = a·x + b·y + c`` over Nx3 points. Returns
        ``((a, b, c), rms_residual_m)``."""
        xy1 = np.column_stack([points[:, 0], points[:, 1], np.ones(len(points))])
        z = points[:, 2]
        coeff, *_ = np.linalg.lstsq(xy1, z, rcond=None)
        resid = z - xy1 @ coeff
        rms = float(np.sqrt(np.mean(resid ** 2)))
        return (float(coeff[0]), float(coeff[1]), float(coeff[2])), rms

    def solve_table_plane(self, camera: str = 'scene') -> tuple[bool, float, str]:
        """Fit the table plane from the touch points + persist z_table (+ tilt)
        into the scene extrinsic YAML. ``z_table`` is the end-effector-frame
        table height (what the grasp descends to) — measured, not assumed.
        Returns (ok, residual_m, message)."""
        with self._lock:
            buf = self._table_touch
            if buf is None or len(buf.points) < TABLE_TOUCH_POINTS_REQUIRED:
                have = len(buf.points) if buf else 0
                need = TABLE_TOUCH_POINTS_REQUIRED - have
                return False, 0.0, f'Es fehlen noch {need} Tisch-Punkte.'
            pts = np.asarray(buf.points, dtype=np.float64)
            # Reject a too-clustered OR near-collinear capture: radial spread
            # catches clustering; the minor singular value of the centred XY
            # cloud catches points strung along a line (which leave the tilt
            # unconstrained).
            xy = pts[:, :2] - pts[:, :2].mean(axis=0)
            radial = float(np.max(np.linalg.norm(xy, axis=1)))
            sv = np.linalg.svd(xy, compute_uv=False)
            minor = float(sv[-1]) / float(np.sqrt(max(1, len(pts))))
            if radial < TABLE_TOUCH_MIN_SPREAD_M or minor < TABLE_TOUCH_MIN_MINOR_M:
                return False, 0.0, (
                    'Die Tisch-Punkte liegen zu nah beisammen oder auf einer '
                    'Linie — bitte über die Arbeitsfläche verteilt tippen '
                    '(Ecken + Mitte).'
                )
            try:
                (a, b, c), rms = self._fit_plane(pts)
            except np.linalg.LinAlgError:
                return False, 0.0, (
                    'Ebene konnte nicht berechnet werden — bitte die Punkte '
                    'weiter verteilen.'
                )
            if not math.isfinite(rms) or rms > TABLE_TOUCH_MAX_RESIDUAL_M:
                return False, float(rms if math.isfinite(rms) else 0.0), (
                    'Die Punkte liegen nicht auf einer ebenen Fläche — bitte '
                    'jeweils sauber auf den Tisch tippen und erneut vermessen.'
                )
            cx, cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
            z_table = float(a * cx + b * cy + c)
            # Loose cross-check vs the camera extrinsic's board height — catches
            # a grossly wrong extrinsic (sign flip / placement), not the
            # finger-length offset between the tip-touch and the board.
            board_z = self._load_board_table_z(camera)
            if board_z is not None and abs(z_table - board_z) > TABLE_TOUCH_VS_CAMERA_MAX_M:
                return False, float(rms), (
                    'Gemessene Tischhöhe passt nicht zur Kamerakalibrierung — '
                    'bitte die Kamera-Ausrichtung wiederholen.'
                )
            buf.plane = (a, b, c)
            if not self._write_table_plane(camera, a, b, c, z_table):
                return False, float(rms), (
                    'Tischebene konnte nicht gespeichert werden.'
                )
            return True, float(rms), (
                f'Tisch vermessen (Restfehler {rms * 1000:.1f} mm).'
            )

    def _load_board_table_z(self, camera: str) -> float | None:
        """Read the SUPPLIED board surface height (``board_table_z``) from the
        scene extrinsic YAML, or None — used only for the touch-off loose
        cross-check (NOT for the grasp z, which is the measured touch-off
        ``z_table``)."""
        path = self._handeye_path(camera)
        if not path.exists():
            return None
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
            node = fs.getNode('board_table_z')
            z = float(node.real()) if not node.empty() else None
            fs.release()
            return z
        except Exception:
            return None

    def _write_table_plane(self, camera: str, a: float, b: float, c: float,
                           z_table: float) -> bool:
        """Re-write the scene extrinsic YAML with the measured z_table + plane
        (a, b, c), preserving the existing camera→base transform."""
        path = self._handeye_path(camera)
        if not path.exists():
            return False
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
            transform = fs.getNode('transform').mat()
            bx = fs.getNode('board_origin_x')
            board_x = float(bx.real()) if not bx.empty() else BOARD_ORIGIN_X_M
            by = fs.getNode('board_origin_y')
            board_y = float(by.real()) if not by.empty() else BOARD_ORIGIN_Y_M
            bz = fs.getNode('board_table_z')
            board_table_z = float(bz.real()) if not bz.empty() else BOARD_TABLE_Z_M
            fs.release()
            if transform is None:
                return False
        except Exception:
            return False
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
            fs.write('transform', transform)
            fs.write('method', 'BOARD_ON_TABLE+TOUCHOFF')
            # z_table = the MEASURED grasp/EE-frame table height (touch-off).
            fs.write('z_table', float(z_table))
            fs.write('table_plane', np.array([a, b, c], dtype=np.float64))
            fs.write('board_table_z', float(board_table_z))
            fs.write('board_origin_x', float(board_x))
            fs.write('board_origin_y', float(board_y))
            fs.write('captured_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
            fs.release()
            return True
        except Exception:
            return False
