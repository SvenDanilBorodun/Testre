#!/usr/bin/env python3
"""Render a sheet of AprilTags (tag36h11) for the EduBotics classroom kit.

Usage:
    python tools/generate_apriltags.py --out classroom_kit/apriltags.pdf

Print at 100% scale and cut along the dotted lines. Each tag is 30 mm so
the gripper-cam can resolve it from ~30 cm; the surrounding white quiet
zone (>= one square edge) is required by the AprilTag detector.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


TAG_SIZE_MM = 30.0
QUIET_ZONE_MM = 8.0
TAGS_PER_ROW = 4
TAGS_PER_COL = 5
TAG_FAMILY = cv2.aruco.DICT_APRILTAG_36H11
DPI = 300


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('classroom_kit/apriltags.pdf'))
    parser.add_argument('--start-id', type=int, default=0, help='First tag id (inclusive).')
    args = parser.parse_args()

    aruco_dict = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    px_per_mm = DPI / 25.4
    cell_mm = TAG_SIZE_MM + 2 * QUIET_ZONE_MM
    cell_px = int(round(cell_mm * px_per_mm))
    tag_px = int(round(TAG_SIZE_MM * px_per_mm))
    quiet_px = int(round(QUIET_ZONE_MM * px_per_mm))

    canvas_w = cell_px * TAGS_PER_ROW
    canvas_h = cell_px * TAGS_PER_COL
    canvas = np.full((canvas_h, canvas_w), 255, dtype=np.uint8)

    tag_id = args.start_id
    for row in range(TAGS_PER_COL):
        for col in range(TAGS_PER_ROW):
            tag_img = aruco_dict.generateImageMarker(tag_id, tag_px)
            x0 = col * cell_px + quiet_px
            y0 = row * cell_px + quiet_px
            canvas[y0:y0 + tag_px, x0:x0 + tag_px] = tag_img

            cv2.putText(
                canvas,
                f'#{tag_id}',
                (col * cell_px + 4, (row + 1) * cell_px - 4),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                0,
                1,
                cv2.LINE_AA,
            )
            tag_id += 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out.with_suffix('.png')), canvas)
    try:
        from PIL import Image
        Image.fromarray(canvas).convert('RGB').save(args.out, format='PDF', resolution=DPI)
    except ImportError:
        print('Pillow not installed — wrote PNG only.')

    print(f'Wrote {args.out} ({canvas_w} x {canvas_h} px @ {DPI} dpi); '
          f'tag ids {args.start_id}..{tag_id - 1}')


if __name__ == '__main__':
    main()
