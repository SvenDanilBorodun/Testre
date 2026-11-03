"""D14 (plan v6.13 §5.5): AprilTag IDs 100–199 are reserved for the OMX camera
station on EVERY robot, station or not. ``register_object`` refuses them in
``code_rpc.validate_value`` and in ``RunSession._register_object`` with the new
sentence; no fixed catalog ID lies in the range; perception still reports such a
detection as ``tagNNN`` (the catalog never matches it)."""

from __future__ import annotations

import os
import tempfile

import pytest

from physical_ai_server.workflow import code_rpc, object_catalog, robot_api
from physical_ai_server.workflow.code_rpc import CodeRpcServer, validate_value

import test_code_rpc_register_object as reg

_SENTENCE = ('„tag_ids“ muss eine Liste mit 1 bis 16 Marker-IDs sein (0 bis 99 oder '
             '200 bis 586; 100 bis 199 sind für die Kamerastation reserviert).')


def _p():
    return robot_api.ApiParam('tag_ids', 'tagids', 'tag_ids')


@pytest.mark.parametrize('ids', [[100], [199], [20, 150], [99, 100]])
def test_validate_value_refuses_the_station_range(ids):
    assert validate_value(_p(), ids) == _SENTENCE


@pytest.mark.parametrize('ids', [[0], [99], [200], [586], [30, 31], [99, 200]])
def test_validate_value_keeps_the_rest_of_the_id_space(ids):
    assert validate_value(_p(), ids) is None


@pytest.fixture
def server():
    with tempfile.TemporaryDirectory(prefix='d14-') as d:
        srv = CodeRpcServer(os.path.join(d, 'rpc.sock'))
        yield srv
        srv.close()


@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower', 'v1_studio'])
def test_register_object_refuses_the_range_on_every_rig(server, pid):
    ctx = reg._ctx(pid)
    session = server.open_run(ctx)
    r = reg._register(session, 'marke', 'Marke', [150], 0.03, 0.015,
                      close=(0.05 if pid == 'v1_studio' else -0.4))
    assert r['ok'] is False
    assert 'für die Kamerastation reserviert' in r['e']
    assert 'marke' not in (getattr(ctx, 'student_objects', None) or {})
    server.close_run(session)


def test_the_run_session_check_is_defence_in_depth(server):
    ctx = reg._ctx('omx_full')
    session = server.open_run(ctx)
    r = session._register_object(['marke', 'Marke', [120], 0.03, 0.015, -0.4, None], 7)
    assert r['ok'] is False and 'reserviert' in r['e'] and r['e'].startswith('Greifobjekt: ')
    server.close_run(session)


def test_no_fixed_catalog_id_lies_in_the_station_range():
    assert object_catalog.STATION_TAG_ID_RANGE == (100, 199)
    for catalog in [object_catalog._FIXED_CATALOG] + [
            c for c, _ in object_catalog._CATALOG_BY_PROFILE.values()]:
        for recipe in catalog['types'].values():
            assert not any(100 <= t <= 199 for t in recipe['tag_ids'])


def test_the_range_constants_agree():
    assert (code_rpc.STATION_TAG_ID_MIN, code_rpc.STATION_TAG_ID_MAX) == \
        object_catalog.STATION_TAG_ID_RANGE
