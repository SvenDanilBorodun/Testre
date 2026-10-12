"""``_preflight_contract``: START_INFERENCE's pre-flight checks, shared with the cloud start (spec §5.4).

The checks moved VERBATIM out of the START_INFERENCE branch into one method that the local
start and the cloud path (``_cloud_preflight`` against ``ready.policy``) both call, so a
student reads the same German sentence whichever target refuses. The five sentences below are
literals copied from ``main`` @ a135bdde BEFORE the refactor (concatenation boundaries included);
a drift in either copy fails here.

physical_ai_server.py imports rclpy, so the methods are exec'd from the AST onto stubs
(the test_record_command_dispatch.py idiom).
"""

from __future__ import annotations

import ast
import contextlib
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from physical_ai_server.data_processing import record_texts_de

_ROOT = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _ROOT / 'physical_ai_server.py'
_PARAM_UTILS = _ROOT / 'utils' / 'parameter_utils.py'

# ── main @ a135bdde, rendered ────────────────────────────────────────────────
TASK_DE = ('Dieses Modell benötigt eine Aufgabenanweisung (z. B. „Greife den roten Würfel"). '
           'Bitte das Feld „Aufgabenanweisung" ausfüllen und erneut starten.')
CAMS_SIND_DE = ('Dieses Modell erwartet die Kamera(s) wrist — verfügbar sind: gripper, scene. '
                'Bitte Kameras prüfen („Hardware neu erkennen") und erneut starten.')
CAMS_IST_DE = ('Dieses Modell erwartet die Kamera(s) gripper, wrist — verfügbar ist: scene. '
               'Bitte Kameras prüfen („Hardware neu erkennen") und erneut starten.')
CAMS_KEINE_DE = ('Dieses Modell erwartet die Kamera(s) scene — verfügbar sind: keine. '
                 'Bitte Kameras prüfen („Hardware neu erkennen") und erneut starten.')
FPS0_DE = ('Ungültige fps-Einstellung (0). Bitte im Inferenz-Panel eine gültige Bildrate setzen '
           '(z. B. 30) und erneut starten.')
FPS_MISMATCH_DE = ('Dieses Modell wurde mit 30 fps trainiert, die Inferenz ist auf 15 fps eingestellt. '
                   'Bitte die fps im Inferenz-Panel auf 30 setzen und erneut starten.')
RECORD_MODE_DE = ('Aufnahme während der Inferenz wird derzeit nicht unterstützt. Bitte den '
                  'Aufnahme-Modus im Inferenz-Panel deaktivieren und erneut starten.')


def _exec_functions(path, names, ns):
    source = path.read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)), str(path), 'exec'), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, missing
    return ns


def _module_constant(name):
    for node in ast.parse(_SERVER_PY.read_text(encoding='utf-8')).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


_UTILS = _exec_functions(_PARAM_UTILS, ('parse_topic_list_with_names',), {})


class _Req:
    IDLE, START_RECORD, START_INFERENCE, STOP, MOVE_TO_NEXT = 0, 1, 2, 3, 4
    RERECORD, FINISH, SKIP_TASK = 5, 6, 7


class _InferenceManager:
    summary = None

    @staticmethod
    def read_policy_summary(path):
        return dict(_InferenceManager.summary)


_NS = _exec_functions(_SERVER_PY, (
    '_preflight_contract', '_available_camera_names', 'user_interaction_callback',
    '_record_command_lock', '_request_record_end'), {
        'parse_topic_list_with_names': _UTILS['parse_topic_list_with_names'],
        'SendCommand': types.SimpleNamespace(Request=_Req),
        'InferenceManager': _InferenceManager,
        'time': time, 'threading': threading, 'contextlib': contextlib,
        'record_texts_de': record_texts_de,
        'COMMAND_LOCK_TIMEOUT_S': _module_constant('COMMAND_LOCK_TIMEOUT_S'),
    })
PREFLIGHT = _NS['_preflight_contract']


def _task(instr=('',), fps=30, record=False):
    return types.SimpleNamespace(task_instruction=list(instr), fps=fps, record_inference_mode=record,
                                 policy_path='/root/.cache/huggingface/hub/x', task_type='inference')


def _summary(cams=('gripper', 'scene'), requires_task=False, trained_fps=None):
    return {'policy_type': 'act', 'expected_cameras': list(cams), 'requires_task': requires_task,
            'trained_fps': trained_fps}


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m, *a, **k):
        self.lines.append(('info', str(m)))

    def warning(self, m, *a, **k):
        self.lines.append(('warning', str(m)))

    def error(self, m, *a, **k):
        self.lines.append(('error', str(m)))


AVAIL = {'gripper', 'scene'}


def test_five_sentences_byte_identical_with_head():
    cases = [
        (_summary(requires_task=True), _task(instr=('  ',)), AVAIL, TASK_DE,
         'Pre-flight: language-conditioned policy started without a task instruction'),
        (_summary(cams=('gripper', 'scene', 'wrist')), _task(), AVAIL, CAMS_SIND_DE,
         "Camera contract pre-flight failed: model expects ['gripper', 'scene', 'wrist'], "
         "available ['gripper', 'scene']"),
        (_summary(cams=('gripper', 'scene', 'wrist')), _task(), {'scene'}, CAMS_IST_DE, None),
        (_summary(cams=('scene',)), _task(), set(), CAMS_KEINE_DE, None),
        (_summary(), _task(fps=0), AVAIL, FPS0_DE, 'Pre-flight: fps <= 0'),
        (_summary(trained_fps=30), _task(fps=15), AVAIL, FPS_MISMATCH_DE,
         'Pre-flight: fps mismatch (trained 30, requested 15.0)'),
        (_summary(), _task(record=True), AVAIL, RECORD_MODE_DE,
         'Pre-flight: record_inference_mode requested but the inference timer has no record() path'),
    ]
    for summary, task, avail, sentence, log in cases:
        got = PREFLIGHT(None, summary, task, avail)
        assert got is not None
        assert got[0] == sentence
        if log is not None:
            assert got[1] == log
    assert PREFLIGHT(None, _summary(trained_fps=30), _task(fps=30.4), AVAIL) is None
    assert PREFLIGHT(None, _summary(trained_fps=None), _task(fps=15), AVAIL) is None
    assert PREFLIGHT(None, _summary(requires_task=True), _task(instr=('Greife',)), AVAIL) is None


def test_check_order():
    every = (_summary(cams=('wrist',), requires_task=True, trained_fps=30),
             _task(instr=('',), fps=0, record=True))
    assert PREFLIGHT(None, every[0], every[1], AVAIL)[0] == TASK_DE
    s = _summary(cams=('wrist',), trained_fps=30)
    assert PREFLIGHT(None, s, _task(fps=0, record=True), {'scene'})[0].startswith(
        'Dieses Modell erwartet die Kamera(s) wrist')
    assert PREFLIGHT(None, _summary(trained_fps=30), _task(fps=0, record=True), AVAIL)[0] == FPS0_DE
    assert PREFLIGHT(None, _summary(trained_fps=30), _task(fps=15, record=True), AVAIL)[0] == FPS_MISMATCH_DE
    assert PREFLIGHT(None, _summary(trained_fps=30), _task(fps=30, record=True), AVAIL)[0] == RECORD_MODE_DE


def test_summary_none_runs_only_summary_free_checks():
    # the cloud start, before `ready` exists: no camera, task or trained-fps check
    assert PREFLIGHT(None, None, _task(instr=('',)), set()) is None
    assert PREFLIGHT(None, None, _task(fps=0), set())[0] == FPS0_DE
    assert PREFLIGHT(None, None, _task(record=True), set())[0] == RECORD_MODE_DE
    assert PREFLIGHT(None, None, _task(fps=0, record=True), set())[0] == FPS0_DE


def test_available_camera_names():
    names = _NS['_available_camera_names']
    node = types.SimpleNamespace(params={'camera_topic_list': ['gripper:/gripper/image_raw/compressed',
                                                               'scene:/scene/image_raw/compressed']})
    assert names(node) == {'gripper', 'scene'}
    assert names(types.SimpleNamespace(params=None)) == set()


def test_start_inference_calls_it_and_holds_no_copy():
    source = _SERVER_PY.read_text(encoding='utf-8')
    for fragment in ('Dieses Modell benötigt eine Aufgabenanweisung',
                     'Dieses Modell erwartet die Kamera(s) ',
                     'Ungültige fps-Einstellung (0).',
                     'fps trainiert, die ',
                     'Aufnahme während der Inferenz wird derzeit '):
        assert source.count(fragment) == 1, fragment
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_preflight_contract')
    seg = ast.get_source_segment(source, fn)
    assert 'Dieses Modell benötigt eine Aufgabenanweisung' in seg
    cb = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == 'user_interaction_callback')
    calls = [ast.unparse(n) for n in ast.walk(cb) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == '_preflight_contract']
    assert calls == ['self._preflight_contract(summary, task_info, self._available_camera_names())']
    assert 'parse_topic_list_with_names' not in ast.unparse(cb)


class _Node:
    def __init__(self):
        self.communicator = types.SimpleNamespace(get_publisher_msg_types=lambda: {'x': 'JointTrajectory'})
        self.on_recording = False
        self.on_inference = False
        self._mode_lock = threading.Lock()
        self.log = _Log()
        self.params = {'camera_topic_list': ['gripper:/g', 'scene:/s']}
        self.inference_manager = types.SimpleNamespace(validate_policy=lambda policy_path: (True, ''))
        self.init_calls = 0
        self._available_camera_names = types.MethodType(_NS['_available_camera_names'], self)
        self._preflight_contract = types.MethodType(PREFLIGHT, self)

    def get_logger(self):
        return self.log

    def _assert_no_other_active(self, mode):
        return True, ''

    def init_robot_control_parameters_from_user_task(self, task_info):
        self.init_calls += 1


@pytest.mark.parametrize('summary,task,sentence', [
    (_summary(requires_task=True), _task(instr=('',)), TASK_DE),
    (_summary(cams=('gripper', 'scene', 'wrist')), _task(), CAMS_SIND_DE),
    (_summary(), _task(fps=0), FPS0_DE),
    (_summary(trained_fps=30), _task(fps=15), FPS_MISMATCH_DE),
    (_summary(), _task(record=True), RECORD_MODE_DE),
])
def test_start_inference_refusal_releases_the_claim_and_answers_the_sentence(summary, task, sentence):
    _InferenceManager.summary = summary
    node = _Node()
    req = types.SimpleNamespace(command=_Req.START_INFERENCE, task_info=task)
    resp = types.SimpleNamespace(success=None, message=None)
    out = _NS['user_interaction_callback'](node, req, resp)
    assert out.success is False and out.message == sentence
    assert node.on_inference is False
    assert node.init_calls == 0
    assert node.log.lines and node.log.lines[-1][0] == 'error'
    assert sentence not in node.log.lines[-1][1]          # the log line is the English one
