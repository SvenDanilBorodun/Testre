"""Route tests for the teacher's read of a student's Roboter Studio programs
and „Abgeben" snapshots (§3.12, migration 040):

  GET /teacher/students/{sid}/workflows
  GET /teacher/students/{sid}/workflows/{wid}
  GET /teacher/students/{sid}/submissions
  GET /teacher/students/{sid}/submissions/{subid}

Rule §4 under a service-role client: every route calls
``_assert_student_owned`` FIRST and scopes its query on
``owner_user_id`` / ``student_user_id``. ``workflows.classroom_id`` is
client-supplied at create with no membership check, so it is never a filter
here — a source-level fence asserts the four bodies contain no
``"classroom_id"`` at all. A foreign student is a German 404, never 403.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

from app.tests.test_workflow_code import _PY_FILES, _FakeDB, _refusal  # noqa: E402
from app.routes import teacher  # noqa: E402

TEACHER_PY = os.path.join(os.path.dirname(HERE), "routes", "teacher.py")
_NEW_ROUTES = (
    "list_student_workflows",
    "get_student_workflow",
    "list_student_submissions",
    "get_student_submission",
)
_T1 = {"id": "t1"}
_T2 = {"id": "t2"}


def _seed(db: _FakeDB) -> dict:
    """Two teachers, two classrooms, two students — and the trap: rows owned
    by s2 whose classroom_id says c1."""
    db.add("users", {"id": "t1", "role": "teacher", "classroom_id": None})
    db.add("users", {"id": "t2", "role": "teacher", "classroom_id": None})
    db.add("users", {"id": "s1", "role": "student", "classroom_id": "c1"})
    db.add("users", {"id": "s2", "role": "student", "classroom_id": "c2"})
    db.add("users", {"id": "s3", "role": "student", "classroom_id": None})
    db.add("classrooms", {"id": "c1", "teacher_id": "t1", "name": "7b"})
    db.add("classrooms", {"id": "c2", "teacher_id": "t2", "name": "9c"})
    rows = {}
    rows["w_s1"] = db.add("workflows", {
        "owner_user_id": "s1", "classroom_id": "c1", "workgroup_id": None,
        "name": "Mein Programm", "description": "Python", "blockly_json": {},
        "sim_scene": {}, "is_template": False,
        "code_language": "python", "code_files": dict(_PY_FILES),
    })
    rows["w_s1_blocks"] = db.add("workflows", {
        "owner_user_id": "s1", "classroom_id": "c1", "workgroup_id": None,
        "name": "Blöcke", "description": "", "blockly_json": {"blocks": {"blocks": []}},
        "sim_scene": {}, "is_template": False, "code_language": "", "code_files": {},
    })
    rows["template_c1"] = db.add("workflows", {
        "owner_user_id": "t1", "classroom_id": "c1", "workgroup_id": None,
        "name": "Vorlage", "description": "", "blockly_json": {}, "sim_scene": {},
        "is_template": True, "code_language": "", "code_files": {},
    })
    # THE TRAP: s2's program claims classroom c1 (the column is client-set).
    rows["w_s2_claims_c1"] = db.add("workflows", {
        "owner_user_id": "s2", "classroom_id": "c1", "workgroup_id": None,
        "name": "Fremd", "description": "", "blockly_json": {}, "sim_scene": {},
        "is_template": False, "code_language": "java", "code_files": {"Main.java": "x"},
    })
    rows["sub_s1"] = db.add("workflow_submissions", {
        "workflow_id": rows["w_s1"]["id"], "student_user_id": "s1", "classroom_id": "c1",
        "name": "Mein Programm", "code_language": "python", "code_files": dict(_PY_FILES),
        "blockly_json": {}, "sim_scene": {}, "note": "Abgabe 1",
    })
    rows["sub_s2_claims_c1"] = db.add("workflow_submissions", {
        "workflow_id": rows["w_s2_claims_c1"]["id"], "student_user_id": "s2", "classroom_id": "c1",
        "name": "Fremd", "code_language": "java", "code_files": {"Main.java": "x"},
        "blockly_json": {}, "sim_scene": {}, "note": "",
    })
    return rows


class _Ctx:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        self._orig = teacher.get_supabase
        teacher.get_supabase = lambda: self._db
        return self

    def __exit__(self, *exc):
        teacher.get_supabase = self._orig


def _run(coro):
    return asyncio.run(coro)


def _refusal_async(fn, *args, **kwargs):
    return _refusal(lambda *a, **k: _run(fn(*a, **k)), *args, **kwargs)


class TestForeignStudentIs404(unittest.TestCase):
    def test_a_foreign_students_programs_are_404_not_403(self):
        db = _FakeDB()
        rows = _seed(db)
        with _Ctx(db):
            for student in ("s2", "s3", "t1", "nobody"):
                for fn, args in (
                    (teacher.list_student_workflows, (student,)),
                    (teacher.get_student_workflow, (student, rows["w_s2_claims_c1"]["id"])),
                    (teacher.list_student_submissions, (student,)),
                    (teacher.get_student_submission, (student, rows["sub_s2_claims_c1"]["id"])),
                ):
                    exc = _refusal_async(fn, *args, teacher=_T1)
                    self.assertEqual(exc.status_code, 404, (fn.__name__, student))
                    self.assertNotEqual(exc.status_code, 403)
            # And the other teacher cannot read s1 either.
            exc = _refusal_async(teacher.list_student_workflows, "s1", teacher=_T2)
            self.assertEqual(exc.status_code, 404)


class TestListStudentWorkflows(unittest.TestCase):
    def test_list_returns_only_the_students_own_non_template_workflows(self):
        db = _FakeDB()
        rows = _seed(db)
        with _Ctx(db):
            out = _run(teacher.list_student_workflows("s1", teacher=_T1))
        self.assertEqual(
            {r.id for r in out}, {rows["w_s1"]["id"], rows["w_s1_blocks"]["id"]}
        )
        # Newest first; summaries carry the language (the document stays out
        # through the SELECT projection — fenced in TestSourceFences).
        self.assertEqual([r.id for r in out], [rows["w_s1_blocks"]["id"], rows["w_s1"]["id"]])
        self.assertEqual({r.code_language for r in out}, {"python", ""})

    def test_the_trap_row_is_not_listed_for_the_classroom_it_claims(self):
        db = _FakeDB()
        rows = _seed(db)
        with _Ctx(db):
            out = _run(teacher.list_student_workflows("s1", teacher=_T1))
        self.assertNotIn(rows["w_s2_claims_c1"]["id"], {r.id for r in out})
        self.assertNotIn(rows["template_c1"]["id"], {r.id for r in out})


class TestGetStudentWorkflow(unittest.TestCase):
    def test_get_document_is_scoped_to_the_student(self):
        db = _FakeDB()
        rows = _seed(db)
        with _Ctx(db):
            doc = _run(teacher.get_student_workflow("s1", rows["w_s1"]["id"], teacher=_T1))
            self.assertEqual(doc.code_files, _PY_FILES)
            self.assertEqual(doc.code_language, "python")
            self.assertEqual(doc.name, "Mein Programm")
            for foreign in (rows["w_s2_claims_c1"]["id"], rows["template_c1"]["id"], "missing"):
                exc = _refusal_async(teacher.get_student_workflow, "s1", foreign, teacher=_T1)
                self.assertEqual(exc.status_code, 404, foreign)


class TestStudentSubmissions(unittest.TestCase):
    def test_submissions_list_and_get_are_scoped_to_the_student(self):
        db = _FakeDB()
        rows = _seed(db)
        with _Ctx(db):
            listed = _run(teacher.list_student_submissions("s1", teacher=_T1))
            self.assertEqual([r.id for r in listed], [rows["sub_s1"]["id"]])
            self.assertEqual(listed[0].note, "Abgabe 1")
            one = _run(teacher.get_student_submission("s1", rows["sub_s1"]["id"], teacher=_T1))
            self.assertEqual(one.code_files, _PY_FILES)
            self.assertEqual(one.workflow_id, rows["w_s1"]["id"])
            exc = _refusal_async(
                teacher.get_student_submission, "s1", rows["sub_s2_claims_c1"]["id"], teacher=_T1
            )
            self.assertEqual(exc.status_code, 404)


# ==================================================================
# source-level fences
# ==================================================================
def _route_defs() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    with open(TEACHER_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=TEACHER_PY)
    found = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in _NEW_ROUTES
    }
    assert set(found) == set(_NEW_ROUTES), set(_NEW_ROUTES) - set(found)
    return found


def _body_without_docstring(fn) -> list[ast.stmt]:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


class TestSourceFences(unittest.TestCase):
    def test_every_new_route_calls_assert_student_owned_first(self):
        for name, fn in _route_defs().items():
            first = _body_without_docstring(fn)[0]
            self.assertIsInstance(first, ast.Expr, name)
            call = first.value
            self.assertIsInstance(call, ast.Call, name)
            self.assertEqual(ast.unparse(call.func), "_assert_student_owned", name)
            self.assertEqual(
                [ast.unparse(a) for a in call.args], ["teacher['id']", "student_id"], name
            )

    def test_the_list_never_filters_on_workflows_classroom_id(self):
        for name, fn in _route_defs().items():
            constants = {
                n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            }
            self.assertNotIn("classroom_id", constants, name)
            for c in constants:
                self.assertNotIn("classroom", c.lower(), (name, c))

    def test_every_new_route_scopes_its_query_on_the_student(self):
        expected = {
            "list_student_workflows": "owner_user_id",
            "get_student_workflow": "owner_user_id",
            "list_student_submissions": "student_user_id",
            "get_student_submission": "student_user_id",
        }
        for name, fn in _route_defs().items():
            eq_filters = {
                (ast.unparse(n.args[0]), ast.unparse(n.args[1]))
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "eq" and len(n.args) == 2
            }
            self.assertIn((repr(expected[name]), "student_id"), eq_filters, name)

    def test_every_new_route_requires_the_teacher_dependency(self):
        for name, fn in _route_defs().items():
            defaults = [ast.unparse(d) for d in fn.args.defaults]
            self.assertIn("Depends(get_current_teacher)", defaults, name)

    def test_the_two_lists_project_summaries_and_the_two_gets_the_document(self):
        # A listing stays light through the SELECT projection (PostgREST honours
        # it; the fake above does not, so this is asserted at the source).
        defs = _route_defs()
        for name in ("list_student_workflows", "list_student_submissions"):
            columns = [
                n.args[0].value for n in ast.walk(defs[name])
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "select" and n.args and isinstance(n.args[0], ast.Constant)
            ]
            self.assertEqual(len(columns), 1, name)
            self.assertNotEqual(columns[0], "*", name)
            for heavy in ("code_files", "blockly_json", "sim_scene"):
                self.assertNotIn(heavy, columns[0], (name, heavy))
        for name in ("get_student_workflow", "get_student_submission"):
            selects = [
                n.args[0].value for n in ast.walk(defs[name])
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "select" and n.args and isinstance(n.args[0], ast.Constant)
            ]
            self.assertEqual(selects, ["*"], name)


if __name__ == "__main__":
    unittest.main()
