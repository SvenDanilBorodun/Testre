"""The managed-training worker API (spec §3.2, §4.5, §4.6): every action, every answer.

The handler is driven directly (fastapi stubbed, house pattern) against the
FakeDB (049's functions), the in-memory R2 and the fake RunPod. Pinned: every
401 condition answers the same body for every action; the reaction-table codes
(409 already_ready / already_refused / not_uploading / no_final_checkpoint /
wrong_image / budget_exhausted, 422 bad_manifest / ckpt_refused); a content
refusal fails the row with S7 at once while a size/sha refusal does not (FR-5);
ckpt-bad's ignored/refused/kept; resume-crash counting; finish never stores host
text (E11); the lock hash (SR-18) and the handler hash (V2-1); budget_s minus the
attempt's own time (FR-18).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.routes import training_worker as route  # noqa: E402
from app.services import artifact_validation as AV  # noqa: E402
from app.services import hf_publish  # noqa: E402
from app.services import r2_client  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import (  # noqa: E402
    SHA, Clock, FakeDB, FakeModal, FakeR2, FakeRunpod, checkpoint_files, manifest_of,
)
from app.tests.test_training_dispatch import settings  # noqa: E402

LOCK = "c" * 64
HANDLER = "e" * 64
# The real accessors (WorkerBase patches both).
_REAL_LOCK_ACCESSOR = route._expected_lock_sha256
_REAL_HANDLER_ACCESSOR = route._expected_handler_sha256
TOKEN = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


class FakeRequest:
    def __init__(self, token=TOKEN, body=None, raw=None, auth=None):
        self.headers = {"authorization": auth if auth is not None else f"Bearer {token}"}
        self._raw = raw if raw is not None else json.dumps(body if body is not None else {}).encode()

    async def body(self):
        return self._raw


def content(resp):
    if hasattr(resp, "body"):
        return json.loads(resp.body)
    return resp.content


class WorkerBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        D.reset_state_for_tests()
        hf_publish.reset_for_tests()
        route.reset_hash_state_for_tests()
        self.clock = Clock()
        self.db = FakeDB(self.clock)
        self.r2 = FakeR2()
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        self.s = settings()
        self.published, self.mirrors, self.logs = [], [], []
        self._cms = [
            patch.object(D, "_now", self.clock), patch.object(D, "_sb", lambda: self.db),
            patch.object(D, "SETTINGS", self.s),
            patch.object(route, "_expected_lock_sha256", lambda: LOCK),
            patch.object(route, "_expected_handler_sha256", lambda: HANDLER),
            patch.object(hf_publish, "schedule_publish", lambda t, n: self.published.append((t, n)) or True),
            patch.object(hf_publish, "schedule_mirror", lambda t, n, st: self.mirrors.append((t, n, st)) or True),
            patch.object(hf_publish, "schedule_failed_log", lambda row: self.logs.append(row["id"]) or True),
            self.r2.installed(), self.rp.installed(), self.modal.installed(),
        ]
        for cm in self._cms:
            cm.__enter__()
        self.row = self.db.add_training(status="running", current_attempt=1, worker_token=TOKEN,
                                        dataset_name="u/ds", model_name="EduBotics-Solutions/m-act-ds-abc",
                                        training_params={"steps": 50000, "save_freq": 2500, "mirror_every": 10000,
                                                         "batch_size": 8, "dataset_revision": SHA},
                                        dataset_fps=30.0, requested_at=self.clock.iso(self.clock.t - 3600))
        self.tid = self.row["id"]
        self.att = self.db.add_attempt(self.tid, 1, state="running", backend="runpod_community", cloud_ref="pod1",
                                       first_heartbeat_at=self.clock.iso(self.clock.t - 1800),
                                       data_center="EU-RO-1", resume_from_step=0)
        self.rp.add_pod(id="pod1", status="RUNNING")

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def call(self, action, body=None, *, tid=None, attempt="1", **req):
        # Every `start` body carries the handler hash an honest worker sends (V2-1), unless the
        # test names it; a test of its ABSENCE sends raw bytes.
        if action == "start" and isinstance(body, dict) and "handler_sha256" not in body:
            body = {**body, "handler_sha256": HANDLER}
        resp = await route.worker_action(str(self.tid if tid is None else tid), attempt, action,
                                         FakeRequest(body=body, **req))
        return resp.status_code, content(resp)

    def upload(self, step, files=None, *, attempt=1, final=False):
        files = files if files is not None else checkpoint_files(self.db.row(self.tid), step, final=final)
        self.r2.put_checkpoint(self.tid, attempt, step, files)
        return files


class Authorization(WorkerBase):
    BODIES = {"start": {"lock_sha256": LOCK, "build": "abc"}, "upload-url": {"kind": "log", "files": {}},
              "ckpt-done": {"step": 2500}, "ckpt-bad": {"step": 2500, "reason": "sha256"},
              "finish": {"outcome": "succeeded"}}

    async def test_every_condition_answers_the_same_401_for_every_action(self):
        conditions = {
            "no header": dict(auth=""),
            "not bearer": dict(auth=f"Token {TOKEN}"),
            "not a uuid": dict(auth="Bearer not-a-uuid"),
            "upper-case uuid": dict(auth=f"Bearer {TOKEN.upper()}"),
            "other token": dict(token="11111111-2222-4333-8444-555555555555"),
        }
        for action, body in self.BODIES.items():
            for name, req in conditions.items():
                with self.subTest(action=action, condition=name):
                    self.assertEqual(await self.call(action, body, **req), (401, {"detail": T.S10, "code": "unauthorized"}))
            for name, mutate in (("not managed", {"managed": False}), ("succeeded", {"status": "succeeded"}),
                                 ("cancel_requested", {"status": "cancel_requested"}),
                                 ("other attempt", {"current_attempt": 2})):
                saved = dict(self.db.row(self.tid))
                self.db.row(self.tid).update(mutate)
                with self.subTest(action=action, condition=name):
                    self.assertEqual(await self.call(action, body), (401, {"detail": T.S10, "code": "unauthorized"}))
                self.db.row(self.tid).update(saved)
            for state in ("lost", "ended"):
                self.att["state"] = state
                with self.subTest(action=action, condition=state):
                    self.assertEqual((await self.call(action, body))[0], 401)
                self.att["state"] = "running"
            with self.subTest(action=action, condition="no row"):
                self.assertEqual((await self.call(action, body, tid=999))[0], 401)
            for bad_id in ("0", "abc", "-1", "1e3"):
                with self.subTest(action=action, id=bad_id):
                    self.assertEqual((await self.call(action, body, tid=bad_id))[0], 401)

    async def test_placing_and_starting_attempts_are_authorised(self):
        for state in ("placing", "starting"):
            self.att["state"] = state
            status, _ = await self.call("start", self.BODIES["start"])
            self.assertEqual(status, 200, state)

    async def test_unknown_action_and_bad_bodies(self):
        self.assertEqual((await self.call("delete-everything", {}))[0], 404)
        self.assertEqual((await self.call("start", raw=b"not json"))[0], 422)
        self.assertEqual((await self.call("start", raw=b"[1,2]"))[0], 422)
        self.assertEqual((await self.call("start", raw=b"{" + b" " * (route.MAX_BODY_BYTES + 1) + b"}"))[0], 422)
        self.assertEqual((await self.call("start", {"lock_sha256": "x", "build": "b"}))[0], 422)
        self.assertEqual((await self.call("start", {"lock_sha256": LOCK, "build": "b" * 65}))[0], 422)

    async def test_a_database_fault_is_503_never_401(self):
        self.db.failures["select:trainings"] = 1
        self.assertEqual((await self.call("start", self.BODIES["start"]))[0], 503)


class Start(WorkerBase):
    async def test_a_fresh_start(self):
        status, body = await self.call("start", {"lock_sha256": LOCK, "build": "git-abc"})
        self.assertEqual(status, 200)
        self.assertEqual(body["spec"], {
            "dataset_name": "u/ds", "model_name": "EduBotics-Solutions/m-act-ds-abc", "model_type": "act",
            "training_params": self.row["training_params"], "total_steps": 50000, "save_freq": 2500,
            "mirror_every": 10000, "dataset_revision": SHA,
            "output_dir": "/tmp/training_output/EduBotics-Solutions_m-act-ds-abc"})
        self.assertIsNone(body["resume"])
        self.assertIs(body["finished"], False)
        # FR-18: minus the current attempt's own 1800 s
        self.assertEqual(body["budget_s"], D.MANAGED_ACT_CAP_S - 0 - 1800)
        self.assertEqual((self.att["resume_from_step"], self.att["resume_from_attempt"]), (0, None))

    async def test_spec_total_steps_is_the_params_not_a_column_a_host_wrote(self):
        self.db.row(self.tid)["total_steps"] = 2500  # FR-1: never trusted
        _, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual(body["spec"]["total_steps"], 50000)

    async def test_a_resume_point_with_presigned_files(self):
        files = self.upload(2500)
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest=manifest_of(files))
        self.db.add_checkpoint(self.tid, 1, 5000, state="refused", manifest={})
        _, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        res = body["resume"]
        self.assertEqual((res["step"], res["step_id"], res["attempt_no"]), (2500, "002500", 1))
        self.assertEqual(set(res["files"]), set(AV.CHECKPOINT_RELPATHS))
        self.assertEqual(res["files"]["pretrained_model/model.safetensors"]["sha256"],
                         manifest_of(files)["pretrained_model/model.safetensors"]["sha256"])
        self.assertIn("?get", res["files"]["pretrained_model/config.json"]["url"])
        self.assertEqual((self.att["resume_from_step"], self.att["resume_from_attempt"]), (2500, 1))

    async def test_ties_go_to_the_higher_attempt(self):
        self.db.add_attempt(self.tid, 2, state="lost")
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest={})
        self.db.add_checkpoint(self.tid, 2, 2500, state="ready", manifest={})
        _, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual(body["resume"]["attempt_no"], 2)

    async def test_finished_when_the_final_is_ready(self):
        self.db.add_checkpoint(self.tid, 1, 50000, state="ready", manifest={})
        _, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertIs(body["finished"], True)
        self.assertIsNone(body["resume"])

    async def test_a_wrong_lock_requeues_logs_and_stops_the_pod(self):
        with self.assertLogs("app.routes.training_worker", level="ERROR"):
            status, body = await self.call("start", {"lock_sha256": "d" * 64, "build": "b"})
        self.assertEqual((status, body["code"], body["detail"]), (409, "wrong_image", T.S10))
        self.assertEqual((self.att["state"], self.att["end_reason"]), ("lost", "host_unfit:wrong_image"))
        self.assertIn("pod1", self.rp.deletes())
        self.assertEqual(self.db.row(self.tid)["status"], "queued")

    async def test_no_lock_module_is_503(self):
        def _missing():
            raise ImportError("training_lock_hash")

        with patch.object(route, "_expected_lock_sha256", _missing):
            self.assertEqual((await self.call("start", {"lock_sha256": LOCK, "build": "b"}))[0], 503)

    def _assert_wrong_image(self, status, body):
        self.assertEqual((status, body["code"], body["detail"]), (409, "wrong_image", T.S10))
        self.assertEqual((self.att["state"], self.att["end_reason"]), ("lost", "host_unfit:wrong_image"))
        self.assertIn("pod1", self.rp.deletes())
        self.assertEqual(self.db.row(self.tid)["status"], "queued")
        self.assertEqual((self.att["resume_from_step"], self.att.get("resume_from_attempt")), (0, None),
                         "a refused start hands out nothing")

    async def test_a_stale_handler_requeues_logs_and_stops_the_pod_like_a_wrong_lock(self):
        # V2-1: the lock matches, the handler is another version (a forgotten Modal deploy).
        with self.assertLogs("app.routes.training_worker", level="ERROR") as logs:
            status, body = await self.call("start", {"lock_sha256": LOCK, "handler_sha256": "d" * 64,
                                                     "build": "b"})
        self._assert_wrong_image(status, body)
        self.assertEqual(logs.output, [
            "ERROR:app.routes.training_worker:training %s attempt 1: wrong image "
            "(handler dddddddddddd…, expected eeeeeeeeeeee…); requeued" % self.tid])

    async def test_a_start_without_a_handler_hash_is_a_stale_image(self):
        # An image from before V2-1 sends no handler hash: wrong_image, never a 422 the
        # worker would retry for ten minutes.
        raw = json.dumps({"lock_sha256": LOCK, "build": "b"}).encode()
        with self.assertLogs("app.routes.training_worker", level="ERROR") as logs:
            status, body = await self.call("start", raw=raw)
        self._assert_wrong_image(status, body)
        self.assertIn("(handler none…, expected eeeeeeeeeeee…)", logs.output[0])

    async def test_the_wrong_lock_log_line_is_unchanged(self):
        with self.assertLogs("app.routes.training_worker", level="ERROR") as logs:
            await self.call("start", {"lock_sha256": "d" * 64, "build": "b"})
        self.assertEqual(logs.output, [
            "ERROR:app.routes.training_worker:training %s attempt 1: wrong image "
            "(lock dddddddddddd…, expected cccccccccccc…); requeued" % self.tid])

    async def test_a_stale_lock_and_handler_requeue_once_and_name_both(self):
        requeues = []
        orig = D.requeue_attempt

        async def spy(*a, **k):
            requeues.append(a[2])
            return await orig(*a, **k)

        with patch.object(D, "requeue_attempt", spy), \
                self.assertLogs("app.routes.training_worker", level="ERROR") as logs:
            status, body = await self.call("start", {"lock_sha256": "d" * 64, "handler_sha256": "f" * 64,
                                                     "build": "b"})
        self._assert_wrong_image(status, body)
        self.assertEqual(requeues, ["host_unfit:wrong_image"])
        self.assertIn("(lock dddddddddddd…, expected cccccccccccc…; handler ffffffffffff…, expected "
                      "eeeeeeeeeeee…)", logs.output[0])

    async def test_a_malformed_handler_hash_is_422(self):
        for bad in ("E" * 64, "e" * 63, "e" * 65, "", 7, True, ["e" * 64], {"sha256": "e" * 64}):
            with self.subTest(handler=bad):
                status, body = await self.call("start", {"lock_sha256": LOCK, "handler_sha256": bad,
                                                         "build": "b"})
                self.assertEqual((status, body["code"]), (422, "bad_request"))
        self.assertEqual(self.att["state"], "running", "a malformed body requeues nothing")

    async def test_no_handler_module_is_503(self):
        def _missing():
            raise ImportError("training_handler_hash")

        with patch.object(route, "_expected_handler_sha256", _missing):
            self.assertEqual((await self.call("start", {"lock_sha256": LOCK, "build": "b"}))[0], 503)
        self.assertEqual(self.att["state"], "running")

    async def test_the_accessors_answer_the_generated_modules(self):
        from app.services import training_handler_hash, training_lock_hash

        self.assertRegex(training_handler_hash.TRAINING_HANDLER_SHA256, r"^[0-9a-f]{64}$")
        self.assertEqual(_REAL_HANDLER_ACCESSOR(), training_handler_hash.TRAINING_HANDLER_SHA256)
        self.assertEqual(_REAL_LOCK_ACCESSOR(), training_lock_hash.TRAINING_LOCK_SHA256)

    async def test_an_exhausted_budget_fails_the_row(self):
        self.db.row(self.tid)["train_seconds_used"] = D.MANAGED_ACT_CAP_S - 1800 - 200
        status, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual((status, body["code"]), (409, "budget_exhausted"))
        self.assertEqual(self.db.row(self.tid)["error_message"], T.s5(D.MANAGED_ACT_CAP_S))
        self.assertIn("pod1", self.rp.deletes())

    async def test_the_wall_term_gives_s6(self):
        self.db.row(self.tid)["requested_at"] = self.clock.iso(self.clock.t - (D.WALL_CAP_S - 100))
        status, body = await self.call("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual((status, self.db.row(self.tid)["error_message"]), (409, T.S6))


class UploadUrl(WorkerBase):
    def _manifest(self, step=2500, final=False):
        return manifest_of(checkpoint_files(self.db.row(self.tid), step, final=final))

    async def test_presigned_puts_with_their_signed_headers(self):
        m = self._manifest()
        status, body = await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m})
        self.assertEqual(status, 200)
        self.assertEqual(body["expires_in"], 900)
        self.assertEqual(set(body["urls"]), set(AV.CHECKPOINT_RELPATHS))
        for rel, meta in m.items():
            self.assertEqual(body["headers"][rel], {"Content-Type": "application/octet-stream",
                                                    "Content-Length": str(meta["size"]), "Content-MD5": meta["md5"]})
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["manifest"]), ("uploading", m))
        keys = {p[1] for p in self.r2.presigned}
        self.assertIn(r2_client.ckpt_key(self.tid, 1, 2500, "pretrained_model/config.json"), keys)
        self.assertTrue(all(k.startswith(f"train/t{self.tid}/a1/s0002500/") for k in keys))

    async def test_the_same_manifest_again_gets_fresh_urls_a_different_one_422(self):
        m = self._manifest()
        await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m})
        self.assertEqual((await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m}))[0], 200)
        m2 = copy.deepcopy(m)
        m2["pretrained_model/model.safetensors"]["size"] += 1
        self.assertEqual(await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m2}),
                         (422, {"detail": T.S11, "code": "bad_manifest"}))

    async def test_ready_deleted_refused(self):
        m = self._manifest()
        for state, want in (("ready", (409, "already_ready", T.S14)), ("deleted", (409, "already_ready", T.S14)),
                            ("refused", (409, "already_refused", T.S12))):
            self.db.tables["training_checkpoints"].clear()
            self.db.add_checkpoint(self.tid, 1, 2500, state=state, manifest=m)
            status, body = await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m})
            with self.subTest(state=state):
                self.assertEqual((status, body["code"], body["detail"]), want)
                self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], state, "never re-upserted")

    async def test_manifest_refusals(self):
        m = self._manifest()
        missing = dict(m)
        missing.pop("training_state/rng_state.safetensors")
        bad_sha = copy.deepcopy(m)
        bad_sha["pretrained_model/config.json"]["sha256"] = "x"
        big = copy.deepcopy(m)
        big["pretrained_model/model.safetensors"]["size"] = 512 * AV.MIB + 1
        for name, body in (("missing file", {"kind": "ckpt", "step": 2500, "files": missing}),
                           ("bad sha", {"kind": "ckpt", "step": 2500, "files": bad_sha}),
                           ("size cap", {"kind": "ckpt", "step": 2500, "files": big}),
                           ("not a multiple", {"kind": "ckpt", "step": 2600, "files": m}),
                           ("beyond total", {"kind": "ckpt", "step": 52500, "files": m})):
            with self.subTest(case=name):
                self.assertEqual((await self.call("upload-url", body))[1]["code"], "bad_manifest")
        self.att["resume_from_step"] = 2500
        self.assertEqual((await self.call("upload-url", {"kind": "ckpt", "step": 2500, "files": m}))[1]["code"],
                         "bad_manifest")
        self.assertEqual((await self.call("upload-url", {"kind": "zip", "files": {}}))[0], 422)

    async def test_the_final_step_takes_the_seven(self):
        m = self._manifest(50000, final=True)
        self.assertEqual((await self.call("upload-url", {"kind": "ckpt", "step": 50000, "files": m}))[0], 200)

    async def test_the_log(self):
        meta = {"size": 100, "sha256": "a" * 64, "md5": manifest_of({"x": b"x"})["x"]["md5"]}
        status, body = await self.call("upload-url", {"kind": "log", "files": {"training_log.txt": meta}})
        self.assertEqual(status, 200)
        self.assertIn(r2_client.log_key(self.tid, 1), body["urls"]["training_log.txt"])
        big = dict(meta, size=AV.LOG_SIZE_CAP + 1)
        self.assertEqual((await self.call("upload-url", {"kind": "log", "files": {"training_log.txt": big}}))[0], 422)


class CkptDone(WorkerBase):
    async def _uploading(self, step=2500, files=None, final=False):
        files = self.upload(step, files, final=final)
        self.db.add_checkpoint(self.tid, 1, step, state="uploading", manifest=manifest_of(files))
        return files

    async def test_a_valid_checkpoint_becomes_ready_and_schedules_the_mirror(self):
        await self._uploading(10000)
        self.assertEqual(await self.call("ckpt-done", {"step": 10000}), (200, {"state": "ready"}))
        c = self.db.checkpoint(self.tid, 1, 10000)
        self.assertEqual(c["state"], "ready")
        self.assertIsNotNone(c["ready_at"])
        self.assertEqual(self.mirrors, [(self.tid, 1, 10000)])
        self.assertEqual(await self.call("ckpt-done", {"step": 10000}), (200, {"state": "ready"}))  # idempotent

    async def test_a_non_mirror_step_schedules_none(self):
        await self._uploading(2500)
        await self.call("ckpt-done", {"step": 2500})
        self.assertEqual(self.mirrors, [])

    async def test_not_uploading(self):
        self.assertEqual(await self.call("ckpt-done", {"step": 2500}),
                         (409, {"detail": T.S15, "code": "not_uploading"}))

    async def test_transport_faults_refuse_the_checkpoint_only(self):
        for name, damage in (
                ("size", lambda f: self.r2.objects.__setitem__(
                    r2_client.ckpt_key(self.tid, 1, 2500, "pretrained_model/model.safetensors"), b"short")),
                ("missing", lambda f: self.r2.objects.pop(
                    r2_client.ckpt_key(self.tid, 1, 2500, "training_state/rng_state.safetensors"))),
                ("sha256", lambda f: self.r2.objects.__setitem__(
                    r2_client.ckpt_key(self.tid, 1, 2500, "pretrained_model/config.json"),
                    f["pretrained_model/config.json"].replace(b"act", b"acx")))):
            with self.subTest(fault=name):
                self.db.tables["training_checkpoints"].clear()
                files = await self._uploading(2500)
                damage(files)
                self.assertEqual(await self.call("ckpt-done", {"step": 2500}),
                                 (422, {"detail": T.S12, "code": "ckpt_refused"}))
                self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "refused")
                self.assertEqual(self.db.row(self.tid)["status"], "running", "the training goes on")

    async def test_a_content_rule_fails_the_training_with_s7(self):
        row = self.db.row(self.tid)
        files = checkpoint_files(row, 2500)
        doc = json.loads(files["pretrained_model/train_config.json"])
        doc["policy"]["type"] = "groot"
        files["pretrained_model/train_config.json"] = json.dumps(doc).encode()
        await self._uploading(2500, files)
        self.assertEqual((await self.call("ckpt-done", {"step": 2500}))[1]["code"], "ckpt_refused")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S7))
        self.assertIn("pod1", self.rp.deletes())
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["refused_by"]), ("refused", 1))

    async def test_a_json_that_does_not_parse_is_a_content_rule(self):
        files = checkpoint_files(self.db.row(self.tid), 2500)
        files["training_state/training_step.json"] = b"{not json"
        await self._uploading(2500, files)
        await self.call("ckpt-done", {"step": 2500})
        self.assertEqual(self.db.row(self.tid)["error_message"], T.S7)

    async def test_an_r2_outage_is_503_not_a_refusal(self):
        await self._uploading(2500)
        self.r2.fail["head"] = 1
        self.assertEqual((await self.call("ckpt-done", {"step": 2500}))[0], 503)
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "uploading")

    async def test_retention_runs_after_ready(self):
        for step in (2500, 5000):
            self.db.add_checkpoint(self.tid, 1, step, state="ready", manifest={"a": {}})
        await self._uploading(7500)
        await self.call("ckpt-done", {"step": 7500})
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "deleted")
        self.assertEqual(self.db.checkpoint(self.tid, 1, 5000)["state"], "ready")


class CkptBad(WorkerBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db.add_attempt(self.tid, 2, state="running", backend="runpod_secure", cloud_ref="pod2",
                            resume_from_step=2500, resume_from_attempt=1)
        self.db.row(self.tid)["current_attempt"] = 2
        self.att["state"] = "lost"
        self.files = self.upload(2500)
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest=manifest_of(self.files))

    async def bad(self, step, reason):
        return await self.call("ckpt-bad", {"step": step, "reason": reason}, attempt="2")

    async def test_ignored_unless_it_names_the_handed_checkpoint(self):
        self.assertEqual(await self.bad(5000, "train_config"), (200, {"state": "ignored"}))
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "ready")

    async def test_an_existing_checkpoint_that_was_not_handed_is_ignored_and_untouched(self):
        # SR-2 / F18 (V1-4 M07): attempt 2 was handed step 2500 of attempt 1; a
        # ckpt-bad naming attempt 1's EXISTING step 5000 never acts on it.
        files = self.upload(5000)
        self.db.add_checkpoint(self.tid, 1, 5000, state="ready", manifest=manifest_of(files))
        before = copy.deepcopy(self.db.checkpoint(self.tid, 1, 5000))
        for reason in route.CKPT_BAD_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(await self.bad(5000, reason), (200, {"state": "ignored"}))
                self.assertEqual(self.db.checkpoint(self.tid, 1, 5000), before)
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "ready")

    async def test_deterministic_reasons_refuse(self):
        for reason in route.DETERMINISTIC_BAD:
            self.db.checkpoint(self.tid, 1, 2500)["state"] = "ready"
            self.assertEqual(await self.bad(2500, reason), (200, {"state": "refused"}), reason)
            c = self.db.checkpoint(self.tid, 1, 2500)
            self.assertEqual((c["state"], c["refused_by"], c["refused_reason"]), ("refused", 2, reason))

    def _count_hashes(self):
        calls = []
        real = self.r2.stream_sha256

        def counted(key):
            calls.append(key)
            return real(key)

        return calls, patch.object(r2_client, "stream_sha256", counted)

    async def test_sha256_confirmed_refuses_and_not_confirmed_keeps(self):
        self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}))
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "ready")
        route.reset_hash_state_for_tests()  # a fresh process: nothing remembered
        self.r2.objects[r2_client.ckpt_key(self.tid, 1, 2500, "training_state/optimizer_state.safetensors")] = b"x"
        self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "refused"}))
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["refused_by"], c["refused_reason"]), ("refused", 2, "sha256"))

    async def test_a_repeat_answers_the_remembered_verdict_without_hashing_again(self):
        # V1-6: every sha256 ask re-streamed the whole checkpoint through the API.
        calls, cm = self._count_hashes()
        with cm:
            self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}))
            first = len(calls)
            self.assertEqual(first, len(self.files))
            for _ in range(5):
                self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}))
            self.assertEqual(len(calls), first, "a remembered verdict hashes nothing")
        route.reset_hash_state_for_tests()
        self.r2.objects[r2_client.ckpt_key(self.tid, 1, 2500, "pretrained_model/config.json")] = b"{}"
        calls, cm = self._count_hashes()
        with cm:
            self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "refused"}))
            first = len(calls)
            self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "refused"}))
            self.assertEqual(len(calls), first, "a refusal is remembered too")

    async def test_concurrent_asks_share_one_hash_on_the_dedicated_pool(self):
        gate = threading.Event()
        threads, calls = [], []
        real = self.r2.stream_sha256

        def slow(key):
            threads.append(threading.current_thread().name)
            calls.append(key)
            gate.wait(5)
            return real(key)

        with patch.object(r2_client, "stream_sha256", slow):
            tasks = [asyncio.ensure_future(self.bad(2500, "sha256")) for _ in range(4)]
            await asyncio.sleep(0.05)
            self.assertEqual(len(calls), 1, "one hash in flight for one checkpoint")
            gate.set()
            answers = await asyncio.gather(*tasks)
        self.assertEqual(answers, [(200, {"state": "kept"})] * 4)
        self.assertEqual(len(calls), len(self.files), "the four asks hashed the checkpoint once")
        self.assertTrue(threads and all(t.startswith("ckpt-hash") for t in threads), threads)
        self.assertEqual(route._HASH_POOL._max_workers, 1)
        self.assertEqual(route._HASH_INFLIGHT, {})

    async def test_a_transport_fault_is_503_and_never_remembered(self):
        self.r2.fail["get"] = 1
        self.assertEqual(await self.bad(2500, "sha256"), (503, {"detail": T.WORKER_UNAVAILABLE_DE,
                                                                 "code": "unavailable"}))
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["state"], "ready")
        self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}), "the next ask hashes again")

    async def test_the_remembered_verdicts_are_bounded_oldest_first(self):
        with patch.object(route, "HASH_VERDICTS_MAX", 3):
            for step in (1, 2, 3):
                route._HASH_VERDICTS[(self.tid, 2, 1, step)] = True
            self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}))
            self.assertEqual(list(route._HASH_VERDICTS),
                             [(self.tid, 2, 1, 2), (self.tid, 2, 1, 3), (self.tid, 2, 1, 2500)])

    async def test_another_attempt_never_reuses_a_kept_verdict(self):
        # Fix round 2026-10-19: attempt 1's PUT links stay usable for PUT_TTL_S after they were
        # minted, so the checkpoint can change after attempt 2 heard `kept` (a same-length body:
        # size and the signed MD5 are all R2 checks). Attempt 3, handed the same checkpoint,
        # hashes it as it then is and refuses it.
        calls, cm = self._count_hashes()
        with cm:
            self.assertEqual(await self.bad(2500, "sha256"), (200, {"state": "kept"}))
            key = r2_client.ckpt_key(self.tid, 1, 2500, "training_state/optimizer_state.safetensors")
            old = self.r2.objects[key]
            self.r2.objects[key] = bytes(b ^ 0xFF for b in old)
            self.assertEqual(len(self.r2.objects[key]), len(old))
            self.db.attempt(self.tid, 2)["state"] = "lost"
            self.db.add_attempt(self.tid, 3, state="running", backend="modal", cloud_ref="fc-3",
                                resume_from_step=2500, resume_from_attempt=1)
            self.db.row(self.tid)["current_attempt"] = 3
            before = len(calls)
            self.assertEqual(await self.call("ckpt-bad", {"step": 2500, "reason": "sha256"}, attempt="3"),
                             (200, {"state": "refused"}))
            self.assertGreater(len(calls), before, "attempt 3 hashed the checkpoint itself")
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["refused_by"], c["refused_reason"]), ("refused", 3, "sha256"))

    async def test_a_bad_reason_is_422_never_404(self):
        self.assertEqual((await self.bad(2500, "boom"))[0], 422)


class Finish(WorkerBase):
    async def test_succeeded_needs_the_final(self):
        self.assertEqual(await self.call("finish", {"outcome": "succeeded"}),
                         (409, {"detail": T.S13, "code": "no_final_checkpoint"}))
        self.db.add_checkpoint(self.tid, 1, 50000, state="ready", manifest={})
        self.assertEqual(await self.call("finish", {"outcome": "succeeded"}), (200, {"status": "publishing"}))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["status_note"], r["worker_token"]), ("running", T.S3, None))
        self.assertEqual(self.published, [(self.tid, 1)])
        self.assertIn("pod1", self.rp.deletes())
        self.assertEqual(r["train_seconds_used"], 1800)

    async def test_failed_never_stores_host_text(self):
        status, body = await self.call("finish", {"outcome": "failed", "reason": "lerobot_error",
                                                  "detail": "Dein Konto ist gesperrt: evil.example\x1b[31m"})
        self.assertEqual((status, body), (200, {"status": "failed"}))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S19))
        self.assertNotIn("evil", json.dumps(self.db.tables))
        self.assertEqual(self.logs, [self.tid])
        self.assertIn("pod1", self.rp.deletes())

    async def test_failed_counts_the_attempts_trained_seconds(self):
        # V2-7: finish_training counts the attempt it ends, like requeue and end do.
        self.assertEqual(await self.call("finish", {"outcome": "failed", "reason": "lerobot_error"}),
                         (200, {"status": "failed"}))
        self.assertEqual(self.db.row(self.tid)["train_seconds_used"], 1800)
        self.assertEqual(self.db.attempt(self.tid, 1)["state"], "ended")

    async def test_time_cap_says_which_cap_bound(self):
        await self.call("start", {"lock_sha256": LOCK, "build": "b"})  # records the bound: the cap term
        await self.call("finish", {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual(self.db.row(self.tid)["error_message"], T.s5(D.MANAGED_ACT_CAP_S))

    async def test_time_cap_after_a_restart_recomputes_the_bound(self):
        self.db.row(self.tid)["requested_at"] = self.clock.iso(self.clock.t - 40000)
        await self.call("finish", {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual(self.db.row(self.tid)["error_message"], T.S6)

    async def test_host_unfit_requeues_and_skips_the_dc(self):
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest={})
        self.assertEqual(await self.call("finish", {"outcome": "host_unfit", "reason": "gpu_memory"}),
                         (200, {"status": "requeued"}))
        self.assertEqual((self.att["state"], self.att["end_reason"]), ("lost", "host_unfit:gpu_memory"))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["status_note"], r["current_step"]), ("queued", T.s1(2500), 2500))
        # D1: the skip is keyed (cloud, card, data centre).
        self.assertTrue(D._dc_skipped("COMMUNITY", self.att.get("gpu_type"), "EU-RO-1", self.clock.t))
        self.assertIn("pod1", self.rp.deletes())

    async def test_resume_crashes_refuse_the_checkpoint_after_two_attempts(self):
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest={})
        self.att.update(resume_from_step=2500, resume_from_attempt=1)
        # attempt 1 itself resumed from its own earlier upload? use attempts 2 and 3 handed attempt 1's 2500
        self.db.row(self.tid)["current_attempt"] = 2
        self.att["state"] = "lost"
        a2 = self.db.add_attempt(self.tid, 2, state="running", backend="runpod_secure", cloud_ref="pod2",
                                 resume_from_step=2500, resume_from_attempt=1, data_center="EU-SE-1")
        self.db.row(self.tid)["worker_token"] = TOKEN
        await self.call("finish", {"outcome": "host_unfit", "reason": "resume_crash"}, attempt="2")
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["crash_attempts"]), ("ready", [2]))
        self.assertEqual(self.db.row(self.tid)["current_step"], 2500)
        self.db.row(self.tid).update(current_attempt=3, status="running", worker_token=TOKEN)
        self.db.add_attempt(self.tid, 3, state="running", backend="modal", cloud_ref="fc-9",
                            resume_from_step=2500, resume_from_attempt=1)
        await self.call("finish", {"outcome": "host_unfit", "reason": "resume_crash"}, attempt="3")
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["refused_reason"], c["crash_attempts"]), ("refused", "resume_crash", [2, 3]))
        self.assertEqual(self.db.row(self.tid)["status_note"], T.S2, "the next start begins fresh")
        self.assertEqual(self.db.row(self.tid)["current_step"], 0)
        self.assertNotIn("fc-9", self.modal.canceled, "a Modal function returns by itself after finish")

    async def test_bad_bodies(self):
        for body in ({"outcome": "succeeded", "reason": "x"}, {"outcome": "failed"}, {"outcome": "host_unfit",
                                                                                      "reason": "aliens"},
                     {"outcome": "failed", "reason": "lerobot_error", "detail": "x" * 501},
                     {"outcome": "failed", "reason": "lerobot_error", "detail": 5}, {"outcome": "canceled"}):
            with self.subTest(body=body):
                self.assertEqual((await self.call("finish", body))[0], 422)
        self.assertEqual(self.db.row(self.tid)["status"], "running")


CHILD_TOKEN = "11111111-2222-3333-4444-555555555555"


class Continuation(WorkerBase):
    """A continuation's first attempt resumes from the parent's kept copy (owner E36),
    and a complaint about that copy is proven, never trusted (owner E43, review M3)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.parent = self.db.add_training(status="succeeded", current_attempt=1, capped_step=42500,
                                           model_name="EduBotics-Solutions/m-act-ds-abc", dataset_name="u/ds",
                                           continuable_until=self.clock.iso(self.clock.t + D.KEEP_S),
                                           training_params=self.row["training_params"], dataset_fps=30.0)
        self.pid = self.parent["id"]
        self.kfiles = checkpoint_files(self.parent, 42500, final=False)
        self.kmanifest = manifest_of(self.kfiles)
        for rel, data in self.kfiles.items():
            self.r2.objects[r2_client.keep_key(self.pid, 42500, rel)] = data
        self.kept = self.db.add_kept(self.pid, 42500, manifest=self.kmanifest,
                                     expires_at=self.clock.iso(self.clock.t + D.KEEP_S))
        self.child = self.db.add_training(status="running", current_attempt=1, worker_token=CHILD_TOKEN,
                                          continued_from=self.pid, model_name=self.parent["model_name"],
                                          dataset_name="u/ds", dataset_fps=30.0,
                                          training_params=self.row["training_params"],
                                          requested_at=self.clock.iso(self.clock.t - 60))
        self.cid = self.child["id"]
        self.catt = self.db.add_attempt(self.cid, 1, state="starting", backend="runpod_secure", cloud_ref="podc")
        self.rp.add_pod(id="podc", status="RUNNING")

    async def ccall(self, action, body=None, attempt="1"):
        if action == "start" and isinstance(body, dict) and "handler_sha256" not in body:
            body = {**body, "handler_sha256": HANDLER}
        resp = await route.worker_action(str(self.cid), attempt, action,
                                         FakeRequest(token=CHILD_TOKEN, body=body))
        return resp.status_code, content(resp)

    async def test_start_hands_the_kept_copy_as_resume(self):
        status, body = await self.ccall("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual(status, 200)
        r = body["resume"]
        self.assertEqual((r["step"], r["step_id"], r["attempt_no"]), (42500, "042500", 0))
        for rel in AV.CHECKPOINT_RELPATHS:
            self.assertIn(r2_client.keep_key(self.pid, 42500, rel), r["files"][rel]["url"])
            self.assertEqual(r["files"][rel]["size"], self.kmanifest[rel]["size"])
            self.assertEqual(r["files"][rel]["sha256"], self.kmanifest[rel]["sha256"])
        a = self.db.attempt(self.cid, 1)
        self.assertEqual((a["resume_from_step"], a["resume_from_attempt"]), (42500, None))
        self.assertEqual(body["budget_s"], D.MANAGED_ACT_CAP_S - 0 - 0)

    async def test_an_unusable_kept_copy_fails_the_child(self):
        for setup in ("refused", "missing_row", "missing_object"):
            with self.subTest(setup=setup):
                if setup == "refused":
                    self.kept["state"] = "refused"
                elif setup == "missing_row":
                    self.db.tables["training_kept_checkpoints"].clear()
                else:
                    self.r2.objects.pop(r2_client.keep_key(self.pid, 42500, "pretrained_model/config.json"), None)
                status, body = await self.ccall("start", {"lock_sha256": LOCK, "build": "b"})
                self.assertEqual((status, body["code"]), (409, "kept_unusable"))
                self.assertEqual((self.db.row(self.cid)["status"], self.db.row(self.cid)["error_message"]),
                                 ("failed", T.S27))
                self.assertIn("podc", self.rp.deletes())
                # reset for the next subTest: exit this run's patches first, or they leak
                # into every later module (verifier m3)
                await self.asyncTearDown()
                await self.asyncSetUp()

    async def test_a_past_expiry_still_resumes_by_presence(self):
        self.db.row(self.pid)["continuable_until"] = self.clock.iso(self.clock.t - 1)
        self.kept["expires_at"] = self.clock.iso(self.clock.t - 1)
        status, body = await self.ccall("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual(status, 200)
        self.assertEqual(body["resume"]["step"], 42500)

    async def test_the_childs_own_checkpoint_wins_over_the_kept_copy(self):
        self.db.add_checkpoint(self.cid, 1, 45000, state="ready", manifest=manifest_of(
            checkpoint_files(self.child, 45000)))
        status, body = await self.ccall("start", {"lock_sha256": LOCK, "build": "b"})
        self.assertEqual((body["resume"]["step"], body["resume"]["attempt_no"]), (45000, 1))

    async def test_upload_url_floor_is_the_kept_step(self):
        self.catt.update(state="running", resume_from_step=42500, resume_from_attempt=None)
        self.assertEqual((await self.ccall("upload-url", self._upload_body(42500)))[0], 422)
        self.assertEqual((await self.ccall("upload-url", self._upload_body(45000)))[0], 200)

    def _upload_body(self, step):
        return {"kind": "ckpt", "step": step, "files": manifest_of(checkpoint_files(self.child, step))}

    async def _resumed(self, attempt_no=1, state="running"):
        # A resumed attempt is running with a fresh worker token (claim_training_attempt
        # minted it; a requeue cleared the previous one).
        self.db.row(self.cid).update(status="running", worker_token=CHILD_TOKEN, current_attempt=attempt_no)
        if attempt_no == 1:
            self.catt.update(state=state, resume_from_step=42500, resume_from_attempt=None)
        else:
            self.db.attempt(self.cid, attempt_no - 1)["state"] = "lost"
            self.catt = self.db.add_attempt(self.cid, attempt_no, state=state, backend="runpod_secure",
                                            cloud_ref="podc", resume_from_step=42500, resume_from_attempt=None)

    async def test_ckpt_bad_sha256_on_an_intact_copy_is_kept_a_flip_refuses(self):
        await self._resumed()
        status, body = await self.ccall("ckpt-bad", {"step": 42500, "reason": "sha256"})
        self.assertEqual((status, body["state"]), (200, "kept"))
        # a flipped byte in a keep object → refuse
        route.reset_hash_state_for_tests()
        key = r2_client.keep_key(self.pid, 42500, "pretrained_model/model.safetensors")
        self.r2.objects[key] = self.r2.objects[key][:-1] + b"\x00"
        status, body = await self.ccall("ckpt-bad", {"step": 42500, "reason": "sha256"})
        self.assertEqual((status, body["state"]), (200, "refused"))
        self.assertEqual(self.db.kept(self.pid)["state"], "refused")
        self.assertLessEqual(D.ts(self.db.row(self.pid)["continuable_until"]), self.clock.t)
        self.assertEqual((self.db.row(self.cid)["status"], self.db.row(self.cid)["error_message"]),
                         ("failed", T.S27))
        self.assertIn("podc", self.rp.deletes())

    async def test_ckpt_bad_file_list_on_an_intact_copy_is_kept(self):
        await self._resumed()
        status, body = await self.ccall("ckpt-bad", {"step": 42500, "reason": "file_list"})
        self.assertEqual((status, body["state"]), (200, "kept"))
        self.assertEqual(self.db.kept(self.pid)["state"], "kept")

    async def test_ckpt_bad_train_config_counts_two_attempts(self):
        await self._resumed()
        status, body = await self.ccall("ckpt-bad", {"step": 42500, "reason": "train_config"})
        self.assertEqual((status, body["state"]), (200, "kept"))
        self.assertEqual(self.db.kept(self.pid)["bad_attempts"], [f"{self.cid}:1"])
        await self._resumed(attempt_no=2)
        status, body = await self.ccall("ckpt-bad", {"step": 42500, "reason": "train_config"}, attempt="2")
        self.assertEqual((status, body["state"]), (200, "refused"))
        self.assertEqual(self.db.row(self.cid)["error_message"], T.S27)

    async def test_finish_resume_crash_refuses_after_two_attempts(self):
        await self._resumed()
        self.assertEqual((await self.ccall("finish", {"outcome": "host_unfit", "reason": "resume_crash"}))[1],
                         {"status": "requeued"})
        self.assertEqual(self.db.kept(self.pid)["bad_attempts"], [f"{self.cid}:1"])
        await self._resumed(attempt_no=2)
        status, body = await self.ccall("finish", {"outcome": "host_unfit", "reason": "resume_crash"}, attempt="2")
        self.assertEqual((status, body), (200, {"status": "failed"}))
        self.assertEqual((self.db.row(self.cid)["status"], self.db.row(self.cid)["error_message"]),
                         ("failed", T.S27))
        self.assertEqual(self.db.kept(self.pid)["state"], "refused")


class TimeCapPublish(WorkerBase):
    async def test_finish_time_cap_publishes_or_fails(self):
        # cap bound + a ready checkpoint → published, NO failed log
        self.db.add_checkpoint(self.tid, 1, 42500, state="ready", manifest={})
        await self.call("start", {"lock_sha256": LOCK, "build": "b"})  # records the cap bound
        status, body = await self.call("finish", {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual((status, body), (200, {"status": "publishing"}))
        self.assertEqual(self.db.attempt(self.tid, 1)["end_reason"], "capped")
        self.assertEqual(self.published, [(self.tid, 1)])
        self.assertEqual(self.logs, [], "no failed log when published")
        self.assertIn("pod1", self.rp.deletes())

    async def test_finish_wall_cap_publishes(self):
        self.db.row(self.tid)["requested_at"] = self.clock.iso(self.clock.t - 40000)  # the wall term bounds
        self.db.add_checkpoint(self.tid, 1, 42500, state="ready", manifest={})
        status, body = await self.call("finish", {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual((status, body), (200, {"status": "publishing"}))
        self.assertEqual(self.db.attempt(self.tid, 1)["end_reason"], "wall_capped")

    async def test_finish_wall_cap_without_a_checkpoint_is_s6_and_a_failed_log(self):
        self.db.row(self.tid)["requested_at"] = self.clock.iso(self.clock.t - 40000)
        status, body = await self.call("finish", {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual((status, body), (200, {"status": "failed"}))
        self.assertEqual(self.db.row(self.tid)["error_message"], T.S6)
        self.assertEqual(self.logs, [self.tid])


class MainWiring(unittest.TestCase):
    def test_router_rate_rule_body_cap_and_per_token_keying(self):
        import ast

        main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        src = open(main_py, encoding="utf-8").read()
        self.assertIn("from app.routes.training_worker import router as training_worker_router", src)
        self.assertIn("app.include_router(training_worker_router)", src)
        tree = ast.parse(src)

        def literal(name):
            for n in ast.walk(tree):
                if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == name:
                    return ast.literal_eval(n.value)
                if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == name for t in n.targets):
                    try:
                        return ast.literal_eval(n.value)
                    except ValueError:
                        continue
            raise AssertionError(name)

        self.assertIn(("POST", "/trainings/worker", 120, 60.0), literal("_RATE_LIMIT_RULES"))
        self.assertIn(("POST", "/trainings/worker"), literal("_BODY_SIZE_LIMITED_PREFIXES"))
        self.assertTrue(any("/trainings/worker/1/1/start".startswith(p) for p in literal("_PER_USER_RATE_LIMIT_PREFIXES")))
        self.assertIn("BOOT_ID = _training_dispatch.BOOT_ID", src)
        self.assertIn('os.environ.get("TRAINING_DISPATCH_DISABLED") == "1"', src)
        self.assertIn('os.environ.get("TRAINING_ACTIVE_SWEEP_DISABLED") == "1"', src)
        self.assertIn("_training_dispatch.managed_supervisor()", src)
        self.assertIn("_validate_managed_training()", src)


if __name__ == "__main__":
    unittest.main()
