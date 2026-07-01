"""Batch 2b — structural contract of the four new manual-control .srv files.

Deps-free: reads the .srv sources + CMakeLists directly (no ROS / rosidl). Guards
the "exactly one `---`" rule and the "listed in CMakeLists" rule the human
Rule §4 check asks for, plus the exact request/response field names the
frontend + cloud agents must match.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SRV_DIR = (
    Path(__file__).resolve().parents[2]
    / 'physical_ai_interfaces' / 'srv'
)
_CMAKELISTS = (
    Path(__file__).resolve().parents[2]
    / 'physical_ai_interfaces' / 'CMakeLists.txt'
)

_NEW_SRVS = [
    'WorkshopJog',
    'WorkshopHandGuide',
    'WorkshopRecordControl',
    'WorkshopReplay',
]

# (request field names, response field names) each srv MUST expose — the fixed
# contract the frontend/cloud agents match.
_EXPECTED = {
    'WorkshopJog': (
        ['mode', 'index', 'delta', 'target_x', 'target_y', 'target_z',
         'duration_s'],
        ['success', 'joints', 'world_x', 'world_y', 'world_z', 'message'],
    ),
    'WorkshopHandGuide': (
        ['enabled'],
        ['success', 'message'],
    ),
    'WorkshopRecordControl': (
        ['action'],
        ['success', 'sample_count', 'duration_s', 'points_json', 'message'],
    ),
    'WorkshopReplay': (
        ['name', 'points_json', 'speed'],
        ['success', 'message'],
    ),
}


def _field_names(section: str) -> list[str]:
    names = []
    for line in section.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            names.append(parts[1])
    return names


@pytest.mark.parametrize('srv', _NEW_SRVS)
def test_srv_exists_and_single_separator(srv):
    path = _SRV_DIR / f'{srv}.srv'
    assert path.exists(), f'{srv}.srv missing'
    text = path.read_text(encoding='utf-8')
    # Exactly one request/response separator line.
    seps = [ln for ln in text.splitlines() if ln.strip() == '---']
    assert len(seps) == 1, f'{srv}.srv must have exactly one --- (got {len(seps)})'


@pytest.mark.parametrize('srv', _NEW_SRVS)
def test_srv_fields_match_contract(srv):
    text = (_SRV_DIR / f'{srv}.srv').read_text(encoding='utf-8')
    req_text, resp_text = text.split('---', 1)
    req_fields = _field_names(req_text)
    resp_fields = _field_names(resp_text)
    exp_req, exp_resp = _EXPECTED[srv]
    assert req_fields == exp_req, f'{srv} request fields {req_fields} != {exp_req}'
    assert resp_fields == exp_resp, f'{srv} response fields {resp_fields} != {exp_resp}'


@pytest.mark.parametrize('srv', _NEW_SRVS)
def test_srv_listed_in_cmakelists(srv):
    cmake = _CMAKELISTS.read_text(encoding='utf-8')
    assert f'"srv/{srv}.srv"' in cmake, f'{srv}.srv not registered in CMakeLists.txt'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
