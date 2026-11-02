"""``lerobot_error`` on a resume is a ``resume_crash`` (review 3 m5, spec §9.6).

The honest worker sends ``failed/lerobot_error`` (S19) only from a FRESH start; on a
resume (a checkpoint of an earlier attempt, or a continuation's kept copy) the API
counts it as a ``resume_crash`` against the handed checkpoint and requeues. It never
reads S19 and never passes the charge waiver.
"""

from __future__ import annotations

import unittest

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.routes import training_worker as route  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import manifest_of  # noqa: E402


class LerobotErrorOnAResume(WorkerBase):
    async def lerobot_error(self, attempt):
        return await self.call("finish", {"outcome": "failed", "reason": "lerobot_error"}, attempt=attempt)

    def resume_from_checkpoint(self, n):
        for a in self.db.tables["training_attempts"]:
            if a["training_id"] == self.tid and a["state"] == "running":
                a["state"] = "lost"
        self.db.add_attempt(self.tid, n, state="running", backend="runpod_secure" if n % 2 else "runpod_community",
                            cloud_ref=f"pod{n}", resume_from_step=2500, resume_from_attempt=1,
                            first_heartbeat_at=self.clock.iso(self.clock.t - 60))
        self.db.row(self.tid).update(current_attempt=n, status="running", worker_token=TOKEN)
        return str(n)

    def no_s19(self):
        r = self.db.row(self.tid)
        self.assertNotEqual(r["error_message"], T.S19)
        for call in self.db.calls("finish_training"):
            self.assertNotEqual(call.get("p_message"), T.S19)
            self.assertNotIn("p_charge_waiver", call)

    async def test_a_checkpoint_resume_counts_resume_crash_and_requeues(self):
        files = self.upload(2500)
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest=manifest_of(files))
        self.assertEqual(await self.lerobot_error(self.resume_from_checkpoint(2)), (200, {"status": "requeued"}))
        a = self.db.attempt(self.tid, 2)
        self.assertEqual(a["end_reason"], "host_unfit:resume_crash")
        self.assertEqual(self.db.checkpoint(self.tid, 1, 2500)["crash_attempts"], [2])
        self.assertEqual(self.db.row(self.tid)["status"], "queued")
        # a second (distinct) attempt crashing on it refuses the checkpoint, still no S19
        self.assertEqual(await self.lerobot_error(self.resume_from_checkpoint(3)), (200, {"status": "requeued"}))
        c = self.db.checkpoint(self.tid, 1, 2500)
        self.assertEqual((c["state"], c["refused_reason"]), ("refused", "resume_crash"))
        self.no_s19()

    async def test_a_kept_copy_resume_counts_against_the_kept_copy(self):
        from app.services import r2_client

        parent = self.db.add_training(status="succeeded", current_attempt=1, capped_step=2500,
                                      continuable_until=self.clock.iso(self.clock.t + 3600))
        files = self.upload(2500)
        for rel, data in files.items():
            self.r2.objects[r2_client.keep_key(parent["id"], 2500, rel)] = data
        self.db.add_kept(parent["id"], 2500, manifest=manifest_of(files))
        self.db.row(self.tid).update(continued_from=parent["id"])
        self.att.update(resume_from_step=2500, resume_from_attempt=None)
        self.assertEqual(await self.lerobot_error("1"), (200, {"status": "requeued"}))
        self.assertEqual(self.db.kept(parent["id"])["bad_attempts"], [f"{self.tid}:1"])
        self.assertEqual(self.att["end_reason"], "host_unfit:resume_crash")
        self.no_s19()

    async def test_a_fresh_start_keeps_the_s19_path(self):
        self.assertEqual(await self.lerobot_error("1"), (200, {"status": "requeued"}))
        self.assertEqual(self.att["end_reason"], route.MARK_LEROBOT_ERROR)


if __name__ == "__main__":
    unittest.main()
