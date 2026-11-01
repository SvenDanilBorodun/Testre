"""An intact checkpoint's train_config complaint needs two distinct hosts (owner R3-2, spec §9.6).

The API cannot reproduce LeRobot's ``train_config`` regeneration, so a ``ckpt-bad
train_config`` about bytes ``ckpt-done`` verified is counted in the checkpoint's
``config_complaints`` (never ``crash_attempts``, resume_crash's own counter) and refuses
it only once two attempts on DISTINCT hosts made it; two attempts on the same coarse
host stay ``kept``. The kept-copy branch of a continuation is unchanged (its own
``bad_attempts``, counted per attempt).
"""

from __future__ import annotations

import unittest

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.tests._managed_fakes import manifest_of  # noqa: E402


class ConfigComplaints(WorkerBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.att.update(state="lost")                      # attempt 1 made checkpoint 2500
        self.files = self.upload(2500)
        self.db.add_checkpoint(self.tid, 1, 2500, state="ready", manifest=manifest_of(self.files))

    def resume(self, n, **host):
        """Attempt n resumes from attempt 1's step 2500 and becomes the current one."""
        for a in self.db.tables["training_attempts"]:
            if a["training_id"] == self.tid and a["state"] == "running":
                a["state"] = "lost"
        spec = dict(backend="runpod_community", gpu_type="NVIDIA RTX A5000", data_center="EU-RO-1")
        spec.update(host)
        self.db.add_attempt(self.tid, n, state="running", cloud_ref=f"pod{n}", resume_from_step=2500,
                            resume_from_attempt=1, first_heartbeat_at=self.clock.iso(self.clock.t - 60), **spec)
        self.db.row(self.tid).update(current_attempt=n, status="running", worker_token=TOKEN)
        return str(n)

    async def complain(self, n):
        return await self.call("ckpt-bad", {"step": 2500, "reason": "train_config"}, attempt=n)

    def ckpt(self):
        return self.db.checkpoint(self.tid, 1, 2500)

    async def test_two_attempts_on_the_same_coarse_host_stay_kept(self):
        self.assertEqual(await self.complain(self.resume(2)), (200, {"state": "kept"}))
        self.assertEqual(await self.complain(self.resume(3, gpu_type=None)), (200, {"state": "kept"}))
        c = self.ckpt()
        self.assertEqual((c["state"], c["config_complaints"], c["crash_attempts"]), ("ready", [2, 3], []))

    async def test_a_distinct_second_host_refuses(self):
        self.assertEqual(await self.complain(self.resume(2)), (200, {"state": "kept"}))
        self.assertEqual(await self.complain(self.resume(3, backend="runpod_secure")), (200, {"state": "refused"}))
        c = self.ckpt()
        self.assertEqual((c["state"], c["refused_reason"], c["refused_by"]), ("refused", "train_config", 3))
        self.assertEqual(c["config_complaints"], [2, 3])
        self.assertEqual(c["crash_attempts"], [], "resume_crash's counter is untouched")

    async def test_the_same_attempt_twice_counts_once(self):
        n = self.resume(2)
        self.assertEqual(await self.complain(n), (200, {"state": "kept"}))
        self.assertEqual(await self.complain(n), (200, {"state": "kept"}))
        self.assertEqual(self.ckpt()["config_complaints"], [2])

    async def test_a_resume_crash_never_counts_as_a_config_complaint(self):
        n = self.resume(2)
        await self.call("finish", {"outcome": "host_unfit", "reason": "resume_crash"}, attempt=n)
        c = self.ckpt()
        self.assertEqual((c["crash_attempts"], c["config_complaints"], c["state"]), ([2], [], "ready"))
        # a train_config complaint from a distinct host afterwards is the FIRST complaint
        self.assertEqual(await self.complain(self.resume(3, backend="runpod_secure")), (200, {"state": "kept"}))

    async def test_differing_bytes_refuse_at_once(self):
        from app.services import r2_client

        self.r2.objects[r2_client.ckpt_key(self.tid, 1, 2500, "pretrained_model/train_config.json")] = b"{}"
        self.assertEqual(await self.complain(self.resume(2)), (200, {"state": "refused"}))
        self.assertEqual(self.ckpt()["config_complaints"], [], "a proven difference needs no count")


class KeptCopyUnchanged(WorkerBase):
    """A continuation's kept copy keeps its own rule: bad_attempts across the children,
    refused at RESUME_CRASH_LIMIT attempts whatever their hosts."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.services import r2_client, training_dispatch as D

        self.D = D
        parent = self.db.add_training(status="succeeded", current_attempt=1, capped_step=2500,
                                      continuable_until=self.clock.iso(self.clock.t + 3600))
        self.parent = parent["id"]
        files = self.upload(2500)
        for rel, data in files.items():
            self.r2.objects[r2_client.keep_key(self.parent, 2500, rel)] = data
        self.db.add_kept(self.parent, 2500, manifest=manifest_of(files),
                         expires_at=self.clock.iso(self.clock.t + 3600))
        self.db.row(self.tid).update(continued_from=self.parent)
        self.att.update(resume_from_step=2500, resume_from_attempt=None)

    async def test_two_attempts_on_one_host_refuse_the_kept_copy(self):
        self.assertEqual(await self.call("ckpt-bad", {"step": 2500, "reason": "train_config"}),
                         (200, {"state": "kept"}))
        self.att.update(state="lost")
        self.db.add_attempt(self.tid, 2, state="running", backend="runpod_community", cloud_ref="pod2",
                            resume_from_step=2500, resume_from_attempt=None)
        self.db.row(self.tid).update(current_attempt=2)
        self.assertEqual(await self.call("ckpt-bad", {"step": 2500, "reason": "train_config"}, attempt="2"),
                         (200, {"state": "refused"}))
        self.assertEqual(self.db.kept(self.parent)["state"], "refused")


if __name__ == "__main__":
    unittest.main()
