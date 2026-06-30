#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Camera calibration for Roboter Studio (SCENE-cam only).

The pipeline is a 3-step state machine driven by the React wizard:
    intrinsic (scene) -> extrinsic (scene, board-on-table) -> table touch-off

(The former 4th colour-profile step was removed in v2.9.4 — colour detection
is gone; the editor unlocks after the touch-off. There is no colour profile.)

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
# W4 (2026-06-24): raised 12 -> 20. SOTA camera-calibration practice is 20-30
# well-spread views (varied tilt + board pushed into the image corners) so the
# RATIONAL_MODEL's 8 distortion coefficients are well-conditioned for the wide
# ~100-degree scene lens; 12 frames under-constrains the k4/k5/k6 terms.
INTRINSIC_FRAMES_REQUIRED = 20
# WS4 (2026-06-17): the SCENE extrinsic is one board-on-table capture. The
# student still presses "Bild erfassen" ONCE; the buffer holds one (averaged)
# sample, so this stays 1.
SCENE_EXTRINSIC_FRAMES_REQUIRED = 1
# P3a (multi-frame averaging): one "Bild erfassen" click internally BURSTS this
# many camera reads of the (L-jig-fixed) board and averages the per-read solvePnP
# poses (translation mean + SO(3) rotation mean), reducing sensor jitter ~sqrt(N)
# with NO change to the student workflow. The board is fixed, so consecutive
# reads differ only by sensor noise — averaging attacks that. Set BURST_FRAMES=1
# to revert to the exact single-shot behaviour. INTERVAL spaces the reads past
# the ~25 fps scene-cam frame period so each read is a distinct frame.
EXTRINSIC_BURST_FRAMES = max(1, _safe_int_env('EDUBOTICS_EXTRINSIC_BURST_FRAMES', 8))
# Clamp ≥ 0: a negative override would make time.sleep() raise ValueError.
EXTRINSIC_BURST_INTERVAL_S = max(0.0, _safe_float_env('EDUBOTICS_EXTRINSIC_BURST_INTERVAL_S', 0.06))
# Minimum reads that must pass the per-frame gates for an averaged capture to be
# accepted (else the board wasn't stably visible — ask for a re-capture).
EXTRINSIC_BURST_MIN_PASS = max(1, EXTRINSIC_BURST_FRAMES // 2)

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
    # Phase-2 wizard UX (set on each successful intrinsic capture; NOT part of
    # capture_frame's tested return tuple): which 4x4 mosaic cell the board
    # centroid landed in (0..15) and a 1/2/3 quality badge from last_view_rms.
    last_coverage_cell: int | None = None
    last_quality: int | None = None


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
    extrinsic + table touch-off). The gripper camera is no longer calibrated.

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
        # #1 (2026-06-19): per-rig intrinsic calibration is MANDATORY — the
        # factory-default seeding was REMOVED. A guessed focal length scales
        # every projected XY (the Innomaker scene cam is a ~102° lens, so a
        # 600 px default could be ~2x wrong), and the touch-off only fixes
        # grasp HEIGHT, not lateral scale. No guessed K ever ships now: the
        # student runs the 20-frame ChArUco intrinsic step (INTRINSIC_FRAMES_
        # REQUIRED), which the extrinsic step already gates on (start_step
        # 'extrinsic' requires has_intrinsics).

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _intrinsic_path(self, camera: str) -> Path:
        return CALIB_DIR / f'{camera}_intrinsics.yaml'

    def _handeye_path(self, camera: str) -> Path:
        return CALIB_DIR / f'{camera}_handeye.yaml'

    def _load_persisted_intrinsics(self) -> None:
        for camera in ('gripper', 'scene'):
            path = self._intrinsic_path(camera)
            if not path.exists():
                continue
            try:
                fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
                K = fs.getNode('camera_matrix').mat()
                dist = fs.getNode('distortion_coefficients').mat()
                src_node = fs.getNode('source')
                source = src_node.string() if not src_node.empty() else ''
                fs.release()
                # #1: a YAML left by the old factory-default seeding is a GUESSED
                # K — ignore it so a rig upgraded from an older install still
                # requires a real per-rig intrinsic calibration (no silent
                # guessed projection).
                if source == 'factory_default':
                    continue
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
            # Anything other than 'intrinsic' / 'handeye' is rejected as
            # unknown so a typo'd frontend call surfaces clearly.
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

            if camera in self._intrinsic_buffers:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                corners, ids = self._detect_charuco(gray)
                if corners is None:
                    return False, 0, 0, 0.0, (
                        'ChArUco-Tafel nicht erkannt — bitte Position anpassen.'
                    )
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
                # Phase-2 wizard UX: stash the 4x4 coverage cell + 1/2/3 quality
                # badge for THIS capture on the buffer so the capture callback can
                # fill CalibrationCaptureFrame.coverage_cell/quality WITHOUT
                # changing capture_frame's (tested) 5-tuple return signature.
                cx, cy = self._charuco_centroid_px(corners)
                buf.last_coverage_cell = self._coverage_cell(cx, cy, this_size)
                buf.last_quality = self._quality_from_rms(rms)
                return True, len(buf.all_corners), INTRINSIC_FRAMES_REQUIRED, float(rms or 0.0), (
                    f'Bild {len(buf.all_corners)}/{INTRINSIC_FRAMES_REQUIRED} erfasst.'
                )

            # Scene extrinsic step (WS4 board-on-table) with P3a multi-frame
            # averaging. NO arm motion, NO gripper pose: the board lies flat on
            # the table, the static scene cam reads it, solvePnP gives the
            # board->camera transform, and the solve combines it with the known
            # board-on-table placement (T_board_to_base) to recover T_cam_to_base.
            # One "Bild erfassen" click BURSTS EXTRINSIC_BURST_FRAMES reads of the
            # fixed board and AVERAGES the per-read poses (jitter reduction); the
            # averaged sample replaces any prior one (latest-wins).
            if not self.has_intrinsics(camera):
                return False, 0, 0, 0.0, (
                    f'Intrinsische Daten für {camera} fehlen.'
                )
            K = self._intrinsics[camera]['K']
            dist = self._intrinsics[camera]['dist']

            # First read = the frame already in hand; then burst additional live
            # reads (each a distinct camera frame, spaced past the frame period).
            # An EXPLICIT bgr (unit tests) is a single deterministic capture — no
            # burst — so the existing single-frame behaviour is preserved exactly.
            samples = []
            first = self._board_pose_from_frame(frame, K, dist)
            if first is not None:
                samples.append(first)
            if bgr is None and self._get_frame is not None:
                for _ in range(EXTRINSIC_BURST_FRAMES - 1):
                    time.sleep(EXTRINSIC_BURST_INTERVAL_S)
                    f = self._get_frame(camera)
                    if f is None:
                        continue
                    s = self._board_pose_from_frame(f, K, dist)
                    if s is not None:
                        samples.append(s)

            min_pass = 1 if bgr is not None else EXTRINSIC_BURST_MIN_PASS
            if len(samples) < min_pass:
                return False, 0, 0, 0.0, (
                    'Die Tafel konnte nicht stabil erfasst werden — bitte die '
                    'Tafel flach und vollständig ins Bild legen und erneut '
                    'erfassen.'
                )

            # W2 (2026-06-24): robustly drop per-frame outliers BEFORE the SVD
            # rotation mean. The mean only attenuates zero-mean jitter; a single
            # frame with a markedly higher reprojection error (a partial/blurred
            # detection that still squeaked under the 1.5 px gate, or a transient
            # bad PnP) would otherwise pull both the translation mean and the
            # SO(3) chordal mean toward a biased pose. A robust MAD threshold
            # (median + k·MAD) is insensitive to up to ~50% contamination; we
            # always keep at least EXTRINSIC_BURST_MIN_PASS frames (or, if too
            # few survive, the lowest-reproj ones) so a tight cluster with one
            # outlier still produces an averaged sample.
            samples = self._reject_reproj_outliers(samples, min_pass)

            R_avg = self._average_rotations([s[0] for s in samples])
            t_avg = np.mean(
                np.stack([np.asarray(s[1], dtype=np.float64).reshape(3) for s in samples],
                         axis=0),
                axis=0,
            ).reshape(3, 1)
            mean_reproj = float(np.mean([s[2] for s in samples]))

            buf = self._handeye_buffers[camera]
            buf.R_target2cam = [R_avg]
            buf.t_target2cam = [t_avg]
            buf.R_gripper2base = []
            buf.t_gripper2base = []

            return True, len(buf.R_target2cam), SCENE_EXTRINSIC_FRAMES_REQUIRED, mean_reproj, (
                f'Bild erfasst ({len(samples)} Messung(en) gemittelt, '
                f'Reprojektionsfehler {mean_reproj:.1f} px) — bitte berechnen '
                '& speichern.'
            )

    def _estimate_view_rms(self, buf: IntrinsicCaptureBuffer) -> float | None:
        """Run a quick calibration on the current set to estimate quality. Cheap
        enough at <=20 frames to surface live RMS feedback in the wizard."""
        if len(buf.all_corners) < 4 or buf.image_size is None:
            return None
        try:
            ret, _, _, _, _ = cv2.aruco.calibrateCameraCharuco(
                buf.all_corners, buf.all_ids, self._board, buf.image_size, None, None,
            )
            return float(ret)
        except cv2.error:
            return None

    # ------------------------------------------------------------------
    # Live preview (lock-free) + wizard coverage/quality helpers
    # ------------------------------------------------------------------
    def detect_preview(
        self, frame: np.ndarray | None
    ) -> tuple[bool, list[float], list[float], int]:
        """LOCK-FREE live ChArUco corner preview for the wizard overlay.

        Detects the board in ONE frame and returns the detected corner pixel
        coordinates + a rough board-area-fill percentage — WITHOUT taking
        ``self._lock``. The wizard polls this a few Hz and it MUST NOT queue
        behind a multi-second intrinsic solve, so it deliberately holds no lock.
        It only READS the detector params / dictionary / board built ONCE in
        ``__init__`` and never mutated thereafter — ``cv2.aruco.detectMarkers`` /
        ``interpolateCornersCharuco`` treat them as const inputs and keep their
        own local working buffers — so it is safe to run concurrently with a
        capture/solve that holds the lock (no shared mutable state is touched).

        Returns ``(detected, corners_x, corners_y, board_area_pct)``. On no
        detection / a missing or malformed frame returns ``(False, [], [], 0)``.
        ``corners_x`` / ``corners_y`` are parallel pixel-coordinate lists in the
        camera's NATIVE resolution (the same resolution the MJPEG stream serves,
        so the React SVG overlay's viewBox lines up — see the callback note)."""
        if frame is None:
            return False, [], [], 0
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except cv2.error:
            return False, [], [], 0
        corners, _ids = self._detect_charuco(gray)
        if corners is None:
            return False, [], [], 0
        pts = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
        if pts.shape[0] == 0:
            return False, [], [], 0
        xs = [float(x) for x in pts[:, 0]]
        ys = [float(y) for y in pts[:, 1]]
        h, w = gray.shape[:2]
        area_pct = 0
        if w > 0 and h > 0 and pts.shape[0] >= 4:
            bw = float(pts[:, 0].max() - pts[:, 0].min())
            bh = float(pts[:, 1].max() - pts[:, 1].min())
            frac = (bw * bh) / float(w * h)
            if math.isfinite(frac):
                area_pct = int(max(0.0, min(100.0, frac * 100.0)))
        return True, xs, ys, area_pct

    def last_intrinsic_capture_meta(self, camera: str) -> tuple[int, int]:
        """Return ``(coverage_cell, quality)`` for the most recent INTRINSIC
        capture of ``camera``, or ``(0, 0)`` when there is no intrinsic buffer /
        no capture yet (e.g. an extrinsic or touch-off capture). Lock-free read
        of two plain ints written under the capture lock — a torn read at worst
        shows a stale cell/quality for the live wizard, never a crash."""
        buf = self._intrinsic_buffers.get(camera)
        if buf is None:
            return 0, 0
        cell = buf.last_coverage_cell if buf.last_coverage_cell is not None else 0
        quality = buf.last_quality if buf.last_quality is not None else 0
        return int(cell), int(quality)

    @staticmethod
    def _charuco_centroid_px(corners) -> tuple[float, float]:
        """Mean (x, y) pixel of the detected ChArUco corners."""
        pts = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
        return float(pts[:, 0].mean()), float(pts[:, 1].mean())

    @staticmethod
    def _coverage_cell(cx: float, cy: float, image_size: tuple[int, int]) -> int:
        """Map a pixel ``(cx, cy)`` to a 0..15 cell index of a 4x4 grid laid over
        the image (row-major: cell = row*4 + col). ``image_size`` is ``(W, H)``.
        Out-of-range / degenerate inputs clamp to a valid cell (0)."""
        w, h = int(image_size[0]), int(image_size[1])
        if w <= 0 or h <= 0 or not (math.isfinite(cx) and math.isfinite(cy)):
            return 0
        col = min(3, max(0, int(cx / w * 4)))
        row = min(3, max(0, int(cy / h * 4)))
        return row * 4 + col

    @staticmethod
    def _quality_from_rms(rms: float | None) -> int:
        """Map a per-view reprojection RMS (px) to a 1/2/3 badge
        (1=POOR, 2=OK, 3=GOOD), or 0 when unknown (None / non-finite). Lower
        RMS = a sharper, better-conditioned view."""
        if rms is None or not math.isfinite(rms):
            return 0
        if rms < 1.0:
            return 3
        if rms < 2.0:
            return 2
        return 1

    def _board_pose_from_frame(self, frame, K, dist):
        """Detect the ChArUco board in ONE frame and solvePnP its pose.

        Returns ``(R_t2c 3x3, t_t2c 3x1, reproj_px)`` or ``None`` when detection
        / matching / solve fails OR the reprojection gate rejects the frame. The
        per-frame quality gates (min corners, reprojection error) are the SAME as
        the legacy single-shot path — multi-frame averaging only feeds the solve
        frames that already pass them, so a bad read can't pull the average."""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except cv2.error:
            return None
        corners, ids = self._detect_charuco(gray)
        if corners is None:
            return None
        match_fn = getattr(self._board, 'matchImagePoints', None)
        if callable(match_fn):
            object_points, image_points = match_fn(corners, ids)
        else:
            object_points, image_points = cv2.aruco.getBoardObjectAndImagePoints(
                self._board, corners, ids,
            )
        if object_points is None or len(object_points) < SCENE_EXTRINSIC_MIN_CORNERS:
            return None
        # W2 (2026-06-24): solve with SQPNP instead of the default ITERATIVE.
        # SQPNP (Terzakis & Lourakis 2020, OpenCV >=4.7) is a GLOBAL-optimal,
        # single-solution PnP that is well-conditioned on the near-planar
        # ChArUco point set (ITERATIVE is a local Levenberg-Marquardt descent
        # from a homography/DLT seed that can settle in a worse local minimum
        # and, on a planar target, suffers the two-fold pose ambiguity). We then
        # polish with solvePnPRefineLM (a few LM iterations seeded by the SQPNP
        # result) to squeeze the last reprojection residual. Both reduce the
        # SYSTEMATIC per-frame pose bias — something the later SO(3) burst
        # averaging cannot do (averaging only attenuates zero-mean jitter).
        # Fallback to ITERATIVE only if SQPNP raises (defensive — it needs >=3
        # points and the board has many, so this should never fire).
        try:
            ok, rvec, tvec = cv2.solvePnP(
                object_points, image_points, K, dist, flags=cv2.SOLVEPNP_SQPNP,
            )
        except cv2.error as e:
            print(
                f'[WARNUNG] SQPNP-Solver fehlgeschlagen ({e}); '
                f'Rückfall auf ITERATIVE.',
                file=sys.stderr, flush=True,
            )
            ok, rvec, tvec = cv2.solvePnP(
                object_points, image_points, K, dist, flags=cv2.SOLVEPNP_ITERATIVE,
            )
        if not ok:
            return None
        # Polish the pose with a Levenberg-Marquardt refinement seeded by the
        # SQPNP result (in-place on rvec/tvec). Best-effort: a refinement error
        # leaves the already-valid SQPNP pose untouched.
        try:
            rvec, tvec = cv2.solvePnPRefineLM(
                object_points, image_points, K, dist, rvec, tvec,
            )
        except cv2.error:
            pass
        reproj = self._reprojection_error_px(object_points, image_points, rvec, tvec, K, dist)
        if not math.isfinite(reproj) or reproj > SCENE_EXTRINSIC_MAX_REPROJ_PX:
            return None
        R_t2c, _ = cv2.Rodrigues(rvec)
        return R_t2c, tvec.reshape(3, 1), float(reproj)

    @staticmethod
    def _reject_reproj_outliers(samples: list, min_keep: int, k: float = 3.0) -> list:
        """Drop burst frames whose per-frame reprojection error is a robust
        outlier (> median + k·MAD), keeping at least ``min_keep`` frames.

        Each sample is ``(R_3x3, t_3x1, reproj_px)``. The MAD (median absolute
        deviation, scaled by 1.4826 so it estimates the Gaussian σ) is used
        instead of the standard deviation because it is not itself dragged by
        the very outliers it is meant to detect. When fewer than ``min_keep``
        frames survive the threshold we fall back to the ``min_keep``
        lowest-reproj frames so a noisy burst with several borderline frames
        still yields the best-available averaged sample (never fewer than the
        min-pass count the caller already validated). With <=2 input frames
        there is nothing robust to estimate, so the set passes through."""
        if len(samples) <= 2:
            return samples
        errs = np.asarray([float(s[2]) for s in samples], dtype=np.float64)
        med = float(np.median(errs))
        mad = float(np.median(np.abs(errs - med)))
        # 1.4826: MAD→σ consistency factor for a normal distribution.
        thresh = med + k * 1.4826 * mad
        kept = [s for s, e in zip(samples, errs) if e <= thresh]
        if len(kept) >= min_keep:
            return kept
        # Too few survived (or MAD==0 edge cases): keep the lowest-reproj frames.
        order = np.argsort(errs)
        n = max(min_keep, 1)
        return [samples[i] for i in order[:n]]

    @staticmethod
    def _average_rotations(Rs: list[np.ndarray]) -> np.ndarray:
        """Chordal L2 mean of rotation matrices, projected back onto SO(3) via
        SVD (the standard rotation average for tightly-clustered rotations — the
        per-read jitter of a fixed board). ``R = U·diag(1,1,det(U·Vᵀ))·Vᵀ``
        guarantees a proper rotation (det +1)."""
        M = np.mean(
            np.stack([np.asarray(R, dtype=np.float64).reshape(3, 3) for R in Rs], axis=0),
            axis=0,
        )
        U, _, Vt = np.linalg.svd(M)
        D = np.eye(3, dtype=np.float64)
        D[2, 2] = float(np.sign(np.linalg.det(U @ Vt)) or 1.0)
        return U @ D @ Vt

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
            # W4 (2026-06-24): CALIB_RATIONAL_MODEL fits the 8-term rational
            # radial distortion (k1,k2,p1,p2,k3,k4,k5,k6) instead of the default
            # 5-term plane (k1,k2,p1,p2,k3). The scene cam is a ~100-degree lens
            # whose strong barrel distortion the 5-term polynomial cannot model
            # to the edges — the extra k4..k6 denominator terms capture it, and
            # all downstream readers feed ``dist`` straight to cv2
            # (undistortPoints / projectPoints / solvePnP) which accept the
            # variable-length vector natively, so the 8-coeff dist is persisted
            # as-is with no slicing/reshaping.
            ret, K, dist, _, _, _, _, _ = cv2.aruco.calibrateCameraCharucoExtended(
                buf.all_corners, buf.all_ids, self._board, buf.image_size,
                None, None, flags=cv2.CALIB_RATIONAL_MODEL,
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

    # ------------------------------------------------------------------
    # W5 — ground-truth XY correction + yaw bias (P3b accuracy verify)
    # ------------------------------------------------------------------
    @staticmethod
    def _umeyama_similarity_2d(
        src: np.ndarray, dst: np.ndarray
    ) -> tuple[np.ndarray, float] | None:
        """Closed-form least-squares 2D SIMILARITY (rotation + uniform scale +
        translation) mapping ``src`` -> ``dst`` via Umeyama (1991).

        ``src`` / ``dst`` are Nx2 (N>=2). Returns ``(M_2x3, det_R)`` where the
        2x3 affine ``M = [sR | t]`` applies as ``dst ≈ (M[:, :2] @ p) + M[:, 2]``
        and ``det_R`` is the determinant of the (pre-scale) rotation block sign
        proxy — actually the determinant of the recovered linear block scaled
        by ``1/s**2`` so a reflection shows as a negative determinant. We force
        a PROPER rotation (no reflection) in the returned ``M`` regardless, and
        report the handedness via the sign so the caller can REJECT a mirror
        rather than silently flatten it. Returns ``None`` on a degenerate input
        (all source points coincident → zero variance).

        Umeyama is preferred over ``cv2.estimateAffinePartial2D`` because it is
        a deterministic closed form (no RANSAC randomness) and exposes the
        reflection cleanly through the diagonal correction term ``S``."""
        src = np.asarray(src, dtype=np.float64).reshape(-1, 2)
        dst = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
        n = src.shape[0]
        mu_src = src.mean(axis=0)
        mu_dst = dst.mean(axis=0)
        src_c = src - mu_src
        dst_c = dst - mu_dst
        var_src = float(np.mean(np.sum(src_c ** 2, axis=1)))
        if not math.isfinite(var_src) or var_src < 1e-12:
            return None
        # Covariance dst<-src (2x2).
        cov = (dst_c.T @ src_c) / n
        U, D, Vt = np.linalg.svd(cov)
        # S corrects the reflection so U·S·Vt is a PROPER rotation.
        S = np.eye(2, dtype=np.float64)
        det_uv = float(np.linalg.det(U) * np.linalg.det(Vt))
        # det_uv < 0 means the optimal alignment is a reflection (mirror); the
        # caller decides what to do. We still build a proper rotation here.
        if det_uv < 0:
            S[-1, -1] = -1.0
        R = U @ S @ Vt
        scale = float(np.trace(np.diag(D) @ S) / var_src)
        sR = scale * R
        t = mu_dst - sR @ mu_src
        M = np.zeros((2, 3), dtype=np.float64)
        M[:, :2] = sR
        M[:, 2] = t
        # det_R proxy: sign carries the handedness (negative => mirror needed).
        det_R = det_uv
        return M, det_R

    @staticmethod
    def apply_xy_correction(xy, M) -> tuple[float, float]:
        """Apply a 2x3 affine correction ``M`` to a base-frame ``(x, y)`` point.

        ``corrected = M[:, :2] @ [x, y] + M[:, 2]``. Provided here (the owner of
        the correction math) so the server/loader applies the SAME convention
        that :func:`fit_xy_correction` solved for. ``M`` may be a list or
        ndarray (2x3)."""
        Mn = np.asarray(M, dtype=np.float64).reshape(2, 3)
        p = np.asarray(xy, dtype=np.float64).reshape(2)
        out = Mn[:, :2] @ p + Mn[:, 2]
        return float(out[0]), float(out[1])

    @staticmethod
    def _circular_mean(angles: np.ndarray) -> float:
        """Circular mean of angles (rad) = ``atan2(Σsin, Σcos)``. Robust to the
        ±π wrap that a plain arithmetic mean of yaw differences would mishandle."""
        a = np.asarray(angles, dtype=np.float64).reshape(-1)
        return float(math.atan2(float(np.sum(np.sin(a))), float(np.sum(np.cos(a)))))

    def fit_xy_correction(
        self,
        truth_xy,
        detected_xy,
        truth_yaws=None,
        detected_yaws=None,
    ) -> dict:
        """Fit a 2D similarity correction ``detected -> truth`` (base-frame
        metres) and the optional yaw bias.

        Args:
            truth_xy, detected_xy: Nx2 base-frame metres, N>=2 (the KNOWN placed
                positions vs the positions the current calibration detected).
            truth_yaws, detected_yaws: optional lists of yaw (rad). When >=1
                paired value is given, ``yaw_bias_rad`` = circular-mean(truth −
                detected); else 0.0.

        Returns a dict:
            ``{ok, message(German), xy_correction(2x3 list), yaw_bias_rad,
               residual_mm_mean, residual_mm_max, mirror_detected, point_count}``

        A mirror / handedness flip (det of the 2x2 linear block < 0) is a real
        axis/orientation error that must NOT be silently "corrected" by folding
        a reflection into the affine — it would happily fit but every grasp's
        joint5 handedness would stay wrong. So we detect it, return ``ok=False,
        mirror_detected=True`` with a German message, and persist NOTHING."""
        truth = np.asarray(truth_xy, dtype=np.float64).reshape(-1, 2)
        detected = np.asarray(detected_xy, dtype=np.float64).reshape(-1, 2)
        n = truth.shape[0]
        if detected.shape[0] != n:
            return {
                'ok': False,
                'message': 'Anzahl der Soll- und Ist-Punkte stimmt nicht überein.',
                'xy_correction': None, 'yaw_bias_rad': 0.0,
                'residual_mm_mean': 0.0, 'residual_mm_max': 0.0,
                'mirror_detected': False, 'point_count': int(n),
            }
        if n < 2:
            return {
                'ok': False,
                'message': ('Mindestens zwei Referenzpunkte werden benötigt — '
                            'bitte mehr Punkte erfassen.'),
                'xy_correction': None, 'yaw_bias_rad': 0.0,
                'residual_mm_mean': 0.0, 'residual_mm_max': 0.0,
                'mirror_detected': False, 'point_count': int(n),
            }
        fit = self._umeyama_similarity_2d(detected, truth)
        if fit is None:
            return {
                'ok': False,
                'message': ('Die Referenzpunkte sind entartet (alle gleich) — '
                            'bitte über die Arbeitsfläche verteilt erfassen.'),
                'xy_correction': None, 'yaw_bias_rad': 0.0,
                'residual_mm_mean': 0.0, 'residual_mm_max': 0.0,
                'mirror_detected': False, 'point_count': int(n),
            }
        M, det_R = fit
        # Mirror / handedness flip: the optimal alignment needed a reflection.
        # det of the recovered 2x2 linear block (= s**2 * det(R)) is negative
        # exactly when the underlying rotation is a reflection.
        det_linear = float(np.linalg.det(M[:, :2]))
        if det_R < 0 or det_linear < 0:
            return {
                'ok': False,
                'message': ('Achsen-/Spiegelungsfehler erkannt: die erkannten '
                            'Positionen sind gegenüber den Soll-Positionen '
                            'gespiegelt. Bitte die Kamera-Ausrichtung '
                            '(Extrinsik) prüfen und die Kalibrierung '
                            'wiederholen.'),
                'xy_correction': None, 'yaw_bias_rad': 0.0,
                'residual_mm_mean': 0.0, 'residual_mm_max': 0.0,
                'mirror_detected': True, 'point_count': int(n),
            }
        # Residuals: apply the fit to the detected points, compare vs truth (mm).
        corrected = (M[:, :2] @ detected.T).T + M[:, 2]
        resid_m = np.linalg.norm(corrected - truth, axis=1)
        residual_mm_mean = float(np.mean(resid_m) * 1000.0)
        residual_mm_max = float(np.max(resid_m) * 1000.0)
        # Yaw bias = circular-mean(truth - detected) over the paired yaws given.
        yaw_bias_rad = 0.0
        if truth_yaws is not None and detected_yaws is not None:
            ty = np.asarray(truth_yaws, dtype=np.float64).reshape(-1)
            dy = np.asarray(detected_yaws, dtype=np.float64).reshape(-1)
            m = min(ty.shape[0], dy.shape[0])
            if m >= 1:
                yaw_bias_rad = self._circular_mean(ty[:m] - dy[:m])
        return {
            'ok': True,
            'message': (f'Korrektur berechnet aus {n} Punkten '
                        f'(Restfehler Ø {residual_mm_mean:.1f} mm, '
                        f'max {residual_mm_max:.1f} mm).'),
            'xy_correction': M.tolist(),
            'yaw_bias_rad': float(yaw_bias_rad),
            'residual_mm_mean': residual_mm_mean,
            'residual_mm_max': residual_mm_max,
            'mirror_detected': False,
            'point_count': int(n),
        }

    def write_verify_correction(
        self,
        camera: str,
        xy_correction,
        yaw_bias_rad: float,
        residual_mm_mean: float,
        point_count: int,
    ) -> bool:
        """Add the ground-truth correction keys to the EXISTING scene extrinsic
        YAML, preserving every other key byte-for-byte.

        Adds: ``xy_correction`` (2x3 opencv-matrix), ``yaw_bias_rad``,
        ``verify_residual_mm``, ``verify_points``. Mirrors the read-modify-write
        pattern of :func:`_write_table_plane` — read the existing transform /
        table / board keys, then rewrite the whole file (FileStorage has no
        in-place append) carrying them forward unchanged plus the new keys."""
        path = self._handeye_path(camera)
        if not path.exists():
            return False
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
            transform = fs.getNode('transform').mat()
            method_node = fs.getNode('method')
            method = method_node.string() if not method_node.empty() else 'BOARD_ON_TABLE'
            # Preserve the touch-off / board keys when present.
            def _real(name, default):
                node = fs.getNode(name)
                return float(node.real()) if not node.empty() else default
            z_table = _real('z_table', None)
            table_plane = fs.getNode('table_plane').mat()
            board_table_z = _real('board_table_z', BOARD_TABLE_Z_M)
            board_x = _real('board_origin_x', BOARD_ORIGIN_X_M)
            board_y = _real('board_origin_y', BOARD_ORIGIN_Y_M)
            fs.release()
            if transform is None:
                return False
        except Exception:
            return False
        try:
            M = np.asarray(xy_correction, dtype=np.float64).reshape(2, 3)
        except Exception:
            return False
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
            fs.write('transform', transform)
            fs.write('method', method)
            if z_table is not None:
                fs.write('z_table', float(z_table))
            if table_plane is not None:
                fs.write('table_plane', np.asarray(table_plane, dtype=np.float64))
            fs.write('board_table_z', float(board_table_z))
            fs.write('board_origin_x', float(board_x))
            fs.write('board_origin_y', float(board_y))
            # New ground-truth correction keys.
            fs.write('xy_correction', M)
            fs.write('yaw_bias_rad', float(yaw_bias_rad))
            fs.write('verify_residual_mm', float(residual_mm_mean))
            fs.write('verify_points', int(point_count))
            fs.write('captured_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
            fs.release()
            return True
        except Exception:
            return False

    def read_verify_correction(self, camera: str = 'scene') -> dict | None:
        """Read the ground-truth correction from the scene extrinsic YAML.

        Returns ``{'xy_correction': np.ndarray(2x3), 'yaw_bias_rad': float}`` or
        ``None`` when the keys are absent (backward-safe: an old YAML without a
        verify step → ``None`` → the caller applies no correction / identity)."""
        path = self._handeye_path(camera)
        if not path.exists():
            return None
        try:
            fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_READ)
            node = fs.getNode('xy_correction')
            if node.empty():
                fs.release()
                return None
            M = node.mat()
            yb_node = fs.getNode('yaw_bias_rad')
            yaw_bias_rad = float(yb_node.real()) if not yb_node.empty() else 0.0
            fs.release()
            if M is None:
                return None
            return {
                'xy_correction': np.asarray(M, dtype=np.float64).reshape(2, 3),
                'yaw_bias_rad': yaw_bias_rad,
            }
        except Exception:
            return None
