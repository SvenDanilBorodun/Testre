#!/usr/bin/env python3
"""Render the EduBotics ChArUco calibration board to a PDF.

Default specs match `physical_ai_server/workflow/calibration_manager.py`:
    7x5 squares, 30 mm square edge, 22 mm marker edge, DICT_5X5_250.

Usage:
    python tools/generate_charuco.py --out classroom_kit/charuco.pdf

Print the PDF at 100% scale (no "fit to page"!) and mount on a foam-board
or thick cardboard. Paper warps and silently corrupts intrinsics.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


SQUARES_X = 7
SQUARES_Y = 5
SQUARE_LENGTH_M = 0.030
MARKER_LENGTH_M = 0.022
ARUCO_DICT = cv2.aruco.DICT_5X5_250
DPI = 300


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--out',
        type=Path,
        default=Path('classroom_kit/charuco.pdf'),
        help='Output PDF path',
    )
    parser.add_argument(
        '--margin-mm',
        type=float,
        default=10.0,
        help='White margin around the board, mm',
    )
    args = parser.parse_args()

    aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
    board = cv2.aruco.CharucoBoard(
        (SQUARES_X, SQUARES_Y),
        SQUARE_LENGTH_M,
        MARKER_LENGTH_M,
        aruco_dict,
    )

    board_w_m = SQUARES_X * SQUARE_LENGTH_M
    board_h_m = SQUARES_Y * SQUARE_LENGTH_M
    margin_m = args.margin_mm / 1000.0

    px_per_m = DPI / 0.0254
    img_w = int(round((board_w_m + 2 * margin_m) * px_per_m))
    img_h = int(round((board_h_m + 2 * margin_m) * px_per_m))
    board_px_w = int(round(board_w_m * px_per_m))
    board_px_h = int(round(board_h_m * px_per_m))

    canvas = np.full((img_h, img_w), 255, dtype=np.uint8)
    margin_px = int(round(margin_m * px_per_m))
    board_img = board.generateImage((board_px_w, board_px_h))
    canvas[margin_px:margin_px + board_px_h, margin_px:margin_px + board_px_w] = board_img

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out.with_suffix('.png')), canvas)

    # OpenCV doesn't write PDF directly; use Pillow for the PDF wrapping.
    try:
        from PIL import Image
        img = Image.fromarray(canvas).convert('RGB')
        img.save(args.out, format='PDF', resolution=DPI)
    except ImportError:
        print('Pillow not installed — wrote PNG only.')

    print(f'Wrote {args.out} ({img_w} x {img_h} px @ {DPI} dpi)')


if __name__ == '__main__':
    main()
