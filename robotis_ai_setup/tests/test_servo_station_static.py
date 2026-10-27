"""Source-level fences for ``tools/servo_station`` — what no behavioural test
can see, because CI has no display and this suite imports no tkinter.

* every station file compiles; ONLY ``station_app.py`` imports tkinter and no
  module imports pyserial at module level (CI installs neither for the logic);
* every operator string is German with literal umlauts (the shipped
  ``german_detail_lint.TRANSLITERATIONS`` over every string constant — CI's
  german-strings-lint does not scan ``tools/``);
* every ``T.NAME`` and every refusal code a module uses exists in
  ``station_texts_de``, and no exception's own (English) text is spliced into
  one of those German sentences — it goes to ``station.log`` only;
* Goal_Position, speed and acceleration are written ONLY by the motion test;
  the re-datum value 128 is never named;
* no service-role key and no logged password or token anywhere;
* the Windows launcher and the requirements are exactly the spec's.
"""

import ast
import importlib.util
import pathlib
import unittest

import servo_station_fakes as sf

import station_texts_de as T  # noqa: E402

_DIR = pathlib.Path(sf.STATION_DIR)
_PY = sorted(_DIR.glob('*.py'))
_LINT = pathlib.Path(sf._REPO) / '.github' / 'scripts' / 'german_detail_lint.py'


def _tree(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _imports(tree, top_level_only=False):
    nodes = tree.body if top_level_only else ast.walk(tree)
    names = []
    for node in nodes:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _texts_alias(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'station_texts_de':
                    return alias.asname or alias.name
    return None


class TestFilesAndImports(unittest.TestCase):
    def test_the_package_is_complete(self):
        names = {p.name for p in _DIR.iterdir() if not p.name.startswith('.') and p.name != '__pycache__'}
        self.assertEqual(names, {'__init__.py', 'station_core.py', 'station_records.py', 'station_cloud.py',
                                 'station_texts_de.py', 'station_controller.py', 'station_app.py',
                                 'Servo-Station.cmd', 'requirements-station.txt'})

    def test_every_station_file_compiles(self):
        for path in _PY:
            with self.subTest(file=path.name):
                compile(path.read_text(encoding='utf-8'), str(path), 'exec')

    def test_only_the_window_imports_tkinter(self):
        for path in _PY:
            uses = [n for n in _imports(_tree(path)) if n.split('.')[0] == 'tkinter']
            with self.subTest(file=path.name):
                if path.name == 'station_app.py':
                    self.assertTrue(uses)
                else:
                    self.assertEqual(uses, [])

    def test_no_module_imports_pyserial_at_module_level(self):
        for path in _PY:
            with self.subTest(file=path.name):
                top = [n for n in _imports(_tree(path), top_level_only=True) if n.split('.')[0] == 'serial']
                self.assertEqual(top, [])

    def test_the_logic_modules_import_without_tkinter_or_pyserial(self):
        for name in ('station_core', 'station_records', 'station_cloud', 'station_controller', 'station_texts_de'):
            spec = importlib.util.spec_from_file_location(f'_static_{name}', _DIR / f'{name}.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)


class TestGermanText(unittest.TestCase):
    def test_every_string_constant_passes_the_transliteration_lint(self):
        spec = importlib.util.spec_from_file_location('gdl_station', _LINT)
        gdl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gdl)
        offenders = []
        for path in _PY:
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    hit = gdl.TRANSLITERATIONS.search(node.value)
                    if hit:
                        offenders.append((path.name, node.lineno, hit.group(0)))
        self.assertEqual(offenders, [])

    def test_the_texts_module_holds_only_operator_strings(self):
        tree = _tree(_DIR / 'station_texts_de.py')
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue                                   # the docstring
            self.assertIsInstance(node, ast.Assign, ast.dump(node)[:80])
            value = ast.literal_eval(node.value)
            if isinstance(value, dict):
                self.assertTrue(all(isinstance(v, str) for v in value.values()), node.targets[0].id)
            else:
                self.assertIsInstance(value, str, node.targets[0].id)
        self.assertEqual(set(T.STEP_NAMES), {'A', 'B', 'C', 'C2', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M'})
        self.assertIn('ü', T.MODE_LEHRE + T.MODE_PRUEFEN)
        self.assertEqual(T.SPEC_NOT_PINNED,
                         'Die Servo-Spezifikation ist noch nicht festgelegt (Prüfstand P0). '
                         'Nur „Referenz lesen“ ist möglich.')
        self.assertNotIn('aussortieren', T.RESTART_UNCLEAR)

    def test_every_text_name_and_refusal_code_a_module_uses_exists(self):
        missing = []
        for path in _PY:
            if path.name == 'station_texts_de.py':
                continue
            tree = _tree(path)
            alias = _texts_alias(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == alias and not hasattr(T, node.attr)):
                    missing.append((path.name, node.attr))
                if (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
                        and getattr(node.func, 'id', getattr(node.func, 'attr', None)) in ('refusal', 'StationError')
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and not hasattr(T, node.args[0].value)):
                    missing.append((path.name, node.args[0].value))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'step'
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value not in T.STEP_NAMES):
                    missing.append((path.name, node.args[0].value))
        self.assertEqual(missing, [])

    def test_no_exception_text_reaches_a_german_sentence(self):
        """Rule §1: inside ``except … as e`` the name ``e`` itself (``str(e)``,
        ``e`` as an argument, an f-string, ``e.args``) never reaches
        ``refusal``, ``StationError`` or a ``.format`` call; our own codes and
        German sentences (``e.code``, ``e.message_de``, …) may."""
        own = {'code', 'message_de', 'kind', 'status', 'data'}

        def bare_uses(node, name):
            parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
            return [n for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == name
                    and not (isinstance(parents.get(n), ast.Attribute) and parents[n].attr in own)]
        offenders = []
        for path in _PY:
            for handler in ast.walk(_tree(path)):
                if not (isinstance(handler, ast.ExceptHandler) and handler.name):
                    continue
                for call in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                    if not isinstance(call, ast.Call):
                        continue
                    callee = getattr(call.func, 'id', getattr(call.func, 'attr', None))
                    if callee not in ('refusal', 'StationError', 'format'):
                        continue
                    for arg in [*call.args, *(k.value for k in call.keywords)]:
                        if bare_uses(arg, handler.name):
                            offenders.append((path.name, call.lineno, callee))
        self.assertEqual(offenders, [])


class TestSafetyFences(unittest.TestCase):
    def _functions_naming(self, names):
        tree = _tree(_DIR / 'station_core.py')
        found = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr in names:
                        found.setdefault(node.attr, set()).add(fn.name)
        return found

    def test_goal_speed_and_acceleration_belong_to_the_motion_test_alone(self):
        found = self._functions_naming({'REG_GOAL_POSITION', 'REG_GOAL_SPEED', 'REG_ACCELERATION', 'REG_GOAL_TIME'})
        self.assertEqual(found, {'REG_GOAL_POSITION': {'motion_test', '_move'},
                                 'REG_GOAL_SPEED': {'motion_test'}, 'REG_ACCELERATION': {'motion_test'}})

    def test_the_motion_test_ends_limp_in_a_finally(self):
        tree = _tree(_DIR / 'station_core.py')
        (motion,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'motion_test']
        tries = [n for n in ast.walk(motion) if isinstance(n, ast.Try) and n.finalbody]
        self.assertEqual(len(tries), 1)
        calls = [n.func.id for n in ast.walk(tries[0].finalbody[0]) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)]
        self.assertEqual(calls, ['_limp'])
        # the edge refusal comes before the try, i.e. before any write
        body = ast.unparse(motion)
        self.assertLess(body.index('EDGE_PARKED'), body.index('try:'))

    def test_torque_enable_is_written_only_as_off_or_on(self):
        for path in _PY:
            text = path.read_text(encoding='utf-8')
            with self.subTest(file=path.name):
                self.assertNotIn('TORQUE_ENABLE_CALIBRATE_MIDPOINT', text)
        found = self._functions_naming({'TORQUE_ENABLE_ON'})
        self.assertEqual(found, {'TORQUE_ENABLE_ON': {'motion_test'}})

    def test_no_service_role_key_anywhere(self):
        for path in _DIR.iterdir():
            if path.is_file():
                with self.subTest(file=path.name):
                    text = path.read_text(encoding='utf-8', errors='replace')
                    self.assertNotIn('SUPABASE_SERVICE_ROLE_KEY', text)
                    self.assertNotIn('service_role', text)

    def test_no_password_or_token_is_logged(self):
        secrets = {'password', '_access', '_refresh_token', 'access_token', 'refresh_token', 'params', 'p'}
        offenders = []
        for path in _PY:
            for node in ast.walk(_tree(path)):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('debug', 'info', 'warning', 'error', 'exception', 'critical')
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == 'log'):
                    for arg in node.args[1:]:
                        for sub in ast.walk(arg):
                            name = getattr(sub, 'id', None) or getattr(sub, 'attr', None)
                            if name in secrets:
                                offenders.append((path.name, node.lineno, name))
        self.assertEqual(offenders, [])


class TestLauncher(unittest.TestCase):
    def test_the_cmd_launcher(self):
        lines = (_DIR / 'Servo-Station.cmd').read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines, ['@echo off', 'cd /d "%~dp0"', 'py -3.12 station_app.py %*', 'if errorlevel 1 pause'])

    def test_the_requirements(self):
        self.assertEqual((_DIR / 'requirements-station.txt').read_text(encoding='utf-8').split(), ['pyserial==3.5'])

    def test_the_window_refuses_an_old_python_and_a_missing_pyserial(self):
        tree = _tree(_DIR / 'station_app.py')
        (main,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main']
        text = ast.unparse(main)
        self.assertIn('sys.version_info < (3, 12)', text)
        self.assertIn('T.PYTHON_TOO_OLD', text)
        self.assertIn('import serial', text)
        self.assertIn('T.PYSERIAL_MISSING', text)
        self.assertLess(text.index('PYTHON_TOO_OLD'), text.index('tk.Tk()'))

    def test_the_bus_factory_is_the_specs(self):
        text = (_DIR / 'station_app.py').read_text(encoding='utf-8')
        self.assertIn('fb.FeetechBus(port, baudrate=baud, timeout_s=0.2)', text)
        self.assertIn('ADAPTER_VID = 0x1A86', text)
        self.assertIn('ADAPTER_PID = 0x55D3', text)


if __name__ == '__main__':
    unittest.main()
