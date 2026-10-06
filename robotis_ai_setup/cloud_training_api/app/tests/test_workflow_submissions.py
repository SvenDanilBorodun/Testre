"""Route tests for „Abgeben" (``workflow_submissions``, migration 040):
``POST /workflows/{id}/submit`` and ``GET /workflows/{id}/submissions``.

Covers:
  * submit snapshots name / language / code_files / blockly_json / sim_scene
    from the ROW and stamps student_user_id + classroom_id SERVER-SIDE —
    classroom_id from users.classroom_id, never from the body and never from
    workflows.classroom_id (a client-supplied column with no membership check)
  * an absent body is an empty note; the note is capped at 500 chars
  * a non-owner cannot submit (404) or list (404)
  * the list is the caller's own rows, newest first, without the documents
  * ``_RATE_LIMIT_RULES`` is unchanged: the POST /workflows 10/min rule already
    covers ``/submit`` by prefix, and no narrower POST rule shadows it
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

from app.tests.test_workflow_code import (  # noqa: E402
    _ATTACKER,
    _OWNER,
    _PY_FILES,
    _Ctx,
    _FakeDB,
    _refusal,
)
from app.routes import workflows as wf  # noqa: E402

MAIN_PY = os.path.join(os.path.dirname(HERE), "main.py")


def _seed(db: _FakeDB) -> dict:
    db.add("users", {"id": "owner", "role": "student", "classroom_id": "klasse-7b"})
    db.add("users", {"id": "attacker", "role": "student", "classroom_id": "klasse-9c"})
    # The workflow's OWN classroom_id is deliberately a different value: it is
    # client-supplied at create and must never be what the submission carries.
    return db.add("workflows", {
        "owner_user_id": "owner", "classroom_id": "vom-client-gesetzt", "workgroup_id": None,
        "name": "Mein Programm", "description": "", "blockly_json": {},
        "sim_scene": {"objects": []}, "is_template": False,
        "code_language": "python", "code_files": dict(_PY_FILES),
    })


class TestSubmit(unittest.TestCase):
    def test_submit_stamps_student_and_classroom_server_side_and_ignores_body_values(self):
        db = _FakeDB()
        row = _seed(db)
        hostile = SimpleNamespace(
            note="Bitte anschauen",
            student_user_id="attacker", classroom_id="klasse-9c", workflow_id="other",
            name="gefälscht", code_language="java", code_files={"Main.java": "x"},
        )
        with _Ctx(db):
            sub = wf.submit_workflow(row["id"], hostile, user=_OWNER)
        stored = db.tables["workflow_submissions"][sub.id]
        self.assertEqual(stored["student_user_id"], "owner")
        self.assertEqual(stored["classroom_id"], "klasse-7b")
        self.assertEqual(stored["workflow_id"], row["id"])
        self.assertEqual(stored["name"], "Mein Programm")
        self.assertEqual(stored["code_language"], "python")
        self.assertEqual(stored["code_files"], _PY_FILES)
        self.assertEqual(stored["blockly_json"], {})
        self.assertEqual(stored["sim_scene"], {"objects": []})
        self.assertEqual(stored["note"], "Bitte anschauen")
        self.assertEqual(sub.student_user_id, "owner")
        self.assertEqual(sub.code_files, _PY_FILES)

    def test_submit_without_a_body_is_an_empty_note(self):
        db = _FakeDB()
        row = _seed(db)
        with _Ctx(db):
            sub = wf.submit_workflow(row["id"], None, user=_OWNER)
        self.assertEqual(db.tables["workflow_submissions"][sub.id]["note"], "")

    def test_submit_note_is_capped(self):
        db = _FakeDB()
        row = _seed(db)
        with _Ctx(db):
            wf.submit_workflow(row["id"], SimpleNamespace(note="x" * 500), user=_OWNER)
            exc = _refusal(wf.submit_workflow, row["id"], SimpleNamespace(note="x" * 501), user=_OWNER)
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(len(db.tables["workflow_submissions"]), 1)

    def test_submit_of_a_pre_040_row_snapshots_the_defaults(self):
        db = _FakeDB()
        db.add("users", {"id": "owner", "role": "student", "classroom_id": None})
        row = db.add("workflows", {
            "owner_user_id": "owner", "classroom_id": None, "workgroup_id": None,
            "name": "Alt", "description": "", "blockly_json": {"blocks": {"blocks": []}},
            "is_template": False,
        })
        with _Ctx(db):
            sub = wf.submit_workflow(row["id"], None, user=_OWNER)
        stored = db.tables["workflow_submissions"][sub.id]
        self.assertEqual((stored["code_language"], stored["code_files"], stored["sim_scene"]), ("", {}, {}))
        self.assertIsNone(stored["classroom_id"])

    def test_submit_by_a_non_owner_is_404(self):
        db = _FakeDB()
        row = _seed(db)
        with _Ctx(db):
            exc = _refusal(wf.submit_workflow, row["id"], None, user=_ATTACKER)
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(db.tables.get("workflow_submissions", {}), {})


class TestListSubmissions(unittest.TestCase):
    def test_list_is_own_rows_newest_first_without_the_documents(self):
        db = _FakeDB()
        row = _seed(db)
        with _Ctx(db):
            first = wf.submit_workflow(row["id"], SimpleNamespace(note="eins"), user=_OWNER)
            second = wf.submit_workflow(row["id"], SimpleNamespace(note="zwei"), user=_OWNER)
            rows = wf.list_submissions(row["id"], user=_OWNER)
        self.assertEqual([r.id for r in rows], [second.id, first.id])
        self.assertEqual([r.note for r in rows], ["zwei", "eins"])
        for r in rows:
            self.assertEqual(r.student_user_id, "owner")
            self.assertEqual(r.code_language, "python")
            self.assertIsNone(r.code_files)
            self.assertIsNone(r.blockly_json)
            self.assertIsNone(r.sim_scene)

    def test_list_is_scoped_to_the_caller_even_under_the_same_workflow(self):
        # Only the owner can submit, so a foreign row under this workflow cannot
        # exist through the API; seed one directly to prove the query is keyed on
        # student_user_id and not only on workflow_id.
        db = _FakeDB()
        row = _seed(db)
        db.add("workflow_submissions", {
            "workflow_id": row["id"], "student_user_id": "attacker", "classroom_id": "klasse-9c",
            "name": "fremd", "code_language": "", "code_files": {}, "blockly_json": {},
            "sim_scene": {}, "note": "",
        })
        with _Ctx(db):
            mine = wf.submit_workflow(row["id"], None, user=_OWNER)
            rows = wf.list_submissions(row["id"], user=_OWNER)
        self.assertEqual([r.id for r in rows], [mine.id])

    def test_list_by_a_non_owner_is_404(self):
        db = _FakeDB()
        row = _seed(db)
        with _Ctx(db):
            wf.submit_workflow(row["id"], None, user=_OWNER)
            exc = _refusal(wf.list_submissions, row["id"], user=_ATTACKER)
        self.assertEqual(exc.status_code, 404)


class TestRateLimitRulesUnchanged(unittest.TestCase):
    """POST /workflows/{id}/submit needs no rule of its own: the existing
    ``("POST", "/workflows", 10, 60.0)`` prefix rule covers it (create, clone,
    restore, trajectory upload AND submit share one per-user 10/min budget).
    The whole table is pinned so a new rule is a deliberate edit here too."""

    _EXPECTED = [
        ("*", "/trainings/start", 10, 60.0),
        ("*", "/trainings/cancel", 20, 60.0),
        ("POST", "/workflows", 10, 60.0),
        ("PATCH", "/workflows", 30, 60.0),
        ("POST", "/teacher/classrooms", 10, 60.0),
        ("POST", "/teacher/workgroups", 20, 60.0),
        ("POST", "/datasets", 20, 60.0),
        ("POST", "/jetson/register", 5, 60.0),
        ("POST", "/jetson/", 30, 60.0),
        ("PATCH", "/me/tutorial-progress", 30, 60.0),
        ("GET", "/me/export", 3, 3600.0),
        # 042 — per-student Hugging Face token, appended in this order.
        ("GET", "/me/hf-token", 60, 60.0),
        ("PUT", "/me/hf-token", 6, 60.0),
        ("DELETE", "/me/hf-token", 10, 60.0),
        ("POST", "/me/hf-token/reveal", 20, 60.0),
        ("POST", "/me/hf-token/verify", 6, 60.0),
        ("GET", "/me/group-members", 30, 60.0),
    ]

    def _rules(self) -> list:
        with open(MAIN_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=MAIN_PY)
        rules = [
            ast.literal_eval(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "_RATE_LIMIT_RULES"
        ]
        self.assertEqual(len(rules), 1, "_RATE_LIMIT_RULES not found in main.py")
        return rules[0]

    def test_the_rule_table_is_unchanged(self):
        self.assertEqual(self._rules(), self._EXPECTED)

    def test_the_workflows_post_rule_is_the_longest_prefix_matching_submit(self):
        path = "/workflows/wf1/submit"
        matching = [
            r for r in self._rules()
            if r[0] in ("*", "POST") and path.startswith(r[1])
        ]
        self.assertEqual(
            max(matching, key=lambda r: len(r[1])), ("POST", "/workflows", 10, 60.0)
        )


if __name__ == "__main__":
    unittest.main()
