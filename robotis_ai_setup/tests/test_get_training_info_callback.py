"""`GetTrainingInfo` — it can actually SUCCEED, and its failures say nothing back.

Two defects in `physical_ai_server.get_training_info_callback`:

  * **It could never return success.** The success message read a BARE
    ``train_config_path``, a name that is never bound anywhere in the function
    — only ``request.train_config_path`` is. The `NameError` fired immediately
    AFTER ``response.success = True``, was swallowed by the function's own
    outer ``except Exception`` and flipped the response back to
    ``success = False`` with a generic English message. Every well-formed
    request got a failure. Invisible only because the service has no UI call
    site (the three `getTrainingInfo` occurrences are all inside
    `useRosServiceCaller.js`, with zero callers).

  * **The not-found branch echoed an absolute path back.** ``config_path`` is
    the CONFINED, absolute path under the model root; reflecting it into a
    German toast leaks the container layout and turns the branch into an
    existence oracle over everything under that root. The confinement REFUSAL
    beside it was already constant-message — the rule had simply been applied
    one branch too narrow.

Extracted with `ast` and exec'd onto a fake node for the same reason as
`test_hf_control_callback.py`: importing `physical_ai_server` pulls rclpy,
torch, lerobot and cv2. `dataset_paths` is loaded for real (stdlib-only) so the
confinement in the function is the shipped one.
"""

import ast
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import textwrap
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG_ROOT = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
)
SERVER_PY = PKG_ROOT / 'physical_ai_server.py'
PATHS_PY = PKG_ROOT / 'data_processing' / 'dataset_paths.py'

_PATHS_MODULE_NAME = '_edubotics_traininfo_dataset_paths'


def _load_dataset_paths():
    if _PATHS_MODULE_NAME in sys.modules:
        return sys.modules[_PATHS_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_PATHS_MODULE_NAME, str(PATHS_PY))
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PATHS_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PATHS_MODULE_NAME, None)
        raise
    return module


_DP = _load_dataset_paths()
_SOURCE = SERVER_PY.read_text(encoding='utf-8')
_TREE = ast.parse(_SOURCE)


def _fn_node(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f'{name} not found in {SERVER_PY.name} — either renamed or this test '
        f'is stale, and both mean nothing below is checked')


_WEIGHT_ROOT = {'path': None}


class _TrainingManager:
    @staticmethod
    def get_weight_save_root_path():
        return _WEIGHT_ROOT['path']


def _extract(name, extra_globals):
    node = _fn_node(name)
    ns = dict(extra_globals)
    src = textwrap.dedent(ast.get_source_segment(_SOURCE, node))
    exec(compile(src, str(SERVER_PY), 'exec'), ns)  # noqa: S102
    return ns[name]


_GET_TRAINING_INFO = _extract(
    'get_training_info_callback',
    {'dataset_paths': _DP, 'TrainingManager': _TrainingManager,
     'json': json, 'Path': pathlib.Path},
)


class _Logger:
    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class _Node:
    def get_logger(self):
        return _Logger()


class _Resp:
    """A ROS srv response as the wire hands it over."""

    def __init__(self):
        self.success = False
        self.message = ''
        self.training_info = types.SimpleNamespace()


_VALID_CONFIG = {
    'dataset': {'repo_id': 'alice/omx_f_pick'},
    'policy': {'type': 'act', 'device': 'cuda'},
    'output_dir': '/workspace/ros2_ws/outputs/train/mein_modell',
    'seed': 7,
    'num_workers': 2,
    'batch_size': 16,
    'steps': 4242,
    'eval_freq': 100,
    'log_freq': 10,
    'save_freq': 50,
}


class _Rig(unittest.TestCase):

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='weights_')).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _WEIGHT_ROOT['path'] = self.root
        self.addCleanup(_WEIGHT_ROOT.update, {'path': None})
        self.node = _Node()

    def _write_config(self, rel, data=None):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(_VALID_CONFIG if data is None else data), encoding='utf-8')
        return target

    def _call(self, train_config_path):
        response = _Resp()
        request = types.SimpleNamespace(train_config_path=train_config_path)
        return _GET_TRAINING_INFO(self.node, request, response)


class TheSuccessPathCanActuallySucceed(_Rig):

    def test_a_valid_config_returns_success(self):
        """The whole defect: this was UNREACHABLE.

        `response.success = True` was set and then a NameError on the very next
        statement sent control to the outer handler, which set it back to
        False. Nothing about the request had to be wrong.
        """
        rel = 'mein_modell/pretrained_model/train_config.json'
        self._write_config(rel)
        resp = self._call(rel)
        self.assertTrue(
            resp.success,
            f'a well-formed request still failed: {resp.message!r}')
        self.assertTrue(resp.message, 'success reported with no message')
        self.assertNotIn('Failed to retrieve training info', resp.message)

    def test_the_config_is_really_parsed(self):
        # Not vacuous: prove the success came from reading the file, not from
        # a short-circuit that never opened it.
        rel = 'mein_modell/pretrained_model/train_config.json'
        self._write_config(rel)
        resp = self._call(rel)
        info = resp.training_info
        self.assertEqual(info.dataset, 'alice/omx_f_pick')
        self.assertEqual(info.policy_type, 'act')
        self.assertEqual(info.policy_device, 'cuda')
        self.assertEqual(info.output_folder_name, 'mein_modell')
        self.assertEqual(info.seed, 7)
        self.assertEqual(info.steps, 4242)

    def test_an_absolute_path_inside_the_model_root_also_succeeds(self):
        # safe_under accepts an absolute spelling that LANDS inside the root —
        # the file browser hands back absolute paths.
        rel = 'mein_modell/pretrained_model/train_config.json'
        target = self._write_config(rel)
        resp = self._call(str(target))
        self.assertTrue(resp.success, resp.message)

    def test_the_bare_name_train_config_path_is_never_read(self):
        """Structural fence on the exact defect.

        `train_config_path` is bound NOWHERE in this function — it exists only
        as `request.train_config_path` (an ast.Attribute). Any bare ast.Name
        load of that id is an unbound read, i.e. a NameError waiting for the
        branch that reaches it. A behavioural test can only catch the ones on
        paths it happens to drive; this catches all of them.
        """
        fn = _fn_node('get_training_info_callback')
        bound = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
        offenders = sorted({
            node.lineno for node in ast.walk(fn)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id == 'train_config_path'
        })
        self.assertNotIn('train_config_path', bound,
                         'the fence below assumes the name is never assigned')
        self.assertEqual(
            offenders, [],
            'a BARE `train_config_path` is read — it is never bound in this '
            'function, so that line raises NameError')


class FailuresDoNotEchoThePathBack(_Rig):

    def test_the_not_found_message_is_a_constant_german_sentence(self):
        resp = self._call('gibt_es_nicht/pretrained_model/train_config.json')
        self.assertFalse(resp.success)
        self.assertTrue(resp.message)
        # Rule §1: literal umlauts, never ae/oe/ue/ss.
        self.assertIn('auswählen', resp.message)
        self.assertNotIn('auswaehlen', resp.message)

    def test_the_not_found_message_does_not_leak_the_model_root(self):
        """`config_path` is the CONFINED absolute path — an echo leaks layout
        and makes the branch an existence oracle over the whole root."""
        resp = self._call('gibt_es_nicht/pretrained_model/train_config.json')
        self.assertNotIn(str(self.root), resp.message)
        self.assertNotIn('gibt_es_nicht', resp.message)

    def test_an_escaping_path_is_refused_without_echoing_it(self):
        # Not vacuous: the confinement refusal is a DIFFERENT branch and must
        # also stay quiet. It forwards dataset_paths' own German text.
        resp = self._call('/etc/passwd')
        self.assertFalse(resp.success)
        self.assertNotIn('/etc/passwd', resp.message)
        self.assertIn('außerhalb', resp.message)

    def test_malformed_json_still_fails_loudly(self):
        rel = 'mein_modell/pretrained_model/train_config.json'
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{not json', encoding='utf-8')
        resp = self._call(rel)
        self.assertFalse(resp.success)
        self.assertTrue(resp.message)


if __name__ == '__main__':
    unittest.main()
