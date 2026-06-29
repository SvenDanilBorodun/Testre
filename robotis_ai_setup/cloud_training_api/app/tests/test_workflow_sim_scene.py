"""Route + validator tests for the Roboter Studio Phase-3 Sim-Szene
(``workflows.sim_scene``, migration 033).

Covers:
  * sim_scene round-trips create -> get -> update -> clone
  * a COMBINED blockly_json + sim_scene PATCH applies sim_scene via the plain
    owner-scoped update BEFORE the snapshot RPC (ordering + IDOR-safe)
  * a non-owner cannot write another user's sim_scene (IDOR -> 404)
  * the validate_sim_scene size/type gate
  * the schema probe (main.py) lists ("workflows", "sim_scene")

Follows the house pattern: stub fastapi + pydantic + the auth / supabase leaf
modules in sys.modules so the route module imports without real deps. The
validators are NOT stubbed — the real size/type logic runs.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from types import SimpleNamespace


def _ensure_stubs() -> None:
    import app  # noqa: F401
    import app.services  # noqa: F401

    if "app.auth" not in sys.modules:
        m = types.ModuleType("app.auth")
        m.get_current_profile = lambda *a, **kw: {}
        m.get_current_user = lambda *a, **kw: SimpleNamespace(id="u1")
        m.get_current_teacher = lambda *a, **kw: {}
        m.get_current_admin = lambda *a, **kw: {}
        m.get_user_profile = lambda _uid: {}
        sys.modules["app.auth"] = m
    if "app.services.supabase_client" not in sys.modules:
        m = types.ModuleType("app.services.supabase_client")
        m.get_supabase = lambda: None
        sys.modules["app.services.supabase_client"] = m

    if "fastapi" not in sys.modules:
        m = types.ModuleType("fastapi")

        class _Router:
            def __init__(self, *a, **k):
                pass

            def _deco(self, *a, **k):
                def d(fn):
                    return fn

                return d

            get = post = patch = delete = put = _deco

        class _HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail or "")
                self.status_code = status_code
                self.detail = detail

        m.APIRouter = _Router
        m.HTTPException = _HTTPException
        m.Depends = lambda *a, **k: None
        m.Query = lambda *a, **k: None
        m.Path = lambda *a, **k: None
        m.Body = lambda *a, **k: None
        m.Header = lambda *a, **k: None
        sys.modules["fastapi"] = m

    if "pydantic" not in sys.modules:
        m = types.ModuleType("pydantic")

        class _BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

            def model_dump(self):
                return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

        def _field(*_a, **_kw):
            return None

        def _field_validator(*_a, **_kw):
            def d(fn):
                return fn

            return d

        m.BaseModel = _BaseModel
        m.Field = _field
        m.field_validator = _field_validator
        sys.modules["pydantic"] = m


# Make cloud_training_api/app importable, then install stubs.
HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import workflows as wf  # noqa: E402
from app.validators.workflow import (  # noqa: E402
    MAX_SIM_SCENE_JSON_BYTES,
    validate_sim_scene,
)


# ------------------------------------------------------------------
# A stateful fake supabase client (workflows + users + rpc).
# ------------------------------------------------------------------
class _FakeQuery:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters: list[tuple] = []

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

    def order(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def limit(self, *a, **k):
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
        if self._table == "users":
            uid = next((v for c, v in self._filters if c == "id"), None)
            urow = db.users.get(uid)
            return SimpleNamespace(data=[urow] if urow is not None else [])
        if self._table == "workflows":
            if self._op == "insert":
                row = dict(self._payload)
                row.setdefault("id", db.new_id())
                row.setdefault("created_at", "t0")
                row.setdefault("updated_at", "t0")
                db.workflows[row["id"]] = row
                return SimpleNamespace(data=[dict(row)])
            rows = [r for r in db.workflows.values() if self._match(r)]
            if self._op == "select":
                return SimpleNamespace(data=[dict(r) for r in rows])
            if self._op == "update":
                if "sim_scene" in (self._payload or {}):
                    db.ops.append("sim_update")
                for r in rows:
                    r.update(self._payload)
                    r["updated_at"] = "t1"
                return SimpleNamespace(data=[dict(r) for r in rows])
            if self._op == "delete":
                for r in rows:
                    db.workflows.pop(r["id"], None)
                return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


class _FakeDB:
    def __init__(self):
        self.workflows: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.rpc_calls: list[tuple] = []
        self.ops: list[str] = []
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"wf{self._n}"

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        self.ops.append("rpc")
        db = self

        class _R:
            def execute(self):
                if name == "update_workflow_blockly":
                    row = db.workflows.get(params["p_workflow_id"])
                    if row is not None:
                        row["blockly_json"] = params["p_blockly_json"]
                        if params.get("p_name") is not None:
                            row["name"] = params["p_name"]
                        if params.get("p_description") is not None:
                            row["description"] = params["p_description"]
                        row["updated_at"] = "t1"
                return SimpleNamespace(data=None)

        return _R()


def _create_payload(**over):
    base = dict(
        name="W1",
        description="",
        blockly_json={"blocks": {"blocks": []}},
        classroom_id=None,
        share_with_group=False,
        sim_scene=None,
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
    )
    base.update(over)
    return SimpleNamespace(**base)


_SCENE = {"version": 1, "objects": [{"type": "wuerfel", "tag_id": 7, "x": 0.2, "y": 0.0, "yaw": 0.3}]}
_SCENE2 = {"version": 1, "objects": [{"type": "wuerfel", "tag_id": 9, "x": 0.15, "y": 0.05, "yaw": 0.0}]}


class _Ctx:
    """Patch wf.get_supabase + wf.get_user_profile for the duration of a test."""

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


class TestSimSceneRoundTrip(unittest.TestCase):
    def test_create_persists_scene(self):
        db = _FakeDB()
        with _Ctx(db):
            res = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
        self.assertEqual(res.sim_scene, _SCENE)
        # And it's actually stored.
        self.assertEqual(db.workflows[res.id]["sim_scene"], _SCENE)

    def test_create_without_scene_defaults_empty(self):
        db = _FakeDB()
        with _Ctx(db):
            res = wf.create_workflow(_create_payload(sim_scene=None), user=_OWNER)
        self.assertEqual(res.sim_scene, {})

    def test_get_round_trips_scene(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
            got = wf.get_workflow(created.id, user=_OWNER)
        self.assertEqual(got.sim_scene, _SCENE)

    def test_update_sim_only_owner_scoped(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
            updated = wf.update_workflow(
                created.id, _update_payload(sim_scene=_SCENE2), user=_OWNER
            )
        self.assertEqual(updated.sim_scene, _SCENE2)
        self.assertEqual(db.workflows[created.id]["sim_scene"], _SCENE2)
        # A sim-only PATCH does NOT route through the snapshot RPC.
        self.assertEqual(db.rpc_calls, [])

    def test_combined_blockly_and_sim_applies_sim_before_rpc(self):
        db = _FakeDB()
        new_blockly = {"blocks": {"blocks": [{"type": "edubotics_log"}]}}
        with _Ctx(db):
            created = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
            db.ops.clear()
            updated = wf.update_workflow(
                created.id,
                _update_payload(blockly_json=new_blockly, sim_scene=_SCENE2),
                user=_OWNER,
            )
        # Both writes landed.
        self.assertEqual(updated.sim_scene, _SCENE2)
        self.assertEqual(updated.blockly_json, new_blockly)
        # The blockly change went through the snapshot RPC.
        self.assertEqual([c[0] for c in db.rpc_calls], ["update_workflow_blockly"])
        # Ordering: the plain owner-scoped sim_scene write precedes the RPC.
        self.assertIn("sim_update", db.ops)
        self.assertIn("rpc", db.ops)
        self.assertLess(db.ops.index("sim_update"), db.ops.index("rpc"))

    def test_clone_copies_scene(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
            cloned = wf.clone_workflow(created.id, user=_OWNER)
        self.assertEqual(cloned.sim_scene, _SCENE)
        self.assertNotEqual(cloned.id, created.id)


class TestSimSceneIDOR(unittest.TestCase):
    def test_non_owner_cannot_write_sim_scene(self):
        db = _FakeDB()
        with _Ctx(db):
            created = wf.create_workflow(_create_payload(sim_scene=_SCENE), user=_OWNER)
            with self.assertRaises(HTTPException) as cm:
                wf.update_workflow(
                    created.id, _update_payload(sim_scene=_SCENE2), user=_ATTACKER
                )
        self.assertEqual(cm.exception.status_code, 404)
        # The owner's scene is byte-untouched.
        self.assertEqual(db.workflows[created.id]["sim_scene"], _SCENE)


class TestValidateSimScene(unittest.TestCase):
    def test_dict_passes(self):
        validate_sim_scene({})
        validate_sim_scene(_SCENE)

    def test_non_dict_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_sim_scene([1, 2, 3])  # type: ignore[arg-type]
        self.assertEqual(cm.exception.status_code, 400)

    def test_oversize_rejected(self):
        big = {"objects": [{"type": "x" * 100} for _ in range(MAX_SIM_SCENE_JSON_BYTES // 50)]}
        with self.assertRaises(HTTPException) as cm:
            validate_sim_scene(big)
        self.assertEqual(cm.exception.status_code, 413)


class TestSchemaProbeListsColumn(unittest.TestCase):
    def test_main_required_columns_includes_sim_scene(self):
        main_path = os.path.join(os.path.dirname(HERE), "main.py")
        with open(main_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('("workflows", "sim_scene")', src)


if __name__ == "__main__":
    unittest.main()
