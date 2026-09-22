"""Route tests for the code half of ``/workflows`` (Roboter Studio code
programs, migration 040): ``WorkflowCreate`` / ``WorkflowUpdate`` /
``WorkflowResponse`` carry ``code_language`` + ``code_files``.

Covers:
  * create with a language stores the files and an EMPTY blockly_json; a
    Blockly create's insert payload is byte-identical to before 040
  * one workflow is one language: a create or PATCH carrying blockly_json AND
    code is refused, and so is a Blockly write onto a code program
  * PATCH code_files goes through the SECURITY DEFINER RPC
    ``update_workflow_code`` (never a plain ``.update``), owner-only (404)
  * the language is immutable after create (409), and a Blockly program can
    never become a code program through PATCH
  * clone carries code_language + code_files (and tolerates a pre-040 source)
  * ``WorkflowResponse`` defaults tolerate a pre-040 row (source-level)
  * no rpc( call site beyond update_workflow_code was added (source-level)

House pattern: the superset stubs of ``test_dataset_identity_routes`` for
fastapi / pydantic / auth / supabase; the validators are NOT stubbed. The
generic fake supabase client below is shared by ``test_workflow_submissions``
and ``test_teacher_student_programs``.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

from app.tests.test_dataset_identity_routes import _ensure_stubs  # noqa: E402

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import workflows as wf  # noqa: E402
from app.validators.workflow import MAX_CODE_FILES  # noqa: E402

ROUTES_DIR = os.path.join(os.path.dirname(HERE), "routes")


# ------------------------------------------------------------------
# A generic stateful fake supabase client: every table is a dict of rows,
# every .eq()/.in_() filter is honoured on every table, ORDER BY … DESC + LIMIT
# are honoured (by insertion sequence), UPDATE/DELETE apply to every matched
# row. rpc('update_workflow_code') behaves like the migration's function:
# owner-only (P0002), language-checked (22023), then the two columns move.
# ------------------------------------------------------------------
def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "_seq"}


class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters: list[tuple] = []
        self._order_desc = False
        self._limit = None

    def select(self, cols="*", *a, **k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, ("__in__", vals)))
        return self

    def order(self, col=None, desc=False, **k):
        self._order_desc = bool(desc)
        return self

    def range(self, *a, **k):
        return self

    def limit(self, n=None, *a, **k):
        self._limit = n
        return self

    def single(self):
        return self

    def _match(self, row) -> bool:
        for col, val in self._filters:
            if isinstance(val, tuple) and val and val[0] == "__in__":
                if row.get(col) not in val[1]:
                    return False
            elif row.get(col) != val:
                return False
        return True

    def execute(self):
        db = self._db
        store = db.tables.setdefault(self._table, {})
        if self._op == "insert":
            rows_in = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in rows_in:
                row = db.add(self._table, dict(p))
                out.append(_public(row))
            return SimpleNamespace(data=out)
        rows = [r for r in store.values() if self._match(r)]
        if self._op == "select":
            rows = sorted(rows, key=lambda r: r["_seq"], reverse=self._order_desc)
            if self._limit is not None:
                rows = rows[: self._limit]
            return SimpleNamespace(data=[_public(r) for r in rows])
        if self._op == "update":
            db.plain_updates.append((self._table, dict(self._payload)))
            for r in rows:
                r.update(self._payload)
                r["updated_at"] = f"u{db.next_seq():06d}"
            return SimpleNamespace(data=[_public(r) for r in rows])
        if self._op == "delete":
            for r in rows:
                store.pop(r["id"], None)
            return SimpleNamespace(data=[])
        raise AssertionError(self._op)


class _FakeDB:
    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}
        self.rpc_calls: list[tuple] = []
        # Every plain .table().update() payload — a code write must never be
        # one of these (it goes through the SECURITY DEFINER RPC).
        self.plain_updates: list[tuple] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def add(self, table: str, row: dict) -> dict:
        """Seed / insert one row; fills id + timestamps like Postgres defaults."""
        seq = self.next_seq()
        row = dict(row)
        row.setdefault("id", f"{table[:2]}{seq}")
        row["_seq"] = seq
        row.setdefault("created_at", f"t{seq:06d}")
        row.setdefault("updated_at", f"t{seq:06d}")
        if table == "workflow_submissions":
            row.setdefault("submitted_at", f"t{seq:06d}")
        self.tables.setdefault(table, {})[row["id"]] = row
        return row

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, dict(params)))
        if name != "update_workflow_code":
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))

        def _execute():
            language = params.get("p_code_language")
            if language not in ("python", "java"):
                raise Exception("22023: Unbekannte Programmiersprache.")
            row = self.tables.get("workflows", {}).get(params["p_workflow_id"])
            if row is None or row.get("owner_user_id") != params["p_user_id"]:
                raise Exception("P0002: Workflow nicht gefunden oder kein Zugriff.")
            row["code_files"] = params["p_code_files"]
            row["code_language"] = language
            row["updated_at"] = f"u{self.next_seq():06d}"
            return SimpleNamespace(data=[_public(row)])

        return SimpleNamespace(execute=_execute)


class _Ctx:
    """Patch wf.get_supabase + wf.get_user_profile for the test duration."""

    def __init__(self, db, profile=None):
        self._db = db
        self._profile = profile or {"id": "owner", "workgroup_id": None}

    def __enter__(self):
        self._orig_sb = wf.get_supabase
        self._orig_pr = wf.get_user_profile
        wf.get_supabase = lambda: self._db
        wf.get_user_profile = lambda _uid: self._profile
        return self

    def __exit__(self, *exc):
        wf.get_supabase = self._orig_sb
        wf.get_user_profile = self._orig_pr


_OWNER = SimpleNamespace(id="owner")
_ATTACKER = SimpleNamespace(id="attacker")
_PY_FILES = {"main.py": "import robot\nrobot.home()\n", "lib/util.py": "def f():\n    return 1\n"}
_JAVA_FILES = {"Main.java": "public class Main { public static void main(String[] a) {} }"}


def _create_payload(**over):
    base = dict(
        name="Programm",
        description="",
        blockly_json={},
        classroom_id=None,
        share_with_group=False,
        sim_scene=None,
        code_language="",
        code_files=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _update_payload(**over):
    base = dict(
        name=None,
        description=None,
        blockly_json=None,
        share_with_group=None,
        sim_scene=None,
        code_language=None,
        code_files=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _refusal(fn, *args, **kwargs) -> HTTPException:
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        return exc
    raise AssertionError("expected a refusal")


# ==================================================================
# create
# ==================================================================
class TestCreateWithCode(unittest.TestCase):
    def test_create_with_python_stores_code_and_empty_blockly(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(
                _create_payload(code_language="python", code_files=_PY_FILES), user=_OWNER
            )
            fetched = wf.get_workflow(created.id, user=_OWNER)
        self.assertEqual(created.code_language, "python")
        self.assertEqual(created.code_files, _PY_FILES)
        self.assertEqual(created.blockly_json, {})
        self.assertEqual(created.owner_user_id, "owner")
        row = db.tables["workflows"][created.id]
        self.assertEqual((row["code_language"], row["code_files"]), ("python", _PY_FILES))
        self.assertEqual(fetched.code_files, _PY_FILES)

    def test_create_with_java(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(
                _create_payload(code_language="java", code_files=_JAVA_FILES), user=_OWNER
            )
        self.assertEqual((created.code_language, created.code_files), ("java", _JAVA_FILES))

    def test_a_blockly_create_sends_no_code_keys(self):
        # The pre-040 insert payload is unchanged for a Blockly program: the two
        # columns come from their Postgres defaults ('' / {}), never from here.
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(
                _create_payload(blockly_json={"blocks": {"blocks": []}}), user=_OWNER
            )
        row = db.tables["workflows"][created.id]
        self.assertNotIn("code_language", row)
        self.assertNotIn("code_files", row)

    def test_create_refuses_a_mixed_document(self):
        db = _FakeDB()
        with _Ctx(db):
            exc = _refusal(
                wf.create_workflow,
                _create_payload(
                    code_language="python", code_files=_PY_FILES,
                    blockly_json={"blocks": {"blocks": [{"type": "edubotics_log"}]}},
                ),
                user=_OWNER,
            )
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.tables.get("workflows", {}), {})

    def test_create_refuses_files_without_a_language(self):
        db = _FakeDB()
        with _Ctx(db):
            exc = _refusal(wf.create_workflow, _create_payload(code_files=_PY_FILES), user=_OWNER)
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.tables.get("workflows", {}), {})

    def test_create_refuses_a_language_without_files(self):
        db = _FakeDB()
        with _Ctx(db):
            exc = _refusal(wf.create_workflow, _create_payload(code_language="python"), user=_OWNER)
            self.assertEqual(exc.status_code, 400)
            exc = _refusal(
                wf.create_workflow, _create_payload(code_language="python", code_files={}), user=_OWNER
            )
            self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.tables.get("workflows", {}), {})

    def test_create_runs_the_file_validator(self):
        db = _FakeDB()
        too_many = dict(_PY_FILES, **{f"m{i}.py": "x = 1\n" for i in range(MAX_CODE_FILES)})
        with _Ctx(db):
            exc = _refusal(
                wf.create_workflow,
                _create_payload(code_language="python", code_files=too_many), user=_OWNER,
            )
            self.assertEqual(exc.status_code, 413)
            exc = _refusal(
                wf.create_workflow,
                _create_payload(code_language="python", code_files={"main.py": "x", "robot.py": "y"}),
                user=_OWNER,
            )
            self.assertEqual(exc.status_code, 400)
            exc = _refusal(
                wf.create_workflow,
                _create_payload(code_language="javascript", code_files={"main.js": "x"}),
                user=_OWNER,
            )
            self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.tables.get("workflows", {}), {})


# ==================================================================
# PATCH code_files
# ==================================================================
def _seed_python(db: _FakeDB, owner: str = "owner") -> dict:
    return db.add("workflows", {
        "owner_user_id": owner, "classroom_id": None, "workgroup_id": None,
        "name": "Programm", "description": "", "blockly_json": {}, "sim_scene": {},
        "is_template": False, "code_language": "python", "code_files": dict(_PY_FILES),
    })


def _seed_blockly(db: _FakeDB, owner: str = "owner") -> dict:
    return db.add("workflows", {
        "owner_user_id": owner, "classroom_id": None, "workgroup_id": None,
        "name": "Blöcke", "description": "", "blockly_json": {"blocks": {"blocks": []}},
        "sim_scene": {}, "is_template": False, "code_language": "", "code_files": {},
    })


class TestPatchCode(unittest.TestCase):
    def test_patch_code_goes_through_update_workflow_code_rpc(self):
        db = _FakeDB()
        row = _seed_python(db)
        new_files = {"main.py": "import robot\nrobot.open_gripper()\n"}
        with _Ctx(db):
            updated = wf.update_workflow(row["id"], _update_payload(code_files=new_files), user=_OWNER)
        self.assertEqual(db.rpc_calls, [(
            "update_workflow_code",
            {"p_workflow_id": row["id"], "p_user_id": "owner",
             "p_code_files": new_files, "p_code_language": "python"},
        )])
        # Never a plain owner-scoped .update() carrying the code (the RPC is
        # what stamps saved_by on the version snapshot).
        self.assertFalse([p for _t, p in db.plain_updates if "code_files" in p or "code_language" in p])
        self.assertEqual(updated.code_files, new_files)
        self.assertEqual(updated.code_language, "python")
        self.assertEqual(db.tables["workflows"][row["id"]]["code_files"], new_files)

    def test_patch_code_with_a_rename_applies_the_name_then_the_rpc(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            updated = wf.update_workflow(
                row["id"], _update_payload(name="Neu", code_files=_PY_FILES), user=_OWNER
            )
        self.assertEqual(updated.name, "Neu")
        self.assertEqual(updated.code_files, _PY_FILES)
        self.assertEqual([n for n, _p in db.rpc_calls], ["update_workflow_code"])
        self.assertEqual(db.plain_updates, [("workflows", {"name": "Neu"})])

    def test_patch_code_runs_the_file_validator(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            exc = _refusal(
                wf.update_workflow, row["id"],
                _update_payload(code_files={"main.py": "x", "Helper.java": "y"}), user=_OWNER,
            )
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.rpc_calls, [])

    def test_language_is_immutable_after_create(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            exc = _refusal(
                wf.update_workflow, row["id"],
                _update_payload(code_language="java", code_files=_JAVA_FILES), user=_OWNER,
            )
            self.assertEqual(exc.status_code, 409)
            exc = _refusal(wf.update_workflow, row["id"], _update_payload(code_language="java"), user=_OWNER)
            self.assertEqual(exc.status_code, 409)
            # Echoing the document's own language is fine.
            updated = wf.update_workflow(
                row["id"], _update_payload(code_language="python", code_files=_PY_FILES), user=_OWNER
            )
        self.assertEqual(updated.code_language, "python")
        self.assertEqual(db.tables["workflows"][row["id"]]["code_language"], "python")
        self.assertEqual([n for n, _p in db.rpc_calls], ["update_workflow_code"])

    def test_a_blockly_program_never_becomes_a_code_program_by_patch(self):
        db = _FakeDB()
        row = _seed_blockly(db)
        with _Ctx(db):
            exc = _refusal(wf.update_workflow, row["id"], _update_payload(code_files=_PY_FILES), user=_OWNER)
            self.assertEqual(exc.status_code, 409)
            exc = _refusal(
                wf.update_workflow, row["id"],
                _update_payload(code_language="python", code_files=_PY_FILES), user=_OWNER,
            )
            self.assertEqual(exc.status_code, 409)
        self.assertEqual(db.rpc_calls, [])
        self.assertEqual(db.tables["workflows"][row["id"]]["code_language"], "")

    def test_a_patch_mixing_blockly_and_code_is_refused(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            exc = _refusal(
                wf.update_workflow, row["id"],
                _update_payload(blockly_json={"blocks": {"blocks": []}}, code_files=_PY_FILES),
                user=_OWNER,
            )
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.rpc_calls, [])
        self.assertEqual(db.plain_updates, [])

    def test_a_blockly_write_onto_a_code_program_is_refused(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            exc = _refusal(
                wf.update_workflow, row["id"],
                _update_payload(blockly_json={"blocks": {"blocks": [{"type": "edubotics_log"}]}}),
                user=_OWNER,
            )
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(db.rpc_calls, [])
        self.assertEqual(db.tables["workflows"][row["id"]]["blockly_json"], {})

    def test_a_pre_040_row_without_the_column_is_a_blockly_program(self):
        # A row read before the migration's defaults exist has no code_language
        # key at all; it is a Blockly program and a Blockly PATCH still works.
        db = _FakeDB()
        row = db.add("workflows", {
            "owner_user_id": "owner", "classroom_id": None, "workgroup_id": None,
            "name": "Alt", "description": "", "blockly_json": {}, "is_template": False,
        })
        with _Ctx(db):
            updated = wf.update_workflow(
                row["id"], _update_payload(blockly_json={"blocks": {"blocks": []}}), user=_OWNER
            )
            exc = _refusal(wf.update_workflow, row["id"], _update_payload(code_files=_PY_FILES), user=_OWNER)
        self.assertEqual(updated.blockly_json, {})  # the RPC fake writes nothing for blockly
        self.assertEqual([n for n, _p in db.rpc_calls], ["update_workflow_blockly"])
        self.assertEqual(exc.status_code, 409)

    def test_patch_code_by_a_non_owner_is_404(self):
        db = _FakeDB()
        row = _seed_python(db)
        with _Ctx(db):
            exc = _refusal(wf.update_workflow, row["id"], _update_payload(code_files=_PY_FILES), user=_ATTACKER)
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(db.rpc_calls, [])
        self.assertEqual(db.tables["workflows"][row["id"]]["code_files"], _PY_FILES)

    def test_the_rpcs_own_ownership_refusal_is_a_404(self):
        # Belt and braces: if the RPC answers P0002 the route says 404, never 500.
        db = _FakeDB()
        row = _seed_python(db)
        original = wf._assert_workflow_owned
        wf._assert_workflow_owned = lambda _u, _w: dict(db.tables["workflows"][row["id"]])
        try:
            with _Ctx(db):
                exc = _refusal(wf.update_workflow, row["id"], _update_payload(code_files=_PY_FILES), user=_ATTACKER)
        finally:
            wf._assert_workflow_owned = original
        self.assertEqual(exc.status_code, 404)


# ==================================================================
# clone
# ==================================================================
class TestCloneCarriesCode(unittest.TestCase):
    def test_clone_carries_code(self):
        db = _FakeDB()
        src = _seed_python(db)
        with _Ctx(db):
            clone = wf.clone_workflow(src["id"], user=_OWNER)
        row = db.tables["workflows"][clone.id]
        self.assertNotEqual(clone.id, src["id"])
        self.assertEqual(row["owner_user_id"], "owner")
        self.assertEqual(row["name"], "Programm (Kopie)")
        self.assertEqual((row["code_language"], row["code_files"]), ("python", _PY_FILES))
        self.assertFalse(row["is_template"])

    def test_clone_of_a_pre_040_source_defaults_to_a_blockly_program(self):
        db = _FakeDB()
        src = db.add("workflows", {
            "owner_user_id": "owner", "classroom_id": None, "workgroup_id": None,
            "name": "Alt", "description": "", "blockly_json": {"blocks": {"blocks": []}},
            "is_template": False,
        })
        with _Ctx(db):
            clone = wf.clone_workflow(src["id"], user=_OWNER)
        row = db.tables["workflows"][clone.id]
        self.assertEqual((row["code_language"], row["code_files"]), ("", {}))


# ==================================================================
# source-level fences
# ==================================================================
def _class_def(path: str, name: str) -> ast.ClassDef:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _rpc_names(path: str) -> set[str]:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rpc"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            names.add(node.args[0].value)
    return names


class TestSourceFences(unittest.TestCase):
    def test_workflow_response_defaults_tolerate_a_pre_040_row(self):
        cls = _class_def(os.path.join(ROUTES_DIR, "workflows.py"), "WorkflowResponse")
        fields = {
            item.target.id: item.value
            for item in cls.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        self.assertIn("code_language", fields)
        self.assertIn("code_files", fields)
        self.assertEqual(ast.unparse(fields["code_language"]), "''")
        self.assertEqual(ast.unparse(fields["code_files"]), "Field(default_factory=dict)")

    def test_no_rpc_call_site_beyond_update_workflow_code(self):
        self.assertEqual(
            _rpc_names(os.path.join(ROUTES_DIR, "workflows.py")),
            {"update_workflow_blockly", "restore_workflow_version", "update_workflow_code"},
        )
        self.assertEqual(
            _rpc_names(os.path.join(ROUTES_DIR, "teacher.py")), {"adjust_student_credits"}
        )


if __name__ == "__main__":
    unittest.main()
