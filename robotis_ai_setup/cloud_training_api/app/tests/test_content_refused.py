"""A confirmed content refusal publishes the last checkpoint that passed (owner R3-1, spec §9.6).

A content rule refuses every checkpoint of a training the same way, so when a second,
distinct host confirms it (owner Q-C) the newest ready checkpoint below the total is
published like a cap (``cap_attempt(bound="content_refused")``: note S34, then S38 and
the commit message „later checkpoint refused" at publish, continuable); only without one
the row fails S7. An unconfirmed refusal requeues as before.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.routes import training_worker as route  # noqa: E402
from app.services import hf_publish as P  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import checkpoint_files, manifest_of  # noqa: E402
from app.tests.test_hf_publish import PublishBase  # noqa: E402


class ContentRefusal(WorkerBase):
    def confirmed_by_attempt_2(self):
        """Attempt 1 (another backend) already reported the content rule; attempt 2 now."""
        self.db.tables["training_attempts"].remove(self.att)
        self.db.add_attempt(self.tid, 1, state="lost", backend="runpod_secure", end_reason=route.MARK_CONTENT,
                            first_heartbeat_at=self.clock.iso(self.clock.t - 7200))
        self.db.row(self.tid).update(current_attempt=2)
        self.att = self.db.add_attempt(self.tid, 2, state="running", backend="runpod_community", cloud_ref="pod1",
                                       first_heartbeat_at=self.clock.iso(self.clock.t - 1800), resume_from_step=0)

    async def poisoned(self, attempt="2", step=7500):
        files = checkpoint_files(self.db.row(self.tid), step)
        doc = json.loads(files["pretrained_model/train_config.json"])
        doc["policy"]["type"] = "groot"
        files["pretrained_model/train_config.json"] = json.dumps(doc).encode()
        self.r2.put_checkpoint(self.tid, int(attempt), step, files)
        self.db.add_checkpoint(self.tid, int(attempt), step, state="uploading", manifest=manifest_of(files))
        return await self.call("ckpt-done", {"step": step}, attempt=attempt)

    async def test_confirmed_with_a_ready_checkpoint_publishes_it(self):
        self.confirmed_by_attempt_2()
        self.db.add_checkpoint(self.tid, 1, 5000, state="ready", manifest={})
        with patch.object(D, "cap_attempt", wraps=D.cap_attempt) as cap:
            self.assertEqual((await self.poisoned())[1]["code"], "ckpt_refused")
        self.assertEqual(cap.call_args.kwargs["bound"], "content_refused")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["status_note"], r["capped_step"], r["error_message"]),
                         ("running", T.S34, 5000, None))
        a = self.db.attempt(self.tid, 2)
        self.assertEqual((a["state"], a["end_reason"]), ("ended", "content_refused"))
        self.assertEqual(self.published, [(self.tid, 2)])
        self.assertIn("pod1", self.rp.deletes())
        self.assertEqual(self.db.checkpoint(self.tid, 2, 7500)["state"], "refused")

    async def test_confirmed_without_a_ready_checkpoint_fails_s7(self):
        self.confirmed_by_attempt_2()
        await self.poisoned()
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S7))
        self.assertEqual(self.db.calls("cap_training_attempt"), [])
        self.assertEqual(self.published, [])

    async def test_a_ready_final_is_published_as_the_training(self):
        self.confirmed_by_attempt_2()
        self.db.add_checkpoint(self.tid, 1, 50000, state="ready", manifest={})
        await self.poisoned()
        self.assertEqual(self.db.attempt(self.tid, 2)["end_reason"], "succeeded")
        self.assertEqual(self.published, [(self.tid, 2)])

    async def test_unconfirmed_it_requeues_as_before(self):
        self.db.add_checkpoint(self.tid, 1, 5000, state="ready", manifest={})
        await self.poisoned(attempt="1")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"], r["capped_step"]), ("queued", None, None))
        self.assertEqual(self.db.attempt(self.tid, 1)["end_reason"], route.MARK_CONTENT)
        self.assertEqual(r["status_note"], T.s1(5000))
        self.assertEqual(self.published, [])


class ContentRefusedPublish(PublishBase):
    """The publish of a content_refused attempt: S38 and its commit message."""

    def setUp(self):
        super().setUp()
        self.db.attempt(self.tid, 1).update(end_reason="content_refused")
        self.db.row(self.tid).update(capped_step=42500, status_note=T.S34)
        files = checkpoint_files(self.row, 42500, final=False)
        self.r2.put_checkpoint(self.tid, 1, 42500, files)
        self.db.add_checkpoint(self.tid, 1, 42500, state="ready", manifest=manifest_of(files))

    def test_s38_and_the_commit_message(self):
        self.assertEqual(P.publish_sync(self.tid, 1), "succeeded")
        (commit,) = self.hub.commits
        self.assertEqual(commit[2], f"EduBotics: model at step 42500 of 50000 (training {self.tid}, "
                                    "later checkpoint refused)")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["status_note"], r["current_step"]), ("succeeded", T.s38(42500, 50000), 42500))
        self.assertIsNotNone(r["continuable_until"])
        self.assertEqual(self.db.kept(self.tid)["step"], 42500)


if __name__ == "__main__":
    unittest.main()
