#!/usr/bin/env python3
"""Render the AprilTag sheet (tag36h11) for the EduBotics classroom kit.

CATALOG-DRIVEN BY DEFAULT: with no arguments the sheet contains exactly the
tag ids of the fleet-wide fixed object set
(physical_ai_server/workflow/object_catalog.py::_FIXED_CATALOG), each cell
labelled ``#<id> <type>``, at the catalog's physical tag size
(EDUBOTICS_TAG_SIZE_M, default 24 mm) — so "print the sheet" can never drift
from what the detector and the grasp maths expect. Adding an object to
_FIXED_CATALOG and re-running this tool yields the matching sheet.

Manual mode (spare/extra tags): pass ``--start-id`` and ``--count`` to render
an arbitrary id range instead; ``--size-mm`` overrides the tag size in both
modes.

Usage:
    python tools/generate_apriltags.py                       # catalog sheet
    python tools/generate_apriltags.py --start-id 40 --count 10 --size-mm 24

Print at 100% scale ("Tatsaechliche Groesse", never fit-to-page) and measure
one tag with a ruler. Cut along the cells KEEPING the white quiet zone around
each black square (the detector requires it). Glue each tag FLAT on the
object's TOP face, same orientation on every copy of a type (the wrist roll
tracks the tag — see the object_catalog.py tag-mounting convention).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np


QUIET_ZONE_MM = 8.0
TAGS_PER_ROW = 4
TAG_FAMILY = cv2.aruco.DICT_APRILTAG_36H11
DPI = 300

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER_PKG_ROOT = _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server'


def _catalog_entries() -> tuple[list[tuple[int, str]], float]:
    """(tag_id, ascii_type_key) pairs + physical tag size (m) from the fixed,
    fleet-wide object set. object_catalog is stdlib-pure, so importing it here
    needs no ROS/container. Labels use the ASCII type key (enforced by the
    catalog's ``^[A-Za-z0-9_]+$`` key regex) because cv2's Hershey fonts
    cannot render the German umlauts of ``label_de``."""
    sys.path.insert(0, str(_SERVER_PKG_ROOT))
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    catalog = fixed_catalog()
    entries: list[tuple[int, str]] = []
    for type_name in catalog.type_names():
        for tag_id in catalog.tag_ids_for_type(type_name):
            entries.append((int(tag_id), type_name))
    return entries, float(catalog.tag_size_m)


def _render_sheet(entries: list[tuple[int, str]], tag_size_mm: float,
                  out: Path) -> None:
    aruco_dict = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    px_per_mm = DPI / 25.4
    cell_mm = tag_size_mm + 2 * QUIET_ZONE_MM
    cell_px = int(round(cell_mm * px_per_mm))
    tag_px = int(round(tag_size_mm * px_per_mm))
    quiet_px = int(round(QUIET_ZONE_MM * px_per_mm))

    cols = min(TAGS_PER_ROW, len(entries))
    rows = math.ceil(len(entries) / TAGS_PER_ROW)
    canvas = np.full((cell_px * rows, cell_px * cols), 255, dtype=np.uint8)

    for i, (tag_id, label) in enumerate(entries):
        row, col = divmod(i, TAGS_PER_ROW)
        tag_img = aruco_dict.generateImageMarker(tag_id, tag_px)
        x0 = col * cell_px + quiet_px
        y0 = row * cell_px + quiet_px
        canvas[y0:y0 + tag_px, x0:x0 + tag_px] = tag_img
        cv2.putText(
            canvas,
            f'#{tag_id} {label}'.strip(),
            (col * cell_px + 4, (row + 1) * cell_px - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            0,
            1,
            cv2.LINE_AA,
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out.with_suffix('.png')), canvas)
    try:
        from PIL import Image
        Image.fromarray(canvas).convert('RGB').save(out, format='PDF', resolution=DPI)
    except ImportError:
        print('Pillow not installed — wrote PNG only.')

    ids = ', '.join(f'#{tid}' for tid, _ in entries)
    print(f'Wrote {out} ({canvas.shape[1]} x {canvas.shape[0]} px @ {DPI} dpi); '
          f'{len(entries)} tags at {tag_size_mm:g} mm: {ids}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('classroom_kit/apriltags.pdf'))
    parser.add_argument(
        '--start-id', type=int, default=None,
        help='Manual mode: first tag id (inclusive). Requires --count.')
    parser.add_argument(
        '--count', type=int, default=None,
        help='Manual mode: number of consecutive ids from --start-id.')
    parser.add_argument(
        '--size-mm', type=float, default=None,
        help='Tag black-square edge in mm (default: the catalog tag size, '
             'i.e. EDUBOTICS_TAG_SIZE_M * 1000).')
    args = parser.parse_args()

    if (args.start_id is None) != (args.count is None):
        parser.error('--start-id and --count must be given together.')

    if args.start_id is not None:
        if args.count <= 0:
            parser.error('--count must be positive.')
        entries = [(args.start_id + i, '') for i in range(args.count)]
        tag_size_mm = args.size_mm
        if tag_size_mm is None:
            # Even in manual mode the physical size should match the fleet
            # expectation unless overridden explicitly.
            _, tag_size_m = _catalog_entries()
            tag_size_mm = tag_size_m * 1000.0
    else:
        entries, tag_size_m = _catalog_entries()
        tag_size_mm = args.size_mm if args.size_mm is not None else tag_size_m * 1000.0

    if tag_size_mm <= 0:
        parser.error('--size-mm must be positive.')
    _render_sheet(entries, tag_size_mm, args.out)


if __name__ == '__main__':
    main()
