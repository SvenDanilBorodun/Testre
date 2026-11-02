"""A checkpoint turns ready only for the live running attempt (V3-M1, 052 fix round 2).

``_ckpt_done`` authorises once and then verifies the R2 objects, which takes seconds to
minutes; its ready write therefore goes through ``mark_checkpoint_ready`` (052), which
decides under the training's row lock: ready only while the row is ``running``, this
attempt is current and ``running`` and the checkpoint still ``uploading`` (an already
ready one answers true). On false nothing after it runs (no retention, no mirror) and the
worker gets 409 ``not_uploading`` (S15), which its table answers with „skipped" and goes
on. The verifier's two scenarios — a ckpt-done held back until the attempt was requeued,
and one held back until the training was failed with the S19 waiver — fail on the code
before this round. ``hf_publish.mirror_sync`` refuses a row that is no longer active or
succeeded (``claim_checkpoint_mirror`` does the same in SQL).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.routes import training_worker as route  # noqa: E402
from app.services import hf_publish as P  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import checkpoint_files, manifest_of  # noqa: E402
from app.tests.test_hf_publish import PublishBase  # noqa: E402

NOT_UPLOADING = (409, {"detail": T.S15, "code": "not_uploading"})


class HeldBackCkptDone(WorkerBase):
    STEP = 10000  # a mirror step: a wrong ready would also schedule a mirror

    async def asyncSetUp(self):
        await super().asyncSetUp()
        files = checkpoint_files(self.db.row(self.tid), self.STEP)
        self.r2.put_checkpoint(self.tid, 1, self.STEP, files)
        self.db.add_checkpoint(self.tid, 1, self.STEP, state="uploading", manifest=manifest_of(files))
        self.retention = []
        real = D.apply_retention
        self._cms.append(patch.object(D, "apply_retention", lambda tid, now=None: self.retention.append(tid)
                                      or real(tid, now)))
        self._cms[-1].__enter__()

    def meanwhile(self, act):
        """Run ``act`` while ckpt-done's verification is in flight (another request)."""
        real = route._verify_checkpoint

        async def held_back(row, ckpt):
            out = await real(row, ckpt)
            await act()
            return out

        return patch.object(route, "_verify_checkpoint", held_back)

    def not_ready(self):
        c = self.db.checkpoint(self.tid, 1, self.STEP)
        self.assertEqual(c["state"], "uploading")
        self.assertIsNone(c["ready_at"])
        self.assertEqual((self.retention, self.mirrors), ([], []))

    async def test_held_back_until_the_attempt_was_requeued(self):
        async def requeue():
            await D.requeue_attempt(self.db.row(self.tid), self.att, "pod_gone", self.clock.t)

        with self.meanwhile(requeue):
            self.assertEqual(await self.call("ckpt-done", {"step": self.STEP}), NOT_UPLOADING)
        self.not_ready()
        self.assertEqual(self.db.row(self.tid)["status"], "queued")
        # the next attempt is not handed the checkpoint the lost one never really saved
        self.assertEqual(D.resume_step_from(D.read_checkpoints([self.tid], ["ready"]), 50000), 0)

    async def test_held_back_until_a_waived_finish(self):
        async def waived():
            await D.fail_training(self.db.row(self.tid), 1, T.S19, charge_waiver="lerobot_error")

        with self.meanwhile(waived):
            self.assertEqual(await self.call("ckpt-done", {"step": self.STEP}), NOT_UPLOADING)
        self.not_ready()
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["charge_waiver"]), ("failed", "lerobot_error"))

    async def test_the_live_attempt_marks_it_ready(self):
        self.assertEqual(await self.call("ckpt-done", {"step": self.STEP}), (200, {"state": "ready"}))
        self.assertEqual(self.db.calls("mark_checkpoint_ready"),
                         [{"p_training_id": self.tid, "p_attempt": 1, "p_step": self.STEP}])
        c = self.db.checkpoint(self.tid, 1, self.STEP)
        self.assertEqual(c["state"], "ready")
        self.assertEqual((self.retention, self.mirrors), ([self.tid], [(self.tid, 1, self.STEP)]))

    async def test_a_concurrent_ready_answers_ready(self):
        async def other_call():
            self.db.checkpoint(self.tid, 1, self.STEP).update(state="ready", ready_at="earlier")

        with self.meanwhile(other_call):
            self.assertEqual(await self.call("ckpt-done", {"step": self.STEP}), (200, {"state": "ready"}))
        self.assertEqual(self.db.checkpoint(self.tid, 1, self.STEP)["ready_at"], "earlier", "ready_at kept")

    def test_the_fake_models_052s_function(self):
        db, tid, step = self.db, self.tid, self.STEP
        cases = [("queued row", lambda: db.row(tid).update(status="queued")),
                 ("cancel_requested row", lambda: db.row(tid).update(status="cancel_requested")),
                 ("unmanaged row", lambda: db.row(tid).update(managed=False)),
                 ("another attempt current", lambda: db.row(tid).update(current_attempt=2)),
                 ("attempt starting", lambda: self.att.update(state="starting")),
                 ("checkpoint refused", lambda: db.checkpoint(tid, 1, step).update(state="refused"))]
        for name, change in cases:
            with self.subTest(case=name):
                db.row(tid).update(status="running", managed=True, current_attempt=1)
                self.att.update(state="running")
                db.checkpoint(tid, 1, step).update(state="uploading", ready_at=None)
                change()
                self.assertIs(db._rpc_mark_checkpoint_ready(tid, 1, step), False)
                self.assertNotEqual(db.checkpoint(tid, 1, step)["state"], "ready")
        self.assertIs(db._rpc_mark_checkpoint_ready(tid, 1, 99999), False, "absent checkpoint")


class MirrorOfAnInactiveRow(PublishBase):
    def setUp(self):
        super().setUp()
        files = checkpoint_files(self.row, 10000)
        self.r2.put_checkpoint(self.tid, 1, 10000, files)
        self.db.add_checkpoint(self.tid, 1, 10000, state="ready", manifest=manifest_of(files))

    def test_the_api_refuses_a_failed_canceled_or_stopping_row(self):
        # claim_checkpoint_mirror of a database without the SQL guard (the fake's old body),
        # so only the API's own check stands between the row and a public mirror
        def old_claim(p_training_id, p_attempt, p_step, p_lease_s, p_owner):
            c = self.db.checkpoint(p_training_id, p_attempt, p_step)
            if not (c and c["state"] == "ready" and c["mirrored_at"] is None):
                return False
            c.update(mirror_claimed_at=self.db.now(), mirror_owner=p_owner)
            return True

        self.db._rpc_claim_checkpoint_mirror = old_claim
        for status in ("failed", "canceled", "cancel_requested"):
            with self.subTest(status=status):
                self.db.row(self.tid)["status"] = status
                self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "gone")
                self.assertEqual(self.hub.commits, [])
                self.assertIsNone(self.db.checkpoint(self.tid, 1, 10000)["mirrored_at"])
        self.assertEqual(P.MIRROR_ROW_STATUSES, ("queued", "running", "succeeded"))

    def test_an_active_or_succeeded_row_still_mirrors(self):
        for status in ("succeeded", "running"):
            with self.subTest(status=status):
                self.db.row(self.tid)["status"] = status
                self.db.checkpoint(self.tid, 1, 10000).update(mirrored_at=None, mirror_claimed_at=None,
                                                              mirror_owner=None)
                self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "mirrored")

    def test_the_fake_claim_refuses_like_052(self):
        for status in ("failed", "canceled", "cancel_requested"):
            self.db.row(self.tid)["status"] = status
            self.assertFalse(self.db._rpc_claim_checkpoint_mirror(self.tid, 1, 10000, 900, "x"), status)


if __name__ == "__main__":
    unittest.main()
