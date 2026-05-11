#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Three-mode perception: LAB color blobs, YOLOX-tiny COCO objects, AprilTags.

Mode selection is per-block in the workflow interpreter:

- ``color``: LAB-space distance from the per-classroom color profile
  (per-channel |x-μ|/σ within a threshold), contours, centroid + bbox,
  label = the German colour name. Audit F59 fixed the misleading "HSV"
  reference in this docstring — the implementation has been LAB-space
  since the color-profile rewrite.
- ``yolo+color``: YOLOX-tiny ONNX inference at 640x640 letterbox; if a
  ``coco_class`` filter is supplied, only that class is returned; if a
  ``color`` filter is also supplied, a 10x10 px HSV patch around the bbox
  centre is sampled and the detection is kept only when the patch falls
  inside the colour's HSV range. This is what powers blocks like
  "alle Bananen" or "alle roten Äpfel".
- ``apriltag``: ``pupil_apriltags`` (BSD), tag36h11 family, with the
  optional ``aruco_id`` filter.

Both the ONNX session and the AprilTag detector are constructed eagerly
in ``__init__`` and any failure raises ``RuntimeError`` with a German
message. Earlier versions returned ``False`` from internal ``_ensure_*``
helpers which caused detection blocks to silently return ``[]`` when
the YOLOX ONNX wasn't baked into the image — see commit history for
the audit that removed that fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from physical_ai_server.workflow.coco_classes import COCO_CLASSES, ID_TO_LABEL

# Detector dispatch — Phase-3 (2026-05). The default is the
# Apache-2.0 YOLOX-tiny ONNX baked into the image. ``EDUBOTICS_DETECTOR``
# is reserved for the future D-FINE-N swap; the postprocessing branches
# below currently assume the YOLOX head shape, so setting
# ``EDUBOTICS_DETECTOR=dfine-n`` today would index out-of-bounds on the
# D-FINE-N output tensor. Until the decode head is wired (tracked in
# ``docs/ROBOTER_STUDIO_DEFERRED.md`` §7.2 and ``tools/dfine_finetune.md``)
# we treat any non-default value as a configuration error and raise at
# import time so the operator sees the issue immediately rather than at
# the first inference tick.
# Audit round-3 §J/§K.
DETECTOR_KIND = os.environ.get('EDUBOTICS_DETECTOR', 'yolox-tiny').strip().lower()
YOLOX_ONNX_PATH = Path(os.environ.get('EDUBOTICS_YOLOX_ONNX', '/opt/edubotics/yolox_tiny.onnx'))
DFINE_ONNX_PATH = Path(os.environ.get('EDUBOTICS_DFINE_ONNX', '/opt/edubotics/dfine_n.onnx'))

if DETECTOR_KIND not in ('yolox-tiny', 'yolox'):
    raise RuntimeError(
        'EDUBOTICS_DETECTOR='
        f'{DETECTOR_KIND!r} ist in dieser Version nicht unterstützt. '
        'Nur "yolox-tiny" funktioniert; die D-FINE-N-Integration ist '
        'noch in Arbeit (siehe ROBOTER_STUDIO_DEFERRED.md §7.2).'
    )

# Which ONNX file we actually load — currently always YOLOX-tiny.
_ACTIVE_ONNX_PATH = YOLOX_ONNX_PATH

YOLOX_INPUT_SIZE = (640, 640)
YOLOX_CONFIDENCE_THRESHOLD = 0.30
YOLOX_NMS_IOU_THRESHOLD = 0.45

LAB_MIN_BLOB_AREA_PX = 100
COLOR_PATCH_SIZE_PX = 10


@dataclass
class Detection:
    centroid_px: tuple[int, int]
    bbox_px: tuple[int, int, int, int]   # x, y, w, h
    confidence: float
    label: str
    aruco_id: int | None = None
    world_xyz_m: tuple[float, float, float] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Perception:
    """Eager-initialised wrapper over HSV, YOLOX, and AprilTag backends.

    Construction raises ``RuntimeError`` (German message) if either
    backend isn't available. The Workshop UX is built around the
    promise that perception either works or fails-loud, never silently
    drops detections.
    """

    def __init__(self) -> None:
        self._yolox_session = None
        self._yolox_input_name: str | None = None
        self._apriltag_detector = None
        # LAB-space colour clusters keyed by colour name. Each value is
        # ``{'center': np.ndarray(3), 'std': np.ndarray(3), 'threshold': float}``
        # — see ``ColorProfileManager.lab_profile``.
        self._color_profile: dict[str, dict] = {}
        self._init_yolox()
        self._init_apriltag()

    def set_color_profile(self, profile: dict[str, dict]) -> None:
        """Inject LAB clusters from ``ColorProfileManager.lab_profile`` outputs."""
        self._color_profile = profile

    def detect(
        self,
        bgr: np.ndarray,
        camera: str,
        mode: str,
        color: str | None = None,
        coco_class: str | None = None,
        aruco_id: int | None = None,
    ) -> list[Detection]:
        if mode == 'color':
            return self._detect_color(bgr, color)
        if mode == 'yolo+color':
            return self._detect_yolo(bgr, coco_class=coco_class, color_filter=color)
        if mode == 'apriltag':
            return self._detect_apriltag(bgr, aruco_id=aruco_id)
        return []

    # ------------------------------------------------------------------
    # LAB colour matching
    # ------------------------------------------------------------------
    def _detect_color(self, bgr: np.ndarray, color: str | None) -> list[Detection]:
        if color is None or color not in self._color_profile:
            return []
        profile = self._color_profile[color]
        center = profile['center']      # np.ndarray shape (3,)
        std = profile['std']            # np.ndarray shape (3,)
        threshold = float(profile.get('threshold', 3.0))

        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        # Per-channel |x - μ| / σ; pixel matches when ALL three channels
        # are within the threshold. The std was floored to 1.0 in the
        # capture step so this never divides by zero.
        diff = np.abs(lab - center.reshape(1, 1, 3)) / std.reshape(1, 1, 3)
        match = np.all(diff <= threshold, axis=2)
        mask = (match.astype(np.uint8)) * 255
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        detections: list[Detection] = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < LAB_MIN_BLOB_AREA_PX:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            cx, cy = x + w // 2, y + h // 2
            detections.append(Detection(
                centroid_px=(cx, cy),
                bbox_px=(x, y, w, h),
                confidence=min(1.0, float(area) / 5000.0),
                label=color,
            ))
        return detections

    # ------------------------------------------------------------------
    # YOLOX
    # ------------------------------------------------------------------
    def _init_yolox(self) -> None:
        """Load the active detector ONNX (YOLOX-tiny by default;
        D-FINE-N when ``EDUBOTICS_DETECTOR=dfine-n``). Fails loudly if
        the file is missing — the perception module promises
        fail-loud and audited callers (Workshop) depend on that.
        D-FINE-N postprocessing is NMS-free; the dispatch in
        ``_detect_yolo`` honours both head shapes.
        """
        path = _ACTIVE_ONNX_PATH
        if not path.exists():
            raise RuntimeError(
                f'Erkennungs-Modell fehlt unter {path} — Image neu '
                f'bauen oder EDUBOTICS_DETECTOR auf einen vorhandenen '
                f'Pfad zeigen.'
            )
        try:
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError(
                'onnxruntime ist nicht installiert — Image neu bauen.'
            ) from e
        try:
            providers = ['CPUExecutionProvider']
            self._yolox_session = ort.InferenceSession(
                str(path), providers=providers,
            )
            self._yolox_input_name = self._yolox_session.get_inputs()[0].name
        except Exception as e:
            raise RuntimeError(
                f'Erkennungs-Modell konnte nicht geladen werden: {e}'
            ) from e

    @staticmethod
    def _letterbox(bgr: np.ndarray, target_size: tuple[int, int]) -> tuple[np.ndarray, float, tuple[int, int]]:
        h, w = bgr.shape[:2]
        target_w, target_h = target_size
        ratio = min(target_w / w, target_h / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        resized = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        padded = np.full((target_h, target_w, 3), 114, dtype=np.uint8)
        pad_x = (target_w - new_w) // 2
        pad_y = (target_h - new_h) // 2
        padded[pad_y:pad_y + new_h, pad_x:pad_x + new_w] = resized
        return padded, ratio, (pad_x, pad_y)

    @staticmethod
    def _yolox_demo_postprocess(
        outputs: np.ndarray,
        img_size: tuple[int, int],
        strides: tuple[int, ...] = (8, 16, 32),
    ) -> np.ndarray:
        """Apply YOLOX grid + stride decoding.

        Megvii's ``yolox_tiny.onnx`` (pinned by SHA in the Dockerfile)
        emits ``(N, 85)`` predictions where the ``(cx, cy, w, h)`` are
        **grid-relative offsets per feature level** at strides
        ``{8, 16, 32}`` (audit F2). Without this decode, predictions
        live in [0..80) px and NMS keeps nothing → silent empty.

        Mirrors the upstream ``demo_postprocess`` in
        ``YOLOX/yolox/utils/demo_utils.py``.
        """
        grids = []
        expanded_strides = []
        hsizes = [img_size[0] // s for s in strides]
        wsizes = [img_size[1] // s for s in strides]
        for hsize, wsize, stride in zip(hsizes, wsizes, strides):
            xv, yv = np.meshgrid(np.arange(wsize), np.arange(hsize))
            grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
            grids.append(grid)
            shape = grid.shape[:2]
            expanded_strides.append(np.full((*shape, 1), stride))
        grids = np.concatenate(grids, 1)
        expanded_strides = np.concatenate(expanded_strides, 1)
        outputs[..., :2] = (outputs[..., :2] + grids) * expanded_strides
        outputs[..., 2:4] = np.exp(outputs[..., 2:4]) * expanded_strides
        return outputs

    def _detect_yolo(
        self,
        bgr: np.ndarray,
        coco_class: str | None,
        color_filter: str | None,
    ) -> list[Detection]:
        # Session is loaded in __init__; if we got here it's ready.
        padded, ratio, (pad_x, pad_y) = self._letterbox(bgr, YOLOX_INPUT_SIZE)
        # YOLOX expects BGR uint8 -> CHW float32 (no normalisation).
        tensor = padded.transpose(2, 0, 1).astype(np.float32)
        tensor = np.expand_dims(tensor, 0)

        outputs = self._yolox_session.run(None, {self._yolox_input_name: tensor})
        # Audit F2: outputs[0] is grid-relative offsets per feature
        # level. Decode through demo_postprocess before slicing.
        # img_size is (H, W); YOLOX_INPUT_SIZE here is (W, H) but they
        # match so the order is irrelevant.
        predictions = self._yolox_demo_postprocess(
            outputs[0].copy(),
            img_size=(YOLOX_INPUT_SIZE[1], YOLOX_INPUT_SIZE[0]),
        )[0]
        if predictions.size == 0:
            return []

        boxes_xywh = predictions[:, :4]
        objectness = predictions[:, 4]
        class_scores = predictions[:, 5:]
        scores = objectness[:, None] * class_scores
        class_ids = scores.argmax(axis=1)
        max_scores = scores.max(axis=1)

        keep = max_scores > YOLOX_CONFIDENCE_THRESHOLD
        if not np.any(keep):
            return []

        boxes_xywh = boxes_xywh[keep]
        class_ids = class_ids[keep]
        confidences = max_scores[keep]

        # Boxes are now at full 640×640 stride in padded image
        # coordinates. Convert to xyxy for NMS / cropping.
        cx = boxes_xywh[:, 0]
        cy = boxes_xywh[:, 1]
        w = boxes_xywh[:, 2]
        h = boxes_xywh[:, 3]
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2

        # Apply NMS class-by-class.
        boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)
        keep_indices = self._class_nms(boxes_xyxy, confidences, class_ids)
        if not keep_indices:
            return []

        wanted_id = COCO_CLASSES[coco_class] if coco_class in COCO_CLASSES else None

        detections: list[Detection] = []
        for i in keep_indices:
            cid = int(class_ids[i])
            if cid not in ID_TO_LABEL:
                # Filter to the curated 16-class subset.
                continue
            if wanted_id is not None and cid != wanted_id:
                continue

            # Map back to original image space.
            ox1 = (boxes_xyxy[i, 0] - pad_x) / ratio
            oy1 = (boxes_xyxy[i, 1] - pad_y) / ratio
            ox2 = (boxes_xyxy[i, 2] - pad_x) / ratio
            oy2 = (boxes_xyxy[i, 3] - pad_y) / ratio
            ox1 = max(0, int(round(ox1)))
            oy1 = max(0, int(round(oy1)))
            ox2 = min(bgr.shape[1] - 1, int(round(ox2)))
            oy2 = min(bgr.shape[0] - 1, int(round(oy2)))
            bw = max(1, ox2 - ox1)
            bh = max(1, oy2 - oy1)
            cx_o = ox1 + bw // 2
            cy_o = oy1 + bh // 2

            label = ID_TO_LABEL[cid]
            if color_filter is not None:
                if not self._patch_matches_color(bgr, cx_o, cy_o, color_filter):
                    continue
                label = f'{label}_{color_filter}'

            detections.append(Detection(
                centroid_px=(cx_o, cy_o),
                bbox_px=(ox1, oy1, bw, bh),
                confidence=float(confidences[i]),
                label=label,
            ))
        return detections

    @staticmethod
    def _class_nms(
        boxes: np.ndarray,
        scores: np.ndarray,
        class_ids: np.ndarray,
    ) -> list[int]:
        """Per-class non-maximum suppression. Returns indices into the
        original arrays.

        Audit F3: ``cv2.dnn.NMSBoxes`` expects ``[x, y, w, h]`` boxes;
        we receive ``[x1, y1, x2, y2]`` from the YOLOX decode. Convert
        before passing or IoU collapses (near-origin boxes look tiny,
        near-bottom-right boxes look huge → wrong dedupe).
        """
        keep_total: list[int] = []
        for cid in np.unique(class_ids):
            mask = class_ids == cid
            sub_boxes = boxes[mask]
            sub_scores = scores[mask]
            if sub_boxes.size == 0:
                continue
            # xyxy -> xywh for NMSBoxes
            x1 = sub_boxes[:, 0]
            y1 = sub_boxes[:, 1]
            x2 = sub_boxes[:, 2]
            y2 = sub_boxes[:, 3]
            sub_boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1)
            indices = cv2.dnn.NMSBoxes(
                bboxes=sub_boxes_xywh.tolist(),
                scores=sub_scores.tolist(),
                score_threshold=YOLOX_CONFIDENCE_THRESHOLD,
                nms_threshold=YOLOX_NMS_IOU_THRESHOLD,
            )
            if indices is None:
                continue
            indices = np.array(indices).reshape(-1)
            if indices.size == 0:
                continue
            original_indices = np.where(mask)[0]
            keep_total.extend(original_indices[indices].tolist())
        return keep_total

    def _patch_matches_color(self, bgr: np.ndarray, cx: int, cy: int, color: str) -> bool:
        if color not in self._color_profile:
            return False
        profile = self._color_profile[color]
        center = profile['center']
        std = profile['std']
        threshold = float(profile.get('threshold', 3.0))
        half = COLOR_PATCH_SIZE_PX // 2
        x0 = max(0, cx - half)
        y0 = max(0, cy - half)
        x1 = min(bgr.shape[1], cx + half)
        y1 = min(bgr.shape[0], cy + half)
        patch = bgr[y0:y1, x0:x1]
        if patch.size == 0:
            return False
        lab = cv2.cvtColor(patch, cv2.COLOR_BGR2LAB).astype(np.float32)
        diff = np.abs(lab - center.reshape(1, 1, 3)) / std.reshape(1, 1, 3)
        match = np.all(diff <= threshold, axis=2)
        # >= 25% of the patch matches the colour cluster.
        return float(match.mean()) > 0.25

    # ------------------------------------------------------------------
    # AprilTag
    # ------------------------------------------------------------------
    def _init_apriltag(self) -> None:
        """Construct the pupil_apriltags detector. Fails loudly on
        missing dependency — no fallback."""
        try:
            from pupil_apriltags import Detector
        except ImportError as e:
            raise RuntimeError(
                'pupil_apriltags ist nicht installiert — Image neu bauen.'
            ) from e
        try:
            self._apriltag_detector = Detector(
                families='tag36h11',
                nthreads=2,
                quad_decimate=1.0,
                quad_sigma=0.0,
                refine_edges=True,
                decode_sharpening=0.25,
                debug=False,
            )
        except Exception as e:
            raise RuntimeError(
                f'AprilTag-Detektor konnte nicht initialisiert werden: {e}'
            ) from e

    def _detect_apriltag(self, bgr: np.ndarray, aruco_id: int | None) -> list[Detection]:
        # Detector is constructed in __init__; if we got here it's ready.
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        results = self._apriltag_detector.detect(gray)
        detections: list[Detection] = []
        for r in results:
            if aruco_id is not None and r.tag_id != aruco_id:
                continue
            cx, cy = int(r.center[0]), int(r.center[1])
            corners = r.corners.astype(int)
            xs, ys = corners[:, 0], corners[:, 1]
            x, y = int(xs.min()), int(ys.min())
            w, h = int(xs.max() - xs.min()), int(ys.max() - ys.min())
            detections.append(Detection(
                centroid_px=(cx, cy),
                bbox_px=(x, y, w, h),
                confidence=float(r.decision_margin) / 100.0,
                label=f'tag{r.tag_id}',
                aruco_id=int(r.tag_id),
            ))
        return detections
