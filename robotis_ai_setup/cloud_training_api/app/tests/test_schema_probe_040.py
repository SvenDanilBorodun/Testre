"""The boot-time schema fingerprint (``main.py::_validate_required_schema``)
knows migration 040.

A deploy that lands the code-program routes before the ALTER TABLE / the
``update_workflow_code`` RPC would pass ``/health`` and 500 on the first code
save (the c56c012 class) — the probe is what makes the Railway deploy abort
with a named cause instead. Read from the source with ``ast`` (main.py's
module-level env checks would need the full app deps to import).

Counts are pinned (15 tables / 11 column sets / 21 RPCs; migration 042 added
one of each) so a later addition to the probe has to move this file
deliberately, the way CLAUDE.md asks.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(os.path.dirname(HERE), "main.py")
MIGRATION_040 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
    "supabase", "migrations", "20260920120000_040_code_programs.sql",
)


def _probe_tuples() -> dict[str, ast.Tuple]:
    with open(MAIN_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=MAIN_PY)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_validate_required_schema"
    )
    out: dict[str, ast.Tuple] = {}
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("required_tables", "required_rpcs")
            and isinstance(node.value, ast.Tuple)
        ):
            out[node.targets[0].id] = node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "required_columns"
            and isinstance(node.value, ast.Tuple)
        ):
            out["required_columns"] = node.value
    assert set(out) == {"required_tables", "required_columns", "required_rpcs"}, out.keys()
    return out


class TestSchemaProbeKnows040(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in cls.probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in cls.probe["required_columns"].elts]
        cls.rpcs = {
            ast.literal_eval(e.elts[0]): e.elts[1]
            for e in cls.probe["required_rpcs"].elts
        }

    def test_the_counts_are_15_11_21(self) -> None:
        self.assertEqual(
            (len(self.tables), len(self.columns), len(self.rpcs)), (15, 11, 21)
        )

    def test_the_submissions_table_is_probed(self) -> None:
        self.assertIn("workflow_submissions", self.tables)

    def test_the_three_column_sets_are_probed(self) -> None:
        self.assertIn(("workflows", "code_files, code_language"), self.columns)
        self.assertIn(("workflow_versions", "code_files, code_language"), self.columns)
        self.assertIn(
            (
                "workflow_submissions",
                "id, student_user_id, workflow_id, classroom_id, code_language, submitted_at",
            ),
            self.columns,
        )

    def test_update_workflow_code_is_probed(self) -> None:
        # PostgREST resolves an RPC on (name, arg-name set): a probe with the
        # wrong names reports the function as MISSING and aborts every deploy.
        # Migration 041 widened the function to five arguments; the exact
        # five-argument shape is pinned in test_schema_probe_041.py.
        self.assertIn("update_workflow_code", self.rpcs)

    def test_the_probe_still_names_every_040_parameter(self) -> None:
        # 041 ADDS p_blockly_json (DEFAULT NULL) and keeps the four 040 names,
        # which is what lets an older API's four named arguments resolve.
        with open(MIGRATION_040, encoding="utf-8") as fh:
            sql = fh.read()
        m = re.search(
            r"CREATE OR REPLACE FUNCTION public\.update_workflow_code\((.*?)\)\s*RETURNS",
            sql, re.S,
        )
        self.assertIsNotNone(m, "update_workflow_code is not defined in migration 040")
        sql_params = {line.strip().split()[0] for line in m.group(1).split(",")}
        probe = self.rpcs["update_workflow_code"]
        self.assertIsInstance(probe, ast.Dict)
        probe_params = {ast.literal_eval(k) for k in probe.keys}
        self.assertEqual(probe_params - sql_params, {"p_blockly_json"})
        self.assertEqual(sql_params - probe_params, set())


if __name__ == "__main__":
    unittest.main()
