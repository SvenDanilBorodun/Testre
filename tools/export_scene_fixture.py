#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Render the React copies of the fixture contract from the server module.

  physical_ai_tools/physical_ai_manager/src/components/Workshop/fixture/sceneFixture.json
      every constant of ``physical_ai_server/workflow/scene_fixture.py`` (CONTRACT_NAMES)
      and its ``contract_hash()``;
  physical_ai_tools/physical_ai_manager/src/components/Workshop/fixture/sceneFixture.cases.json
      the case table of the work-zone predicate (``in_zone`` / ``zone_az_hi``): every knot,
      0.1 deg inside and outside each cut segment, the sector edges, the radii — the table the
      JS twin is held equal by.

Both files are fenced against a fresh render (``tests/test_scene_fixture_lockstep.py`` runs
``--check``). The check compares VALUES, not bytes: the same structure and keys, every string,
boolean and integer exactly, every float within ``FLOAT_REL_TOL`` / ``FLOAT_ABS_TOL``. The case
table's coordinates come from ``math.cos`` / ``math.sin``, whose last digit differs between
platforms' maths libraries (one unit in the last place, macOS against glibc, seen on the CI
runner), so a byte comparison failed on Linux for a file rendered on a Mac; any change that
matters (a knot, a radius, an ``in_zone`` verdict) is many orders above the tolerance.

Usage: python3 tools/export_scene_fixture.py           write both files
       python3 tools/export_scene_fixture.py --check   exit 1 when either file differs in value
Deterministic: the same module on the same platform gives the same bytes."""

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE_DIR = (REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components' / 'Workshop'
               / 'fixture')
FIXTURE_JSON = FIXTURE_DIR / 'sceneFixture.json'
CASES_JSON = FIXTURE_DIR / 'sceneFixture.cases.json'

sys.path.insert(0, str(REPO / 'physical_ai_tools' / 'physical_ai_server'))
from physical_ai_server.workflow import scene_fixture as sf  # noqa: E402

_STEP_DEG = 0.1          # the case table's distance on either side of an edge
_RADIUS_STEP_M = 1e-4    # the same, across the zone's radii


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n'


def render_fixture() -> str:
    return _dump({
        'contract_hash': sf.contract_hash(),
        'constants': sf.contract_values(),
        'source': 'physical_ai_tools/physical_ai_server/physical_ai_server/workflow/scene_fixture.py '
                  '(rendered by tools/export_scene_fixture.py; never edit by hand)',
    })


def _case(spec, what, r, az):
    x = r * math.cos(math.radians(az))
    y = r * math.sin(math.radians(az))
    return {'what': what, 'r_m': r, 'az_deg': az, 'x_f': x, 'y_f': y, 'in_zone': sf.in_zone(spec, x, y)}


def _zone_cases(spec):
    knots = spec['zone_cut_knots']
    lo = spec['zone_sector_deg'][0]
    r_in, r_out = spec['zone_inner_m'], spec['zone_outer_m']
    rows = []
    for k, (r, az) in enumerate(knots):
        rows.append(_case(spec, f'knot {k}', r, az))
        rows.append(_case(spec, f'knot {k} -0.1 deg', r, az - _STEP_DEG))
        rows.append(_case(spec, f'knot {k} +0.1 deg', r, az + _STEP_DEG))
    for k, ((r0, _a0), (r1, _a1)) in enumerate(zip(knots, knots[1:])):
        r = (r0 + r1) / 2.0
        edge = sf.zone_az_hi(spec, r)
        rows.append(_case(spec, f'segment {k} -0.1 deg', r, edge - _STEP_DEG))
        rows.append(_case(spec, f'segment {k} +0.1 deg', r, edge + _STEP_DEG))
    r_mid = (r_in + r_out) / 2.0
    rows.append(_case(spec, 'sector low -0.1 deg', r_mid, lo - _STEP_DEG))
    rows.append(_case(spec, 'sector low +0.1 deg', r_mid, lo + _STEP_DEG))
    if knots[-1][0] < r_out:                  # beyond the last knot the sector edge is the zone's edge
        r = (knots[-1][0] + r_out) / 2.0
        hi = spec['zone_sector_deg'][1]
        rows.append(_case(spec, 'sector high -0.1 deg', r, hi - _STEP_DEG))
        rows.append(_case(spec, 'sector high +0.1 deg', r, hi + _STEP_DEG))
    rows.append(_case(spec, 'inner radius', r_in, 0.0))
    rows.append(_case(spec, 'inside the inner radius', r_in - _RADIUS_STEP_M, 0.0))
    rows.append(_case(spec, 'outer radius', r_out, 0.0))
    rows.append(_case(spec, 'outside the outer radius', r_out + _RADIUS_STEP_M, 0.0))
    return rows


def _edge_cases(spec):
    knots = spec['zone_cut_knots']
    radii = [r for r, _a in knots]
    radii += [(r0 + r1) / 2.0 for (r0, _a0), (r1, _a1) in zip(knots, knots[1:])]
    radii += [spec['zone_outer_m'], knots[-1][0] + 0.001]
    return [{'r_m': r, 'az_hi_deg': sf.zone_az_hi(spec, r)} for r in sorted(set(radii))]


def render_cases() -> str:
    return _dump({
        'in_zone': {key: _zone_cases(spec) for key, spec in sf.ARMS.items()},
        'zone_az_hi': {key: _edge_cases(spec) for key, spec in sf.ARMS.items()},
        'source': 'scene_fixture.in_zone / zone_az_hi (rendered by tools/export_scene_fixture.py; never edit by hand)',
    })


FLOAT_REL_TOL = 1e-12
FLOAT_ABS_TOL = 1e-15


def same_values(a, b) -> bool:
    """True when two JSON documents hold the same values (see the module docstring): equal
    structure and keys, exact strings, booleans, integers and nulls, floats within the tolerance."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, float) or isinstance(b, float):
        return (isinstance(a, (int, float)) and isinstance(b, (int, float))
                and math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_values(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_values(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def main(argv) -> int:
    check = '--check' in argv
    stale = []
    for path, text in ((FIXTURE_JSON, render_fixture()), (CASES_JSON, render_cases())):
        if check:
            if not path.exists() or not same_values(json.loads(path.read_text(encoding='utf-8')), json.loads(text)):
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
    for path in stale:
        print(f'stale: {path.relative_to(REPO)}', file=sys.stderr)
    return 1 if stale else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
