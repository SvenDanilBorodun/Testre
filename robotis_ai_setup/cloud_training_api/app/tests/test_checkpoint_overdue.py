"""A checkpoint is due every two intervals (owner R3-3, spec §9.10).

The sweep (in memory, ledger rows only) starts a clock the first round the reported
step is more than 2 × the row's ``save_freq`` past ``overdue_ref`` (the attempt's resume
step or the highest step of ITS OWN ready checkpoints). A lowered step never resets it;
only a new own ready checkpoint that raises the ref clears it (an ``uploading`` one does
not). After ``CKPT_OVERDUE_GRACE_S`` (1 200 s) the attempt is lost as
``checkpoint_overdue`` (a host fault: the Community data centre is skipped), with note
S39 / S40. A ready final publishes first; the loss is not dataset-class.
"""

from __future__ import annotations

from app.tests.test_training_dispatch import Base  # noqa: F401  (stubs installed there)

from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402


class Overdue(Base):
    def running(self, *, step, save_freq=2500, resume=0, scheme="ledger"):
        params = {"steps": 50000, "save_freq": save_freq, "mirror_every": 10000, "batch_size": 8}
        row = self.db.add_training(status="running", current_attempt=2, worker_token="tok", credit_scheme=scheme,
                                   reserved_hours=3, current_step=step, training_params=params,
                                   last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        self.db.add_attempt(row["id"], 1, state="lost", backend="runpod_secure", end_reason="pod_gone")
        a = self.db.add_attempt(row["id"], 2, state="running", backend="runpod_community", cloud_ref="pod1",
                                gpu_type="NVIDIA RTX A5000", data_center="US-TX-3", resume_from_step=resume,
                                resume_from_attempt=1 if resume else None,
                                first_heartbeat_at=self.clock.iso(self.clock.t - 60))
        self.rp.add_pod(id="pod1", dataCenterId="US-TX-3", cudaVersion="12.8", status="RUNNING")
        return row, a

    async def round(self, advance=0.0, step=None):
        """One sweep round ``advance`` seconds later; the host keeps heartbeating."""
        self.clock.advance(advance)
        for r in self.db.tables["trainings"]:
            r["last_progress_at"] = r["step_advanced_at"] = self.clock.iso()
            if step is not None:
                r["current_step"] = step
        await D.sweep_tick()

    def state(self, row):
        return self.db.attempt(row["id"], 2)["state"]

    def test_the_reference_step(self):
        a = {"attempt_no": 2, "resume_from_step": 2500}
        ckpts = [{"attempt_no": 2, "step": 5000, "state": "ready"}, {"attempt_no": 2, "step": 7500, "state": "uploading"},
                 {"attempt_no": 1, "step": 20000, "state": "ready"}, {"attempt_no": 2, "step": 6000, "state": "refused"}]
        self.assertEqual(D.overdue_ref(a, ckpts), 5000, "own ready checkpoints only")
        self.assertEqual(D.overdue_ref(a, []), 2500)
        self.assertEqual(D.overdue_ref({"attempt_no": 2}, []), 0)
        self.assertEqual(D.CKPT_OVERDUE_GRACE_S, 1200)

    async def test_exactly_two_intervals_is_not_overdue(self):
        row, _ = self.running(step=5000)
        await self.round()
        await self.round(D.CKPT_OVERDUE_GRACE_S + 1)
        self.assertEqual(self.state(row), "running")
        self.assertEqual(D.STATE.overdue_first, {})

    async def test_overdue_for_1200_s_loses_the_attempt_with_s40(self):
        row, _ = self.running(step=5001)
        await self.round()
        self.assertIn((row["id"], 2), D.STATE.overdue_first)
        await self.round(D.CKPT_OVERDUE_GRACE_S - 1)
        self.assertEqual(self.state(row), "running")
        await self.round(1)
        a = self.db.attempt(row["id"], 2)
        r = self.db.row(row["id"])
        self.assertEqual((a["state"], a["end_reason"]), ("lost", "checkpoint_overdue"))
        self.assertEqual((r["status"], r["status_note"]), ("queued", T.S40))
        self.assertIn("pod1", self.rp.deletes())
        # a host fault: this host is skipped like a start timeout (skip_community_dc)
        self.assertIn(("COMMUNITY", "NVIDIA RTX A5000", "US-TX-3"), D.STATE.dc_skip)
        self.assertNotIn((row["id"], 2), D.STATE.overdue_first)

    async def test_with_a_resume_point_the_note_is_s39(self):
        row, _ = self.running(step=17501, resume=12500)
        self.db.add_checkpoint(row["id"], 1, 12500, state="ready", manifest={})
        await self.round()
        await self.round(D.CKPT_OVERDUE_GRACE_S)
        r = self.db.row(row["id"])
        self.assertEqual((r["status_note"], self.db.attempt(row["id"], 2)["end_reason"]),
                         (T.s39(12500), "checkpoint_overdue"))
        self.assertIn("12.500", r["status_note"])

    async def test_a_lowered_step_does_not_reset_the_clock(self):
        row, _ = self.running(step=9000)
        await self.round()
        await self.round(600, step=100)       # an in-place restart reports a lower step
        await self.round(600)
        self.assertEqual(self.db.attempt(row["id"], 2)["end_reason"], "checkpoint_overdue")

    async def test_only_a_new_own_ready_checkpoint_stops_the_clock(self):
        row, _ = self.running(step=9000)
        await self.round()
        self.db.add_checkpoint(row["id"], 2, 2500, state="uploading", manifest={})
        self.db.add_checkpoint(row["id"], 1, 7500, state="ready", manifest={})   # another attempt's
        await self.round(600)
        self.assertIn((row["id"], 2), D.STATE.overdue_first)
        first = D.STATE.overdue_first[(row["id"], 2)]
        self.assertEqual(first[1], 0)
        self.db.checkpoint(row["id"], 2, 2500)["state"] = "ready"
        await self.round(1)
        # ref 0 → 2500 clears the old clock; 9000 is still more than 2 × 2500 past 2500, so a
        # NEW clock starts in the same round, from now
        self.assertEqual(D.STATE.overdue_first[(row["id"], 2)], (self.clock.t, 2500))
        await self.round(D.CKPT_OVERDUE_GRACE_S - 1)
        self.assertEqual(self.state(row), "running", "the grace counts from the new clock")
        await self.round(1)
        self.assertEqual(self.db.attempt(row["id"], 2)["end_reason"], "checkpoint_overdue")

    async def test_the_threshold_follows_the_rows_own_save_freq(self):
        row, _ = self.running(step=40, save_freq=20)     # the e2e cadence: 2 × 20
        await self.round()
        self.assertEqual(D.STATE.overdue_first, {})
        await self.round(step=41)
        self.assertIn((row["id"], 2), D.STATE.overdue_first)

    async def test_a_ready_final_publishes_first(self):
        row, _ = self.running(step=50000)
        await self.round()
        self.db.add_checkpoint(row["id"], 2, 50000, state="ready", manifest={})
        await self.round(D.CKPT_OVERDUE_GRACE_S)
        self.assertEqual(self.db.attempt(row["id"], 2)["end_reason"], "succeeded")
        self.assertEqual(self.published, [(row["id"], 2)])

    async def test_a_derived_row_has_no_overdue_rule(self):
        row, _ = self.running(step=40000, scheme="derived")
        await self.round()
        await self.round(D.CKPT_OVERDUE_GRACE_S + 5)
        self.assertEqual(self.state(row), "running")

    async def test_heartbeats_without_step_progress_no_longer_hold_the_attempt(self):
        # 052 fix round (V1-m1, A's update_training_progress): a running write resets
        # step_advanced_at only when NULL, on the first queued → running write or when the
        # step rises, so a host that only heartbeats meets the stall rule
        row, _ = self.running(step=4000)
        tok = self.db.row(row["id"])["worker_token"]
        for _ in range(D.STALL_S // 240 + 1):
            self.clock.advance(240)
            self.db._rpc_update_training_progress(row["id"], tok, p_status="running", p_current_step=4000)
            await D.sweep_tick()
            if self.state(row) != "running":
                break
        self.assertEqual(self.db.attempt(row["id"], 2)["end_reason"], "stalled")
        # a rising step still resets the clock
        r = self.db.row(row["id"])
        r.update(status="running", current_step=10, step_advanced_at=None, worker_token="t2")
        self.db._rpc_update_training_progress(row["id"], "t2", p_status="running", p_current_step=10)
        first = r["step_advanced_at"]
        self.clock.advance(60)
        self.db._rpc_update_training_progress(row["id"], "t2", p_status="running", p_current_step=10)
        self.assertEqual(r["step_advanced_at"], first, "a heartbeat at the same step keeps it")
        self.db._rpc_update_training_progress(row["id"], "t2", p_status="running", p_current_step=11)
        self.assertEqual(r["step_advanced_at"], self.clock.iso())

    def test_the_loss_is_not_dataset_class(self):
        self.assertEqual(T.CHECKPOINT_OVERDUE, "checkpoint_overdue")
        self.assertNotIn(T.CHECKPOINT_OVERDUE, T.DATASET_CLASS_LOSSES)
        self.assertEqual(T.attempts_out_message([T.CHECKPOINT_OVERDUE] * 4), T.S37)
        self.assertEqual(T.loss_note(0, T.CHECKPOINT_OVERDUE), T.S40)
        self.assertEqual(T.loss_note(2500, T.CHECKPOINT_OVERDUE), T.s39(2500))
        self.assertEqual(T.loss_note(2500, "pod_gone"), T.s1(2500))


if __name__ == "__main__":
    import unittest

    unittest.main()
