#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Camera + hand-eye + colour-profile calibration for Roboter Studio.

The pipeline is a 4-step state machine driven by the React wizard:
    intrinsic (gripper) -> intrinsic (scene) -> hand-eye (gripper, eye-in-hand)
    -> hand-eye (scene, eye-to-base) -> colour profile

ChArUco board: 7x5 squares, 30 mm square, 22 mm marker, DICT_5X5_250. Hand-eye
is dual-solved with PARK + TSAI; the manager warns if the two methods
disagree by more than the configured thresholds (~2 deg / 5 mm).

Calibration state is persisted to per-camera YAML files under
CALIB_DIR (a docker named volume mount inside the physical_ai_server
container). Re-running a step overwrites the corresponding YAML.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

CALIB_DIR = Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))
INTRINSIC_FRAMES_REQUIRED = 12
HANDEYE_FRAMES_REQUIRED = 14
ANGLE_DISAGREEMENT_WARN_DEG = 4.0
TRANSLATION_DISAGREEMENT_WARN_M = 0.010

CHARUCO_SQUARES_X = 7
CHARUCO_SQUARES_Y = 5
CHARUCO_SQUARE_LENGTH_M = 0.030
CHARUCO_MARKER_LENGTH_M = 0.022


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
    """Aligned per-pose lists: (R_target2cam, t_target2cam) from the board
    detection, and (R_gripper2base, t_gripper2base) from the joint-state +
    forward kinematics callback."""

    R_target2cam: list[np.ndarray] = field(default_factory=list)
    t_target2cam: list[np.ndarray] = field(default_factory=list)
    R_gripper2base: list[np.ndarray] = field(default_factory=list)
    t_gripper2base: list[np.ndarray] = field(default_factory=list)


class CalibrationManager:
    """State machine + ChArUco / hand-eye math for Roboter Studio camera setup.

    Designed for single-threaded interaction from the ROS service callbacks
    (the manager's lock serialises capture/solve calls). Image and gripper
    pose acquisition is delegated to provider callables so the manager
    stays unit-testable with synthetic frames.
    """

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
        self._intrinsics: dict[str, dict] = {}
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        self._load_persisted_intrinsics()

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

    def has_intrinsics(self, camera: str) -> bool:
        return camera in self._intrinsics

    def has_handeye(self, camera: str) -> bool:
        return self._handeye_path(camera).exists()

    def has_color_profile(self) -> bool:
        return self._color_profile_path().exists()

    # ------------------------------------------------------------------
    # Step lifecycle
    # ------------------------------------------------------------------
    def start_step(self, camera: str, step: str) -> tuple[bool, str]:
        with self._lock:
            if step == 'intrinsic':
                # Drop any in-flight hand-eye buffer for this camera so
                # capture_frame doesn't route to the wrong solver after the
                # student switches steps.
                self._handeye_buffers.pop(camera, None)
                self._intrinsic_buffers[camera] = IntrinsicCaptureBuffer()
                return True, f'Intrinsische Kalibrierung für {camera} gestartet.'
            if step == 'handeye':
                if not self.has_intrinsics(camera):
                    return False, (
                        f'Bitte erst die intrinsische Kalibrierung der '
                        f'{camera}-Kamera abschließen.'
                    )
                # Drop any leftover intrinsic buffer for this camera —
                # without this, capture_frame's "if camera in
                # _intrinsic_buffers" precedence routes hand-eye captures
                # into the intrinsic buffer and the student never collects
                # a single hand-eye sample.
                self._intrinsic_buffers.pop(camera, None)
                self._handeye_buffers[camera] = HandEyeCaptureBuffer()
                return True, f'Hand-Auge-Kalibrierung für {camera} gestartet.'
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
                return True, 'Alle Kalibrier-Schritte abgebrochen.'
            self._intrinsic_buffers.pop(camera, None)
            self._handeye_buffers.pop(camera, None)
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

            # Hand-eye step
            if not self.has_intrinsics(camera):
                return False, 0, 0, 0.0, (
                    f'Intrinsische Daten für {camera} fehlen.'
                )
            if self._get_gripper_pose is None:
                return False, 0, 0, 0.0, 'Roboter-Pose-Provider fehlt.'
            pose = self._get_gripper_pose()
            if pose is None:
                return False, 0, 0, 0.0, 'Aktuelle Roboter-Pose unbekannt.'

            R_g2b, t_g2b = pose
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
            if object_points is None or len(object_points) < 4:
                return False, 0, 0, 0.0, 'Zu wenige Tafel-Punkte erkannt.'
            ok, rvec, tvec = cv2.solvePnP(object_points, image_points, K, dist)
            if not ok:
                return False, 0, 0, 0.0, 'Pose der Tafel konnte nicht bestimmt werden.'
            R_t2c, _ = cv2.Rodrigues(rvec)
            t_t2c = tvec.reshape(3, 1)

            buf = self._handeye_buffers[camera]
            buf.R_target2cam.append(R_t2c)
            buf.t_target2cam.append(t_t2c)
            buf.R_gripper2base.append(R_g2b)
            buf.t_gripper2base.append(t_g2b.reshape(3, 1))

            return True, len(buf.R_target2cam), HANDEYE_FRAMES_REQUIRED, 0.0, (
                f'Pose {len(buf.R_target2cam)}/{HANDEYE_FRAMES_REQUIRED} erfasst.'
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

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------
    def solve(self, camera: str, step: str) -> tuple[bool, float, float, str]:
        """Solve the named step. Returns (success, reprojection_error,
        method_disagreement_deg, message)."""
        with self._lock:
            if step == 'intrinsic':
                return self._solve_intrinsic(camera)
            if step == 'handeye':
                return self._solve_handeye(camera)
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

    def _solve_handeye(self, camera: str) -> tuple[bool, float, float, str]:
        buf = self._handeye_buffers.get(camera)
        if buf is None or len(buf.R_target2cam) < HANDEYE_FRAMES_REQUIRED:
            need = HANDEYE_FRAMES_REQUIRED - len(buf.R_target2cam) if buf else HANDEYE_FRAMES_REQUIRED
            return False, 0.0, 0.0, f'Es fehlen noch {need} Posen.'

        R_g2b = buf.R_gripper2base
        t_g2b = buf.t_gripper2base
        R_t2c = buf.R_target2cam
        t_t2c = buf.t_target2cam

        if camera == 'scene':
            # Eye-to-base setup: camera is fixed in the world, ChArUco rides
            # on the gripper. Inverting the gripper pose lets us reuse the
            # same `calibrateHandEye` API: we pass base->gripper in place of
            # gripper->base, and the result is the camera->base transform.
            R_inv = [r.T for r in R_g2b]
            t_inv = [-r.T @ t for r, t in zip(R_g2b, t_g2b)]
            R_g2b, t_g2b = R_inv, t_inv

        try:
            R_park, t_park = cv2.calibrateHandEye(
                R_g2b, t_g2b, R_t2c, t_t2c, method=cv2.CALIB_HAND_EYE_PARK,
            )
            R_tsai, t_tsai = cv2.calibrateHandEye(
                R_g2b, t_g2b, R_t2c, t_t2c, method=cv2.CALIB_HAND_EYE_TSAI,
            )
        except cv2.error as e:
            return False, 0.0, 0.0, f'Hand-Auge-Solver-Fehler: {e}'

        R_disagreement = R_park.T @ R_tsai
        cos_theta = max(-1.0, min(1.0, (np.trace(R_disagreement) - 1.0) / 2.0))
        angle_deg = float(np.degrees(np.arccos(cos_theta)))
        translation_diff_m = float(np.linalg.norm(t_park - t_tsai))

        # Audit §3.3 — refuse to persist a hand-eye solve where PARK and
        # TSAI disagree by more than the warn thresholds. The v1 code
        # only emitted an "Achtung:" string but always wrote the
        # transform; that meant a noisy capture set silently calibrated
        # the arm to drift several centimetres. Re-solving from the
        # already-captured 14 poses is free, so promote warn → block.
        if (
            angle_deg > ANGLE_DISAGREEMENT_WARN_DEG
            or translation_diff_m > TRANSLATION_DISAGREEMENT_WARN_M
        ):
            # Audit F14: drop the captured buffer so a re-press of the
            # solve button doesn't keep failing on the SAME noisy poses.
            # The student has to re-capture from scratch — which is the
            # correct response since the captured set is the input that
            # produced the disagreement.
            self._handeye_buffers.pop(camera, None)
            msg = (
                f'Hand-Auge-Solve abgewiesen: PARK ↔ TSAI weichen um '
                f'{angle_deg:.2f}° / {translation_diff_m * 1000:.1f} mm '
                f'ab (Limits: {ANGLE_DISAGREEMENT_WARN_DEG:.1f}° / '
                f'{TRANSLATION_DISAGREEMENT_WARN_M * 1000:.1f} mm). '
                'Bitte Posen erneut erfassen — am häufigsten hilft '
                'gleichmäßigere Beleuchtung und eine plane Tafel.'
            )
            return False, 0.0, angle_deg, msg

        T = np.eye(4)
        T[:3, :3] = R_park
        T[:3, 3] = t_park.reshape(3)

        z_table = self._derive_z_table(camera, T) if camera == 'scene' else None

        path = self._handeye_path(camera)
        fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
        fs.write('transform', T)
        fs.write('method', 'PARK')
        fs.write('angular_disagreement_deg', angle_deg)
        fs.write('translation_disagreement_m', translation_diff_m)
        if z_table is not None:
            fs.write('z_table', float(z_table))
        fs.write('captured_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
        fs.release()

        return True, 0.0, angle_deg, 'Hand-Auge-Kalibrierung gespeichert.'

    def _derive_z_table(self, camera: str, T_cam_to_base: np.ndarray) -> float | None:
        """Median z-coordinate of the board plane across the captured poses,
        expressed in base frame. Used to project pixel clicks onto the table.

        Audit F15: the prior implementation medianed only the BOARD
        ORIGIN. A tilted ChArUco puts the origin 2-3 cm above the table
        even when the corners touch it → projected cubes mis-located by
        that bias. Fix by sampling the four board corners per frame
        (origin + the diagonally opposite corner at full extent + the
        two cross corners) so a tilted-but-flat board averages out to
        the actual table plane z.
        """
        buf = self._handeye_buffers.get(camera)
        if buf is None:
            return None
        # The four extreme corners of the ChArUco board in the BOARD
        # coordinate frame (z=0 by construction). Adding these gives us
        # 4 z-samples per frame instead of 1.
        board_w = (CHARUCO_SQUARES_X - 1) * CHARUCO_SQUARE_LENGTH_M
        board_h = (CHARUCO_SQUARES_Y - 1) * CHARUCO_SQUARE_LENGTH_M
        sample_pts = np.array(
            [
                [0.0,     0.0,     0.0, 1.0],
                [board_w, 0.0,     0.0, 1.0],
                [0.0,     board_h, 0.0, 1.0],
                [board_w, board_h, 0.0, 1.0],
            ],
            dtype=np.float64,
        ).T  # shape (4, 4) homogeneous columns
        zs: list[float] = []
        for R, t in zip(buf.R_target2cam, buf.t_target2cam):
            T_target_to_cam = np.eye(4)
            T_target_to_cam[:3, :3] = R
            T_target_to_cam[:3, 3] = t.reshape(3)
            T_target_to_base = T_cam_to_base @ T_target_to_cam
            pts_base = T_target_to_base @ sample_pts  # 4x4
            zs.extend(float(z) for z in pts_base[2, :].tolist())
        if not zs:
            return None
        return float(np.median(zs))
