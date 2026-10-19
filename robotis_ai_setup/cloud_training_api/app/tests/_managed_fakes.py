"""Test doubles for the managed-training tests (not a test module itself).

* ``Clock`` — one time source for the code under test and the fakes.
* ``FakeDB`` — supabase-py's query builder over three tables plus the 049
  RPCs, written to follow migration 049's SQL statement by statement (the SQL
  itself is proven by ``supabase/tests/049_managed_training_assertions.sql``).
  ``fail`` makes the next N reads or RPCs raise (outage tests).
* ``FakeR2`` — R2 objects in memory behind ``r2_client``'s function names.
* ``FakeRunpod`` — the RunPod REST v2 surface of spec §3.6 as an
  ``httpx.MockTransport`` handler, with programmable answers.
* ``FakeModal`` — ``modal_client``'s three managed calls.
* ``checkpoint_files`` — the bytes of a checkpoint built from the REAL LeRobot
  JSON fixtures (``fixtures/managed_training/omx_000010``), adapted to a row.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "managed_training")
SHA = "b1c3ecbae7f244acc039a3dbc255a00dad1372b9"


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def iso(self, t: float | None = None) -> str:
        return datetime.fromtimestamp(self.t if t is None else t, tz=timezone.utc).isoformat()

    def advance(self, s: float) -> None:
        self.t += s


class PgError(Exception):
    def __init__(self, code: str, msg: str = "x"):
        super().__init__({"code": code, "message": msg})
        self.code = code


# ---------------------------------------------------------------------------
# FakeDB
# ---------------------------------------------------------------------------

TRAINING_DEFAULTS = {
    "status": "queued", "managed": False, "current_attempt": 0, "train_seconds_used": 0, "status_note": None,
    "step_advanced_at": None, "dataset_fps": None, "unplaced_since": None, "last_progress_at": None,
    "total_steps": 0, "training_params": {}, "terminated_at": None, "log_url": None, "cancel_attempts": 0,
    "cancel_attempted_at": None, "current_step": 0, "worker_token": None, "cloud_job_id": None,
    "error_message": None, "loss_history": [], "workgroup_id": None, "current_loss": None, "model_type": "act",
}
ATTEMPT_DEFAULTS = {
    "backend": None, "cloud_ref": None, "gpu_type": None, "data_center": None, "cuda_version": None,
    "cost_per_hr": None, "state": "placing", "started_at": None, "first_heartbeat_at": None, "ended_at": None,
    "end_reason": None, "resume_from_step": None, "resume_from_attempt": None, "allowed_dcs": None,
    "publish_claimed_at": None, "publish_owner": None, "publish_first_at": None, "publish_tries": 0,
}
CHECKPOINT_DEFAULTS = {
    "state": "uploading", "ready_at": None, "mirrored_at": None, "mirror_tries": 0, "refused_reason": None,
    "refused_by": None, "crash_attempts": [], "mirror_claimed_at": None, "mirror_owner": None,
}
PKS = {"trainings": ("id",), "training_attempts": ("training_id", "attempt_no"),
       "training_checkpoints": ("training_id", "attempt_no", "step")}


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() if isinstance(v, str) else None


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = []
        self.cols = None
        self.op = "select"
        self.payload = None
        self._order = []
        self._limit = None
        self._range = None
        self._negate = False

    # chain ------------------------------------------------------------
    @property
    def not_(self):
        self._negate = True
        return self

    def _f(self, kind, col, val):
        self.filters.append((kind, col, val, self._negate))
        self._negate = False
        return self

    def select(self, cols="*", count=None):
        self.cols = cols
        return self

    def eq(self, c, v):
        return self._f("eq", c, v)

    def neq(self, c, v):
        return self._f("neq", c, v)

    def in_(self, c, v):
        return self._f("in", c, list(v))

    def is_(self, c, v):
        return self._f("is", c, v)

    def gte(self, c, v):
        return self._f("gte", c, v)

    def lt(self, c, v):
        return self._f("lt", c, v)

    def order(self, c, desc=False):
        self._order.append((c, desc))  # supabase-py: each call adds a key
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def update(self, payload):
        self.op, self.payload = "update", dict(payload)
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", dict(payload)
        return self

    def delete(self):
        self.op = "delete"
        return self

    # execute ----------------------------------------------------------
    def _match(self, row):
        for kind, col, val, neg in self.filters:
            cur = row.get(col)
            if kind == "eq":
                ok = cur == val
            elif kind == "neq":
                ok = cur != val
            elif kind == "in":
                ok = cur in val
            elif kind == "is":
                ok = cur is None if val == "null" else cur is val
            elif kind == "gte":
                ok = cur is not None and (_ts(cur) or cur) >= (_ts(val) or val)
            elif kind == "lt":
                ok = cur is not None and (_ts(cur) or cur) < (_ts(val) or val)
            else:
                raise AssertionError(kind)
            if neg:
                ok = not ok
            if not ok:
                return False
        return True

    def _project(self, row):
        if self.cols in (None, "*"):
            return copy.deepcopy(row)
        cols = [c.strip() for c in self.cols.split(",") if c.strip()]
        for c in cols:
            if c not in row:
                raise PgError("PGRST204", f"column {self.table}.{c} does not exist")
        return {c: copy.deepcopy(row[c]) for c in cols}

    def execute(self):
        db = self.db
        with db.lock:
            db.ops.append((self.op, self.table, list(self.filters), copy.deepcopy(self.payload)))
            db.maybe_fail(f"{self.op}:{self.table}")
            rows = db.tables[self.table]
            if self.op == "insert":
                row = db.new_row(self.table, self.payload)
                key = tuple(row[k] for k in PKS[self.table])
                if any(tuple(r[k] for k in PKS[self.table]) == key for r in rows):
                    raise PgError("23505", "duplicate key")
                rows.append(row)
                return SimpleNamespace(data=[copy.deepcopy(row)], count=1)
            hit = [r for r in rows if self._match(r)]
            if self.op == "update":
                for r in hit:
                    for k, v in self.payload.items():
                        if k not in r:
                            raise PgError("PGRST204", f"column {k} does not exist")
                        r[k] = copy.deepcopy(v)
                return SimpleNamespace(data=[copy.deepcopy(r) for r in hit], count=len(hit))
            if self.op == "delete":
                for r in hit:
                    rows.remove(r)
                return SimpleNamespace(data=[copy.deepcopy(r) for r in hit], count=len(hit))
            for col, desc in reversed(self._order):  # stable: the first key decides last
                hit = sorted(hit, key=lambda r, col=col: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._range is not None:
                hit = hit[self._range[0]: self._range[1] + 1]
            if self._limit is not None:
                hit = hit[: self._limit]
            if db.max_rows is not None:  # PostgREST's max_rows: a reply never carries more
                hit = hit[: db.max_rows]
            return SimpleNamespace(data=[self._project(r) for r in hit], count=len(hit))


class _Rpc:
    def __init__(self, db, name, args):
        self.db, self.name, self.args = db, name, dict(args)

    def execute(self):
        db = self.db
        with db.lock:
            db.rpcs.append((self.name, copy.deepcopy(self.args)))
            db.maybe_fail(f"rpc:{self.name}")
            fn = getattr(db, "_rpc_" + self.name)
            return SimpleNamespace(data=fn(**self.args))


class FakeDB:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.tables = {"trainings": [], "training_attempts": [], "training_checkpoints": []}
        self.ops: list = []
        self.rpcs: list = []
        self.failures: dict = {}
        self.max_rows = None
        self.lock = threading.RLock()
        self._next_id = 100
        self.credits: dict = {}

    # helpers ------------------------------------------------------------
    def maybe_fail(self, key):
        for pattern, n in list(self.failures.items()):
            if n > 0 and (pattern == "*" or key.startswith(pattern)):
                self.failures[pattern] = n - 1
                raise RuntimeError(f"injected failure on {key}")

    def now(self) -> str:
        return self.clock.iso()

    def new_row(self, table, payload):
        defaults = {"trainings": TRAINING_DEFAULTS, "training_attempts": ATTEMPT_DEFAULTS,
                    "training_checkpoints": CHECKPOINT_DEFAULTS}[table]
        row = copy.deepcopy(defaults)
        row.update(copy.deepcopy(payload))
        if table == "trainings" and "id" not in row:
            self._next_id += 1
            row["id"] = self._next_id
        if table in ("training_attempts", "training_checkpoints"):
            row.setdefault("created_at", self.now())
        if table == "trainings":
            row.setdefault("requested_at", self.now())
        return row

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, args):
        return _Rpc(self, name, args)

    def row(self, tid):
        return next((r for r in self.tables["trainings"] if r["id"] == tid), None)

    def attempt(self, tid, n):
        return next((a for a in self.tables["training_attempts"]
                     if a["training_id"] == tid and a["attempt_no"] == n), None)

    def checkpoint(self, tid, n, step):
        return next((c for c in self.tables["training_checkpoints"]
                     if c["training_id"] == tid and c["attempt_no"] == n and c["step"] == step), None)

    def add_training(self, **kw):
        kw.setdefault("user_id", "u-1")
        kw.setdefault("dataset_name", "u/ds")
        kw.setdefault("model_name", "EduBotics-Solutions/m-act-ds-abc")
        kw.setdefault("managed", True)
        params = kw.setdefault("training_params", {"steps": 50000, "save_freq": 2500, "mirror_every": 10000,
                                                   "batch_size": 8, "timeout_hours": 4.0})
        kw.setdefault("total_steps", params.get("steps", 0))
        row = self.new_row("trainings", kw)
        self.tables["trainings"].append(row)
        return row

    def add_attempt(self, tid, n, **kw):
        a = self.new_row("training_attempts", dict(kw, training_id=tid, attempt_no=n))
        self.tables["training_attempts"].append(a)
        return a

    def add_checkpoint(self, tid, n, step, **kw):
        c = self.new_row("training_checkpoints", dict(kw, training_id=tid, attempt_no=n, step=step))
        self.tables["training_checkpoints"].append(c)
        return c

    def calls(self, name):
        return [a for n, a in self.rpcs if n == name]

    # the 049 RPCs (statement by statement) ------------------------------------
    def _rpc_claim_training_attempt(self, p_training_id, p_expected_attempt):
        t = self.row(p_training_id)
        if t is None:
            raise PgError("P0002")
        if not t["managed"] or t["status"] != "queued" or t["current_attempt"] != p_expected_attempt:
            raise PgError("P0049")
        if p_expected_attempt > 0:
            a = self.attempt(p_training_id, p_expected_attempt)
            if a is None or a["state"] not in ("lost", "ended"):
                raise PgError("P0049")
        if self.attempt(p_training_id, p_expected_attempt + 1) is not None:
            raise PgError("23505")
        self.add_attempt(p_training_id, p_expected_attempt + 1, state="placing")
        token = str(uuid.uuid4())
        t["current_attempt"] = p_expected_attempt + 1
        t["worker_token"] = token
        return [{"o_attempt_no": p_expected_attempt + 1, "o_worker_token": token}]

    def _rpc_attach_training_attempt(self, p_training_id, p_attempt, p_backend, p_cloud_ref):
        if p_backend not in ("runpod_community", "runpod_secure", "modal") or not p_cloud_ref:
            raise PgError("22023")
        t = self.row(p_training_id)
        a = self.attempt(p_training_id, p_attempt)
        if not (t and a and a["cloud_ref"] is None and a["state"] in ("placing", "starting", "running")
                and t["managed"] and t["current_attempt"] == p_attempt and t["status"] in ("queued", "running")):
            raise PgError("P0049")
        if a["state"] == "placing":
            a["state"] = "starting"
        a["backend"], a["cloud_ref"] = p_backend, p_cloud_ref
        a["started_at"] = a["started_at"] or self.now()
        t["unplaced_since"] = None
        return None

    def _rpc_release_training_attempt(self, p_training_id, p_attempt, p_note):
        t = self.row(p_training_id)
        a = self.attempt(p_training_id, p_attempt)
        if not (t and a and a["state"] == "placing" and a["cloud_ref"] is None and t["managed"]
                and t["current_attempt"] == p_attempt and t["status"] == "queued"):
            raise PgError("P0049")
        self.tables["training_attempts"].remove(a)
        t["current_attempt"] = p_attempt - 1
        t["worker_token"] = None
        t["status_note"] = p_note if p_note is not None else t["status_note"]
        t["unplaced_since"] = t["unplaced_since"] or self.now()
        return None

    def _rpc_requeue_training(self, p_training_id, p_attempt, p_reason, p_note, p_resume_step, p_train_seconds):
        t = self.row(p_training_id)
        a = self.attempt(p_training_id, p_attempt)
        if not (t and a and a["state"] in ("placing", "starting", "running") and t["managed"]
                and t["current_attempt"] == p_attempt and t["status"] in ("queued", "running")):
            raise PgError("P0049")
        a.update(state="lost", ended_at=self.now(), end_reason=p_reason)
        resume = p_resume_step or 0
        hist = [e for e in t["loss_history"] or [] if int(e["s"]) <= resume]
        last = hist[-1].get("l") if hist else None
        t.update(status="queued", worker_token=None, status_note=p_note, current_step=resume, step_advanced_at=None,
                 loss_history=hist, current_loss=last if isinstance(last, (int, float)) else None)
        t["train_seconds_used"] += max(p_train_seconds or 0, 0)
        return None

    def _rpc_end_training_attempt(self, p_training_id, p_attempt, p_reason, p_note, p_train_seconds):
        t = self.row(p_training_id)
        a = self.attempt(p_training_id, p_attempt)
        if not (t and a and a["state"] in ("placing", "starting", "running", "lost") and t["managed"]
                and t["current_attempt"] == p_attempt and t["status"] in ("queued", "running")):
            raise PgError("P0049")
        a.update(state="ended", ended_at=self.now(), end_reason=p_reason)
        t.update(status="running", worker_token=None, status_note=p_note)
        t["train_seconds_used"] += max(p_train_seconds or 0, 0)
        return None

    def _rpc_finish_training(self, p_training_id, p_attempt, p_status, p_message, p_log_url):
        if p_status not in ("succeeded", "failed"):
            raise PgError("22023")
        t = self.row(p_training_id)
        if not (t and t["managed"] and t["current_attempt"] == p_attempt and t["status"] in ("queued", "running")):
            raise PgError("P0049")
        a = self.attempt(p_training_id, p_attempt)
        first = _ts(a["first_heartbeat_at"]) if a and a["state"] not in ("lost", "ended") else None
        t["train_seconds_used"] += max(int(self.clock.t - first), 0) if first is not None else 0
        t.update(status=p_status, terminated_at=self.now(), worker_token=None, status_note=None)
        if p_status == "failed":
            t["error_message"] = p_message
        t["log_url"] = p_log_url if p_log_url is not None else t["log_url"]
        if p_status == "succeeded":
            t["current_step"] = t["total_steps"]
        a = self.attempt(p_training_id, p_attempt)
        if a and a["state"] not in ("lost", "ended"):
            a.update(state="ended", ended_at=a["ended_at"] or self.now(), end_reason=a["end_reason"] or p_status)
        return None

    def _rpc_cancel_request_training(self, p_training_id, p_user_id):
        t = self.row(p_training_id)
        if t is None or t["user_id"] != p_user_id:
            raise PgError("P0002")
        if t["status"] not in ("queued", "running", "cancel_requested"):
            raise PgError("P0050")
        a = None
        if t["managed"] and t["current_attempt"] > 0:
            a = self.attempt(p_training_id, t["current_attempt"])
            if a and a["state"] == "ended" and a["end_reason"] == "succeeded":
                raise PgError("P0051")
        before = t["cancel_attempts"]
        t.update(status="cancel_requested", cancel_attempted_at=self.now(), cancel_attempts=before + 1)
        return [{"o_managed": t["managed"], "o_cloud_job_id": t["cloud_job_id"],
                 "o_current_attempt": t["current_attempt"], "o_attempt_state": a["state"] if a else None,
                 "o_backend": a["backend"] if a else None, "o_cloud_ref": a["cloud_ref"] if a else None,
                 "o_cancel_attempts": before + 1}]

    def _rpc_complete_cancel_training(self, p_training_id, p_final, p_message):
        if p_final not in ("canceled", "failed"):
            raise PgError("22023")
        t = self.row(p_training_id)
        if not (t and t["status"] == "cancel_requested"):
            raise PgError("P0049")
        t.update(status=p_final, terminated_at=self.now(), worker_token=None, status_note=None)
        if p_final == "failed":
            t["error_message"] = p_message
        a = self.attempt(p_training_id, t["current_attempt"])
        if a and a["state"] not in ("lost", "ended"):
            a.update(state="ended", ended_at=self.now(), end_reason="canceled")
        return None

    def _lease(self, holder, claimed_key, owner_key, p_lease_s, p_owner):
        claimed = _ts(holder[claimed_key])
        return claimed is None or holder[owner_key] == p_owner or claimed < self.clock.t - p_lease_s

    def _rpc_claim_training_publish(self, p_training_id, p_attempt, p_lease_s, p_owner):
        t = self.row(p_training_id)
        a = self.attempt(p_training_id, p_attempt)
        if not (t and a and a["state"] == "ended" and a["end_reason"] == "succeeded"
                and t["current_attempt"] == p_attempt and t["status"] == "running"):
            return False
        if not self._lease(a, "publish_claimed_at", "publish_owner", p_lease_s, p_owner):
            return False
        a.update(publish_claimed_at=self.now(), publish_owner=p_owner,
                 publish_first_at=a["publish_first_at"] or self.now())
        return True

    def _rpc_claim_checkpoint_mirror(self, p_training_id, p_attempt, p_step, p_lease_s, p_owner):
        c = self.checkpoint(p_training_id, p_attempt, p_step)
        if not (c and c["state"] == "ready" and c["mirrored_at"] is None):
            return False
        if not self._lease(c, "mirror_claimed_at", "mirror_owner", p_lease_s, p_owner):
            return False
        c.update(mirror_claimed_at=self.now(), mirror_owner=p_owner)
        return True

    def _rpc_update_training_progress(self, p_training_id, p_token, p_status=None, p_current_step=None,
                                      p_total_steps=None, p_current_loss=None, p_error_message=None, p_log_url=None):
        if p_status is not None and p_status not in ("queued", "running", "succeeded", "failed", "canceled"):
            raise PgError("22023")
        t = self.row(p_training_id)
        managed = bool(t and t["managed"])
        if managed and p_status is not None and p_status != "running":
            raise PgError("22023")
        if not (t and t["worker_token"] == p_token
                and t["status"] not in ("succeeded", "failed", "canceled", "cancel_requested")):
            raise PgError("P0001")
        if managed:  # V1-3: the API's range on a managed row's chart
            if p_current_step is not None:
                p_current_step = max(p_current_step, 0)
                if t["total_steps"] is not None:
                    p_current_step = min(p_current_step, t["total_steps"])
            if p_current_loss is not None and not (math.isfinite(p_current_loss) and abs(p_current_loss) <= 1e30):
                p_current_loss = None
        hist = list(t["loss_history"] or [])
        if p_current_step is not None and p_current_loss is not None:
            hist.append({"s": p_current_step, "l": p_current_loss, "t": int(self.clock.t * 1000)})
        prev_step = t["current_step"] or 0
        prev_status = t["status"]
        if p_status == "running" and p_current_step is not None and p_current_step < prev_step:
            hist = [e for e in hist if int(e["s"]) <= p_current_step]
        if t["step_advanced_at"] is None or p_status == "running" or (
                p_current_step is not None and p_current_step > prev_step):
            t["step_advanced_at"] = self.now()
        t["status"] = p_status or t["status"]
        t["current_step"] = p_current_step if p_current_step is not None else t["current_step"]
        if not managed:
            t["total_steps"] = p_total_steps if p_total_steps is not None else t["total_steps"]
            t["error_message"] = p_error_message if p_error_message is not None else t["error_message"]
        t["current_loss"] = p_current_loss if p_current_loss is not None else t["current_loss"]
        t["loss_history"] = hist
        t["last_progress_at"] = self.now()
        if managed and p_status == "running" and prev_status == "queued":
            t["status_note"] = None
        if managed and p_status == "running":
            a = self.attempt(p_training_id, t["current_attempt"])
            if a and a["state"] in ("placing", "starting"):
                a["state"] = "running"
                a["first_heartbeat_at"] = a["first_heartbeat_at"] or self.now()
        return None

    def _rpc_start_training_safe(self, p_user_id, p_dataset_name, p_model_name, p_model_type, p_training_params,
                                 p_total_steps, p_worker_token, p_managed=False):
        credits = self.credits.get(p_user_id, 100)
        used = sum(1 for r in self.tables["trainings"]
                   if r["user_id"] == p_user_id and r["status"] not in ("failed", "canceled"))
        if used >= credits:
            raise PgError("P0003", "No training credits remaining")
        row = self.add_training(user_id=p_user_id, dataset_name=p_dataset_name, model_name=p_model_name,
                                model_type=p_model_type, training_params=copy.deepcopy(p_training_params),
                                total_steps=p_total_steps, worker_token=p_worker_token, managed=bool(p_managed),
                                status="queued")
        return [{"training_id": row["id"], "remaining": credits - used - 1}]


# ---------------------------------------------------------------------------
# FakeR2
# ---------------------------------------------------------------------------


class FakeR2:
    def __init__(self):
        self.objects: dict = {}
        self.deleted: list = []
        self.presigned: list = []
        self.fail: dict = {}

    def _maybe_fail(self, op):
        n = self.fail.get(op, 0)
        if n:
            self.fail[op] = n - 1
            raise RuntimeError(f"r2 {op} failed")

    def presign_put(self, key, size, md5_b64):
        self._maybe_fail("presign")
        self.presigned.append(("put", key, size, md5_b64))
        return (f"https://r2.test/{key}?X-Amz-SignedHeaders=content-length%3Bcontent-md5%3Bcontent-type%3Bhost",
                {"Content-Type": "application/octet-stream", "Content-Length": str(int(size)), "Content-MD5": md5_b64})

    def presign_get(self, key):
        self.presigned.append(("get", key))
        return f"https://r2.test/{key}?get"

    def head_size(self, key):
        from app.services import r2_client

        self._maybe_fail("head")
        if key not in self.objects:
            raise r2_client.R2Missing(key)
        return len(self.objects[key])

    def exists(self, key):
        return key in self.objects

    def get_bytes(self, key, max_bytes):
        from app.services import r2_client

        self._maybe_fail("get")
        if key not in self.objects:
            raise r2_client.R2Missing(key)
        return self.objects[key][: max_bytes + 1]

    def stream_to_file(self, key, path, should_continue=None):
        from app.services import r2_client

        self._maybe_fail("get")
        if key not in self.objects:
            raise r2_client.R2Missing(key)
        data = self.objects[key]
        with open(path, "wb") as fh:
            fh.write(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def stream_sha256(self, key):
        from app.services import r2_client

        self._maybe_fail("get")
        if key not in self.objects:
            raise r2_client.R2Missing(key)
        data = self.objects[key]
        return len(data), hashlib.sha256(data).hexdigest()

    def delete_keys(self, keys):
        self._maybe_fail("delete")
        for k in keys:
            self.deleted.append(k)
            self.objects.pop(k, None)

    @contextmanager
    def installed(self):
        from app.services import r2_client

        with ExitStack() as st:
            for name in ("presign_put", "presign_get", "head_size", "exists", "get_bytes", "stream_to_file",
                         "stream_sha256", "delete_keys"):
                st.enter_context(patch.object(r2_client, name, getattr(self, name)))
            yield self

    def put_checkpoint(self, tid, attempt, step, files: dict):
        from app.services import r2_client

        for rel, data in files.items():
            self.objects[r2_client.ckpt_key(tid, attempt, step, rel)] = data


# ---------------------------------------------------------------------------
# FakeRunpod (an httpx.MockTransport handler)
# ---------------------------------------------------------------------------


def catalog_entry(gpu_id, dcs, availability="HIGH"):
    return {"id": gpu_id, "name": gpu_id, "memory": 24, "price": {"secure": 0.3, "community": 0.2},
            "availability": availability, "dataCenters": [{"id": d, "name": d, "availability": availability}
                                                          for d in dcs], "cudaVersions": ["12.8"]}


class FakeRunpod:
    def __init__(self):
        self.calls: list = []
        self.pods: dict = {}
        self.catalog = {"COMMUNITY": [], "SECURE": []}
        self.gdpr = ["EU-RO-1", "EU-SE-1", "EUR-IS-1"]
        self.regions = {"EU-RO-1": "EUROPE", "EU-SE-1": "EUROPE", "EUR-IS-1": "EUROPE", "EU-CZ-1": "EUROPE",
                        "US-KS-2": "NORTH_AMERICA", "CA-MTL-1": "NORTH_AMERICA", "XX-1": "UNKNOWN"}
        self.create_answers: list = []      # [(status, body|None, headers)] consumed in order, then 201
        self.delete_answers: dict = {}      # pod id -> [status ...]
        self.list_status = 200
        self.probe_status = 200
        self.catalog_status = 200
        self.dc_status = 200
        self.page_size = None
        self._n = 0

    def add_pod(self, **kw):
        self._n += 1
        pid = kw.pop("id", f"pod{self._n}")
        pod = {"id": pid, "name": f"pod-{pid}", "image": "other/image", "env": {}, "status": "RUNNING",
               "cloud": "COMMUNITY", "dataCenterId": None, "cudaVersion": None, "cost": 0.2,
               "createdAt": "2030-01-01T00:00:00+00:00", "locked": False}
        pod.update(kw)
        self.pods[pid] = pod
        return pod

    def handler(self, request):
        import httpx

        path = request.url.path
        method = request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, dict(request.url.params), body))
        if method == "GET" and path == "/v2/catalog/gpus":
            if self.catalog_status != 200:
                return httpx.Response(self.catalog_status, json={"detail": "x"})
            return httpx.Response(200, json={"gpus": self.catalog[request.url.params["cloud"]]})
        if method == "GET" and path == "/v2/catalog/datacenters":
            if self.dc_status != 200:
                return httpx.Response(self.dc_status, json={"detail": "x"})
            if request.url.params.get("compliance") == "GDPR":
                return httpx.Response(200, json={"dataCenters": [{"id": d, "region": "EUROPE"} for d in self.gdpr]})
            return httpx.Response(200, json={"dataCenters": [{"id": d, "region": r} for d, r in self.regions.items()]})
        if method == "GET" and path == "/v2/pods":
            if request.url.params.get("limit") == "1":
                return httpx.Response(self.probe_status, json={"pods": [], "pagination": {"nextCursor": None,
                                                                                         "hasNextPage": False}})
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"detail": "x"})
            pods = list(self.pods.values())
            if self.page_size:
                start = int(request.url.params.get("cursor") or 0)
                page = pods[start:start + self.page_size]
                nxt = start + self.page_size
                more = nxt < len(pods)
                return httpx.Response(200, json={"pods": page, "pagination": {
                    "nextCursor": str(nxt) if more else None, "hasNextPage": more}})
            return httpx.Response(200, json={"pods": pods, "pagination": {"nextCursor": None, "hasNextPage": False}})
        if method == "POST" and path == "/v2/pods":
            if self.create_answers:
                status, rbody, headers = self.create_answers.pop(0)
                if status == "network":
                    raise httpx.ConnectError("boom")
                if status != 201:
                    return httpx.Response(status, json=rbody or {"title": "x", "status": status, "detail": f"d{status}"},
                                          headers=headers or {})
            pod = self.add_pod(name=body["name"], image=body["image"], env=body["env"], cloud=body["cloud"],
                               status="PROVISIONING", gpu={"id": body["gpu"]["id"], "count": 1},
                               dataCenterId=None, cudaVersion=None)
            return httpx.Response(201, json=pod)
        m = re.fullmatch(r"/v2/pods/([^/]+)", path)
        if m:
            pid = m.group(1)
            if method == "DELETE":
                answers = self.delete_answers.get(pid)
                status = answers.pop(0) if answers else (204 if pid in self.pods else 404)
                if status in (204, 404):
                    if pid in self.pods and not self.pods[pid].get("locked"):
                        self.pods.pop(pid)
                    elif pid in self.pods and self.pods[pid].get("locked"):
                        return httpx.Response(409, json={"detail": "locked"})
                return httpx.Response(status) if status == 204 else httpx.Response(status, json={"detail": "x"})
            if method == "PATCH":
                if pid in self.pods:
                    self.pods[pid].update(body or {})
                    return httpx.Response(200, json=self.pods[pid])
                return httpx.Response(404, json={"detail": "x"})
            if method == "GET":
                if pid in self.pods:
                    return httpx.Response(200, json=self.pods[pid])
                return httpx.Response(404, json={"detail": "x"})
        return httpx.Response(404, json={"detail": "no route"})

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1] == "/v2/pods"]

    def deletes(self):
        return [c[1].rsplit("/", 1)[1] for c in self.calls if c[0] == "DELETE"]

    def patches(self):
        return [(c[1].rsplit("/", 1)[1], c[3]) for c in self.calls if c[0] == "PATCH"]

    @contextmanager
    def installed(self):
        import httpx

        from app.services import runpod_client

        runpod_client.set_transport_for_tests(httpx.MockTransport(self.handler))
        try:
            yield self
        finally:
            runpod_client.set_transport_for_tests(None)


# ---------------------------------------------------------------------------
# FakeModal
# ---------------------------------------------------------------------------


class FakeModal:
    def __init__(self):
        self.spawned: list = []
        self.canceled: list = []
        self.status: dict = {}
        self.spawn_raises = None
        self.cancel_raises = None
        self._n = 0

    async def start_managed_training_job(self, **kw):
        if self.spawn_raises:
            raise self.spawn_raises
        self._n += 1
        self.spawned.append(kw)
        return f"fc-{self._n}"

    async def cancel_training_job(self, call_id):
        self.canceled.append(call_id)
        if self.cancel_raises:
            raise self.cancel_raises
        return True

    async def get_job_status(self, call_id):
        return self.status.get(call_id, "IN_PROGRESS")

    @contextmanager
    def installed(self):
        from app.services import modal_client

        with ExitStack() as st:
            for name in ("start_managed_training_job", "cancel_training_job", "get_job_status"):
                st.enter_context(patch.object(modal_client, name, getattr(self, name), create=True))
            yield self


# ---------------------------------------------------------------------------
# Checkpoint bytes from the real fixtures
# ---------------------------------------------------------------------------


def _fixture(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as fh:
        return json.load(fh)


def checkpoint_docs(row, step, *, final=False, device="cuda", resume_from=None) -> dict:
    """The JSON documents of a checkpoint LeRobot 0.5.1 would write for this row."""
    from app.services import artifact_validation as AV

    params = row["training_params"]
    total = params["steps"]
    cfg = _fixture("omx_000010", "pretrained_model", "config.json")
    tc = _fixture("omx_000010", "pretrained_model", "train_config.json")
    out = AV.output_dir_for(row["model_name"])
    cfg["device"] = device
    tc["dataset"].update(repo_id=row["dataset_name"], root=None, revision=params.get("dataset_revision"))
    tc.update(output_dir=out, steps=total, save_freq=params["save_freq"], log_freq=params.get("log_freq", 200),
              seed=params.get("seed", 1000), num_workers=7)
    if resume_from is not None:
        d = AV.step_id(resume_from, total)
        tc.update(resume=True, checkpoint_path=f"{out}/checkpoints/{d}")
        cfg["pretrained_path"] = f"{out}/checkpoints/{d}/pretrained_model"
    tc["policy"] = copy.deepcopy(cfg)
    docs = {
        "pretrained_model/config.json": cfg,
        "pretrained_model/train_config.json": tc,
        "pretrained_model/policy_preprocessor.json": _fixture("omx_000010", "pretrained_model", "policy_preprocessor.json"),
        "pretrained_model/policy_postprocessor.json": _fixture("omx_000010", "pretrained_model", "policy_postprocessor.json"),
    }
    if not final:
        docs["training_state/training_step.json"] = {"step": step}
        docs["training_state/optimizer_param_groups.json"] = _fixture("omx_000010", "training_state",
                                                                      "optimizer_param_groups.json")
    return docs


def checkpoint_files(row, step, *, final=False, **kw) -> dict:
    """relpath -> bytes for every file of the checkpoint (safetensors are filler)."""
    from app.services import artifact_validation as AV

    docs = checkpoint_docs(row, step, final=final, **kw)
    files = {rel: json.dumps(doc, indent=4).encode() for rel, doc in docs.items()}
    for rel in (AV.FINAL_RELPATHS if final else AV.CHECKPOINT_RELPATHS):
        if rel not in files:
            files[rel] = hashlib.sha256(f"{rel}:{step}".encode()).digest() * 4
    return files


def manifest_of(files: dict) -> dict:
    return {rel: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest(),
                  "md5": base64.b64encode(hashlib.md5(b).digest()).decode()} for rel, b in files.items()}
