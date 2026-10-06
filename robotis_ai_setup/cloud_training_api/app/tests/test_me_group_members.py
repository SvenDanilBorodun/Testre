"""Route tests for ``GET /me/group-members`` (Daten 2.0, spec §E6, R-26).

House pattern (``test_dataset_identity_routes._ensure_stubs``): fastapi,
pydantic and the heavy leaf modules are stubbed in sys.modules, the async
handler is driven with ``asyncio.run`` and the route module's ``get_supabase``
is patched with a recording fake.

Covers:
  * the members are the CALLER's workgroup only: the query is scoped by the
    workgroup on the JWT-resolved profile, never by anything the request names
    (the handler takes no parameter but the profile, so a query parameter
    naming another workgroup cannot reach it)
  * each member carries exactly ``full_name``, ``hf_username``, ``is_me`` — no
    login ``username`` and no ``user_id``
  * no workgroup → the caller alone, and the database is not asked
  * a database error → 500 with the German sentence
  * ``main.py`` carries the rate-limit rule and keys the prefix per user
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import me as meroute  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_PY = os.path.join(os.path.dirname(HERE), "main.py")

ME = {
    "id": "u-lena",
    "role": "student",
    "username": "lena.schmidt",
    "full_name": "Lena Schmidt",
    "hf_username": "lena-schmidt",
    "classroom_id": "c-1",
    "workgroup_id": "wg-a",
    "training_credits": 5,
}

USERS = [
    {"id": "u-lena", "username": "lena.schmidt", "full_name": "Lena Schmidt",
     "hf_username": "lena-schmidt", "workgroup_id": "wg-a"},
    {"id": "u-max", "username": "max.weber", "full_name": "Max Weber",
     "hf_username": "max-weber", "workgroup_id": "wg-a"},
    {"id": "u-ali", "username": "ali.yilmaz", "full_name": "Ali Yilmaz",
     "hf_username": None, "workgroup_id": "wg-a"},
    {"id": "u-eve", "username": "eve.other", "full_name": "Eve Andere",
     "hf_username": "eve-andere", "workgroup_id": "wg-b"},
    {"id": "u-solo", "username": "solo", "full_name": "Solo",
     "hf_username": "solo-hf", "workgroup_id": None},
]


class _FakeDB:
    """Records every call; answers a users select filtered like PostgREST."""

    def __init__(self, rows=USERS, fail=False):
        self.rows = rows
        self.fail = fail
        self.calls = []

    def table(self, name):
        outer = self
        outer.calls.append(("table", name))

        class _Q:
            def __init__(self):
                self.filters = []
                self.columns = None

            def select(self, columns, *a, **k):
                self.columns = [c.strip() for c in columns.split(",")]
                outer.calls.append(("select", columns))
                return self

            def eq(self, col, val):
                self.filters.append((col, val))
                outer.calls.append(("eq", col, val))
                return self

            def order(self, col, *a, **k):
                outer.calls.append(("order", col))
                return self

            def execute(self):
                if outer.fail:
                    raise RuntimeError("connection reset by peer (postgrest)")
                rows = [
                    r for r in outer.rows
                    if all(r.get(c) == v for c, v in self.filters)
                ]
                if self.columns:
                    rows = [{c: r.get(c) for c in self.columns} for r in rows]
                return SimpleNamespace(data=rows)

        return _Q()


def _run(profile, db):
    with patch.object(meroute, "get_supabase", return_value=db):
        return asyncio.run(meroute.read_my_group_members(profile=profile))


class TestGroupMembers(unittest.TestCase):
    def test_members_of_the_callers_workgroup_only(self):
        db = _FakeDB()
        body = _run(ME, db)
        self.assertEqual(body["workgroup_id"], "wg-a")
        names = sorted(m["full_name"] for m in body["members"])
        self.assertEqual(names, ["Ali Yilmaz", "Lena Schmidt", "Max Weber"])
        self.assertIn(("table", "users"), db.calls)
        self.assertIn(("eq", "workgroup_id", "wg-a"), db.calls)
        # Scoped by nothing else: never by a client-supplied id.
        self.assertEqual([c for c in db.calls if c[0] == "eq"], [("eq", "workgroup_id", "wg-a")])

    def test_each_member_carries_exactly_three_keys(self):
        body = _run(ME, _FakeDB())
        self.assertEqual(set(body), {"workgroup_id", "members"})
        for m in body["members"]:
            self.assertEqual(set(m), {"full_name", "hf_username", "is_me"})
            self.assertNotIn("username", m)
            self.assertNotIn("user_id", m)
            self.assertNotIn("id", m)
        flat = repr(body)
        for login in ("lena.schmidt", "max.weber", "ali.yilmaz", "u-max", "u-ali"):
            self.assertNotIn(login, flat)

    def test_is_me_marks_the_caller_alone(self):
        body = _run(ME, _FakeDB())
        mine = [m for m in body["members"] if m["is_me"]]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["hf_username"], "lena-schmidt")
        others = {m["hf_username"] for m in body["members"] if not m["is_me"]}
        self.assertEqual(others, {"max-weber", None})

    def test_no_workgroup_is_the_caller_alone_and_asks_nothing(self):
        db = _FakeDB()
        solo = dict(ME, id="u-solo", full_name="Solo", hf_username="solo-hf", workgroup_id=None)
        body = _run(solo, db)
        self.assertEqual(
            body,
            {"workgroup_id": None, "members": [{"full_name": "Solo", "hf_username": "solo-hf", "is_me": True}]},
        )
        self.assertEqual(db.calls, [])

    def test_a_teacher_gets_themself(self):
        teacher = {"id": "t-1", "role": "teacher", "username": "frau.k", "full_name": "Frau K",
                   "hf_username": None, "workgroup_id": None, "training_credits": 0}
        body = _run(teacher, _FakeDB())
        self.assertEqual(body["members"], [{"full_name": "Frau K", "hf_username": None, "is_me": True}])

    def test_the_caller_is_kept_when_the_query_misses_their_row(self):
        rows = [r for r in USERS if r["id"] != "u-lena"]
        body = _run(ME, _FakeDB(rows=rows))
        self.assertTrue(body["members"][0]["is_me"])
        self.assertEqual(body["members"][0]["hf_username"], "lena-schmidt")
        self.assertEqual(sum(1 for m in body["members"] if m["is_me"]), 1)

    def test_a_query_parameter_naming_another_workgroup_is_ignored(self):
        # The handler accepts the JWT-resolved profile and nothing else: FastAPI
        # cannot bind ?workgroup_id=wg-b to any parameter, so it is dropped.
        params = list(inspect.signature(meroute.read_my_group_members).parameters)
        self.assertEqual(params, ["profile"])
        # And the profile's workgroup decides even when the caller's own row
        # carried a forged extra key.
        db = _FakeDB()
        body = _run(dict(ME, workgroup="wg-b", requested_workgroup_id="wg-b"), db)
        self.assertEqual(body["workgroup_id"], "wg-a")
        self.assertNotIn("Eve Andere", [m["full_name"] for m in body["members"]])
        self.assertNotIn(("eq", "workgroup_id", "wg-b"), db.calls)

    def test_a_database_error_is_a_german_500(self):
        with self.assertRaises(HTTPException) as cm:
            _run(ME, _FakeDB(fail=True))
        self.assertEqual(cm.exception.status_code, 500)
        self.assertEqual(cm.exception.detail, "Gruppenmitglieder konnten nicht geladen werden.")
        # The raw database text never reaches the client.
        self.assertNotIn("postgrest", str(cm.exception.detail))


def _main_literal(name):
    with open(MAIN_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=MAIN_PY)
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in main.py")


def _match(rules, method, path):
    """The middleware's matcher: longest prefix first, method-pinned, anchored."""
    for rule_method, prefix, limit, window in sorted(rules, key=lambda r: len(r[1]), reverse=True):
        if rule_method != "*" and rule_method != method:
            continue
        matched = path.startswith(prefix) if prefix.endswith("/") else (
            path == prefix or path.startswith(prefix + "/"))
        if matched:
            return (rule_method, prefix, limit, window)
    return None


class TestGroupMembersRateLimit(unittest.TestCase):
    def setUp(self):
        self.rules = _main_literal("_RATE_LIMIT_RULES")

    def test_the_rule_exists(self):
        self.assertIn(("GET", "/me/group-members", 30, 60.0), self.rules)
        self.assertEqual(
            _match(self.rules, "GET", "/me/group-members"), ("GET", "/me/group-members", 30, 60.0)
        )

    def test_the_rule_shadows_nothing(self):
        self.assertIsNone(_match(self.rules, "GET", "/me"))
        self.assertIsNone(_match(self.rules, "GET", "/me/group-membersX"))
        self.assertEqual(_match(self.rules, "GET", "/me/export"), ("GET", "/me/export", 3, 3600.0))

    def test_the_prefix_is_keyed_per_user(self):
        prefixes = _main_literal("_PER_USER_RATE_LIMIT_PREFIXES")
        self.assertIn("/me/group-members", prefixes)
        # The middleware's own test: path.startswith(p) for p in the tuple.
        self.assertTrue(any("/me/group-members".startswith(p) for p in prefixes))
        self.assertFalse(any("/me".startswith(p) for p in prefixes))


if __name__ == "__main__":
    unittest.main()
