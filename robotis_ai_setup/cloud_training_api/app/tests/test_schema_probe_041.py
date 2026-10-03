"""The boot-time schema fingerprint (``main.py::_validate_required_schema``)
knows migration 041: ``update_workflow_code`` is probed with its FIVE
arguments.

PostgREST resolves an RPC on (name, arg-name set). A probe that names
``p_blockly_json`` answers PGRST202 on a database without 041 (the 040
function has four parameters), so the Railway deploy of an API that sends the
Ziele aborts with a named cause instead of 500-ing the first code save —
measured on the local stack before this shipped. On a database WITH 041 the
dummy ids answer P0002, which proves the function exists.

Three shapes must agree, pinned here from the source (``ast``/regex, because
main.py's module-level env checks need the full app deps to import):
  * the probe's argument names,
  * migration 041's parameter names,
  * the argument names the route passes in ``update_workflow``.
The 15 / 11 / 21 counts are pinned in ``test_schema_probe_040.py`` (042 added
one of each; 041 adds no table, column set or RPC).
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from app.tests.test_schema_probe_040 import _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_WORKFLOWS = os.path.join(os.path.dirname(HERE), "routes", "workflows.py")
MIGRATION_041 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
    "supabase", "migrations", "20260927120000_041_code_destinations.sql",
)


def _migration_params() -> set[str]:
    with open(MIGRATION_041, encoding="utf-8") as fh:
        sql = fh.read()
    m = re.search(
        r"CREATE OR REPLACE FUNCTION public\.update_workflow_code\((.*?)\)\s*RETURNS",
        sql, re.S,
    )
    assert m is not None, "update_workflow_code is not defined in migration 041"
    return {line.strip().split()[0] for line in m.group(1).split(",")}


def _route_rpc_args() -> set[str]:
    with open(ROUTES_WORKFLOWS, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=ROUTES_WORKFLOWS)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rpc"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "update_workflow_code"
        ):
            found.append(node)
    assert len(found) == 1, f"expected one update_workflow_code call site, found {len(found)}"
    args = found[0].args[1]
    assert isinstance(args, ast.Dict)
    return {ast.literal_eval(k) for k in args.keys}


class TestSchemaProbeKnows041(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = _probe_tuples()
        cls.rpcs = {
            ast.literal_eval(e.elts[0]): e.elts[1]
            for e in probe["required_rpcs"].elts
        }

    def test_update_workflow_code_is_probed_with_five_arguments(self) -> None:
        self.assertEqual(
            ast.unparse(self.rpcs["update_workflow_code"]),
            "{'p_workflow_id': dummy, 'p_user_id': dummy, "
            "'p_code_files': {}, 'p_code_language': 'python', 'p_blockly_json': None}",
        )

    def test_the_probe_arg_names_are_the_migrations_parameter_names(self) -> None:
        probe = self.rpcs["update_workflow_code"]
        self.assertIsInstance(probe, ast.Dict)
        probe_params = {ast.literal_eval(k) for k in probe.keys}
        self.assertEqual(probe_params, _migration_params())
        self.assertIn("p_blockly_json", probe_params)

    def test_the_route_passes_exactly_the_migrations_parameter_names(self) -> None:
        self.assertEqual(_route_rpc_args(), _migration_params())


if __name__ == "__main__":
    unittest.main()
