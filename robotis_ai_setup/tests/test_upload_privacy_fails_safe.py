"""Dataset upload privacy fails SAFE, at both layers (2026-08-06).

Classroom recordings can contain children's faces and audio. Two independent
layers decide whether a recorded dataset lands in a PUBLIC HuggingFace repo,
and until 2026-08-06 both failed OPEN:

  1. ``TaskInfo.msg``'s ``bool private_mode`` carried NO default. ROS 2
     booleans default to FALSE, so any rosbridge client that simply OMITTED the
     field got a PUBLIC repo. This is the OPERATIVE defect — the Daten/Aufnahme
     surface has no auth gate and rosbridge is unauthenticated, so "any client"
     is not hypothetical. React always sends ``private_mode: true``, which is
     the only reason it stayed latent.
  2. ``data_manager._upload_dataset(self, tags, private=False)`` — which
     contradicted its OWN docstring ("It defaults to True so a missing/garbled
     flag fails safe to private").

Both now default to private. This test is the lockstep: the two must agree, and
neither may be flipped back alone.

Deps-free by construction — the ``.msg`` is parsed as text and ``data_manager``
is read with ``ast`` (importing it would pull lerobot/torch).
"""

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TASK_INFO_MSG = (
    _REPO_ROOT / 'physical_ai_tools/physical_ai_interfaces/msg/TaskInfo.msg')
_DATA_MANAGER = (
    _REPO_ROOT
    / 'physical_ai_tools/physical_ai_server/physical_ai_server'
      '/data_processing/data_manager.py')

# ROS 2 IDL field-with-default syntax is `<type> <name> <default>` — no `=`.
# `=` means a CONSTANT, which is a different thing entirely and would NOT give
# the field a default (TaskStatus.msg's `uint8 READY = 0` is a constant).
_FIELD_RE = re.compile(
    r'^\s*bool\s+private_mode(?:\s+(?P<default>\S+))?\s*(?:#.*)?$')


def _msg_lines():
    return _TASK_INFO_MSG.read_text(encoding='utf-8').splitlines()


def _private_mode_declaration():
    for line in _msg_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        m = _FIELD_RE.match(line)
        if m:
            return line, m.group('default')
    return None, None


class TaskInfoMsgDefaultsToPrivate(unittest.TestCase):

    def setUp(self):
        self.assertTrue(_TASK_INFO_MSG.is_file(), _TASK_INFO_MSG)
        # Zero-file floor: an empty/renamed file must not make this vacuous.
        self.assertTrue(_msg_lines(), 'TaskInfo.msg is empty')

    def test_private_mode_is_declared_at_all(self):
        line, _ = _private_mode_declaration()
        self.assertIsNotNone(
            line, 'TaskInfo.msg no longer declares `bool private_mode`')

    def test_private_mode_carries_an_explicit_default(self):
        _, default = _private_mode_declaration()
        self.assertIsNotNone(
            default,
            'bool private_mode has NO default. ROS 2 booleans default to '
            'FALSE, so a rosbridge client that omits the field would publish '
            "classroom video of children to a PUBLIC repo.")

    def test_that_default_is_true(self):
        _, default = _private_mode_declaration()
        self.assertEqual(
            default, 'true',
            'the ROS default must be lowercase `true` (ROS 2 IDL boolean '
            'literal); anything else is either false or a parse error')

    def test_the_default_uses_field_syntax_not_constant_syntax(self):
        """`bool private_mode = true` would be a CONSTANT, not a default.

        A constant does not give the field a default value, so that spelling
        would look like a fix while leaving the wire behaviour unchanged —
        exactly the silent-failure shape this file exists to prevent.
        """
        line, _ = _private_mode_declaration()
        self.assertNotIn(
            '=', line.split('#')[0],
            'private_mode uses `=`, which declares a CONSTANT rather than a '
            'field default')


class UploadHelperDefaultsToPrivate(unittest.TestCase):
    """The Python half, read by AST (data_manager imports lerobot/torch)."""

    def setUp(self):
        self.assertTrue(_DATA_MANAGER.is_file(), _DATA_MANAGER)
        tree = ast.parse(_DATA_MANAGER.read_text(encoding='utf-8'))
        self.func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_upload_dataset':
                self.func = node
                break
        self.assertIsNotNone(self.func, '_upload_dataset not found')

    def _private_default(self):
        args = self.func.args
        # Defaults align to the TAIL of args.
        names = [a.arg for a in args.args]
        defaults = args.defaults
        offset = len(names) - len(defaults)
        for i, name in enumerate(names):
            if name == 'private':
                self.assertGreaterEqual(
                    i, offset, '`private` has no default at all')
                return defaults[i - offset]
        self.fail('_upload_dataset has no `private` parameter')

    def test_private_defaults_to_true(self):
        node = self._private_default()
        self.assertIsInstance(node, ast.Constant)
        self.assertIs(
            node.value, True,
            '_upload_dataset(private=...) must fail safe to private — its own '
            'docstring says so, and classroom recordings can contain '
            "children's faces / audio")

    def test_the_docstring_still_states_the_fail_safe_rule(self):
        # The contradiction between the docstring and the signature is what
        # hid this for so long; keep the rule written down beside the code.
        doc = ast.get_docstring(self.func) or ''
        self.assertIn('fails safe to private', doc)


class BothLayersAgree(unittest.TestCase):
    """Neither layer may be flipped back on its own.

    The ROS default is the one that actually protects an omitting client; the
    Python default only covers a caller that passes nothing. A change to either
    alone is almost certainly a mistake, so pin them together.
    """

    def test_ros_default_and_python_default_are_both_private(self):
        _, ros_default = _private_mode_declaration()
        tree = ast.parse(_DATA_MANAGER.read_text(encoding='utf-8'))
        py_default = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == '_upload_dataset':
                names = [a.arg for a in node.args.args]
                offset = len(names) - len(node.args.defaults)
                idx = names.index('private')
                py_default = node.args.defaults[idx - offset].value
        self.assertEqual(
            (ros_default, py_default), ('true', True),
            'the ROS and Python upload-privacy defaults disagree')


if __name__ == '__main__':
    unittest.main()
