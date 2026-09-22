"""Lockstep between the cloud API's code-project validator and the server
package it mirrors (deps-free, AST-only — the cloud runs on Railway and the
server package needs numpy/ROS; neither can import the other, so the caps are
duplicated the way TEMPO_MIN/MAX and MAX_SIM_SCENE_OBJECTS already are).

What is fenced:

  * the three product caps and the path rule — ``MAX_CODE_FILES``,
    ``MAX_CODE_FILE_BYTES``, ``MAX_CODE_PROJECT_BYTES``, ``CODE_PATH_RE`` —
    in ``validators/workflow.py`` against ``robot_api.py``, the ONE source
    ``code_rpc.py`` and the runner read (``code_rpc.py`` must keep importing
    exactly those names from ``robot_api``, or the chain is broken);
  * the language set, the entry file per language, the extension per language
    and the reserved stems against ``code_program.py``'s literals;
  * the project-bytes measure: the validator's ``json.dumps`` call is the SAME
    ``ensure_ascii=False`` call as ``CodeProgram.from_payload``'s, keyword for
    keyword (a project the cloud accepted must be a project the server
    accepts);
  * the SQL floor in migration 040: the two CHECK constraints carry the file
    count and per-file caps EXACTLY and a rendered-bytes bound that is never
    UNDER the product cap (a direct PostgREST write cannot get under the
    Python validator; the Python validator is never looser than Postgres).
"""

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'workflow'
_ROBOT_API = _WORKFLOW / 'robot_api.py'
_CODE_RPC = _WORKFLOW / 'code_rpc.py'
_CODE_PROGRAM = _WORKFLOW / 'code_program.py'
_VALIDATOR = _REPO_ROOT / 'robotis_ai_setup' / 'cloud_training_api' / 'app' / 'validators' / 'workflow.py'
_MIGRATION_040 = (_REPO_ROOT / 'robotis_ai_setup' / 'supabase' / 'migrations'
                  / '20260920120000_040_code_programs.sql')

_CAP_NAMES = ('MAX_CODE_FILES', 'MAX_CODE_FILE_BYTES', 'MAX_CODE_PROJECT_BYTES', 'CODE_PATH_RE')
# validator name → code_program.py name
_PROGRAM_TWINS = {
    'CODE_LANGUAGES': 'CODE_LANGUAGES',
    'CODE_ENTRY_FILE': '_ENTRY_FILE',
    'CODE_EXT_FOR': '_EXT_FOR',
    'CODE_RESERVED_STEMS': '_RESERVED_STEMS',
    'CODE_RESERVED_PREFIX': '_RESERVED_PREFIX',
}


def _tree(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def _module_constants(tree):
    """``NAME = <literal>`` at module level → value (tuples/dicts included)."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


def _dumps_call(fn):
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == 'json.dumps']
    assert len(calls) == 1, ast.unparse(fn)
    return calls[0]


class ProductCapsTwin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (_ROBOT_API, _CODE_RPC, _CODE_PROGRAM, _VALIDATOR):
            assert path.is_file(), path
        cls.api = _module_constants(_tree(_ROBOT_API))
        cls.validator = _module_constants(_tree(_VALIDATOR))
        cls.program = _module_constants(_tree(_CODE_PROGRAM))

    def test_the_caps_and_the_path_rule_are_robot_apis_literals(self):
        for name in _CAP_NAMES:
            self.assertIn(name, self.api, name)
            self.assertIn(name, self.validator, name)
            self.assertEqual(self.validator[name], self.api[name], name)
            self.assertIs(type(self.validator[name]), type(self.api[name]), name)

    def test_code_rpc_still_reads_the_caps_from_robot_api(self):
        imported = set()
        for node in _tree(_CODE_RPC).body:
            if isinstance(node, ast.ImportFrom) \
                    and node.module == 'physical_ai_server.workflow.robot_api':
                imported |= {alias.name for alias in node.names}
        self.assertLessEqual(set(_CAP_NAMES), imported, set(_CAP_NAMES) - imported)

    def test_languages_entry_files_extensions_and_reserved_names_are_twins(self):
        for ours, theirs in _PROGRAM_TWINS.items():
            self.assertIn(theirs, self.program, theirs)
            self.assertIn(ours, self.validator, ours)
            self.assertEqual(self.validator[ours], self.program[theirs], ours)
        # One entry file and one extension per language, and nothing for a
        # language nobody routes on.
        languages = set(self.validator['CODE_LANGUAGES'])
        self.assertEqual(set(self.validator['CODE_ENTRY_FILE']), languages)
        self.assertEqual(set(self.validator['CODE_EXT_FOR']), languages)


class ProjectBytesMeasureTwin(unittest.TestCase):
    def test_the_validator_measures_the_project_the_way_from_payload_does(self):
        ours = _dumps_call(_function(_tree(_VALIDATOR), 'validate_code_files'))
        theirs = _dumps_call(_function(_tree(_CODE_PROGRAM), 'from_payload'))
        keywords = {kw.arg: ast.unparse(kw.value) for kw in ours.keywords}
        self.assertEqual(keywords, {'ensure_ascii': 'False'})
        self.assertEqual(keywords, {kw.arg: ast.unparse(kw.value) for kw in theirs.keywords})


class SqlFloorTwin(unittest.TestCase):
    """Migration 040's CHECKs on ``workflows.code_files`` and
    ``workflow_submissions.code_files`` (two constraints, one shape)."""

    @classmethod
    def setUpClass(cls):
        assert _MIGRATION_040.is_file(), _MIGRATION_040
        cls.sql = _MIGRATION_040.read_text(encoding='utf-8')
        cls.api = _module_constants(_tree(_ROBOT_API))

    def _bounds(self, pattern):
        found = [int(v) for v in re.findall(pattern, self.sql)]
        self.assertEqual(len(found), 2, pattern)
        return found

    def test_the_file_count_cap_is_exact_in_both_checks(self):
        for value in self._bounds(r'jsonb_object_key_count\(code_files\) <= (\d+)'):
            self.assertEqual(value, self.api['MAX_CODE_FILES'])

    def test_the_per_file_cap_is_exact_in_both_checks(self):
        for value in self._bounds(r'jsonb_max_text_value_bytes\(code_files\) <= (\d+)'):
            self.assertEqual(value, self.api['MAX_CODE_FILE_BYTES'])

    def test_the_rendered_bound_is_never_under_the_project_cap(self):
        for value in self._bounds(r'octet_length\(code_files::text\) <= (\d+)'):
            self.assertGreaterEqual(value, self.api['MAX_CODE_PROJECT_BYTES'])


if __name__ == '__main__':
    unittest.main()
