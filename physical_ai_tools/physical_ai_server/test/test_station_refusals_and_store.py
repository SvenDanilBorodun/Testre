"""Station answers and the station YAML (fix round 1, F14–F17).

F14 (§8.2): ``resolve_camera`` on an arm that is not idle answers BUSY with the
sentence of what holds it; FAILED stays the re-lock's.
F15 (§6.5): the YAML's schema and image size must be exact integers.
F16 (§6.5): ``restore_previous`` is refused unless the session is ACTIVE.
F17 (§6.5): ``store.write_station`` validates the new content before it
replaces anything; an invalid one changes neither file.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')

import station_fixture as F  # noqa: E402
from physical_ai_server.station import store  # noqa: E402
from physical_ai_server.station import texts_de as T  # noqa: E402
from test_station_service import (  # noqa: E402
    LAY, LAYOUTS, _Resp, _activate, _req, _session, _svc)


@pytest.fixture(scope='module')
def fields():
    return F.station_fields(LAY)


# ── F14 ───────────────────────────────────────────────────────────────────────
def test_resolve_camera_on_an_arm_a_mode_holds_is_busy_with_that_sentence(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)
    node.on_manual = True
    r = svc.check_command(_req(command='resolve_camera', client_id='A', session_id=sid), _Resp())
    assert r.result == 'BUSY' and r.success is False
    assert r.message == node._assert_no_other_active('calibration')[1]
    assert r.message and r.job_id == 0 and not svc.worker.station_job_pending()


def test_resolve_camera_while_a_station_job_is_pending_is_busy(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    sid = _activate(svc, node)
    first = svc.check_command(_req(command='resolve_camera', client_id='A', session_id=sid),
                              _Resp())
    assert first.result == 'QUEUED'
    r = svc.check_command(_req(command='resolve_camera', client_id='A', session_id=sid), _Resp())
    assert (r.result, r.message) == ('BUSY', T.JOB_PENDING_DE)


# ── F16 ───────────────────────────────────────────────────────────────────────
def test_restore_previous_is_refused_until_the_session_is_active(tmp_path):
    svc, node = _svc(tmp_path, configured=True)
    node.communicator.push(F.render(LAY, F.camera_pose(), ss=1))
    r = _session(svc, 'enter', 'A')
    assert r.accepted and svc.machine.state == 'ENTERING'
    ans = svc.commission_command(_req(command='restore_previous', client_id='A',
                                      session_id=r.session_id), _Resp())
    assert (ans.result, ans.message) == ('REFUSED', T.CHECK_NOT_ACTIVE_DE)
    assert not svc.worker.station_job_pending()
    node.clock.t += 1.6
    _session(svc, 'keepalive', 'A', r.session_id)
    svc.tick()
    assert svc.machine.state == 'ACTIVE'
    ans = svc.commission_command(_req(command='restore_previous', client_id='A',
                                      session_id=r.session_id), _Resp())
    assert ans.result == 'QUEUED', ans.message


# ── F15 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('change,valid', [
    (dict(schema=6.5), False),
    (dict(image_width=1920.7), False),
    (dict(image_height=1079.5), False),
    (dict(schema=6.0, image_width=1920.0, image_height=1080.0), True),
])
def test_schema_and_image_size_must_be_exact_integers(tmp_path, fields, change, valid):
    path = store.yaml_path(tmp_path)
    store.write_atomic(path, dict(fields, **change))
    assert (store.load(path, LAYOUTS)[0] is not None) is valid


# ── F17 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('bad', [
    dict(camera_matrix=np.eye(4)),
    dict(layout_id='OMX-STATION-A'),            # a draft layout never validates
    dict(schema=7),
])
def test_an_invalid_new_station_yaml_changes_neither_file(tmp_path, fields, bad):
    assert store.write_station(fields, tmp_path, LAYOUTS) == (True, '')
    assert store.write_station(dict(fields, world_rms_mm=2.5), tmp_path, LAYOUTS) == (True, '')
    current = store.yaml_path(tmp_path).read_bytes()
    prev = store.prev_path(tmp_path).read_bytes()
    ok, reason = store.write_station(dict(fields, **bad), tmp_path, LAYOUTS)
    assert (ok, reason) == (False, T.WRITE_FAILED_DE)
    assert store.yaml_path(tmp_path).read_bytes() == current
    assert store.prev_path(tmp_path).read_bytes() == prev
    assert store.load(store.yaml_path(tmp_path), LAYOUTS)[0] is not None
    leftovers = [p.name for p in store.station_dir(tmp_path).iterdir()
                 if p.name not in ('station.yaml', 'station.prev.yaml')]
    assert leftovers == []


def test_a_commit_whose_new_yaml_is_invalid_keeps_the_valid_calibration(tmp_path, monkeypatch):
    """The commit path: the write refuses and the job's answer is WRITE_FAILED_DE;
    the station stays configured on its previous, valid file."""
    svc, node = _svc(tmp_path, configured=True)
    before = store.yaml_path(tmp_path).read_bytes()
    com = _a_commission(svc)
    monkeypatch.setattr(com, 'yaml_fields', lambda solution, profile: dict(
        F.station_fields(LAY), camera_matrix=np.eye(4)))
    svc.commission = com
    ok, msg, cal = svc._write_commit(com.commission_id, {'any': 1})
    assert (ok, msg, cal) == (False, T.WRITE_FAILED_DE, None)
    assert store.yaml_path(tmp_path).read_bytes() == before


def _a_commission(svc):
    from physical_ai_server.station import commission as CM
    return CM.Commission('c-bad', LAY, store.staging_dir('c-bad', svc.base))
