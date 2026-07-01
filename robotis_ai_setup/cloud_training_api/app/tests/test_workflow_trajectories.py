"""Route + validator tests for the Roboter Studio Batch-2b recorded replay
trajectories (``workflow_trajectories``, migration 034).

Covers:
  * validate_trajectory shape/size/finite gates (CONTRACT B points)
  * create -> list -> get -> get-by-name round-trip
  * create sets owner_user_id + workflow_id SERVER-SIDE (never from the body)
  * by-name returns the run-payload {fps, points}; newest-wins on a re-record
  * clone_workflow carries the source's trajectories under the new workflow_id
    + the cloner's ownership
  * IDOR: a non-owner cannot create / delete another user's trajectory (404),
    and cannot list a workflow they can't see (404)
  * the schema probe (main.py) lists "workflow_trajectories"

Follows the house pattern: stub fastapi + pydantic + the auth / supabase leaf
modules in sys.modules so the route module imports without real deps. The
validators are NOT stubbed — the real size/type/finite logic runs.
"""

from __future__ import annotations

import ast
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


HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import workflows as wf  # noqa: E402
from app.validators.workflow import (  # noqa: E402
    INT4_MAX,
    MAX_TRAJECTORY_JSON_BYTES,
    MAX_TRAJECTORY_POINTS,
    TRAJECTORY_FPS_MAX,
    validate_trajectory,
    validate_trajectory_metadata,
    validate_trajectory_name,
)


# ------------------------------------------------------------------
# A stateful fake supabase client (workflows + workflow_trajectories +
# users + rpc). Column projection is ignored (the fake returns whole
# rows); ordering by created_at DESC + limit ARE honoured so newest-wins
# and the clone cap behave.
# ------------------------------------------------------------------
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

    def _sorted_limited(self, rows):
        rows = sorted(rows, key=lambda r: r.get("_seq", 0), reverse=self._order_desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows

    def execute(self):
        db = self._db
        if self._table == "users":
            uid = next((v for c, v in self._filters if c == "id"), None)
            urow = db.users.get(uid)
            return SimpleNamespace(data=[urow] if urow is not None else [])
        if self._table == "workgroup_memberships":
            uid = next((v for c, v in self._filters if c == "user_id"), None)
            rows = [r for r in db.memberships if r.get("user_id") == uid]
            return SimpleNamespace(data=[dict(r) for r in rows])
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
            if self._op == "delete":
                for r in rows:
                    db.workflows.pop(r["id"], None)
                return SimpleNamespace(data=[])
        if self._table == "workflow_trajectories":
            if self._op == "insert":
                payload = self._payload
                rows_in = payload if isinstance(payload, list) else [payload]
                out = []
                for p in rows_in:
                    row = dict(p)
                    row["id"] = row.get("id") or db.new_traj_id()
                    row["_seq"] = db.next_seq()
                    row.setdefault("created_at", f"t{row['_seq']:06d}")
                    row.setdefault("updated_at", "t0")
                    db.trajectories[row["id"]] = row
                    out.append(_public(row))
                return SimpleNamespace(data=out)
            rows = [r for r in db.trajectories.values() if self._match(r)]
            if self._op == "select":
                rows = self._sorted_limited(rows)
                return SimpleNamespace(data=[_public(r) for r in rows])
            if self._op == "delete":
                for r in rows:
                    db.trajectories.pop(r["id"], None)
                return SimpleNamespace(data=[])
        return SimpleNamespace(data=[])


def _public(row: dict) -> dict:
    """Strip the fake-internal _seq key from a returned row."""
    return {k: v for k, v in row.items() if k != "_seq"}


class _FakeDB:
    def __init__(self):
        self.workflows: dict[str, dict] = {}
        self.trajectories: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.memberships: list[dict] = []
        self.rpc_calls: list[tuple] = []
        self._n = 0
        self._t = 0
        self._seq = 0

    def new_id(self) -> str:
        self._n += 1
        return f"wf{self._n}"

    def new_traj_id(self) -> str:
        self._t += 1
        return f"tr{self._t}"

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))


def _create_wf_payload(**over):
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


def _traj_payload(**over):
    base = dict(
        name="Bewegung1",
        fps=30.0,
        points=[[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.0], [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.033]],
        point_count=None,
        duration_s=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


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


# ==================================================================
# validate_trajectory
# ==================================================================
class TestValidateTrajectory(unittest.TestCase):
    def _pt(self, t=0.0):
        return [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, t]

    def test_valid_passes(self):
        validate_trajectory([self._pt(0.0), self._pt(0.033)], fps=30.0)

    def test_valid_without_fps_passes(self):
        validate_trajectory([self._pt()])

    def test_non_list_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory({"points": []})  # type: ignore[arg-type]
        self.assertEqual(cm.exception.status_code, 400)

    def test_empty_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([])
        self.assertEqual(cm.exception.status_code, 400)

    def test_too_many_points_rejected(self):
        many = [self._pt(i * 0.001) for i in range(MAX_TRAJECTORY_POINTS + 1)]
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory(many)
        self.assertEqual(cm.exception.status_code, 413)

    def test_oversize_rejected(self):
        # Small point count but each value a huge... no — values must be numbers.
        # Instead build enough points to exceed the byte cap but stay under the
        # 5000-point cap so the SIZE gate is what fires (413).
        # Each full-precision point is ~ >50 bytes; ~4000 points > 256 KB.
        n = (MAX_TRAJECTORY_JSON_BYTES // 40) + 10
        n = min(n, MAX_TRAJECTORY_POINTS)  # keep under the point cap
        big = [[-1.5707963267948966] * 7 for _ in range(n)]
        # If this doesn't exceed the byte cap at n<=5000, the test is a no-op;
        # assert the precondition so it fails loudly if the caps ever change.
        import json as _json

        self.assertGreater(len(_json.dumps(big).encode("utf-8")), MAX_TRAJECTORY_JSON_BYTES)
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory(big)
        self.assertEqual(cm.exception.status_code, 413)

    def test_wrong_point_length_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([[0.0, 0.1, 0.2]])  # only 3
        self.assertEqual(cm.exception.status_code, 400)

    def test_non_number_component_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([[0.0, 0.1, 0.2, 0.3, 0.4, "x", 0.0]])
        self.assertEqual(cm.exception.status_code, 400)

    def test_bool_component_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([[0.0, 0.1, 0.2, 0.3, 0.4, True, 0.0]])
        self.assertEqual(cm.exception.status_code, 400)

    def test_nan_component_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([[0.0, 0.1, 0.2, 0.3, 0.4, float("nan"), 0.0]])
        self.assertEqual(cm.exception.status_code, 400)

    def test_inf_component_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([[0.0, 0.1, 0.2, 0.3, 0.4, float("inf"), 0.0]])
        self.assertEqual(cm.exception.status_code, 400)

    def test_non_list_point_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory(["nope"])
        self.assertEqual(cm.exception.status_code, 400)

    def test_fps_zero_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([self._pt()], fps=0)
        self.assertEqual(cm.exception.status_code, 400)

    def test_fps_negative_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([self._pt()], fps=-5)
        self.assertEqual(cm.exception.status_code, 400)

    def test_fps_nan_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([self._pt()], fps=float("nan"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_fps_bool_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([self._pt()], fps=True)
        self.assertEqual(cm.exception.status_code, 400)

    def test_fps_too_high_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory([self._pt()], fps=TRAJECTORY_FPS_MAX + 1)
        self.assertEqual(cm.exception.status_code, 400)


# ==================================================================
# validate_trajectory_metadata (point_count / duration_s)
# ==================================================================
class TestValidateTrajectoryMetadata(unittest.TestCase):
    def test_both_none_passes(self):
        validate_trajectory_metadata(None, None)

    def test_valid_values_pass(self):
        validate_trajectory_metadata(42, 1.5)
        validate_trajectory_metadata(0, 0)
        validate_trajectory_metadata(INT4_MAX, 0.0)

    def test_point_count_overflow_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(INT4_MAX + 1, None)
        self.assertEqual(cm.exception.status_code, 400)

    def test_point_count_huge_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(99999999999, None)
        self.assertEqual(cm.exception.status_code, 400)

    def test_point_count_negative_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(-1, None)
        self.assertEqual(cm.exception.status_code, 400)

    def test_point_count_float_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(3.5, None)
        self.assertEqual(cm.exception.status_code, 400)

    def test_point_count_bool_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(True, None)
        self.assertEqual(cm.exception.status_code, 400)

    def test_duration_inf_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(None, float("inf"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_duration_nan_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(None, float("nan"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_duration_negative_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(None, -0.1)
        self.assertEqual(cm.exception.status_code, 400)

    def test_duration_non_number_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(None, "5")
        self.assertEqual(cm.exception.status_code, 400)

    def test_duration_bool_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_metadata(None, True)
        self.assertEqual(cm.exception.status_code, 400)


# ==================================================================
# validate_trajectory_name (CL-1: mirror the ROS backend + Blockly)
# ==================================================================
class TestValidateTrajectoryName(unittest.TestCase):
    def test_valid_name_passes_and_is_trimmed(self):
        # A leading/trailing space is trimmed; the trimmed form is returned.
        self.assertEqual(validate_trajectory_name("  Greifen  "), "Greifen")

    def test_valid_name_with_umlauts_and_symbols(self):
        self.assertEqual(
            validate_trajectory_name("Bewegung_Öl-1 Zurück"), "Bewegung_Öl-1 Zurück"
        )

    def test_valid_40_char_name_with_umlauts_accepted(self):
        # Exactly 40 chars of allowed charset (incl. umlauts) must pass.
        name = "ÄÖÜäöüß" + "a" * 33  # 7 + 33 = 40
        self.assertEqual(len(name), 40)
        self.assertEqual(validate_trajectory_name(name), name)

    def test_41_char_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("a" * 41)
        self.assertEqual(cm.exception.status_code, 400)

    def test_41_char_after_trim_rejected(self):
        # Whitespace is trimmed BEFORE the length check, so surrounding spaces
        # don't help a 41-char core sneak through.
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("   " + "a" * 41 + "   ")
        self.assertEqual(cm.exception.status_code, 400)

    def test_empty_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("")
        self.assertEqual(cm.exception.status_code, 400)

    def test_whitespace_only_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("     ")
        self.assertEqual(cm.exception.status_code, 400)

    def test_slash_name_rejected(self):
        # A `/` breaks the by-name path route; it is out of charset.
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("greif/en")
        self.assertEqual(cm.exception.status_code, 400)

    def test_emoji_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("Greifen 🚀")
        self.assertEqual(cm.exception.status_code, 400)

    def test_control_char_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name("Greif\nen")
        self.assertEqual(cm.exception.status_code, 400)

    def test_non_string_name_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            validate_trajectory_name(123)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.status_code, 400)


# ==================================================================
# routes
# ==================================================================
class TestTrajectoryRoutes(unittest.TestCase):
    def _seed_workflow(self, db, owner="owner", **over):
        with _Ctx(db):
            return wf.create_workflow(_create_wf_payload(**over), user=SimpleNamespace(id=owner))

    def test_create_persists_and_sets_owner_and_workflow(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            res = wf.create_trajectory(created_wf.id, _traj_payload(), user=_OWNER)
        # owner_user_id + workflow_id are server-side.
        self.assertEqual(res.owner_user_id, "owner")
        self.assertEqual(res.workflow_id, created_wf.id)
        # point_count falls back to len(points).
        self.assertEqual(res.point_count, 2)
        # samples stored as CONTRACT B.
        stored = db.trajectories[res.id]
        self.assertEqual(stored["samples"]["fps"], 30.0)
        self.assertEqual(len(stored["samples"]["points"]), 2)

    def test_create_ignores_body_owner_and_workflow_spoof(self):
        # The Pydantic model doesn't even carry owner/workflow — prove the
        # stored row keys on the PATH workflow + the JWT user, not any body.
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        payload = _traj_payload()
        payload.owner_user_id = "attacker"  # smuggled — must be ignored
        payload.workflow_id = "wfEVIL"       # smuggled — must be ignored
        with _Ctx(db):
            res = wf.create_trajectory(created_wf.id, payload, user=_OWNER)
        self.assertEqual(res.owner_user_id, "owner")
        self.assertEqual(res.workflow_id, created_wf.id)

    def test_create_idor_non_owner_404(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(created_wf.id, _traj_payload(), user=_ATTACKER)
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(db.trajectories, {})

    def test_create_invalid_points_400(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(
                    created_wf.id, _traj_payload(points=[[0.0, 0.1]]), user=_OWNER
                )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(db.trajectories, {})

    def test_create_invalid_name_400_no_row(self):
        # CL-1: an out-of-charset name (a `/`, which also breaks the by-name
        # path route) is rejected at the gate before any INSERT.
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(
                    created_wf.id, _traj_payload(name="greif/en"), user=_OWNER
                )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(db.trajectories, {})

    def test_create_whitespace_only_name_400_no_row(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(
                    created_wf.id, _traj_payload(name="   "), user=_OWNER
                )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(db.trajectories, {})

    def test_create_trims_stored_name(self):
        # CL-1: a surrounding-whitespace name is accepted but stored TRIMMED,
        # so a leading/trailing space never survives to Postgres.
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            res = wf.create_trajectory(
                created_wf.id, _traj_payload(name="  Greifen  "), user=_OWNER
            )
        self.assertEqual(res.name, "Greifen")
        self.assertEqual(db.trajectories[res.id]["name"], "Greifen")

    def test_create_overflow_point_count_400_no_row(self):
        # A body point_count past int4 range would overflow the INTEGER column
        # (uncaught 500). It is rejected at the gate before any INSERT.
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(
                    created_wf.id, _traj_payload(point_count=99999999999), user=_OWNER
                )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(db.trajectories, {})

    def test_create_inf_duration_400_no_row(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.create_trajectory(
                    created_wf.id, _traj_payload(duration_s=float("inf")), user=_OWNER
                )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(db.trajectories, {})

    def test_create_valid_metadata_preserved(self):
        # A valid in-range point_count + finite duration are stored verbatim.
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            res = wf.create_trajectory(
                created_wf.id,
                _traj_payload(point_count=2, duration_s=0.066),
                user=_OWNER,
            )
        self.assertEqual(res.point_count, 2)
        self.assertEqual(res.duration_s, 0.066)

    def test_list_returns_metadata_owner(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            wf.create_trajectory(created_wf.id, _traj_payload(name="A"), user=_OWNER)
            wf.create_trajectory(created_wf.id, _traj_payload(name="B"), user=_OWNER)
            rows = wf.list_trajectories(created_wf.id, user=_OWNER)
        self.assertEqual({r.name for r in rows}, {"A", "B"})
        # newest first (B created after A).
        self.assertEqual(rows[0].name, "B")

    def test_list_idor_non_owner_404(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            wf.create_trajectory(created_wf.id, _traj_payload(), user=_OWNER)
            with self.assertRaises(HTTPException) as cm:
                wf.list_trajectories(created_wf.id, user=_ATTACKER)
        self.assertEqual(cm.exception.status_code, 404)

    def test_get_by_name_returns_run_payload(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        pts = [[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.0]]
        with _Ctx(db):
            wf.create_trajectory(
                created_wf.id, _traj_payload(name="Greifen", points=pts, fps=25.0), user=_OWNER
            )
            got = wf.get_trajectory_by_name(created_wf.id, "Greifen", user=_OWNER)
        self.assertEqual(got.fps, 25.0)
        self.assertEqual(got.points, pts)

    def test_get_by_name_newest_wins(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        old_pts = [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
        new_pts = [[1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0]]
        with _Ctx(db):
            wf.create_trajectory(created_wf.id, _traj_payload(name="X", points=old_pts), user=_OWNER)
            wf.create_trajectory(created_wf.id, _traj_payload(name="X", points=new_pts), user=_OWNER)
            got = wf.get_trajectory_by_name(created_wf.id, "X", user=_OWNER)
        self.assertEqual(got.points, new_pts)

    def test_get_by_name_missing_404(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            with self.assertRaises(HTTPException) as cm:
                wf.get_trajectory_by_name(created_wf.id, "Fehlt", user=_OWNER)
        self.assertEqual(cm.exception.status_code, 404)

    def test_get_by_name_defaults_fps_when_all_null(self):
        # FIX 7: a legacy/edge row with fps NULL in BOTH the column and the samples
        # blob must not 500 — get_trajectory_by_name defaults fps to a safe value
        # rather than passing None to TrajectorySamples(fps: float).
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        pts = [[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.0]]
        # Seed a raw row bypassing create_trajectory so fps can be all-NULL.
        db.trajectories["trNull"] = {
            "id": "trNull",
            "workflow_id": created_wf.id,
            "owner_user_id": "owner",
            "name": "AltRow",
            "samples": {"points": pts},  # no 'fps' key inside the blob
            "fps": None,                 # column NULL too
            "_seq": db.next_seq(),
            "created_at": "t000001",
            "updated_at": "t0",
        }
        with _Ctx(db):
            got = wf.get_trajectory_by_name(created_wf.id, "AltRow", user=_OWNER)
        self.assertEqual(got.points, pts)
        self.assertIsInstance(got.fps, float)
        self.assertGreater(got.fps, 0.0)

    def test_get_single_includes_samples(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            created = wf.create_trajectory(created_wf.id, _traj_payload(), user=_OWNER)
            got = wf.get_trajectory(created_wf.id, created.id, user=_OWNER)
        self.assertEqual(got.samples["points"], db.trajectories[created.id]["samples"]["points"])

    def test_delete_owner_removes_row(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            created = wf.create_trajectory(created_wf.id, _traj_payload(), user=_OWNER)
            res = wf.delete_trajectory(created_wf.id, created.id, user=_OWNER)
        self.assertEqual(res, {"ok": True})
        self.assertEqual(db.trajectories, {})

    def test_delete_idor_non_owner_404_and_row_survives(self):
        db = _FakeDB()
        created_wf = self._seed_workflow(db)
        with _Ctx(db):
            created = wf.create_trajectory(created_wf.id, _traj_payload(), user=_OWNER)
            with self.assertRaises(HTTPException) as cm:
                wf.delete_trajectory(created_wf.id, created.id, user=_ATTACKER)
        self.assertEqual(cm.exception.status_code, 404)
        self.assertIn(created.id, db.trajectories)


class TestCloneCarriesTrajectories(unittest.TestCase):
    def test_clone_copies_trajectories_under_new_workflow(self):
        db = _FakeDB()
        with _Ctx(db):
            src = wf.create_workflow(_create_wf_payload(), user=_OWNER)
            wf.create_trajectory(src.id, _traj_payload(name="A"), user=_OWNER)
            wf.create_trajectory(src.id, _traj_payload(name="B"), user=_OWNER)
            clone = wf.clone_workflow(src.id, user=_OWNER)
            cloned_rows = wf.list_trajectories(clone.id, user=_OWNER)
        self.assertNotEqual(clone.id, src.id)
        self.assertEqual({r.name for r in cloned_rows}, {"A", "B"})
        # Each carried row keys on the NEW workflow + the cloner's ownership.
        for r in cloned_rows:
            self.assertEqual(r.workflow_id, clone.id)
            self.assertEqual(r.owner_user_id, "owner")
        # Source rows are untouched (still 2 under the source workflow).
        with _Ctx(db):
            src_rows = wf.list_trajectories(src.id, user=_OWNER)
        self.assertEqual(len(src_rows), 2)

    def test_clone_without_trajectories_ok(self):
        db = _FakeDB()
        with _Ctx(db):
            src = wf.create_workflow(_create_wf_payload(), user=_OWNER)
            clone = wf.clone_workflow(src.id, user=_OWNER)
            cloned_rows = wf.list_trajectories(clone.id, user=_OWNER)
        self.assertEqual(cloned_rows, [])


class TestSchemaProbeListsTable(unittest.TestCase):
    def test_main_required_tables_includes_workflow_trajectories(self):
        main_path = os.path.join(os.path.dirname(HERE), "main.py")
        with open(main_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"workflow_trajectories"', src)


def _extract_per_user_prefixes() -> tuple:
    """Parse main.py (without importing it — its module-level env/secret checks
    would need the full app deps) and return the literal value assigned to
    _PER_USER_RATE_LIMIT_PREFIXES."""
    main_path = os.path.join(os.path.dirname(HERE), "main.py")
    with open(main_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=main_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_PER_USER_RATE_LIMIT_PREFIXES"
                ):
                    return tuple(ast.literal_eval(node.value))
    raise AssertionError("_PER_USER_RATE_LIMIT_PREFIXES not found in main.py")


class TestWorkflowsRateLimitedPerUser(unittest.TestCase):
    """CL-2: POST /workflows (+ /clone, /versions/{id}/restore, /trajectories)
    share one prefix-matched rate bucket; keying it per-user (not per-IP) stops a
    NAT'd classroom from collectively blowing the shared IP bucket → spurious 429.
    Every /workflows route requires a JWT, so per-user keying has no downside."""

    def test_workflows_prefix_is_per_user(self):
        prefixes = _extract_per_user_prefixes()
        self.assertIn("/workflows", prefixes)

    def test_existing_per_user_prefixes_not_regressed(self):
        prefixes = _extract_per_user_prefixes()
        # The two pre-existing per-user prefixes must still be present.
        self.assertIn("/jetson/", prefixes)
        self.assertIn("/trainings/", prefixes)

    def test_workflows_prefix_has_no_trailing_slash(self):
        # A trailing slash would fail to match the bare `POST /workflows` create
        # path (`path.startswith("/workflows/")` is False for "/workflows").
        prefixes = _extract_per_user_prefixes()
        self.assertNotIn("/workflows/", prefixes)
        # Sanity: the no-slash prefix DOES cover both the bare create path and
        # the sub-resource POSTs via startswith.
        self.assertTrue("/workflows".startswith("/workflows"))
        self.assertTrue("/workflows/wf1/trajectories".startswith("/workflows"))

    def test_all_workflows_post_routes_require_jwt(self):
        # The safety precondition for CL-2: no /workflows POST is unauthenticated
        # (which would leave a no-JWT path IP-keyed with no bucket at all).
        # Confirm each POST handler declares the get_current_user dependency by
        # inspecting the route module source.
        wf_path = os.path.join(os.path.dirname(HERE), "routes", "workflows.py")
        with open(wf_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=wf_path)

        def _is_post(fn: ast.FunctionDef) -> bool:
            for deco in fn.decorator_list:
                # @router.post(...) — Call whose func is Attribute .post
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr == "post"
                ):
                    return True
            return False

        def _has_current_user_dep(fn: ast.FunctionDef) -> bool:
            for arg, default in zip(
                reversed(fn.args.args),
                reversed(fn.args.defaults),
            ):
                if (
                    isinstance(default, ast.Call)
                    and isinstance(default.func, ast.Name)
                    and default.func.id == "Depends"
                    and default.args
                    and isinstance(default.args[0], ast.Name)
                    and default.args[0].id == "get_current_user"
                ):
                    return True
            return False

        post_fns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and _is_post(n)
        ]
        # There are exactly the four documented POST routes.
        self.assertEqual(
            {fn.name for fn in post_fns},
            {
                "create_workflow",
                "clone_workflow",
                "restore_workflow_version",
                "create_trajectory",
            },
        )
        for fn in post_fns:
            self.assertTrue(
                _has_current_user_dep(fn),
                f"{fn.name} must require Depends(get_current_user)",
            )


if __name__ == "__main__":
    unittest.main()
