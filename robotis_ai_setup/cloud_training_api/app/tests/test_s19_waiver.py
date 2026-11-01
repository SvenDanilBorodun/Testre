"""The S19 charge waiver needs a provable fresh start (owner fix round 2026-11-01, V1-M1).

The waiver (``finish_training(…, p_charge_waiver='lerobot_error')``) charges the WHOLE
training 0, so ``_finish`` asks for it only when the database says the attempt is a
fresh start: ``resume_from_step`` 0 (NULL means ``/start`` never ran), state
``running``, no continuation, no counted training second and no ready checkpoint of the
training. Otherwise a ``lerobot_error`` is requeued like a lost attempt (charged by E50)
with ``MARK_LEROBOT_ERROR_NOT_FRESH``; ``resume_from_step > 0`` stays a resume_crash. If the
database refuses the waiver anyway (P0073) the API logs an ERROR and requeues without it.
The two verifier scenarios (hosts that skip ``/start``; a waiver after an earlier ready
checkpoint) are the first two tests and fail on the code before this round.
"""

from __future__ import annotations

import unittest

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.routes import training_worker as route  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import PgError, checkpoint_files, manifest_of  # noqa: E402


class Waiver(WorkerBase):
    def setup_attempts(self, *, current, earlier_marker=route.MARK_LEROBOT_ERROR, **row):
        """Attempt 1 (lost, another GPU = a distinct host) already reported lerobot_error;
        attempt 2 is the current one with the given fields."""
        self.db.tables["training_attempts"] = [a for a in self.db.tables["training_attempts"]
                                               if a["training_id"] != self.tid]
        self.db.add_attempt(self.tid, 1, state="lost", end_reason=earlier_marker, backend="runpod_community",
                            gpu_type="NVIDIA RTX 3090", data_center=None, resume_from_step=0,
                            first_heartbeat_at=self.clock.iso(self.clock.t - 7200))
        spec = dict(state="running", backend="runpod_community", cloud_ref="pod1", gpu_type="NVIDIA RTX A5000",
                    data_center=None, resume_from_step=0, first_heartbeat_at=self.clock.iso(self.clock.t - 600))
        spec.update(current)
        self.att = self.db.add_attempt(self.tid, 2, **spec)
        self.db.row(self.tid).update(current_attempt=2, status="running", worker_token=TOKEN, **row)

    async def lerobot_error(self):
        return await self.call("finish", {"outcome": "failed", "reason": "lerobot_error"}, attempt="2")

    def never_waived(self):
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"], r["charge_waiver"]), ("queued", None, None))
        self.assertEqual(self.db.calls("finish_training"), [])
        self.assertEqual(self.db.attempt(self.tid, 2)["end_reason"], route.MARK_LEROBOT_ERROR_NOT_FRESH)

    async def test_hosts_that_skip_start_are_never_waived(self):
        # verifier scenario 1: neither host called /start (resume_from_step NULL)
        self.setup_attempts(current={"state": "starting", "resume_from_step": None, "first_heartbeat_at": None})
        self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        self.never_waived()

    async def test_no_waiver_after_an_earlier_ready_checkpoint(self):
        # verifier scenario 2 (repro_waiver.py): an earlier attempt trained and saved; the
        # waiver would have charged its counted 7000 s as 0
        self.setup_attempts(current={"state": "starting", "resume_from_step": None, "first_heartbeat_at": None},
                            earlier_marker="pod_gone", train_seconds_used=7000)
        files = checkpoint_files(self.db.row(self.tid), 25000)
        self.r2.put_checkpoint(self.tid, 1, 25000, files)
        self.db.add_checkpoint(self.tid, 1, 25000, state="ready", manifest=manifest_of(files))
        self.db.add_attempt(self.tid, 3, state="lost", end_reason=route.MARK_LEROBOT_ERROR, backend="runpod_secure")
        self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        self.never_waived()
        self.assertEqual(self.db.row(self.tid)["train_seconds_used"], 7000, "the counted seconds stay charged")

    async def test_every_blocker_alone_prevents_the_waiver(self):
        cases = {
            "not_running": ({"state": "starting"}, {}),
            "continuation": ({}, {"continued_from": 999}),
            "trained_seconds": ({}, {"train_seconds_used": 1}),
            "ready_checkpoint": ({}, {}),
            "stepped": ({}, {"current_step": 100}),
        }
        for name, (current, row) in cases.items():
            with self.subTest(blocker=name):
                self.db.rpcs.clear()
                self.db.tables["training_checkpoints"] = []
                self.setup_attempts(current=current, **{"continued_from": None, "train_seconds_used": 0,
                                                        "current_step": 0, **row})
                if name == "ready_checkpoint":
                    self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest={})
                self.assertEqual(route._waiver_blockers(self.db.row(self.tid), self.att,
                                                        [c for c in self.db.tables["training_checkpoints"]
                                                         if c["state"] == "ready"]), [name])
                self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
                self.never_waived()

    async def test_a_reported_step_prevents_the_waiver_even_from_a_distinct_host(self):
        # an honest worker sends lerobot_error only when it logged no new step
        self.setup_attempts(current={}, current_step=500)
        self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        self.never_waived()

    async def test_a_provable_fresh_start_confirmed_by_a_distinct_host_is_waived(self):
        self.setup_attempts(current={})
        self.assertEqual(route._waiver_blockers(self.db.row(self.tid), self.att, []), [])
        self.assertEqual(await self.lerobot_error(), (200, {"status": "failed"}))
        (call,) = self.db.calls("finish_training")
        self.assertEqual((call["p_message"], call["p_charge_waiver"]), (T.S19, "lerobot_error"))

    async def test_a_resume_stays_a_resume_crash(self):
        self.setup_attempts(current={"resume_from_step": 2500, "resume_from_attempt": 1})
        self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        self.assertEqual(self.db.attempt(self.tid, 2)["end_reason"], "host_unfit:resume_crash")
        self.assertEqual(self.db.calls("finish_training"), [])

    async def test_the_databases_p0073_fails_closed(self):
        self.setup_attempts(current={})
        self.db.errors["finish_training"] = [PgError("P0073")]
        with self.assertLogs("app.routes.training_worker", level="ERROR") as logs:
            self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        self.assertTrue(any("P0073" in m for m in logs.output))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"], r["charge_waiver"]), ("queued", None, None))
        self.assertEqual(self.db.attempt(self.tid, 2)["end_reason"], route.MARK_LEROBOT_ERROR_WAIVER_REFUSED)
        self.assertEqual(self.logs, [], "no failed log: the training goes on")

    async def test_the_fake_database_refuses_an_unprovable_waiver_itself(self):
        # the API's fake models A's contract: finish_training raises P0073 for the waiver
        # without a provable fresh start, so an API that skipped its own check still fails closed
        self.setup_attempts(current={"state": "starting", "resume_from_step": None})
        with self.assertRaises(PgError) as ctx:
            self.db._rpc_finish_training(self.tid, 2, "failed", T.S19, None, p_charge_waiver="lerobot_error")
        self.assertEqual(ctx.exception.code, "P0073")
        self.assertEqual(self.db.row(self.tid)["status"], "running")


class EveryConditionOnItsOwn(unittest.TestCase):
    """``_waiver_blockers`` directly, one case per condition (the API's six conditions;
    ``resume_from_step`` 0 is two names: NULL = ``no_start``, > 0 = ``resumed``)."""

    FRESH_ROW = {"continued_from": None, "train_seconds_used": 0, "current_step": 0}
    FRESH_ATT = {"resume_from_step": 0, "state": "running"}

    def blockers(self, row=None, att=None, ready=()):
        return route._waiver_blockers({**self.FRESH_ROW, **(row or {})}, {**self.FRESH_ATT, **(att or {})},
                                      list(ready))

    def test_a_fresh_start_has_none(self):
        self.assertEqual(self.blockers(), [])

    def test_no_start(self):
        self.assertEqual(self.blockers(att={"resume_from_step": None}), ["no_start"])

    def test_resumed(self):
        # unreachable from _finish (a resume becomes a resume_crash first): defence in depth
        self.assertEqual(self.blockers(att={"resume_from_step": 2500}), ["resumed"])

    def test_not_running(self):
        for state in ("placing", "starting"):
            self.assertEqual(self.blockers(att={"state": state}), ["not_running"], state)

    def test_continuation(self):
        self.assertEqual(self.blockers(row={"continued_from": 41}), ["continuation"])

    def test_trained_seconds(self):
        self.assertEqual(self.blockers(row={"train_seconds_used": 1}), ["trained_seconds"])

    def test_ready_checkpoint(self):
        self.assertEqual(self.blockers(ready=[{"step": 2500, "state": "ready"}]), ["ready_checkpoint"])

    def test_stepped(self):
        self.assertEqual(self.blockers(row={"current_step": 1}), ["stepped"])

    def test_they_add_up(self):
        self.assertEqual(self.blockers(row={"continued_from": 1, "train_seconds_used": 9, "current_step": 9},
                                       att={"resume_from_step": None, "state": "starting"}, ready=[{}]),
                         ["no_start", "not_running", "continuation", "trained_seconds", "ready_checkpoint", "stepped"])


if __name__ == "__main__":
    unittest.main()
