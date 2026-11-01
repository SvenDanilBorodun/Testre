"""S4 and S18 publish like a cap (owner Q-A, spec §9.7).

``_budget_finish`` between attempts: four attempts used (S4 / S37) and 30 minutes
without a GPU (S18) publish the newest ready checkpoint (``attempts_out`` /
``no_gpu``, note S34, continuable) instead of failing; only without one the row fails,
S4 vs S37 chosen by the loss reasons (at least two dataset-class losses → S4).
"""

from __future__ import annotations

from app.tests.test_training_dispatch import Base  # noqa: F401  (stubs installed there)

from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402


class GiveUpPublish(Base):
    def attempts_out(self, reasons=(), ckpt=None):
        row = self.queued(current_attempt=D.MAX_ATTEMPTS, lost_reasons=list(reasons), credit_scheme="ledger",
                          reserved_hours=3)
        self.db.add_attempt(row["id"], D.MAX_ATTEMPTS, state="lost", backend="runpod_secure")
        if ckpt is not None:
            self.db.add_checkpoint(row["id"], 2, ckpt, state="ready", manifest={})
        return row

    def no_gpu(self, n=1, ckpt=None):
        row = self.queued(current_attempt=n, credit_scheme="ledger", reserved_hours=3,
                          unplaced_since=self.clock.iso(self.clock.t - D.PLACEMENT_GIVEUP_S - 1))
        if n:
            self.db.add_attempt(row["id"], n, state="lost", backend="runpod_community")
        if ckpt is not None:
            self.db.add_checkpoint(row["id"], n, ckpt, state="ready", manifest={})
        return row

    async def test_s4_with_a_ready_checkpoint_publishes_attempts_out(self):
        row = self.attempts_out(ckpt=27500)
        await D.dispatch_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["status_note"], r["capped_step"], r["error_message"]),
                         ("running", T.S34, 27500, None))
        self.assertEqual(self.db.attempt(row["id"], D.MAX_ATTEMPTS)["end_reason"], "attempts_out")
        (call,) = self.db.calls("cap_training_attempt")
        self.assertEqual((call["p_reason"], call["p_step"], call["p_train_seconds"]), ("attempts_out", 27500, 0))
        self.assertEqual(self.published, [(row["id"], D.MAX_ATTEMPTS)])
        self.assertEqual(self.db.calls("claim_training_attempt"), [], "no fifth attempt")

    async def test_s18_with_a_ready_checkpoint_publishes_no_gpu(self):
        row = self.no_gpu(n=2, ckpt=5000)
        await D.dispatch_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["status_note"], r["capped_step"]), ("running", T.S34, 5000))
        self.assertEqual(self.db.attempt(row["id"], 2)["end_reason"], "no_gpu")
        self.assertEqual(self.published, [(row["id"], 2)])

    async def test_without_a_checkpoint_two_dataset_class_losses_read_s4(self):
        row = self.attempts_out(reasons=["stalled", "pod_gone", "host_unfit:lerobot_crash", "heartbeat_lost"])
        await D.dispatch_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S4))
        self.assertEqual(self.published, [])

    async def test_two_unwaivable_s19_claims_read_s4(self):
        # 052 fix round 2 (owner): an S19 claim that could not lead to the waiver still says
        # LeRobot failed before any step — two of them point at the dataset
        for reasons in (["host_unfit:lerobot_error_not_fresh", "pod_gone", "host_unfit:lerobot_error_not_fresh"],
                        ["host_unfit:lerobot_error_waiver_refused", "host_unfit:lerobot_error_not_fresh"],
                        ["host_unfit:lerobot_error_waiver_refused"] * 2):
            with self.subTest(reasons=reasons):
                row = self.attempts_out(reasons=reasons)
                await D.dispatch_tick()
                self.assertEqual(self.db.row(row["id"])["error_message"], T.S4)
        self.assertEqual(T.attempts_out_message(["host_unfit:lerobot_error_not_fresh", "pod_gone"]), T.S37)

    async def test_without_a_checkpoint_host_losses_read_s37(self):
        for reasons in ([], ["pod_gone", "heartbeat_lost", "start_timeout", "stalled"],
                        ["checkpoint_overdue"] * 4, ["host_unfit:time_cap_early", "host_unfit:download"]):
            with self.subTest(reasons=reasons):
                row = self.attempts_out(reasons=reasons)
                await D.dispatch_tick()
                self.assertEqual(self.db.row(row["id"])["error_message"], T.S37)

    def test_the_dataset_class_rule(self):
        self.assertEqual(T.DATASET_CLASS_LOSSES, frozenset({
            "stalled", "host_unfit:lerobot_crash", "host_unfit:resume_crash",
            "host_unfit:lerobot_error_unconfirmed", "host_unfit:content_unconfirmed",
            "host_unfit:lerobot_error_not_fresh", "host_unfit:lerobot_error_waiver_refused"}))
        self.assertEqual(T.attempts_out_message(["stalled", "stalled"]), T.S4)
        self.assertEqual(T.attempts_out_message(["stalled", None, 7]), T.S37)
        self.assertEqual(T.attempts_out_message(None), T.S37)

    async def test_s18_without_a_checkpoint_fails_with_the_s18_text(self):
        for n in (0, 1):
            with self.subTest(attempt=n):
                row = self.no_gpu(n=n)
                await D.dispatch_tick()
                r = self.db.row(row["id"])
                self.assertEqual((r["status"], r["error_message"]), ("failed", T.S18))
                (call,) = [c for c in self.db.calls("finish_training") if c["p_training_id"] == row["id"]]
                self.assertEqual((call["p_attempt"], call["p_status"]), (n, "failed"))
        self.assertNotIn("erstatt", T.S18)
        self.assertEqual(self.published, [])

    async def test_attempts_out_is_judged_before_no_gpu(self):
        row = self.attempts_out(ckpt=2500)
        self.db.row(row["id"])["unplaced_since"] = self.clock.iso(self.clock.t - D.PLACEMENT_GIVEUP_S - 1)
        await D.dispatch_tick()
        self.assertEqual(self.db.attempt(row["id"], D.MAX_ATTEMPTS)["end_reason"], "attempts_out")

    async def test_a_ready_final_still_publishes_as_a_success(self):
        row = self.attempts_out()
        self.db.add_checkpoint(row["id"], 3, 50000, state="ready", manifest={})
        await D.dispatch_tick()
        self.assertEqual(self.db.attempt(row["id"], D.MAX_ATTEMPTS)["end_reason"], "succeeded")
        self.assertEqual(self.db.calls("cap_training_attempt"), [])
        self.assertEqual(self.published, [(row["id"], D.MAX_ATTEMPTS)])


if __name__ == "__main__":
    import unittest

    unittest.main()
