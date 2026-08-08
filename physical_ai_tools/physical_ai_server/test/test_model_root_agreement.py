#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""„Modellpfad auswählen" opens where checkpoints actually are.

THREE PLACES USED TO DISAGREE about where a downloaded model checkpoint lives,
and after the 2026-08-06 browse confinement the disagreement stopped being
merely useless and became a hard refusal:

  * ``data_manager.download_huggingface_repo(repo_type='model')`` WRITES to
    ``~/ros2_ws/outputs/train`` — deliberately, since v2.5.0, because the image
    build ``rm -rf``'s the vendored ``ros2_ws/src/physical_ai_tools/lerobot``
    tree;
  * ``TrainingManager.get_weight_save_root_path()`` derived a DIFFERENT path
    from ``lerobot.__file__`` — the pip site-packages install, which nothing
    writes to. ``dataset_paths.model_root()`` took the browsable root from it,
    so the file browser's allowlist named an empty directory;
  * React's ``constants/paths.js`` ``POLICY_MODEL_PATH`` named a THIRD path,
    ``/root/ros2_ws/src/physical_ai_tools/lerobot/outputs/train/`` — inside the
    tree the build deletes — from a ``REACT_APP_LEROBOT_OUTPUTS_PATH`` that no
    Dockerfile sets.

Measured before/after: the modal used to open on an empty directory and let the
student navigate UP to find the checkpoint; with confinement in place it opened
outside every browsable root and returned a German security refusal with
nowhere to go.

Nothing here needs lerobot, ROS or numpy: ``dataset_paths`` is stdlib-only by
design and the React side is read as text. The DRIFT this file exists for is
cross-language and silent in the same way the old one was — a rename on either
side leaves both suites green while the student's modal refuses.
"""

import ast
import pathlib
import re
import unittest

_HERE = pathlib.Path(__file__).resolve()
_PKG = _HERE.parents[1] / 'physical_ai_server'
_REPO_ROOT = _HERE.parents[3]
_PATHS_JS = (
    _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager'
    / 'src' / 'constants' / 'paths.js'
)
_DATA_MANAGER = _PKG / 'data_processing' / 'data_manager.py'
_TRAINING_MANAGER = _PKG / 'training' / 'training_manager.py'

# The container runs as root — the physical_ai_server image declares no USER —
# so `Path.home()` inside it is `/root`. Stated here rather than computed,
# because this test runs on a developer host where `Path.home()` is something
# else entirely, and the React side can only ever carry an absolute literal.
_CONTAINER_HOME = '/root'


def _code_of(tree, name):
    """A function's body as source, WITHOUT its docstring.

    ``ast.unparse`` keeps the docstring, and every docstring here explains the
    defect by naming exactly the symbols the assertions ban — so a scan over the
    raw unparse would only ever fail on the prose.
    """
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if not body:
        raise AssertionError(f'{name} has no code, only a docstring')
    return '\n'.join(ast.unparse(stmt) for stmt in body)


def _js_const(name):
    """Resolve a `const NAME = ...;` in paths.js down to a plain string.

    Handles the two forms the file uses — a bare literal and a template string
    interpolating consts declared above it — which is enough to compare against
    the Python constant without importing a JS engine.
    """
    text = _PATHS_JS.read_text(encoding='utf-8')
    consts = {}
    for m in re.finditer(
            r"^const\s+([A-Za-z_$][\w$]*)\s*=\s*([`'\"])(.*?)\2\s*;",
            text, re.MULTILINE):
        consts[m.group(1)] = m.group(3)
    # DEFAULT_PATHS members are template strings over those consts.
    for m in re.finditer(
            r"^\s*([A-Z_]+):\s*([`'\"])(.*?)\2\s*,", text, re.MULTILINE):
        consts[m.group(1)] = m.group(3)

    def expand(value, depth=0):
        if depth > 5:
            raise AssertionError(f'template expansion loop at {value!r}')
        out = re.sub(
            r'\$\{([A-Za-z_$][\w$]*)\}',
            lambda mm: consts.get(mm.group(1), f'<<UNRESOLVED:{mm.group(1)}>>'),
            value)
        return out if out == value else expand(out, depth + 1)

    if name not in consts:
        raise AssertionError(
            f'{name} not found in paths.js — either it was renamed or this '
            f'test\'s parser is stale, and both mean nothing is compared')
    return expand(consts[name])


class TheParserActuallyReadsPathsJs(unittest.TestCase):
    """Zero-find floor: every assertion below derives from this parse."""

    def test_the_file_exists(self):
        self.assertTrue(
            _PATHS_JS.is_file(),
            f'{_PATHS_JS} is missing — the React half of the contract')

    def test_it_resolves_a_template_string_to_an_absolute_path(self):
        value = _js_const('POLICY_MODEL_PATH')
        self.assertNotIn('<<UNRESOLVED', value, value)
        self.assertTrue(value.startswith('/'), value)


class ReactAndTheServerNameTheSameModelRoot(unittest.TestCase):

    def setUp(self):
        from physical_ai_server.data_processing import dataset_paths
        self.dataset_paths = dataset_paths

    def test_policy_model_path_is_the_servers_model_root(self):
        expected = f'{_CONTAINER_HOME}/{self.dataset_paths.MODEL_ROOT_RELATIVE}/'
        self.assertEqual(
            _js_const('POLICY_MODEL_PATH'), expected,
            'React opens „Modellpfad auswählen" somewhere the server does not '
            'consider a browsable root, so it answers with a German security '
            'refusal and the student cannot navigate anywhere')

    def test_dataset_path_is_the_servers_dataset_root(self):
        expected = f'{_CONTAINER_HOME}/{self.dataset_paths.DATASET_ROOT_RELATIVE}/'
        self.assertEqual(_js_const('DATASET_PATH'), expected)

    def test_both_react_paths_keep_their_trailing_slash(self):
        # InferencePanel builds `POLICY_MODEL_PATH + repoId` and
        # LocalDatasetQuickPick builds `DATASET_PATH + user + '/' + name`.
        for key in ('POLICY_MODEL_PATH', 'DATASET_PATH'):
            self.assertTrue(_js_const(key).endswith('/'), key)

    def test_react_no_longer_points_inside_the_deleted_lerobot_tree(self):
        # docker/physical_ai_server/Dockerfile does
        # `rm -rf /root/ros2_ws/src/physical_ai_tools/lerobot`, so no shipped
        # path may live under it.
        text = _PATHS_JS.read_text(encoding='utf-8')
        code = '\n'.join(
            ln for ln in text.split('\n')
            if not re.match(r'^\s*(//|\*|/\*)', ln))
        self.assertNotIn('physical_ai_tools/lerobot', code)


class TheServerHasOneModelRootAndEveryReaderUsesIt(unittest.TestCase):

    def setUp(self):
        from physical_ai_server.data_processing import dataset_paths
        self.dataset_paths = dataset_paths

    def test_model_root_is_derived_from_the_shared_constant(self):
        import pathlib as _pl
        self.assertEqual(
            self.dataset_paths.model_root(),
            _pl.Path.home() / self.dataset_paths.MODEL_ROOT_RELATIVE)

    def test_model_root_needs_no_lerobot(self):
        # It used to import TrainingManager (hence lerobot) function-locally and
        # return None on failure — which silently DROPPED the model root from
        # the browsable set instead of failing loudly.
        src = (_PKG / 'data_processing' / 'dataset_paths.py').read_text(
            encoding='utf-8')
        body = _code_of(ast.parse(src), 'model_root')
        self.assertNotIn('lerobot', body)
        self.assertNotIn('TrainingManager', body)

    def test_the_browsable_set_always_contains_the_model_root(self):
        roots = [str(r) for r in self.dataset_paths.browsable_roots()]
        self.assertIn(str(self.dataset_paths.model_root()), roots)
        self.assertIn(str(self.dataset_paths.dataset_root()), roots)

    def test_the_model_root_itself_is_browsable(self):
        # The browser OPENS at exactly this path (allow_root=True in
        # FileBrowseUtils._confine), so the root must not be refused.
        self.assertTrue(
            self.dataset_paths.is_inside(
                self.dataset_paths.model_root(),
                self.dataset_paths.model_root(),
                allow_root=True))

    def test_the_downloader_writes_to_that_same_root(self):
        """`download_huggingface_repo`'s map must CALL it, not respell it.

        A literal here is how the three paths drifted apart in the first place.
        """
        tree = ast.parse(_DATA_MANAGER.read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == 'download_huggingface_repo')
        dicts = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)]
        self.assertTrue(dicts, 'the download_path map is gone')
        mapping = {}
        for d in dicts:
            for k, v in zip(d.keys, d.values):
                if isinstance(k, ast.Constant):
                    mapping[k.value] = ast.unparse(v)
        self.assertIn('model', mapping)
        self.assertIn('dataset', mapping)
        self.assertIn('model_root()', mapping['model'])
        self.assertIn('dataset_root()', mapping['dataset'])

    def test_the_weight_save_root_delegates_instead_of_deriving(self):
        """Both model-list callbacks read this; a second derivation re-splits it."""
        tree = ast.parse(_TRAINING_MANAGER.read_text(encoding='utf-8'))
        body = _code_of(tree, 'get_weight_save_root_path')
        self.assertIn('model_root()', body)
        self.assertNotIn(
            'lerobot.__file__', body,
            'get_weight_save_root_path derives its own path again — that named '
            'the pip site-packages install, which nothing writes to')


if __name__ == '__main__':
    unittest.main()
