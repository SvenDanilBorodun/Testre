"""Every Cloud-Inferenz sentence the robot shows a student (spec §5.3, Rule §1, F-4, R-15, D38).

german-strings-lint's grep only looks at [FEHLER]/[WARNUNG]/[STOPP] lines and its AST walker only
at the cloud API and the Pi agent, so this file is the fence for cloud_texts_de.py: literal
umlauts (no ae/oe/ue/ss transliteration from the lint's own word list), no token or URL shape, no
unrendered ``{``, no icon glyph (the shipped-Python glyph test's own detector), a sentence for
every non-retry wire code, and the end-sentence mapping.

Also here: no new server-package file of this feature carries an ``EDUBOTICS_[A-Z0-9_]+`` literal
(ci.yml env-forwarding-guard would demand a compose forward for it, spec §2.6/§5.1).
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from collections import namedtuple
from pathlib import Path

import pytest

from physical_ai_server.inference import cloud_protocol as cp
from physical_ai_server.inference import cloud_texts_de as t

_REPO = Path(__file__).resolve().parents[3]
_INFERENCE = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'inference'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LINT = _load(_REPO / '.github' / 'scripts' / 'german_detail_lint.py', '_german_detail_lint')
_GLYPHS = _load(_REPO / 'robotis_ai_setup' / 'tests' / 'test_no_icon_glyphs.py', '_no_icon_glyphs')

End = namedtuple('End', 'kind code reason server_v')


def _constants():
    return {k: v for k, v in vars(t).items() if k.endswith('_DE') and isinstance(v, str)}


def _rendered():
    out = dict(_constants())
    out['image_size_de'] = t.image_size_de('scene', 1280, 720, 640, 480)
    out['not_jpeg_de'] = t.not_jpeg_de('gripper')
    out['stall_de'] = t.stall_de(15.0)
    out['no_data_de_cam'] = t.no_data_de('Kamera „scene“')
    out['no_data_de_arm'] = t.no_data_de('Der Follower-Arm')
    out['image_too_big_de'] = t.image_too_big_de('scene')
    out['state_dim_de'] = t.state_dim_de(7, 6)
    out['action_dim_de'] = t.action_dim_de(7, 6)
    return out


def test_every_sentence_is_german_without_transliteration_token_or_url():
    texts = _rendered()
    assert len(texts) == 37          # 29 constants + 8 rendered helpers
    for name, text in texts.items():
        assert text and text == text.strip(), name
        assert not _LINT.TRANSLITERATIONS.search(text), (name, text)
        assert 'hf_' not in text and 'http' not in text, name
        assert '{' not in text and '}' not in text, name
        assert 'Bearer' not in text and not re.search(r'[A-Za-z0-9_-]{30,}', text), name
        assert '–' not in text or name in ('MODEL_TYPE_DE', 'NONFINITE_DE', 'MALFORMED_DE', 'stall_de'), name


def test_no_icon_glyph_in_any_sentence():
    source = (_INFERENCE / 'cloud_texts_de.py').read_text(encoding='utf-8')
    assert _GLYPHS.glyph_hits(source) == []
    for name, text in _rendered().items():
        assert _GLYPHS.banned_code_points(text) == [], name


def test_the_spec_sentences_verbatim():
    assert t.STARTED_DE == 'Cloud-Inferenz gestartet.'
    assert t.STOPPED_DE == 'Cloud-Inferenz gestoppt.'
    assert t.NOT_RUNNING_DE == 'Gerade läuft keine Cloud-Inferenz.'
    assert t.LEADER_ACTIVE_DE == (
        'Die Cloud-Inferenz kann nicht starten, solange der Leader-Arm aktiv ist. Bitte zuerst '
        'in den Follower-Modus wechseln (Leader-Schalter im Inferenz-Tab) und erneut starten.')
    assert t.LEADER_BECAME_ACTIVE_DE == (
        'Die Cloud-Inferenz wurde beendet, weil der Leader-Arm aktiv geworden ist. Bitte '
        'den Follower-Modus einschalten und erneut starten.')
    assert t.image_size_de('scene', 1280, 720, 640, 480) == (
        'Kamera „scene“ liefert 1280×720 Pixel, das Modell wurde mit 640×480 Pixeln trainiert. '
        'Bitte die Kamera-Auflösung prüfen.')
    assert t.not_jpeg_de('gripper') == 'Kamera „gripper“ liefert kein JPEG-Bild.'
    assert t.no_data_de('Der Follower-Arm') == (
        'Vom Roboter kamen keine vollständigen Daten: Der Follower-Arm liefert nichts. '
        'Bitte prüfen und erneut starten.')
    assert t.state_dim_de(7, 6) == 'Das Modell erwartet 7 Gelenkwerte, der Roboter hat 6.'
    assert t.action_dim_de(7, 6) == 'Das Modell liefert 7 Gelenkwerte, der Roboter erwartet 6.'


def test_stall_sentence_renders_the_clamped_value():
    assert t.stall_de(15.0).startswith('Seit 15 Sekunden kam kein neuer Plan aus der Cloud')
    assert t.stall_de(4.0).startswith('Seit 4 Sekunden ')
    assert t.schedule_end_sentence('stall', 7.0) == t.stall_de(7.0)
    assert t.schedule_end_sentence('loading_timeout', 15.0) == t.LOADING_TIMEOUT_DE
    assert t.schedule_end_sentence('nonfinite', 15.0) == t.NONFINITE_DE
    assert t.schedule_end_sentence('malformed', 15.0) == t.MALFORMED_DE
    assert t.schedule_end_sentence('whatever', 15.0) == t.INTERNAL_DE


def test_every_non_retry_code_has_a_sentence():
    for code, retry in cp.ERROR_CODES.items():
        if retry:
            continue
        reason = 'server_error' if code == 'ended' else None
        server_v = (1,) if code == 'version' else None
        s = t.end_sentence(End('server', code, reason, server_v))
        assert s and s != t.INTERNAL_DE, code
    assert t.end_sentence(End('server', 'bad_frame', None, None)) == t.BAD_FRAME_DE
    assert t.end_sentence(End('server', 'auth', None, None)) == t.SESSION_INVALID_DE
    assert t.end_sentence(End('server', 'replaced', None, None)) == t.REPLACED_DE
    assert t.end_sentence(End('server', 'unavailable', None, None)) == t.SESSION_CHECK_FAILED_DE
    assert t.end_sentence(End('server', 'model', None, None)) == t.MODEL_LOAD_FAILED_DE
    assert t.end_sentence(End('server', 'model_type', None, None)) == t.MODEL_TYPE_DE
    assert t.end_sentence(End('server', 'cameras', None, None)) == t.CAMERAS_DE
    assert t.end_sentence(End('server', 'shape', None, None)) == t.IMAGE_SIZE_GENERIC_DE
    assert t.end_sentence(End('server', 'state', None, None)) == t.STATE_DE


def test_ended_reasons():
    for reason in ('user_stop', 'robot_stop'):
        assert t.end_sentence(End('server', 'ended', reason, None)) == ''
    assert t.end_sentence(End('server', 'ended', 'expired', None)) == t.EXPIRED_DE
    assert t.end_sentence(End('server', 'ended', 'superseded', None)) == t.SUPERSEDED_DE
    assert t.end_sentence(End('server', 'ended', 'client_gone', None)) == t.CLIENT_GONE_DE
    for reason in ('server_error', 'stall', 'never_connected', None, 'x' * 64):
        assert t.end_sentence(End('server', 'ended', reason, None)) == t.ENDED_DE


def test_version_wording_from_the_comparison():
    v = cp.PROTOCOL_VERSION
    assert t.end_sentence(End('server', 'version', None, (v - 1,))) == t.VERSION_CLOUD_OLD_DE
    assert t.end_sentence(End('server', 'version', None, (v + 1,))) == t.VERSION_ROBOT_OLD_DE
    assert t.end_sentence(End('server', 'version', None, (v + 1, v))) == t.VERSION_ROBOT_OLD_DE
    assert t.end_sentence(End('server', 'version', None, None)) == t.VERSION_ROBOT_OLD_DE
    assert t.end_sentence(End('server', 'version', None, ())) == t.VERSION_ROBOT_OLD_DE


def test_robot_kinds_and_session_gone():
    assert t.end_sentence(End('robot', 'unreachable', None, None)) == t.UNREACHABLE_DE
    assert t.end_sentence(End('robot', 'cloud_unavailable', None, None)) == t.CLOUD_UNAVAILABLE_DE
    assert t.end_sentence(End('robot', 'internal', None, None)) == t.INTERNAL_DE
    assert t.end_sentence(End('robot', 'session_gone', None, None)) == t.SESSION_INVALID_DE
    assert t.end_sentence(End('robot', 'session_gone', 'after_ready', None)) == t.ENDED_DE
    assert t.end_sentence(End('robot', 'nonsense', None, None)) == t.INTERNAL_DE
    assert t.end_sentence(None) == t.INTERNAL_DE


def test_end_sentence_never_raises_on_odd_input():
    for end in (End('server', 'rate', None, None), End('server', 'internal', None, None),
                End('server', None, None, None), End(None, None, None, None), object(), 'x', 5):
        s = t.end_sentence(end)
        assert isinstance(s, str) and s


@pytest.mark.parametrize('name', ['cloud_schedule.py', 'cloud_contract.py', 'cloud_texts_de.py',
                                  'cloud_client.py'])
def test_no_edubotics_literal_in_new_inference_files(name):
    source = (_INFERENCE / name).read_text(encoding='utf-8')
    assert not re.search(r'EDUBOTICS_[A-Z0-9_]+', source), name


def test_the_module_is_pure():
    source = inspect.getsource(t)
    for banned in ('import rclpy', 'physical_ai_interfaces', 'aiohttp', 'print('):
        assert banned not in source
