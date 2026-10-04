"""The boot-time schema fingerprint (``main.py::_validate_required_schema``)
knows migration 042: the per-student Hugging Face token table, its column set
and the ``store_user_hf_credential`` writer.

A deploy that lands the /me/hf-token routes before the migration would pass
``/health`` and 500 on the first token save (the c56c012 class); the probe is
what aborts the Railway deploy with a named cause instead. PostgREST resolves
an RPC on (name, arg-name set), so the probe's argument names must equal the
CURRENT definition's parameter names exactly or EVERY deploy would report the
function missing. Since migration 045 that definition has seven parameters
(``p_expected_fp`` last, defaulted), and 045 drops the six-parameter one. Read from the source with ``ast``/regex (main.py's module-level env
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
# The writer's CURRENT definition (045 replaced 042's six-argument one).
MIGRATION_045 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))),
    "supabase", "migrations", "20261004140000_045_hf_credential_expected_fp.sql",
)

_COLUMNS = (
    "user_id, token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at"
)


def _migration_sql() -> str:
    with open(MIGRATION_042, encoding="utf-8") as fh:
        return fh.read()


def _migration_params(path: str = MIGRATION_045) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        sql = fh.read()
    m = re.search(
        r"CREATE OR REPLACE FUNCTION public\.store_user_hf_credential\((.*?)\)\s*RETURNS",
        sql, re.S,
    )
    assert m is not None, f"store_user_hf_credential is not defined in {os.path.basename(path)}"
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

    def test_the_writer_is_probed_with_the_seven_current_parameters(self) -> None:
        self.assertIn("store_user_hf_credential", self.rpcs)
        probe = self.rpcs["store_user_hf_credential"]
        self.assertIsInstance(probe, ast.Dict)
        probe_params = [ast.literal_eval(k) for k in probe.keys]
        self.assertEqual(probe_params, _migration_params())
        self.assertEqual(
            probe_params,
            ["p_user_id", "p_ciphertext", "p_fp", "p_hint", "p_hf_username", "p_role",
             "p_expected_fp"],
        )
        # 042's six are a prefix: an older API's six named arguments still resolve.
        self.assertEqual(_migration_params(MIGRATION_042), probe_params[:6])

    def test_045_drops_the_six_argument_writer_and_defaults_the_seventh(self) -> None:
        with open(MIGRATION_045, encoding="utf-8") as fh:
            sql = fh.read()
        drop = sql.index(
            "DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text);")
        create = sql.index("CREATE OR REPLACE FUNCTION public.store_user_hf_credential(")
        self.assertLess(drop, create)
        self.assertRegex(sql, r"p_expected_fp\s+text DEFAULT NULL")

    def test_the_probe_uses_the_dummy_user_id_so_p0002_answers_before_any_write(self) -> None:
        probe = self.rpcs["store_user_hf_credential"]
        first_value = probe.values[0]
        self.assertIsInstance(first_value, ast.Name)
        self.assertEqual(first_value.id, "dummy")

    def test_the_route_calls_the_rpc_with_the_same_parameter_names(self) -> None:
        self.assertEqual(_route_rpc_args(), _migration_params())

    def test_the_045_writer_raises_p0002_before_it_reads_the_expectation_or_writes(self) -> None:
        with open(MIGRATION_045, encoding="utf-8") as fh:
            full = fh.read()
        sql = full[full.index("CREATE OR REPLACE FUNCTION public.store_user_hf_credential("):]
        lock = sql.index("FOR UPDATE;")
        p0002 = sql.index("ERRCODE = 'P0002'")
        expected = sql.index("ERRCODE = 'P0045'")
        insert = sql.index("INSERT INTO public.user_hf_credentials")
        self.assertLess(lock, p0002)
        self.assertLess(p0002, expected)
        self.assertLess(expected, insert)

    def test_the_route_maps_p0045_to_its_409(self) -> None:
        with open(ROUTES_HF_TOKEN, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"P0045" in str(exc)', src)
        self.assertIn('"p_expected_fp": expected_fp', src)

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
