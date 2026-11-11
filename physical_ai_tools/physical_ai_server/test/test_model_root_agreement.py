#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The server has ONE model root, and every reader of it uses it.

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
  * React's ``constants/paths.js`` named a THIRD path inside the tree the build
    deletes.

The React half (``constants/paths.js``, read by the Inferenz page's local model
picker and its download dialog) went with that local-inference UI on
2026-11-09: the page runs „In der Cloud" only and names no robot path. What
stays is the server half — one root, derived from ``MODEL_ROOT_RELATIVE``, and
every server reader calling it instead of respelling it.

Nothing here needs lerobot, ROS or numpy: ``dataset_paths`` is stdlib-only by
design.
"""

import ast
import pathlib
import unittest

_HERE = pathlib.Path(__file__).resolve()
_PKG = _HERE.parents[1] / 'physical_ai_server'
_DATA_MANAGER = _PKG / 'data_processing' / 'data_manager.py'
_TRAINING_MANAGER = _PKG / 'training' / 'training_manager.py'


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
