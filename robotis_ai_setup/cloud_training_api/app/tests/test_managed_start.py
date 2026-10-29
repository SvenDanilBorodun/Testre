"""POST /trainings/start, /trainings/cancel and the legacy reconcilers with managed rows (spec §4.1, §4.4).

Pinned: since the training-hour ledger (052, §8.2) every start is managed ACT: another
policy is refused (POLICY_REFUSED_DE, recorded), TRAINING_DISPATCH=legacy answers 503
(DISPATCH_OFF_DE, not recorded) and the legacy Modal dispatch branch is gone; the
cadences written and num_workers dropped before the dedupe; the dedupe ignores
dataset_revision; the anonymous v3.0 → commit resolution (E17) with the PEP 440 fallback
and TR-1's error mapping; S20 before any GPU (FR-5); p_managed / no token / the key and
the cap; the cancel compare-and-set (now ``stop_training``, §4.9) in every attempt
state, S21 at the cap (FR-22); the legacy reconcilers leave managed rows alone and keep
their own stall rule (SR-13). The credit routes' own matrix is test_credit_routes.py.
"""

from __future__ import annotations

import asyncio
import copy
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()


def _complete_the_modal_stub() -> None:
    """The house stub of modal_client carries only cancel_training_job; routes/
    training.py imports two more names at its top (no earlier test imported it)."""
    import sys

    stub = sys.modules["app.services.modal_client"]
    if getattr(stub, "__file__", None):  # the real module (an environment with modal): nothing to add
        return

    async def _unset(*a, **k):
        raise AssertionError("patch this in the test")

    for name in ("get_job_status", "start_managed_training_job"):
        if not hasattr(stub, name):
            setattr(stub, name, _unset)


_complete_the_modal_stub()

from fastapi import HTTPException  # noqa: E402
from app.routes import training as route  # noqa: E402
from app.services import dataset_preflight, hf_publish, training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.services.credit_texts_de import DISPATCH_OFF_DE, NO_HOURS_DE, POLICY_REFUSED_DE  # noqa: E402
from app.services.training_sweep import LEGACY_CANCEL_GIVEUP_DE, MAX_CANCEL_ATTEMPTS  # noqa: E402
from app.tests._managed_fakes import SHA, Clock, FakeDB, FakeModal, FakeRunpod, PgError  # noqa: E402
from app.tests.test_training_dispatch import settings  # noqa: E402

USER = SimpleNamespace(id="u-1")


def info(features=None, fps=30):
    feats = features or {
        "observation.state": {"dtype": "float32", "shape": [6], "names": ["j"] * 6},
        "action": {"dtype": "float32", "shape": [6], "names": ["j"] * 6},
        "observation.images.gripper": {"dtype": "video", "shape": [480, 640, 3], "names": ["height", "width", "channels"]},
        "observation.images.scene": {"dtype": "video", "shape": [480, 640, 3], "names": ["height", "width", "channels"]},
    }
    return {"codebase_version": "v3.0", "fps": fps, "features": feats}


def request(model_type="act", **params):
    p = {"steps": 50000, "batch_size": 32, "num_workers": 4, "save_freq": 999, "seed": 1, "log_freq": 100}
    p.update(params)
    tp = SimpleNamespace(output_folder_name=None, steps=p["steps"],
                         model_dump=lambda exclude_none=True: {k: v for k, v in p.items() if v is not None})
    return SimpleNamespace(dataset_name="u/ds", model_type=model_type, training_params=tp)


class StartBase(unittest.IsolatedAsyncioTestCase):
    dispatch = "managed"

    async def asyncSetUp(self):
        self.clock = Clock(datetime.now(timezone.utc).timestamp())
        self.db = FakeDB(self.clock)
        self.preflight_calls, self.resolve_calls, self.kicks, self.legacy = [], [], [], []
        self.preflight_result = info()
        self.resolved = SHA
        self.s = settings(TRAINING_DISPATCH=self.dispatch)

        def _preflight(name, token, model_type, revision="v3.0"):
            self.preflight_calls.append((name, token, model_type, revision))
            if isinstance(self.preflight_result, BaseException):
                raise self.preflight_result
            return copy.deepcopy(self.preflight_result)

        def _resolve(name):
            self.resolve_calls.append(name)
            if isinstance(self.resolved, BaseException):
                raise self.resolved
            return self.resolved

        self._cms = [
            patch.object(route, "get_supabase", lambda: self.db),
            patch.object(route, "get_user_profile",
                         lambda uid: {"id": uid, "workgroup_id": self.db.workgroup_of.get(uid),
                                      "role": self.db.roles.get(uid, "student")}),
            patch.object(dataset_preflight, "preflight_dataset", _preflight),
            patch.object(dataset_preflight, "resolve_v3_commit", _resolve),
            patch.object(D, "SETTINGS", self.s),
            patch.object(D, "kick", lambda: self.kicks.append(1)),
            patch.dict("os.environ", {"HF_TOKEN": "hf_platform"}),
        ]
        for cm in self._cms:
            cm.__enter__()

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def start(self, req=None):
        return await route.start_training(req or request(), user=USER)


class ManagedStart(StartBase):
    async def test_a_managed_act_start(self):
        resp = await self.start()
        self.assertEqual(resp.status, "queued")
        (call,) = self.db.calls("start_training_safe")
        self.assertIs(call["p_managed"], True)
        self.assertIsNone(call["p_worker_token"])
        params = call["p_training_params"]
        self.assertEqual(params["save_freq"], 2500)
        self.assertEqual(params["mirror_every"], 10000)
        self.assertNotIn("num_workers", params)
        self.assertEqual(params["batch_size"], 8, "ACT is pinned to batch 8")
        self.assertEqual(params["dataset_revision"], SHA)
        self.assertEqual(call["p_total_steps"], 50000)
        row = self.db.row(resp.training_id)
        self.assertEqual((row["managed"], row["dataset_fps"], row["status"]), (True, 30, "queued"))
        self.assertEqual(self.kicks, [1])
        # 052: the reservation, its key and the deploy's ceiling
        self.assertEqual((call["p_idempotency_key"], call["p_max_hours"]), (None, 3))
        self.assertEqual((resp.reserved_hours, resp.replay), (3, False))
        self.assertEqual((row["credit_scheme"], row["reserved_hours"]), ("ledger", 3))
        # E17: anonymous, at the resolved commit
        self.assertEqual(self.resolve_calls, ["u/ds"])
        self.assertEqual(self.preflight_calls, [("u/ds", False, "act", SHA)])

    async def test_the_pep440_fallback_runs_unpinned(self):
        self.resolved = None
        resp = await self.start()
        params = self.db.row(resp.training_id)["training_params"]
        self.assertNotIn("dataset_revision", params)
        self.assertEqual(self.preflight_calls[0][3], "v3.0")

    async def test_the_e2e_cadences(self):
        self.s = settings(TRAINING_SAVE_FREQ="20", TRAINING_MIRROR_EVERY="40", TRAINING_E2E="1")
        with patch.object(D, "SETTINGS", self.s):
            resp = await self.start()
        params = self.db.row(resp.training_id)["training_params"]
        self.assertEqual((params["save_freq"], params["mirror_every"]), (20, 40))

    async def test_the_dedupe_ignores_the_stored_revision(self):
        first = await self.start()
        second = await self.start()
        self.assertEqual(first.training_id, second.training_id)
        self.assertEqual(len(self.db.calls("start_training_safe")), 1)
        self.assertEqual(len(self.preflight_calls), 1, "a dedupe hit costs no Hub call")

    async def test_s20_before_any_credit(self):
        base = info()["features"]
        five = dict(base, **{f"observation.images.c{i}": base["observation.images.scene"] for i in range(3)})
        env = dict(base, **{"observation.environment_state": {"dtype": "float32", "shape": [4]}})
        nocam = {k: v for k, v in base.items() if not k.startswith("observation.images.")}
        for name, feats in (("fifth camera", five), ("environment state", env), ("no camera", nocam)):
            self.preflight_result = info(feats)
            with self.subTest(case=name):
                with self.assertRaises(HTTPException) as ctx:
                    await self.start(request(steps=50001 + len(self.preflight_calls)))
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (400, T.S20))
        self.assertEqual(self.db.calls("start_training_safe"), [])

    async def test_preflight_refusals_and_outages(self):
        self.preflight_result = dataset_preflight.DatasetRefused("Dataset 'u/ds' enthält keine Episoden.")
        with self.assertRaises(HTTPException) as ctx:
            await self.start()
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (400, "Dataset 'u/ds' enthält keine Episoden."))
        self.preflight_result = RuntimeError("dns")
        with self.assertRaises(HTTPException) as ctx:
            await self.start()
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (502, T.HF_UNREACHABLE_DE))
        self.preflight_result = info()
        self.resolved = dataset_preflight.DatasetRefused(dataset_preflight.not_found_sentence("u/ds"))
        with self.assertRaises(HTTPException) as ctx:
            await self.start()
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("nicht gefunden oder ist privat", ctx.exception.detail)
        self.assertEqual(self.db.calls("start_training_safe"), [])

    async def test_a_hung_hub_is_502_within_the_bound(self):
        import threading

        gate = threading.Event()

        def _hang(name):
            gate.wait(5)
            return SHA

        real_wait_for = asyncio.wait_for

        async def _short(aw, timeout):
            return await real_wait_for(aw, 0.2)

        with patch.object(dataset_preflight, "resolve_v3_commit", _hang), patch.object(route.asyncio, "wait_for", _short):
            with self.assertRaises(HTTPException) as ctx:
                await self.start()
        gate.set()
        self.assertEqual(ctx.exception.status_code, 502)

    async def test_another_policy_is_refused_and_recorded(self):
        # 052 (H4, decision 12): only managed ACT trains; the route's 400 is German and the
        # refusal is recorded (a pydantic validator's 422 was English and recorded nothing).
        for model_type in ("diffusion", "pi0", "PI0"):
            with self.subTest(model_type=model_type):
                with self.assertRaises(HTTPException) as ctx:
                    await self.start(request(model_type=model_type))
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (400, POLICY_REFUSED_DE))
        self.assertEqual(self.db.calls("start_training_safe"), [])
        self.assertEqual(self.preflight_calls, [], "refused before the 60 s dataset check")
        self.assertEqual([(r["code"], r["model_type"]) for r in self.db.refusals],
                         [("policy", "diffusion"), ("policy", "pi0"), ("policy", "pi0")])
        self.assertFalse(hasattr(route, "ALLOWED_POLICIES"))
        self.assertEqual(route.TRAINABLE_POLICIES, frozenset({"act"}))
        self.assertEqual(route.POLICY_MAX_TIMEOUT_HOURS["act"], 3.0)

    async def test_the_clients_hidden_form_fields_never_reach_the_row(self):
        # Owner D4/E3: the server fixes batch 8, drops num_workers and writes its own cadences
        await self.start(request(num_workers=9, save_freq=123, batch_size=64))
        (call,) = self.db.calls("start_training_safe")
        params = call["p_training_params"]
        self.assertNotIn("num_workers", params)
        self.assertEqual((params["save_freq"], params["mirror_every"], params["batch_size"]), (2500, 10000, 8))
        self.assertFalse(hasattr(D, "LEGACY_ACT_NUM_WORKERS"))
        self.assertFalse(hasattr(D, "LEGACY_ACT_SAVE_FREQ"))

    def test_the_training_job_model_carries_the_note_and_the_hours(self):
        ann = route.TrainingJob.__annotations__
        for name in ("status_note", "managed", "reserved_hours", "charged_hours", "stop_requested_by_role",
                     "lost_seconds"):
            self.assertIn(name, ann)


class LegacyDispatchStart(StartBase):
    """052 (§9.1): TRAINING_DISPATCH=legacy no longer dispatches anything new."""

    dispatch = "legacy"

    async def test_a_start_under_legacy_answers_503_and_records_nothing(self):
        for model_type in ("act", "diffusion"):
            with self.subTest(model_type=model_type):
                with self.assertRaises(HTTPException) as ctx:
                    await self.start(request(model_type=model_type))
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (503, DISPATCH_OFF_DE))
        self.assertEqual(self.db.calls("start_training_safe"), [])
        self.assertEqual(self.db.refusals, [], "not the student's doing: not recorded")
        self.assertEqual(self.preflight_calls, [])

    def test_the_legacy_modal_dispatch_is_gone(self):
        import inspect

        src = inspect.getsource(route)
        self.assertNotIn("start_training_job", src)
        self.assertNotIn("LEGACY_ACT", src)


class ResolveV3Commit(unittest.TestCase):
    """TR-1: the tag resolution's Hub errors."""

    def _run(self, refs=None, raises=None):
        import httpx

        class Err(Exception):
            def __init__(self, status=None):
                super().__init__("x")
                self.response = SimpleNamespace(status_code=status) if status else None

        class NotFound(Err):
            pass

        class Gated(NotFound):
            pass

        class Api:
            def __init__(self, token=None):
                assert token is False

            def list_repo_refs(self, repo_id, repo_type=None):
                assert repo_type == "dataset"
                if raises is not None:
                    raise raises(Err, NotFound, Gated)
                return refs

        hub = SimpleNamespace(HfApi=Api, HfHubHTTPError=Err, RepositoryNotFoundError=NotFound,
                              RevisionNotFoundError=NotFound, GatedRepoError=Gated, hf_hub_download=None)
        with patch.object(dataset_preflight, "_hub", lambda: hub):
            return dataset_preflight.resolve_v3_commit("u/ds")

    def test_the_tag_resolves(self):
        refs = SimpleNamespace(tags=[SimpleNamespace(name="v2.1", target_commit="1" * 40),
                                     SimpleNamespace(name="v3.0", target_commit=SHA)])
        self.assertEqual(self._run(refs), SHA)

    def test_no_tag_is_the_preflights_sentence(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(SimpleNamespace(tags=[]))
        self.assertIn("'v3.0'-Versions-Tag", str(ctx.exception))

    def test_a_pep440_shaped_sha_runs_unpinned(self):
        for sha in ("1234567890123456789a12345678901234567890", "1" * 40, "12345678901234567890b1234567890123456789"):
            refs = SimpleNamespace(tags=[SimpleNamespace(name="v3.0", target_commit=sha)])
            with self.subTest(sha=sha):
                self.assertIsNone(self._run(refs))
        refs = SimpleNamespace(tags=[SimpleNamespace(name="v3.0", target_commit="1e" + "0" * 38)])
        self.assertEqual(self._run(refs), "1e" + "0" * 38)

    def test_missing_private_gated_are_400_others_propagate(self):
        for raiser in (lambda E, NF, G: NF(), lambda E, NF, G: G(), lambda E, NF, G: E(401), lambda E, NF, G: E(403),
                       lambda E, NF, G: E(404)):
            with self.assertRaises(ValueError) as ctx:
                self._run(raises=raiser)
            self.assertIn("nicht gefunden oder ist privat", str(ctx.exception))
        for raiser in (lambda E, NF, G: E(500), lambda E, NF, G: E(None), lambda E, NF, G: TimeoutError()):
            with self.assertRaises(Exception) as ctx:
                self._run(raises=raiser)
            self.assertNotIsInstance(ctx.exception, ValueError)

    def test_a_malformed_target_is_not_a_400(self):
        with self.assertRaises(RuntimeError):
            self._run(SimpleNamespace(tags=[SimpleNamespace(name="v3.0", target_commit="main")]))


class Cancel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        D.reset_state_for_tests()
        self.clock = Clock()
        self.db = FakeDB(self.clock)
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        from app.services import stop_actions

        self._cms = [patch.object(route, "get_supabase", lambda: self.db), patch.object(D, "_sb", lambda: self.db),
                     patch.object(stop_actions, "_sb", lambda: self.db),
                     patch.object(hf_publish, "schedule_publish", lambda tid, n: True),
                     self.rp.installed(), self.modal.installed()]
        for cm in self._cms:
            cm.__enter__()

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def cancel(self, tid, user="u-1"):
        return await route.cancel_training(SimpleNamespace(training_id=tid), user=SimpleNamespace(id=user))

    def managed(self, state=None, backend="runpod_community", ref="pod1", **kw):
        row = self.db.add_training(status="running", current_attempt=1 if state else 0, **kw)
        if state:
            self.db.add_attempt(row["id"], 1, state=state, backend=backend, cloud_ref=ref)
        if backend.startswith("runpod") and ref:
            self.rp.add_pod(id=ref)
        return row

    async def test_errors(self):
        with self.assertRaises(HTTPException) as ctx:
            await self.cancel(999)
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (404, T.TRAINING_NOT_FOUND_DE))
        row = self.db.add_training(status="succeeded")
        with self.assertRaises(HTTPException) as ctx:
            await self.cancel(row["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (400, T.TRAINING_NOT_ACTIVE_DE))
        with self.assertRaises(HTTPException) as ctx:
            await self.cancel(row["id"], user="someone-else")
        self.assertEqual(ctx.exception.status_code, 404)
        pub = self.managed("ended")
        self.db.attempt(pub["id"], 1)["end_reason"] = "succeeded"
        with self.assertRaises(HTTPException) as ctx:
            await self.cancel(pub["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S9))

    async def test_every_attempt_state(self):
        for state, stops in (("running", True), ("starting", True), ("placing", False), ("lost", False), (None, False)):
            with self.subTest(state=state):
                self.rp.calls.clear()
                row = self.managed(state, ref=f"pod-{state}" if state in ("running", "starting") else None)
                out = await self.cancel(row["id"])
                self.assertEqual(out["status"], "canceled")
                self.assertEqual(self.db.row(row["id"])["status"], "canceled")
                self.assertEqual(bool(self.rp.deletes()), stops)

    async def test_the_cancel_counts_the_trained_seconds_of_the_attempt_it_ends(self):
        # Fix round 2026-10-19: complete_cancel_training adds now − first_heartbeat_at, as finish_training does.
        row = self.managed("running", ref="pod-s", train_seconds_used=100)
        self.db.attempt(row["id"], 1)["first_heartbeat_at"] = self.clock.iso(self.clock.t - 900)
        self.assertEqual((await self.cancel(row["id"]))["status"], "canceled")
        self.assertEqual(self.db.row(row["id"])["train_seconds_used"], 1000)
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "ended")

    async def test_the_cancel_never_counts_an_attempt_a_requeue_already_counted(self):
        row = self.managed("lost", ref=None, train_seconds_used=300)
        self.db.attempt(row["id"], 1)["first_heartbeat_at"] = self.clock.iso(self.clock.t - 900)
        self.assertEqual((await self.cancel(row["id"]))["status"], "canceled")
        self.assertEqual(self.db.row(row["id"])["train_seconds_used"], 300)

    async def test_a_modal_attempt_is_cancelled_on_modal(self):
        row = self.managed("running", backend="modal", ref="fc-7")
        await self.cancel(row["id"])
        self.assertEqual(self.modal.canceled, ["fc-7"])

    async def test_the_answer_carries_the_outcome_and_the_charge(self):
        # 052 §8.2: {status, training_id, outcome, checkpoint_step, charged_hours}
        row = self.managed("running", ref="pod-x", credit_scheme="ledger", reserved_hours=3)
        self.db.attempt(row["id"], 1)["first_heartbeat_at"] = self.clock.iso(self.clock.t - 5000)
        out = await self.cancel(row["id"])
        self.assertEqual(out, {"status": "canceled", "training_id": row["id"], "outcome": "canceled",
                               "checkpoint_step": None, "charged_hours": 2})
        (call,) = self.db.calls("stop_training")
        self.assertEqual((call["p_actor_role"], call["p_note"], call["p_user_id"], call["p_actor_id"]),
                         ("student", T.S30, "u-1", "u-1"))
        self.assertEqual(self.db.row(row["id"])["stop_requested_by_role"], "student")

    async def test_a_failed_stop_waits_and_the_cap_gives_s21(self):
        row = self.managed("running", ref="pod9")
        self.rp.delete_answers["pod9"] = [500] * 20
        out = await self.cancel(row["id"])
        self.assertEqual(out["status"], "cancel_requested")
        self.db.row(row["id"])["cancel_attempts"] = MAX_CANCEL_ATTEMPTS - 1
        out = await self.cancel(row["id"])
        self.assertEqual(out["status"], "failed")
        self.assertEqual(self.db.row(row["id"])["error_message"], T.S21)

    async def test_legacy_rows_keep_their_behaviour_through_the_cas(self):
        # a pre-052 (derived) unmanaged row takes stop_training's cancel branch and today's
        # Modal cancel (modal_client.cancel_training_job, the FakeModal here)
        row = self.db.add_training(status="running", managed=False, cloud_job_id="fc-L")
        self.assertEqual((await self.cancel(row["id"]))["status"], "canceled")
        self.assertEqual(self.modal.canceled, ["fc-L"])
        row2 = self.db.add_training(status="running", managed=False, cloud_job_id="fc-M",
                                    cancel_attempts=MAX_CANCEL_ATTEMPTS - 1)
        self.modal.cancel_raises = RuntimeError("modal down")
        self.assertEqual((await self.cancel(row2["id"]))["status"], "failed")
        self.assertEqual(self.db.row(row2["id"])["error_message"], LEGACY_CANCEL_GIVEUP_DE)
        self.modal.cancel_raises = None
        row3 = self.db.add_training(status="queued", managed=False, cloud_job_id=None)
        self.assertEqual((await self.cancel(row3["id"]))["status"], "canceled")

    async def test_a_lost_complete_answers_the_current_status(self):
        row = self.managed(None)
        self.db._rpc_complete_cancel_training = lambda **kw: (_ for _ in ()).throw(PgError("P0049"))
        self.db.row(row["id"])  # status becomes cancel_requested via the CAS
        out = await self.cancel(row["id"])
        self.assertEqual(out["status"], "cancel_requested")
        self.assertEqual(out["outcome"], "cancel_requested")


class LegacyReconcilers(unittest.IsolatedAsyncioTestCase):
    async def test_a_managed_row_is_returned_untouched(self):
        db = FakeDB(Clock())
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        row = db.add_training(status="queued", managed=True, cloud_job_id=None, requested_at=old)
        status = AsyncMock(return_value="FAILED")
        with patch.object(route, "get_supabase", lambda: db), patch.object(route, "get_job_status", status):
            out = await route._sync_modal_status(dict(row))
        self.assertEqual(out["status"], "queued")
        status.assert_not_awaited()
        self.assertEqual([o for o in db.ops if o[0] == "update"], [])

    async def test_the_legacy_stall_rule_is_last_progress_at(self):
        db = FakeDB(Clock())
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        row = db.add_training(status="running", managed=False, cloud_job_id="fc-1", last_progress_at=old,
                              step_advanced_at=datetime.now(timezone.utc).isoformat())
        with patch.object(route, "get_supabase", lambda: db), \
                patch.object(route, "get_job_status", AsyncMock(return_value="IN_PROGRESS")), \
                patch.object(route, "cancel_training_job", AsyncMock()):
            out = await route._sync_modal_status(dict(row))
        self.assertEqual(out["status"], "failed")

    async def test_the_start_sweep_skips_managed_rows(self):
        db = FakeDB(Clock())
        db.add_training(status="running", managed=True, user_id="u-1")
        db.add_training(status="running", managed=False, user_id="u-1", cloud_job_id="fc-2")
        seen = []

        async def _sync(row):
            seen.append(row["managed"])
            return row

        with patch.object(route, "get_supabase", lambda: db), patch.object(route, "_sync_modal_status", _sync):
            await route._sweep_user_running_jobs("u-1")
        self.assertEqual(seen, [False])

    async def test_the_active_sweep_reads_only_legacy_rows(self):
        from app.services import training_sweep

        db = FakeDB(Clock())
        db.add_training(status="running", managed=True)
        legacy = db.add_training(status="running", managed=False, cloud_job_id="fc-3")
        seen = []

        async def _sync(row):
            seen.append(row["id"])
            return row

        with patch("app.services.supabase_client.get_supabase", lambda: db), patch.object(route, "_sync_modal_status", _sync):
            n = await training_sweep.active_sweep_tick()
        self.assertEqual((n, seen), (1, [legacy["id"]]))


class Continue(StartBase):
    """POST /trainings/continue (owner E32–E37, E46): a second credit, a managed child."""

    def _parent(self, **kw):
        defaults = dict(user_id="u-1", status="succeeded", managed=True, capped_step=42500, total_steps=50000,
                        model_name="EduBotics-Solutions/m-act-ds-abc", dataset_name="u/ds", dataset_fps=30.0,
                        training_params={"steps": 50000, "save_freq": 2500, "dataset_revision": SHA},
                        continuable_until=self.clock.iso(self.clock.t + D.KEEP_S))
        defaults.update(kw)
        p = self.db.add_training(**defaults)
        if p["status"] == "succeeded" and p.get("capped_step") is not None:
            self.db.add_kept(p["id"], 42500, manifest={"a": {"size": 1}},
                             expires_at=self.clock.iso(self.clock.t + D.KEEP_S))
        return p

    async def cont(self, pid, user_id="u-1"):
        return await route.continue_training(SimpleNamespace(training_id=pid), user=SimpleNamespace(id=user_id))

    async def test_happy_path(self):
        parent = self._parent()
        resp = await self.cont(parent["id"])
        child = self.db.row(resp.training_id)
        self.assertEqual((child["managed"], child["status"], child["continued_from"]),
                         (True, "queued", parent["id"]))
        self.assertEqual((child["current_step"], child["capped_step"], child["current_attempt"]), (42500, None, 0))
        self.assertEqual(child["status_note"], T.s28(42500))
        self.assertEqual(child["training_params"], parent["training_params"])
        self.assertEqual((child["model_name"], child["dataset_fps"]), (parent["model_name"], 30.0))
        self.assertEqual(child["user_id"], "u-1")
        self.assertEqual((resp.model_name, resp.status), (parent["model_name"], "queued"))
        self.assertEqual(self.kicks, [1])
        self.assertEqual(self.preflight_calls, [("u/ds", False, "act", SHA)])
        self.assertEqual(self.resolve_calls, [], "the pinned revision is reused, never re-resolved (E17)")
        self.assertEqual(self.db.row(parent["id"])["status"], "succeeded", "the parent is unchanged")

    async def test_404_for_another_user_and_a_missing_id(self):
        parent = self._parent(user_id="someone-else")
        self.db.tables["trainings"][-1]["user_id"] = "someone-else"
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(parent["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (404, T.TRAINING_NOT_FOUND_DE))
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(999999)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(len(self.db.tables["trainings"]), 1, "no row created")

    async def test_404_for_another_users_non_continuable_row_without_a_preflight(self):
        # The route's own owner check (verifier m5): another user's row answers 404 whatever its
        # state, never 409 S26/S24 (an existence/state oracle), and costs no dataset preflight.
        for kw in ({"status": "failed"}, {"capped_step": None}, {"managed": False},
                   {"continuable_until": self.clock.iso(self.clock.t - 1)}):
            with self.subTest(kw=kw):
                p = self._parent(user_id="someone-else", **kw)
                with self.assertRaises(HTTPException) as ctx:
                    await self.cont(p["id"])
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (404, T.TRAINING_NOT_FOUND_DE))
        self.assertEqual(self.preflight_calls, [])

    async def test_409_s26_for_a_non_continuable_parent(self):
        for kw in ({"capped_step": None}, {"status": "failed"}, {"managed": False}):
            with self.subTest(kw=kw):
                p = self._parent(**kw)
                with self.assertRaises(HTTPException) as ctx:
                    await self.cont(p["id"])
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S26))

    async def test_409_s26_under_legacy_dispatch(self):
        p = self._parent()
        with patch.object(D, "SETTINGS", settings(TRAINING_DISPATCH="legacy")):
            with self.assertRaises(HTTPException) as ctx:
                await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S26))

    async def test_409_s24_when_expired(self):
        p = self._parent(continuable_until=self.clock.iso(self.clock.t - 1))
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S24))

    async def test_409_s24_from_the_database_p0053(self):
        p = self._parent()
        self.db._rpc_start_continuation_safe = lambda **kw: (_ for _ in ()).throw(PgError("P0053"))
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S24))

    async def test_409_s25_while_a_child_is_queued_then_ok_after_it_failed(self):
        p = self._parent()
        await self.cont(p["id"])
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (409, T.S25))
        for c in self.db.tables["trainings"]:
            if c.get("continued_from") == p["id"]:
                c["status"] = "failed"
        resp = await self.cont(p["id"])
        self.assertTrue(resp.training_id)

    async def test_403_without_free_hours(self):
        p = self._parent()
        self.db.hours[("student", "u-1")] = 0
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (403, NO_HOURS_DE))
        self.assertEqual([(r["request_kind"], r["code"]) for r in self.db.refusals], [("continue", "no_hours")])

    async def test_a_workgroup_member_spends_the_groups_hours(self):
        self.db.workgroup_of["u-1"] = "g1"
        self.db.hours[("group", "g1")] = 2
        p = self._parent(workgroup_id="g1")
        resp = await self.cont(p["id"])
        self.assertEqual(self.db.row(resp.training_id)["workgroup_id"], "g1")
        self.assertEqual(resp.reserved_hours, 2, "a new reservation, capped by the free hours")
        (call,) = self.db.calls("start_continuation_safe")
        self.assertEqual(call["p_max_hours"], 3)

    async def test_only_a_student_continues(self):
        # 052 (H6): a continuation is a new reservation, so the role rule applies — a teacher
        # never trains, not even their own earlier training.
        p = self._parent()
        self.db.roles["u-1"] = "teacher"
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual([(r["request_kind"], r["code"]) for r in self.db.refusals], [("continue", "role")])
        self.assertEqual(self.db.calls("start_continuation_safe"), [])

    async def test_400_and_502_on_the_dataset_check(self):
        p = self._parent()
        self.preflight_result = dataset_preflight.DatasetRefused(T.S20)
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual(ctx.exception.status_code, 400)
        self.preflight_result = RuntimeError("hub down")
        with self.assertRaises(HTTPException) as ctx:
            await self.cont(p["id"])
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (502, T.HF_UNREACHABLE_DE))
        self.assertFalse(any(c.get("continued_from") == p["id"] for c in self.db.tables["trainings"]))

    async def test_list_and_get_carry_is_mine(self):
        parent = self._parent()
        with patch.object(route, "resolve_visible_workgroup_ids", lambda uid: []):
            mine = await route.list_trainings(user=SimpleNamespace(id="u-1"))
            self.assertTrue(all(t["is_mine"] for t in mine if t["id"] == parent["id"]))
            got = await route.get_training(parent["id"], user=SimpleNamespace(id="u-1"))
            self.assertTrue(got["is_mine"])
        # a groupmate reads the same row (visible via the group) with is_mine False,
        # through /trainings/list too: the page's jobs come from there (verifier M3).
        self.db.tables["trainings"][-1]["workgroup_id"] = "g1"
        with patch.object(route, "resolve_visible_workgroup_ids", lambda uid: ["g1"]):
            got = await route.get_training(parent["id"], user=SimpleNamespace(id="other"))
            listed = await route.list_trainings(user=SimpleNamespace(id="other"))
        self.assertFalse(got["is_mine"])
        rows = [t for t in listed if t["id"] == parent["id"]]
        self.assertEqual(len(rows), 1, "the groupmate sees the group's row in the list")
        self.assertIs(rows[0]["is_mine"], False)

    async def test_a_fresh_start_is_not_deduped_onto_a_child(self):
        parent = self._parent()
        await self.cont(parent["id"])  # a child with the same dataset/model/params exists
        resp = await self.start(request(steps=50000, save_freq=2500))
        new = self.db.row(resp.training_id)
        self.assertIsNone(new["continued_from"], "the fresh start is its own row, not the child")


if __name__ == "__main__":
    unittest.main()
