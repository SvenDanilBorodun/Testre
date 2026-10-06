"""The Daten sidecar's import fence (spec §B1, §B4, §J.A step 2 and 8): nothing
reachable from ``daten/http_server.py`` — the server, its heavy half
(``library``, ``hub_reads``) and everything THEY import at module level —
imports ``rclpy``, ``physical_ai_interfaces`` or ``lerobot``; and importing
``v3_surgery`` imports no LeRobot (the engine keeps it function-local).

Two tiers:
* static (always, stdlib): the module-level import graph walked from the AST,
  following ``physical_ai_server.*`` imports and the ``_sibling``/``_daten``
  path loaders to their files;
* real (when numpy, pyarrow and PyAV are importable — the CI job installs them):
  a fresh interpreter imports the server and builds its heavy half with a meta
  path finder that FAILS any import of the forbidden names, so even an image
  that has rclpy and LeRobot installed proves the fence.
"""

import ast
import importlib.util
import os
import pathlib
import subprocess
import sys
import textwrap
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG_PARENT = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server'
PKG = PKG_PARENT / 'physical_ai_server'
FORBIDDEN = ('rclpy', 'physical_ai_interfaces', 'lerobot')
LOADER_CALLS = {'_sibling': 'data_processing', '_daten': 'daten'}


def _module_level_imports(path):
    """``(module names, sibling files)`` imported when ``path`` is IMPORTED:
    module-level statements incl. ``if``/``try``/``with`` blocks and class
    bodies, never a function body; ``_sibling('x')``/``_daten('x')`` calls at
    that level are followed to ``x.py`` beside the file."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names, siblings = [], []

    def loader_targets(node):
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in LOADER_CALLS \
                    and sub.args and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
                siblings.append(path.parent / f'{sub.args[0].value}.py')

    def visit(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.Import):
                names.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, f'{path}: a relative import — extend this walk'
                names.append(node.module)
                names.extend(f'{node.module}.{a.name}' for a in node.names)
            elif isinstance(node, ast.ClassDef):
                visit(node.body)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ('body', 'orelse', 'finalbody'):
                    visit(getattr(node, field, []))
                for handler in getattr(node, 'handlers', []):
                    visit(handler.body)
            else:
                loader_targets(node)

    visit(tree.body)
    return names, siblings


def _package_file(module):
    """The repo file of a ``physical_ai_server.*`` module, else None."""
    parts = module.split('.')
    if parts[0] != 'physical_ai_server':
        return None
    base = PKG.joinpath(*parts[1:])
    if base.with_suffix('.py').is_file():
        return base.with_suffix('.py')
    if (base / '__init__.py').is_file():
        return base / '__init__.py'
    return None


def reachable(start):
    """Every (file, imported top-level name) reachable at module level from ``start``."""
    seen, todo, external = set(), [start], set()
    while todo:
        path = todo.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        names, siblings = _module_level_imports(path)
        for name in names:
            f = _package_file(name)
            if f is not None:
                todo.append(f)
                # a package import runs every __init__ on the way
                parts = name.split('.')
                for k in range(1, len(parts)):
                    init = PKG.joinpath(*parts[1:k]) / '__init__.py'
                    if init.is_file():
                        todo.append(init)
            else:
                external.add((path, name.split('.')[0]))
        todo.extend(s for s in siblings if s.is_file())
    return seen, external


class StaticFence(BoundedTestCase):

    def test_the_sidecar_reaches_no_ros_and_no_lerobot(self):
        for start in ('http_server.py', 'library.py', 'hub_reads.py'):
            seen, external = reachable(PKG / 'daten' / start)
            with self.subTest(start=start):
                bad = sorted((str(p.relative_to(PKG)), n) for p, n in external if n in FORBIDDEN)
                self.assertEqual(bad, [])
                self.assertIn(PKG / 'data_processing' / 'v3_surgery.py', reachable(PKG / 'daten' / 'library.py')[0])

    def test_the_walk_has_teeth(self):
        # data_manager imports LeRobot's wrapper at module level: the walk must see it
        _, external = reachable(PKG / 'data_processing' / 'data_manager.py')
        self.assertTrue({n for _, n in external} & {'lerobot', 'rclpy', 'torch'})

    def test_v3_surgery_imports_no_lerobot_at_module_level(self):
        _, external = reachable(PKG / 'data_processing' / 'v3_surgery.py')
        self.assertNotIn('lerobot', {n for _, n in external})

    def test_the_heavy_half_is_loaded_only_when_a_server_is_built(self):
        names, siblings = _module_level_imports(PKG / 'daten' / 'http_server.py')
        self.assertEqual(sorted(s.name for s in siblings), ['contract.py', 'link_tokens.py'])
        self.assertFalse([n for n in names if n.split('.')[0] in ('av', 'numpy', 'pyarrow', 'huggingface_hub')])


HAVE_DEPS = all(importlib.util.find_spec(m) is not None for m in ('numpy', 'pyarrow', 'av'))

PROBE = textwrap.dedent('''
    import importlib.abc, sys
    FORBIDDEN = {forbidden!r}
    class Fence(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] in FORBIDDEN:
                raise ImportError('fenced: ' + name)
            return None
    sys.meta_path.insert(0, Fence())
    import physical_ai_server.daten.http_server as h
    library, hub_reads = h.load_modules()
    from physical_ai_server.data_processing import v3_surgery
    h._rig_robot_type()
    loaded = sorted(m for m in sys.modules if m.split('.')[0] in FORBIDDEN)
    print('LOADED', loaded)
    print('OK', library.__name__, hub_reads.__name__)
''')


@unittest.skipUnless(HAVE_DEPS, 'numpy, pyarrow and PyAV are installed in CI and the image')
class RealImport(BoundedTestCase):

    def test_a_fresh_interpreter_builds_the_sidecar_without_them(self):
        env = dict(os.environ, PYTHONPATH=str(PKG_PARENT))
        r = subprocess.run([sys.executable, '-c', PROBE.format(forbidden=set(FORBIDDEN))],
                           capture_output=True, text=True, env=env, timeout=120, cwd=str(PKG_PARENT))
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("LOADED []", r.stdout)
        self.assertIn('OK physical_ai_server.daten.library physical_ai_server.daten.hub_reads', r.stdout)

    def test_the_fence_has_teeth(self):
        env = dict(os.environ, PYTHONPATH=str(PKG_PARENT))
        probe = PROBE.format(forbidden={'pyarrow'})
        r = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, env=env,
                           timeout=120, cwd=str(PKG_PARENT))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('fenced: pyarrow', r.stderr)


if __name__ == '__main__':
    unittest.main()
