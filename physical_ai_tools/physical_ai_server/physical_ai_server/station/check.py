#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Station checks (plan v6.13 §7): reference detection, the canonical corner
mapping, the camera-check metrics, the robot check, the re-solve bounds and the
2D rigid fit the registration uses. Pure (numpy + OpenCV), no rclpy.

Units: the image in pixels, everything else in METRES in the mat / base frame;
the metrics are reported in millimetres. ``T_cam_*`` maps camera coordinates
into the named frame (p_frame = T @ p_cam), the convention of
``projection.project_pixel_to_table``.
"""

import math

import numpy as np

from physical_ai_server.station import texts_de as T

STATION_ID_MIN, STATION_ID_MAX = 100, 199
MAT_X = (-0.075, 0.733)
MAT_Y = (-0.300, 0.300)
MAT_GROW_M = 0.060
# §7.1 OK band (D11: against the commissioned corners).
OK_RMS_MM = 3.0
OK_MAX_MM = 6.0
OK_CENTROID_MM = 3.0
OK_ROTATION_DEG = 0.5
FAR_MAX_MM = 25.0
# §7.2
ROBOT_NEAR_DOT_MM = 12.0
ROBOT_DXY_MM = 4.0
ROBOT_DZ_MM = 3.0
MAX_TILT_DEG = 12.0
JOINTS_MAX_AGE_S = 0.5
# §7.3
RESOLVE_SHIFT_MM = 25.0
RESOLVE_ROT_DEG = 3.0
_UNDISTORT_CRITERIA = None


def _cv2():
    import cv2
    return cv2


def _criteria():
    cv2 = _cv2()
    return (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-10)


def make_detector():
    """OpenCV ArucoDetector for AprilTag 36h11 with sub-pixel corner refinement."""
    cv2 = _cv2()
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return cv2.aruco.ArucoDetector(dictionary, params)


def detect_tags(frame, detector=None):
    """[(id, 4×2 corners px)] for every AprilTag 36h11 in a BGR or gray frame."""
    cv2 = _cv2()
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    det = detector or make_detector()
    corners, ids, _ = det.detectMarkers(gray)
    if ids is None:
        return []
    return [(int(i), np.asarray(c, dtype=np.float64).reshape(4, 2))
            for i, c in zip(ids.reshape(-1), corners)]


def nominal_corners_m(centre_mm, black_mm):
    """A reference's four corners in the canonical order, metres: OpenCV's decoded
    order equals the printed page TL, TR, BR, BL, which maps to base
    (x+s/2, y+s/2), (x+s/2, y−s/2), (x−s/2, y−s/2), (x−s/2, y+s/2)."""
    x, y = centre_mm[0] / 1000.0, centre_mm[1] / 1000.0
    h = black_mm / 2000.0
    return np.array([[x + h, y + h], [x + h, y - h], [x - h, y - h], [x - h, y + h]])


def backproject(points_px, K, dist, T_cam_frame, z=0.0):
    """Undistort pixels with K / dist and intersect their rays with the plane
    z = ``z`` of the target frame. Returns N×2 (x, y) in metres (NaN where the
    ray misses)."""
    cv2 = _cv2()
    pts = np.asarray(points_px, dtype=np.float64).reshape(-1, 1, 2)
    und = cv2.undistortPointsIter(pts, np.asarray(K, dtype=np.float64),
                                  np.asarray(dist, dtype=np.float64).reshape(-1),
                                  None, None, _criteria()).reshape(-1, 2)
    R = np.asarray(T_cam_frame, dtype=np.float64)[:3, :3]
    t = np.asarray(T_cam_frame, dtype=np.float64)[:3, 3]
    out = np.full((len(und), 2), np.nan)
    for i, (u, v) in enumerate(und):
        d = R @ np.array([u, v, 1.0])
        if abs(d[2]) < 1e-12:
            continue
        s = (z - t[2]) / d[2]
        if s <= 0:
            continue
        p = t + s * d
        out[i] = p[:2]
    return out


def on_this_mat(xy, grow=MAT_GROW_M):
    x, y = float(xy[0]), float(xy[1])
    return (math.isfinite(x) and math.isfinite(y)
            and MAT_X[0] - grow <= x <= MAT_X[1] + grow
            and MAT_Y[0] - grow <= y <= MAT_Y[1] + grow)


def filter_this_mat(detections, K, dist, T_cam_mat):
    """§7.1 "this mat only": keep a tag whose centre, back-projected to z = 0
    with the saved calibration, lies on this mat grown by 60 mm."""
    kept = []
    for tag_id, corners in detections:
        centre = backproject(corners.mean(axis=0).reshape(1, 2), K, dist, T_cam_mat)[0]
        if on_this_mat(centre):
            kept.append((tag_id, corners))
    return kept


def classify_refs(detections, expected_ids):
    """('WRONG_MAT' | 'REFS_MISSING' | 'OK', {id: corners}) per §7.1's table."""
    expected = list(expected_ids)
    station = [(i, c) for i, c in detections if STATION_ID_MIN <= i <= STATION_ID_MAX]
    if any(i not in expected for i, _ in station):
        return 'WRONG_MAT', {}
    counts = {i: 0 for i in expected}
    by_id = {}
    for i, c in station:
        counts[i] += 1
        by_id[i] = c
    if any(n != 1 for n in counts.values()):
        return 'REFS_MISSING', {}
    return 'OK', by_id


def kabsch_2d(src, dst):
    """Proper 2D rigid fit dst ≈ R(θ)·src + t (no scale, det +1).
    Returns (theta_rad, t (2,), residuals (N,))."""
    a = np.asarray(src, dtype=np.float64).reshape(-1, 2)
    b = np.asarray(dst, dtype=np.float64).reshape(-1, 2)
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    H = (a - ca).T @ (b - cb)
    theta = math.atan2(H[0, 1] - H[1, 0], H[0, 0] + H[1, 1])
    c, s = math.cos(theta), math.sin(theta)
    R = np.array([[c, -s], [s, c]])
    t = cb - R @ ca
    res = np.linalg.norm((a @ R.T + t) - b, axis=1)
    return theta, t, res


def camera_metrics(measured_m, commissioned_m):
    """§7.1's v5 formulas on 16 corner errors E (measured − commissioned), mm:
    per-ref RMS, overall RMS, max ‖E‖, centroid ‖mean E‖, |Kabsch angle|."""
    m = np.asarray(measured_m, dtype=np.float64).reshape(-1, 2) * 1000.0
    c = np.asarray(commissioned_m, dtype=np.float64).reshape(-1, 2) * 1000.0
    E = m - c
    norms = np.linalg.norm(E, axis=1)
    per_ref = [float(np.sqrt(np.mean(norms[i:i + 4] ** 2))) for i in range(0, len(norms), 4)]
    theta, _, _ = kabsch_2d(c - c.mean(axis=0), m - m.mean(axis=0))
    return {
        'per_ref_rms_mm': per_ref,
        'rms_mm': float(np.sqrt(np.mean(norms ** 2))),
        'max_mm': float(norms.max()),
        'centroid_mm': float(np.linalg.norm(E.mean(axis=0))),
        'rotation_deg': abs(math.degrees(theta)),
    }


def classify_metrics(metrics):
    ok = (metrics['rms_mm'] <= OK_RMS_MM and metrics['max_mm'] <= OK_MAX_MM
          and metrics['centroid_mm'] <= OK_CENTROID_MM
          and metrics['rotation_deg'] <= OK_ROTATION_DEG)
    return 'OK' if ok else 'MOVED'


def moved_sentence(metrics):
    return T.MOVED_FAR_DE if metrics['max_mm'] > FAR_MAX_MM else T.MOVED_DE


def measure_refs(by_id, ref_ids, K, dist, T_cam_mat):
    """The 16 measured corners (mat frame, metres) in ref_ids order."""
    rows = [backproject(by_id[i], K, dist, T_cam_mat) for i in ref_ids]
    return np.vstack(rows)


def camera_check(frames, cal):
    """§7.1 on up to three distinct frames: detection, this-mat filter, the
    outcome table, then the metrics against ``ref_corners_commissioned``.
    ``cal`` carries K, dist, T_cam_mat, ref_ids, ref_corners_commissioned.
    Returns a dict with ``result`` (OK / MOVED / WRONG_MAT / REFS_MISSING)."""
    detector = make_detector()
    last = 'REFS_MISSING'
    for frame in frames:
        dets = filter_this_mat(detect_tags(frame, detector), cal['K'], cal['dist'],
                               cal['T_cam_mat'])
        outcome, by_id = classify_refs(dets, cal['ref_ids'])
        if outcome == 'WRONG_MAT':
            return {'result': 'WRONG_MAT', 'message': T.WRONG_MAT_DE}
        if outcome == 'REFS_MISSING':
            last = outcome
            continue
        measured = measure_refs(by_id, cal['ref_ids'], cal['K'], cal['dist'], cal['T_cam_mat'])
        if not np.all(np.isfinite(measured)):
            last = 'REFS_MISSING'
            continue
        m = camera_metrics(measured, cal['ref_corners_commissioned'])
        result = classify_metrics(m)
        m.update(result=result, message=T.CHECK_OK_DE if result == 'OK' else moved_sentence(m),
                 measured=measured)
        return m
    return {'result': last, 'message': T.REFS_MISSING_DE}


def tool_tip(R, t, axis_local, tip_below_ee_m):
    """§6.3: the tool-tip point t + R·axis·tool_tip_below_ee_m (never the EE origin)."""
    axis = np.asarray(R, dtype=np.float64).reshape(3, 3) @ np.asarray(axis_local, dtype=np.float64)
    n = float(np.linalg.norm(axis))
    if not math.isfinite(n) or n < 1e-9:
        return None
    return np.asarray(t, dtype=np.float64).reshape(3) + axis / n * float(tip_below_ee_m)


def tilt_deg(R, axis_local):
    axis = np.asarray(R, dtype=np.float64).reshape(3, 3) @ np.asarray(axis_local, dtype=np.float64)
    n = float(np.linalg.norm(axis))
    if not math.isfinite(n) or n < 1e-9:
        return None
    c = max(-1.0, min(1.0, float(np.dot(axis / n, [0.0, 0.0, -1.0]))))
    return math.degrees(math.acos(c))


def robot_check(tip_xyz, tilt, joints_age_s, dot_tips):
    """§7.2 steps 2–4. ``dot_tips`` is 4×3 (robot frame, metres)."""
    if joints_age_s is None or joints_age_s > JOINTS_MAX_AGE_S or tip_xyz is None:
        return {'result': 'NO_FRAME', 'message': T.NO_JOINTS_DE}
    if tilt is None or tilt > MAX_TILT_DEG:
        return {'result': 'NOT_VERTICAL', 'message': T.NOT_VERTICAL_DE}
    tips = np.asarray(dot_tips, dtype=np.float64).reshape(-1, 3)
    dxy = np.linalg.norm(tips[:, :2] - np.asarray(tip_xyz)[:2], axis=1) * 1000.0
    i = int(np.argmin(dxy))
    dz = float(abs(tip_xyz[2] - tips[i, 2]) * 1000.0)
    out = {'dot_id': i + 1, 'dot_dxy_mm': float(dxy[i]), 'dot_dz_mm': dz}
    if dxy[i] > ROBOT_NEAR_DOT_MM:
        out.update(result='NO_DOT', message=T.NO_DOT_DE)
    elif dxy[i] <= ROBOT_DXY_MM and dz <= ROBOT_DZ_MM:
        out.update(result='OK', message=T.ROBOT_OK_DE)
    else:
        out.update(result='ROBOT_MOVED', message=T.ROBOT_MOVED_DE)
    return out


def pose_difference(T_a, T_b):
    """(camera-centre shift mm, rotation deg) between two camera poses."""
    A = np.asarray(T_a, dtype=np.float64)
    B = np.asarray(T_b, dtype=np.float64)
    shift = float(np.linalg.norm(A[:3, 3] - B[:3, 3]) * 1000.0)
    Rd = A[:3, :3].T @ B[:3, :3]
    c = max(-1.0, min(1.0, (float(np.trace(Rd)) - 1.0) / 2.0))
    return shift, math.degrees(math.acos(c))


def resolve_within_bounds(T_new, T_commissioned):
    """§7.3 step 3: against T_cam_mat_commissioned, never the last re-solve."""
    shift, rot = pose_difference(T_new, T_commissioned)
    return shift <= RESOLVE_SHIFT_MM and rot <= RESOLVE_ROT_DEG, shift, rot


def registration_matrix(theta, t):
    """REG (4×4): rotation about z + translation in the table plane, mat → base."""
    c, s = math.cos(theta), math.sin(theta)
    M = np.eye(4)
    M[:2, :2] = [[c, -s], [s, c]]
    M[0, 3], M[1, 3] = float(t[0]), float(t[1])
    return M
