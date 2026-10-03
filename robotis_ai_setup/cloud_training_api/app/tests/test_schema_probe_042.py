"""The boot-time schema fingerprint (``main.py::_validate_required_schema``)
knows migration 042: the per-student Hugging Face token table, its column set
and the ``store_user_hf_credential`` writer.

A deploy that lands the /me/hf-token routes before the migration would pass
``/health`` and 500 on the first token save (the c56c012 class); the probe is
what aborts the Railway deploy with a named cause instead. PostgREST resolves
an RPC on (name, arg-name set), so the probe's six argument names must equal the
migration's parameter names exactly or EVERY deploy would report the function
missing. Read from the source with ``ast``/regex (main.py's module-level env
checks would need the full app deps to import).

The 15 / 11 / 21 counts are pinned in ``test_schema_probe_040.py``.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from app.tests.test_schema_probe_040 import _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_HF_TOKEN = os.path.join(os.path.dirname(HERE), "routes", "hf_token.py")
MIGRATION_042 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
    "supabase", "migrations", "20261003120000_042_user_hf_credentials.sql",
)

_COLUMNS = (
    "user_id, token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at"
)


def _migration_sql() -> str:
    with open(MIGRATION_042, encoding="utf-8") as fh:
        return fh.read()


def _migration_params() -> list[str]:
    m = re.search(
        r"CREATE OR REPLACE FUNCTION public\.store_user_hf_credential\((.*?)\)\s*RETURNS",
        _migration_sql(), re.S,
    )
    assert m is not None, "store_user_hf_credential is not defined in migration 042"
    return [line.strip().split()[0] for line in m.group(1).split(",")]


def _route_rpc_args() -> list[str]:
    with open(ROUTES_HF_TOKEN, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=ROUTES_HF_TOKEN)
    found = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "rpc"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "store_user_hf_credential"
    ]
    assert len(found) == 1, f"expected one store_user_hf_credential call site, found {len(found)}"
    args = found[0].args[1]
    assert isinstance(args, ast.Dict)
    return [ast.literal_eval(k) for k in args.keys]


class TestSchemaProbeKnows042(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = {
            ast.literal_eval(e.elts[0]): e.elts[1] for e in probe["required_rpcs"].elts
        }

    def test_the_credentials_table_is_probed(self) -> None:
        self.assertIn("user_hf_credentials", self.tables)

    def test_the_column_set_is_probed_and_equals_the_columns_the_routes_select(self) -> None:
        self.assertIn(("user_hf_credentials", _COLUMNS), self.columns)
        # Every probed column exists in the migration's CREATE TABLE.
        for col in _COLUMNS.split(", "):
            self.assertRegex(_migration_sql(), rf"\n\s+{col}\s", col)

    def test_the_writer_is_probed_with_the_six_migration_parameters(self) -> None:
        self.assertIn("store_user_hf_credential", self.rpcs)
        probe = self.rpcs["store_user_hf_credential"]
        self.assertIsInstance(probe, ast.Dict)
        probe_params = [ast.literal_eval(k) for k in probe.keys]
        self.assertEqual(probe_params, _migration_params())
        self.assertEqual(
            probe_params,
            ["p_user_id", "p_ciphertext", "p_fp", "p_hint", "p_hf_username", "p_role"],
        )

    def test_the_probe_uses_the_dummy_user_id_so_p0002_answers_before_any_write(self) -> None:
        probe = self.rpcs["store_user_hf_credential"]
        first_value = probe.values[0]
        self.assertIsInstance(first_value, ast.Name)
        self.assertEqual(first_value.id, "dummy")

    def test_the_route_calls_the_rpc_with_the_same_parameter_names(self) -> None:
        self.assertEqual(_route_rpc_args(), _migration_params())

    def test_the_migration_writer_raises_p0002_before_it_writes(self) -> None:
        full = _migration_sql()
        # Only the function body: the header comments also mention P0002.
        sql = full[full.index("CREATE OR REPLACE FUNCTION public.store_user_hf_credential("):]
        lock = sql.index("FOR UPDATE;")
        p0002 = sql.index("ERRCODE = 'P0002'")
        insert = sql.index("INSERT INTO public.user_hf_credentials")
        self.assertLess(lock, p0002)
        self.assertLess(p0002, insert)


if __name__ == "__main__":
    unittest.main()
