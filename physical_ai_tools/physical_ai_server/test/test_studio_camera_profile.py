#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The robot's scene-camera profile is the arm container's STATEMENT (D63).

``StudioCamera.profile()`` is exactly what the latest statement on
``scene_provenance.PROFILE_TOPIC`` says (``{"v":1,"profile":…}``) and ``None``
while there is none: silence is unknown, never ``legacy``, however long it
lasts. The camera state only refines the stamp's ``profile_id`` and
``platform`` (from the latest state whose profile is ``tower_v1``); a camera
that faults never changes the profile (56 N-14). ``provider()`` is what the
node injects into the DataManager, on the line where R-B injected
``scene_provenance.LEGACY_PROVIDER`` (55 B-1, M-4).
"""

from __future__ import annotations

import ast
import textwrap
import types

from physical_ai_server import scene_provenance
from physical_ai_server.workflow import studio_camera as SC
from test_studio_camera import _CLASS, _SERVER_PY, _SRC, make_kit  # noqa: E402 — the shared fakes

UNKNOWN = {'profile': None, 'profile_id': None, 'platform': None}
TOWER_STATE = {'v': 1, 'profile': 'tower_v1', 'state': 'streaming', 'width': 1920, 'height': 1080,
               'fps': 29.9, 'fps_low': False, 'passthrough': True, 'outside': '640x480',
               'profile_id': '3efca10f3a05', 'platform': 'windows', 'fault': '', 'dropped': 0}


def _stmt(profile, v=1):
    return {'v': v, 'profile': profile}


def test_a_no_statement_for_30_s_is_unknown(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.clock.t += 30.0
    kit.cam._release_if_idle()
    kit.state(TOWER_STATE)              # a camera state is no statement
    assert kit.cam.profile() is None
    assert kit.cam.provider() == UNKNOWN


def test_b_a_tower_statement_after_30_s_is_tower_at_once(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.clock.t += 30.0
    kit.cam._release_if_idle()
    assert kit.cam.profile() is None
    kit.say(_stmt('tower_v1'))
    assert kit.cam.profile() == 'tower_v1'


def test_c_a_tower_statement_without_any_camera_state(monkeypatch):
    """A Windows rig whose GUI started no scene worker: still a tower rig."""
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.clock.t += 30.0
    assert kit.cam.camera_state() is None
    assert kit.cam.profile() == 'tower_v1'
    assert kit.cam.provider() == {'profile': 'tower_v1', 'profile_id': None, 'platform': None}


def test_d_a_faulted_camera_never_changes_the_profile(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.state(TOWER_STATE)
    kit.state(dict(TOWER_STATE, state='fault', fault='no_frames'))
    assert kit.cam.profile() == 'tower_v1'
    kit.state(dict(TOWER_STATE, state='closed', fault='open_failed'))
    assert kit.cam.profile() == 'tower_v1'
    assert kit.cam.provider()['profile'] == 'tower_v1'


def test_e_a_legacy_statement_is_legacy(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.say(_stmt('legacy'))
    assert kit.cam.profile() == 'legacy'
    kit.state(TOWER_STATE)              # a stray state never refines a legacy answer
    assert kit.cam.provider() == {'profile': 'legacy', 'profile_id': None, 'platform': None}


def test_the_latest_statement_wins_and_an_unreadable_one_is_unknown(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.say(_stmt('legacy'))
    assert kit.cam.profile() == 'legacy'
    for bad in ('not json', '[1]', '"tower_v1"', _stmt('tower_v2'), _stmt('tower_v1', v=2),
                {'v': True, 'profile': 'tower_v1'}, {'profile': 'tower_v1'},
                {'v': 1, 'profile': 'tower_v1', 'pad': 'x' * 5000}):
        kit.say(_stmt('tower_v1'))
        assert kit.cam.profile() == 'tower_v1'
        kit.say(bad)
        assert kit.cam.profile() is None, bad
    kit.say({'v': 1, 'profile': 'tower_v1', 'extra': 'ignored'})
    assert kit.cam.profile() == 'tower_v1'


def test_a_hostile_statement_or_state_never_raises_into_the_executor(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.state(TOWER_STATE)
    kit.say('\ud800')                   # a lone surrogate: not encodable, never a raise
    kit.state('\ud800')
    assert kit.cam.profile() is None
    assert kit.cam.camera_state() == TOWER_STATE

    def too_deep(*a, **k):
        raise RecursionError('maximum recursion depth exceeded')

    monkeypatch.setattr(SC.json, 'loads', too_deep)
    kit.say(_stmt('tower_v1'))
    kit.state(dict(TOWER_STATE, fault='no_frames'))
    assert kit.cam.profile() is None
    assert kit.cam.camera_state() == TOWER_STATE


def test_the_provider_takes_the_ids_from_the_latest_tower_state(monkeypatch):
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.state(TOWER_STATE)
    assert kit.cam.provider() == {'profile': 'tower_v1', 'profile_id': '3efca10f3a05',
                                  'platform': 'windows'}
    kit.state(dict(TOWER_STATE, profile_id='8b7f72cb250c', platform='pi'))
    assert kit.cam.provider() == {'profile': 'tower_v1', 'profile_id': '8b7f72cb250c',
                                  'platform': 'pi'}
    # A state of another profile, or a malformed one, refines nothing.
    kit.state(dict(TOWER_STATE, profile='legacy', profile_id='0123456789ab', platform='windows'))
    kit.state('{"v":1,')
    assert kit.cam.provider()['profile_id'] == '8b7f72cb250c'
    # A tower state with an unusable field keeps the other one (the stamp is still written).
    kit.state(dict(TOWER_STATE, profile_id='NOT-HEX', platform='windows'))
    assert kit.cam.provider() == {'profile': 'tower_v1', 'profile_id': None, 'platform': 'windows'}
    kit.state(dict(TOWER_STATE, profile_id='3efca10f3a05', platform='mac'))
    assert kit.cam.provider() == {'profile': 'tower_v1', 'profile_id': '3efca10f3a05',
                                  'platform': None}


def test_every_provider_answer_passes_the_recorders_check_unchanged(monkeypatch):
    kit = make_kit(monkeypatch)
    seen = [kit.cam.provider()]
    kit.say(_stmt('tower_v1'))
    seen.append(kit.cam.provider())
    kit.state(TOWER_STATE)
    seen.append(kit.cam.provider())
    kit.state(dict(TOWER_STATE, profile_id=7, platform=['pi']))
    seen.append(kit.cam.provider())
    kit.say(_stmt('legacy'))
    seen.append(kit.cam.provider())
    for answer in seen:
        assert scene_provenance.live_profile(lambda a=answer: a) == answer


# ── the node injects the camera's provider (the SAME unconditional line) ─────

class _FakeDataManager:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._dataset_lease = None


class _FakeTimerManager:
    def __init__(self, node):
        pass

    def stop_all(self):
        pass

    def set_timer(self, **kwargs):
        pass

    def start(self, timer_name):
        pass


class _Log:
    def info(self, *a, **k):
        pass


def _init_method():
    func = next(n for n in _CLASS.body if isinstance(n, ast.FunctionDef)
                and n.name == 'init_robot_control_parameters_from_user_task')
    ns = {'DataManager': _FakeDataManager, 'TimerManager': _FakeTimerManager,
          'SlotSampler': None, 'TakeIntegrity': None}
    exec(compile(textwrap.dedent(ast.get_source_segment(_SRC, func)), str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return func, ns['init_robot_control_parameters_from_user_task']


def _node(daten, studio_camera):
    node = types.SimpleNamespace(
        DEFAULT_SAVE_ROOT_PATH='/nonexistent', robot_type='omx_f', operation_mode='inference',
        timer_manager=None, _record_cb_group=object(), _enqueue_dataset_upload=None,
        timer_callback_dict={'inference': lambda: None}, daten=daten,
        communicator=types.SimpleNamespace(clear_latest_data=lambda: None, camera_topic_msgs={}),
        get_logger=lambda: _Log())
    if studio_camera is not None:
        node.studio_camera = studio_camera
    return node


def test_the_node_injects_the_studio_cameras_provider_with_and_without_daten(monkeypatch):
    _, init = _init_method()
    kit = make_kit(monkeypatch)
    kit.say(_stmt('tower_v1'))
    kit.state(TOWER_STATE)
    daten = types.SimpleNamespace(claim_record_lease=object(), start_sync_download=object())
    for d in (None, daten):
        node = _node(d, kit.cam)
        init(node, types.SimpleNamespace(fps=30))
        provider = node.data_manager._scene_camera_profile
        assert provider == kit.cam.provider
        assert scene_provenance.live_profile(provider) == {
            'profile': 'tower_v1', 'profile_id': '3efca10f3a05', 'platform': 'windows'}
    assert node.data_manager._dataset_lease is daten.claim_record_lease


def test_a_node_without_a_studio_camera_records_with_an_unknown_profile():
    _, init = _init_method()
    node = _node(None, None)
    init(node, types.SimpleNamespace(fps=30))
    assert scene_provenance.live_profile(node.data_manager._scene_camera_profile) == UNKNOWN


def test_the_injection_is_one_unconditional_statement_and_the_constant_is_gone():
    func, _ = _init_method()
    assigns = [n for n in ast.walk(func) if isinstance(n, ast.Assign)
               and ast.unparse(n.targets[0]) == 'self.data_manager._scene_camera_profile']
    assert len(assigns) == 1
    assert assigns[0] in func.body              # never inside the `if daten` block, nor any other
    assert 'LEGACY_PROVIDER' not in _SRC
