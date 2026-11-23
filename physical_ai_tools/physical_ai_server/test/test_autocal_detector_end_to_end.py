#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The real AprilTag detector, end to end through the mat check (spec 40 WP-18).

The V1 scene mat (its 14 field tags as ``tools/generate_scene_mat.py::draw_tag``
prints them: tag36h11, the black square ``MAT_TAG_SIZE_M``, upright with the
top toward +x) and the tower's two fixture tags on their labels are drawn pixel
by pixel through the contract camera (``mat_check.contract_camera``, a
Kannala-Brandt lens inside ``41`` H1's band), each pixel supersampled, the
image blurred like a lens and given sensor noise per look. ``Perception`` (the
production path: ``pupil_apriltags`` plus the ``cornerSubPix`` refinement of
A13) reads three looks, and ``autocal.mat_check`` judges them against an arm
check that stored the camera about 2 mm and 0.2° away: the check is ``ok`` and
the camera it reports lies within 0.5 mm of the contract camera, so the solve
moved from where it started to the truth on real detector corners.

Needs ``pupil_apriltags`` (``pupil_required.require_pupil``). CI installs it
and sets ``EDUBOTICS_REQUIRE_PUPIL=1`` (``ci.yml::python-tests``), so a lost
detector fails this file instead of skipping it.
"""

from __future__ import annotations

import math
import sys

import cv2
import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import chain, mat_check as mc, tags
from physical_ai_server.workflow.autocal.camera_model import unproject_normalised
from physical_ai_server.workflow.perception import Perception
import pupil_required
from pupil_required import require_pupil

KIND = 'v1'
TABLE_GREY, PAPER_GREY, INK_GREY = 95.0, 225.0, 30.0
FIXTURE_LABEL_M = 0.035        # the fixture tags' labels, their pockets' size (generate_calibration_target.POCKET_MM)
SUPERSAMPLE = 4                # samples per pixel and axis on the tags and their cards
BLUR_PX = 0.7                  # the lens: Gaussian sigma in pixels
NOISE_GREY = 2.0               # the sensor: per look
LOOKS = 3
# Where the stored arm check had the camera: turned and moved off the contract camera.
ARM_CHECK_TURN_RAD = np.radians([0.1, -0.15, 0.1])
ARM_CHECK_MOVE_M = np.array([0.0012, -0.0010, 0.0012])


# ── drawing the scene ────────────────────────────────────────────────────────
def _modules(tag_id) -> np.ndarray:
    """The 8 x 8 modules of a tag36h11 tag (black border included), row 0 on
    top; True = black (``generate_scene_mat._tag_bits``)."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    return dictionary.generateImageMarker(int(tag_id), 8, borderBits=1) < 128


def _hit(cam, u, v, z) -> np.ndarray:
    """Where the rays through the pixels ``(u, v)`` meet the plane ``z`` (W);
    NaN beyond the lens's valid field or behind the camera."""
    rays = unproject_normalised((u - cam.cx) / cam.f, (v - cam.cy) / (cam.a * cam.f), cam.k1, cam.k2) @ cam.R
    lens = cam.lens
    s = (z - lens[2]) / rays[..., 2]
    points = lens + s[..., None] * rays
    points[~(s > 0.0)] = np.nan
    return points


def _on_mat(kit, points):
    """``(x, y)`` of W points in the mat's printed layout (``mat_tags_f``'s frame)."""
    m = kit.mat
    dx = points[..., 0] - (kit.ax + m['dxy'][0])
    dy = points[..., 1] - m['dxy'][1]
    c, s = math.cos(m['dpsi']), math.sin(m['dpsi'])
    return (c * dx + s * dy) / m['s'][0], (-s * dx + c * dy) / m['s'][1]


def _on_fixture(kit, points):
    """``(x, y)`` of W points in F (``FIXTURE_TAGS_F``'s frame)."""
    return points[..., 0] - kit.ax, points[..., 1]


def _shade(x, y, tag_id, centre, size, card):
    """The grey of a tag on its white card at layout points ``(x, y)``, NaN off
    the card: module (r, c) covers x in ``cx + (3 - r … 4 - r)·m``, y in
    ``cy + (3 - c … 4 - c)·m`` (``generate_scene_mat.draw_tag``)."""
    cx, cy = centre
    m = size / 8.0
    grey = np.where((np.abs(x - cx) <= card / 2.0) & (np.abs(y - cy) <= card / 2.0), PAPER_GREY, np.nan)
    row = np.floor(4.0 - (x - cx) / m)
    col = np.floor(4.0 - (y - cy) / m)
    inside = (row >= 0) & (row < 8) & (col >= 0) & (col < 8)
    black = np.zeros(x.shape, dtype=bool)
    black[inside] = _modules(tag_id)[row[inside].astype(int), col[inside].astype(int)]
    return np.where(black, INK_GREY, grey)


def _draw(kit, cam) -> np.ndarray:
    """The noiseless grey image (float) of the mat on the table and the two
    fixture tags, as ``cam`` sees them."""
    width, height = int(sf.CAPTURE['width']), int(sf.CAPTURE['height'])
    v, u = np.mgrid[0:height, 0:width].astype(np.float64)
    x, y = _on_mat(kit, _hit(cam, u, v, sf.MAT_THICKNESS_M))
    (sx0, sy0), (sx1, sy1) = kit.spec['mat_sheet_f']
    notch = (x <= sf.MAT_NOTCH_F['x_max']) & (np.abs(y) <= sf.MAT_NOTCH_F['y_abs'])
    image = np.where((x >= sx0) & (x <= sx1) & (y >= sy0) & (y <= sy1) & ~notch, PAPER_GREY, TABLE_GREY)

    pieces = [(tid, xy, sf.MAT_TAG_SIZE_M, sf.MAT_CARD_M, sf.MAT_THICKNESS_M, _on_mat)
              for tid, xy in kit.spec['mat_tags_f'].items()]
    pieces += [(tid, xy, sf.FIXTURE_TAG_SIZE_M, FIXTURE_LABEL_M, sf.FIXTURE_TAG_Z_M, _on_fixture)
               for tid, xy in sf.FIXTURE_TAGS_F.items()]
    offsets = (np.arange(SUPERSAMPLE) + 0.5) / SUPERSAMPLE - 0.5
    for tid, centre, size, card, z, to_layout in pieces:
        # the card's pixel box, from its four corners projected
        h = card / 2.0
        layout = np.array([[centre[0] + a * h, centre[1] + b * h] for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
        if to_layout is _on_mat:
            m = kit.mat
            c, s = math.cos(m['dpsi']), math.sin(m['dpsi'])
            lx, ly = layout[:, 0] * m['s'][0], layout[:, 1] * m['s'][1]
            corners = np.stack([kit.ax + m['dxy'][0] + c * lx - s * ly, m['dxy'][1] + s * lx + c * ly], axis=1)
        else:
            corners = layout + (kit.ax, 0.0)
        uv = cam.project(np.hstack([corners, np.full((4, 1), z)]))
        u0, v0 = np.maximum(np.floor(uv.min(axis=0)).astype(int) - 2, 0)
        u1, v1 = np.minimum(np.ceil(uv.max(axis=0)).astype(int) + 2, (width - 1, height - 1))
        v, u = np.mgrid[v0:v1 + 1, u0:u1 + 1].astype(np.float64)
        under = image[v0:v1 + 1, u0:u1 + 1].copy()
        total = np.zeros(u.shape)
        for dv in offsets:
            for du in offsets:
                grey = _shade(*to_layout(kit, _hit(cam, u + du, v + dv, z)), tid, centre, size, card)
                total += np.where(np.isnan(grey), under, grey)
        image[v0:v1 + 1, u0:u1 + 1] = total / SUPERSAMPLE ** 2
    return cv2.GaussianBlur(image, (0, 0), BLUR_PX)


def _looks(kit, cam, seed=18) -> list:
    """``LOOKS`` 8-bit BGR frames of the scene, each with its own sensor noise."""
    image = _draw(kit, cam)
    out = []
    for k in range(LOOKS):
        noisy = image + np.random.default_rng([seed, k]).normal(0.0, NOISE_GREY, image.shape)
        out.append(cv2.cvtColor(np.clip(np.round(noisy), 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR))
    return out


def _frames(perception, images) -> list:
    """What the real detector reads in each look, as the mat check takes it."""
    frames = []
    for bgr in images:
        found = perception.detect(bgr, 'scene', 'apriltag')
        frames.append(tags.Frame(
            bgr.shape[1], bgr.shape[0], float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean()),
            {d.aruco_id: tags.TagObs(np.asarray(d.corners_px, dtype=np.float64), d.confidence * 100.0)
             for d in found}))
    return frames


# ── the test ─────────────────────────────────────────────────────────────────
def test_the_real_detector_reads_the_v1_mat_and_the_mat_check_finds_the_contract_camera():
    require_pupil()
    perception = Perception()
    assert perception.apriltag_available()

    kit = S.clean_kit(KIND, cam_lens_m=0.0, cam_rot_deg=0.0)
    cam = kit.cam
    R0, t0 = mc.contract_camera(kit.ax)
    np.testing.assert_allclose(cam.R, R0, rtol=0, atol=1e-12)                 # the contract camera ...
    np.testing.assert_allclose(cam.lens, np.add(sf.LENS_F, (kit.ax, 0.0, 0.0)), rtol=0, atol=1e-12)
    assert S.in_lens_band(cam.f, cam.a, cam.cx, cam.cy, cam.k1, cam.k2)        # ... with a lens of the band
    assert min(abs(cam.k1), abs(cam.k2)) > 0.01                               # (not the equidistant one)
    assert cam.p1 == cam.p2 == 0.0                                            # _hit draws without decentering

    arm_check_cam = cam.moved(dR=chain.rodrigues(ARM_CHECK_TURN_RAD), dL=ARM_CHECK_MOVE_M)
    cal = S.ideal_calibration(kit, arm_check_cam)
    assert np.linalg.norm(np.asarray(cal['lens_w']) - cam.lens) > 0.0015       # the solve starts ~2 mm off

    frames = _frames(perception, _looks(kit, cam))
    every = sorted([*kit.spec['mat_tags_f'], *tags.FIXTURE_IDS])
    assert [sorted(fr.tags) for fr in frames] == [every] * LOOKS

    r = mc.run(frames, S.FakeStore(cal), sf.ARMS[KIND], profile_id=S.PROFILE[KIND], profile=sf.FIXTURE_ID,
               camera_streaming=True)
    assert (r.ok, r.code, r.notes) == (True, 'ok', ())
    assert r.tags_seen == 14 and r.rms_px < mc.RMS_MAX_PX
    # the camera the check reports is the contract camera
    assert np.linalg.norm(r.T_cam_to_w[:3, 3] - cam.lens) < 0.0005
    assert mc.angle_between(r.T_cam_to_w[:3, :3], cam.R.T) < math.radians(0.1)


# ── the gate this file runs behind ───────────────────────────────────────────
def test_a_missing_detector_skips_and_fails_under_the_require_variable(monkeypatch):
    """``require_pupil`` with ``pupil_apriltags`` made unimportable: a skip,
    and under ``EDUBOTICS_REQUIRE_PUPIL=1`` (what CI sets) a failure."""
    monkeypatch.setitem(sys.modules, 'pupil_apriltags', None)
    assert pupil_required.REQUIRE_ENV == 'EDUBOTICS_REQUIRE_PUPIL'
    monkeypatch.delenv(pupil_required.REQUIRE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_pupil()
    monkeypatch.setenv(pupil_required.REQUIRE_ENV, '0')
    with pytest.raises(pytest.skip.Exception):
        require_pupil()
    monkeypatch.setenv(pupil_required.REQUIRE_ENV, '1')
    with pytest.raises(pytest.fail.Exception,
                       match='^EDUBOTICS_REQUIRE_PUPIL=1 but pupil_apriltags cannot be imported'):
        require_pupil()
