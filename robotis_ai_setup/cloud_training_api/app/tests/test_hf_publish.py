"""hf_publish: publish, mirror, failed-log upload, leases, executor limits (spec §4.5, §4.6, R4, FR-4, TR-3, TR-8).

Against the FakeDB (049's lease functions), the in-memory R2 and a fake Hub
that stores what a commit carried and answers get_paths_info like the Hub
(LFS: sha256 + size; small files: the git blob sha1). Pinned: ONE commit with
the 8 root files, the 7 under checkpoints/<step_id>/pretrained_model/ and the
log; our only own file in the legacy byte format; the read-back; a refused
model → S7 at once; HF faults and read-back mismatches keep the row publishing
with back-off until 6 h → S8; the BOOT_ID lease, its renewal, and a lost
renewal writing nothing more; 2 publishes + 1 mirror; the failed-training log
surviving an API restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import artifact_validation as AV  # noqa: E402
from app.services import hf_publish as P  # noqa: E402
from app.services import r2_client  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import SHA, Clock, FakeDB, FakeR2, checkpoint_files, manifest_of  # noqa: E402
from app.tests.test_training_dispatch import settings  # noqa: E402

LFS_THRESHOLD = 10 * 1024


class Op:
    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo = path_in_repo
        self.path_or_fileobj = path_or_fileobj


class FakeHub:
    def __init__(self):
        self.repos, self.commits, self.files = [], [], {}
        self.fail_commit = 0
        self.tamper = None

    def make(self):
        hub = self

        class Api:
            def create_repo(self, repo_id, repo_type=None, exist_ok=False):
                hub.repos.append((repo_id, repo_type, exist_ok))

            def create_commit(self, repo_id, operations, commit_message, repo_type=None):
                if hub.fail_commit:
                    hub.fail_commit -= 1
                    raise RuntimeError("hub 503")
                paths = []
                for op in operations:
                    data = op.path_or_fileobj
                    if isinstance(data, str):
                        with open(data, "rb") as fh:
                            data = fh.read()
                    hub.files[(repo_id, op.path_in_repo)] = data
                    paths.append(op.path_in_repo)
                hub.commits.append((repo_id, paths, commit_message))

            def get_paths_info(self, repo_id, paths, expand=False, repo_type=None):
                out = []
                for p in paths:
                    data = hub.files.get((repo_id, p))
                    if data is None:
                        continue
                    if hub.tamper == p:
                        data = data + b"!"
                    if len(data) > LFS_THRESHOLD:
                        out.append(SimpleNamespace(path=p, blob_id="x", lfs=SimpleNamespace(
                            sha256=hashlib.sha256(data).hexdigest(), size=len(data))))
                    else:
                        out.append(SimpleNamespace(path=p, blob_id=P.git_blob_sha1(data), lfs=None))
                return out

        return Api(), Op


class PublishBase(unittest.TestCase):
    def setUp(self):
        D.reset_state_for_tests()
        P.reset_for_tests()
        self.clock = Clock()
        self.db = FakeDB(self.clock)
        self.r2 = FakeR2()
        self.hub = FakeHub()
        self.s = settings()
        self._cms = [patch.object(D, "_now", self.clock), patch.object(D, "_sb", lambda: self.db),
                     patch.object(D, "SETTINGS", self.s), patch.object(P, "_hf", self.hub.make),
                     patch.object(P, "_submit", lambda fn, *a: fn(*a)), self.r2.installed()]
        for cm in self._cms:
            cm.__enter__()
        params = {"steps": 50000, "save_freq": 2500, "mirror_every": 10000, "batch_size": 8, "dataset_revision": SHA}
        self.row = self.db.add_training(status="running", current_attempt=1, training_params=params,
                                        model_name="EduBotics-Solutions/m-act-ds-abc", dataset_name="u/ds",
                                        dataset_fps=30.0, status_note=T.S3)
        self.tid = self.row["id"]
        self.db.add_attempt(self.tid, 1, state="ended", end_reason="succeeded", backend="runpod_secure")
        self.files = checkpoint_files(self.row, 50000, final=True)
        self.r2.put_checkpoint(self.tid, 1, 50000, self.files)
        self.ckpt = self.db.add_checkpoint(self.tid, 1, 50000, state="ready", manifest=manifest_of(self.files))
        self.r2.objects[r2_client.log_key(self.tid, 1)] = b"step: 50000 loss: 0.01\n"

    def tearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)


class Publish(PublishBase):
    def test_one_commit_read_back_finish_janitor(self):
        self.assertEqual(P.publish_sync(self.tid, 1), "succeeded")
        self.assertEqual(self.hub.repos, [("EduBotics-Solutions/m-act-ds-abc", "model", True)])
        (commit,) = self.hub.commits
        paths = set(commit[1])
        root = {r.split("/", 1)[1] for r in AV.FINAL_RELPATHS} | {"edubotics_model_meta.json"}
        ckpt = {f"checkpoints/050000/pretrained_model/{r.split('/', 1)[1]}" for r in AV.FINAL_RELPATHS}
        self.assertEqual(paths, root | ckpt | {"training_log.txt"})
        self.assertEqual(len(root), 8)
        meta = self.hub.files[("EduBotics-Solutions/m-act-ds-abc", "edubotics_model_meta.json")]
        self.assertEqual(meta, json.dumps({"dataset_repo_id": "u/ds", "dataset_fps": 30, "policy_type": "act"},
                                          indent=2).encode())
        for rel in AV.FINAL_RELPATHS:  # byte-identical (R4)
            self.assertEqual(self.hub.files[("EduBotics-Solutions/m-act-ds-abc", rel.split("/", 1)[1])], self.files[rel])
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["current_step"], r["status_note"]), ("succeeded", 50000, None))
        self.assertEqual(r["log_url"], "https://huggingface.co/EduBotics-Solutions/m-act-ds-abc/blob/main/training_log.txt")
        self.assertEqual(self.db.attempt(self.tid, 1)["publish_owner"], D.BOOT_ID)
        self.assertEqual(self.db.checkpoint(self.tid, 1, 50000)["state"], "deleted")
        self.assertEqual(self.r2.objects, {}, "the janitor removed every object")

    def test_without_a_log(self):
        self.r2.objects.pop(r2_client.log_key(self.tid, 1))
        self.assertEqual(P.publish_sync(self.tid, 1), "succeeded")
        self.assertNotIn("training_log.txt", self.hub.commits[0][1])
        self.assertIsNone(self.db.row(self.tid)["log_url"])

    def test_a_read_back_mismatch_keeps_it_publishing(self):
        self.hub.tamper = "model.safetensors"
        self.assertEqual(P.publish_sync(self.tid, 1), "retry")
        r = self.db.row(self.tid)
        self.assertEqual(r["status"], "running")
        self.assertEqual(self.db.attempt(self.tid, 1)["publish_tries"], 1)
        self.hub.tamper = "config.json"  # a small (non-LFS) file: the blob sha1
        self.assertEqual(P.publish_sync(self.tid, 1), "retry")
        self.assertEqual(self.db.attempt(self.tid, 1)["publish_tries"], 2)

    def test_an_hf_fault_keeps_it_publishing(self):
        self.hub.fail_commit = 1
        self.assertEqual(P.publish_sync(self.tid, 1), "retry")
        self.assertEqual(self.db.row(self.tid)["status"], "running")
        self.assertEqual(P.publish_sync(self.tid, 1), "succeeded", "our own lease is renewed by its owner")

    def test_a_changed_byte_in_r2_refuses_the_model(self):
        key = r2_client.ckpt_key(self.tid, 1, 50000, "pretrained_model/model.safetensors")
        self.r2.objects[key] = self.r2.objects[key][:-1] + b"\x00"
        self.assertEqual(P.publish_sync(self.tid, 1), "refused")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S7))
        self.assertEqual(self.hub.commits, [])

    def test_a_content_rule_refuses_the_model(self):
        doc = json.loads(self.files["pretrained_model/policy_preprocessor.json"])
        doc["steps"][0]["class"] = "os.system"
        data = json.dumps(doc).encode()
        self.r2.objects[r2_client.ckpt_key(self.tid, 1, 50000, "pretrained_model/policy_preprocessor.json")] = data
        self.ckpt["manifest"]["pretrained_model/policy_preprocessor.json"] = manifest_of({"x": data})["x"]
        self.assertEqual(P.publish_sync(self.tid, 1), "refused")
        self.assertEqual(self.db.row(self.tid)["error_message"], T.S7)

    def test_a_null_fps_is_read_again_at_the_pinned_revision(self):
        self.row["dataset_fps"] = None
        calls = []

        def _download(repo_id, filename, repo_type, revision, token):
            calls.append((repo_id, filename, revision, token))
            import os
            import tempfile

            path = os.path.join(tempfile.mkdtemp(), "info.json")
            with open(path, "w") as fh:
                json.dump({"fps": 15}, fh)
            return path

        with patch.object(sys.modules["huggingface_hub"], "hf_hub_download", _download, create=True):
            self.assertEqual(P.publish_sync(self.tid, 1), "succeeded")
        self.assertEqual(calls, [("u/ds", "meta/info.json", SHA, False)])
        meta = json.loads(self.hub.files[("EduBotics-Solutions/m-act-ds-abc", "edubotics_model_meta.json")])
        self.assertEqual(meta["dataset_fps"], 15)

    def test_another_instances_lease_is_respected(self):
        self.db.attempt(self.tid, 1).update(publish_claimed_at=self.clock.iso(), publish_owner="other-boot")
        self.assertEqual(P.publish_sync(self.tid, 1), "not_claimed")
        self.assertEqual(self.hub.commits, [])
        out = asyncio.run(P.recover_publish(self.db.row(self.tid), self.db.attempt(self.tid, 1), self.clock.t))
        self.assertEqual(out, "other_instance")

    def test_a_lost_renewal_stops_before_any_further_write(self):
        claims = []
        real = self.db._rpc_claim_training_publish

        def claim(**kw):
            claims.append(kw)
            return real(**kw) if len(claims) == 1 else False

        self.db._rpc_claim_training_publish = claim
        slow = self.r2.stream_to_file

        def _slow(key, path, should_continue=None):
            time.sleep(0.15)
            if should_continue is not None and not should_continue():
                raise RuntimeError("stopped")
            return slow(key, path, should_continue)

        with patch.object(D, "PUBLISH_RENEW_S", 0.02), patch.object(r2_client, "stream_to_file", _slow):
            self.assertEqual(P.publish_sync(self.tid, 1), "lost")
        self.assertGreaterEqual(len(claims), 2)
        self.assertEqual((self.hub.repos, self.hub.commits), ([], []))
        self.assertEqual(self.db.row(self.tid)["status"], "running")
        self.assertEqual(self.db.attempt(self.tid, 1)["publish_tries"], 0, "no database write after the loss")

    def test_a_row_that_is_no_longer_publishing_is_left_alone(self):
        self.db.row(self.tid)["status"] = "canceled"
        self.assertEqual(P.publish_sync(self.tid, 1), "not_claimed")


class Recovery(PublishBase):
    def _recover(self):
        return asyncio.run(P.recover_publish(self.db.row(self.tid), self.db.attempt(self.tid, 1), self.clock.t))

    def test_the_backoff_table(self):
        self.assertEqual([P.publish_backoff_s(n) for n in range(7)], [0, 60, 120, 300, 600, 900, 900])

    def test_retries_wait_their_backoff(self):
        with patch.object(P, "schedule_publish", lambda t, n: True):
            self.assertEqual(self._recover(), "scheduled")  # never claimed
            self.db.attempt(self.tid, 1).update(publish_claimed_at=self.clock.iso(self.clock.t - 30),
                                                publish_owner=D.BOOT_ID, publish_tries=1,
                                                publish_first_at=self.clock.iso(self.clock.t - 30))
            self.assertEqual(self._recover(), "backoff")
            self.clock.advance(31)
            self.assertEqual(self._recover(), "scheduled")

    def test_six_hours_end_with_s8(self):
        self.db.attempt(self.tid, 1).update(publish_claimed_at=self.clock.iso(self.clock.t - 1000),
                                            publish_owner=D.BOOT_ID, publish_tries=9,
                                            publish_first_at=self.clock.iso(self.clock.t - D.PUBLISH_RETRY_S - 1))
        self.assertEqual(self._recover(), "gave_up")
        self.assertEqual(self.db.row(self.tid)["error_message"], T.S8)

    def test_the_give_up_waits_for_a_live_foreign_lease(self):
        self.db.attempt(self.tid, 1).update(publish_claimed_at=self.clock.iso(), publish_owner="other",
                                            publish_first_at=self.clock.iso(self.clock.t - D.PUBLISH_RETRY_S - 1))
        self.assertEqual(self._recover(), "busy")
        self.assertEqual(self.db.row(self.tid)["status"], "running")


class Mirror(PublishBase):
    def setUp(self):
        super().setUp()
        self.mfiles = checkpoint_files(self.row, 10000)
        self.r2.put_checkpoint(self.tid, 1, 10000, self.mfiles)
        self.mck = self.db.add_checkpoint(self.tid, 1, 10000, state="ready", manifest=manifest_of(self.mfiles))

    def test_a_mirror_commits_the_seven_under_its_step(self):
        self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "mirrored")
        (commit,) = self.hub.commits
        self.assertEqual(set(commit[1]), {f"checkpoints/010000/pretrained_model/{r.split('/', 1)[1]}"
                                          for r in AV.FINAL_RELPATHS})
        c = self.db.checkpoint(self.tid, 1, 10000)
        self.assertIsNotNone(c["mirrored_at"])
        self.assertEqual(c["mirror_owner"], D.BOOT_ID)
        self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "not_claimed", "a mirrored checkpoint is done")

    def test_a_failure_counts_a_try_and_the_claim_lapses(self):
        self.hub.fail_commit = 1
        self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "retry")
        self.assertEqual(self.db.checkpoint(self.tid, 1, 10000)["mirror_tries"], 1)

    def test_a_foreign_live_claim(self):
        self.mck.update(mirror_claimed_at=self.clock.iso(), mirror_owner="other")
        self.assertEqual(P.mirror_sync(self.tid, 1, 10000), "not_claimed")
        self.assertFalse(P.mirror_due(self.row, self.mck, self.clock.t))
        self.clock.advance(D.MIRROR_LEASE_S + 1)
        self.assertTrue(P.mirror_due(self.row, self.mck, self.clock.t))

    def test_mirror_due(self):
        self.assertTrue(P.mirror_due(self.row, self.mck, self.clock.t))
        for change in ({"step": 12500}, {"step": 50000}, {"mirrored_at": "x"}, {"mirror_tries": 3}, {"state": "uploading"}):
            with self.subTest(change=change):
                self.assertFalse(P.mirror_due(self.row, dict(self.mck, **change), self.clock.t))


class ExecutorLimits(unittest.TestCase):
    def setUp(self):
        P.reset_for_tests()
        self.queued = []
        self._cm = patch.object(P, "_submit", lambda fn, *a: self.queued.append(fn))
        self._cm.__enter__()

    def tearDown(self):
        for fn in self.queued:  # release the semaphores
            with patch.object(P, "publish_sync", lambda *a: None), patch.object(P, "mirror_sync", lambda *a: None), \
                    patch.object(P, "upload_failed_log_sync", lambda *a: None):
                fn()
        self._cm.__exit__(None, None, None)

    def test_two_publishes_and_one_mirror(self):
        self.assertEqual([P.schedule_mirror(1, 1, s) for s in (10000, 20000, 30000)], [True, False, False])
        self.assertEqual([P.schedule_publish(t, 1) for t in (1, 2, 3)], [True, True, False])
        self.assertFalse(P.schedule_failed_log({"id": 4, "current_attempt": 1}), "a log upload takes a publish slot")
        self.assertFalse(P.schedule_publish(1, 1), "single flight per training")
        self.assertEqual(len(self.queued), 3)

    def test_the_executor(self):
        self.assertEqual(P._EXECUTOR._max_workers, 3)
        self.assertEqual(P._EXECUTOR._thread_name_prefix, "training-publish")


class FailedLog(PublishBase):
    def test_the_log_reaches_the_repo_and_log_url(self):
        self.db.row(self.tid).update(status="failed", terminated_at=self.clock.iso(), error_message=T.S19)
        self.assertEqual(P.upload_failed_log_sync(self.db.row(self.tid)), "uploaded")
        self.assertEqual(self.hub.commits[0][1], ["training_log.txt"])
        self.assertTrue(self.db.row(self.tid)["log_url"].endswith("/blob/main/training_log.txt"))

    def test_an_api_restart_between_finish_and_the_upload_still_sets_log_url(self):
        """TR-3: the in-memory task died with the old process; the next process's
        janitor finds the log in R2 with log_url NULL and uploads it."""
        self.db.row(self.tid).update(status="failed", terminated_at=self.clock.iso(), error_message=T.S19,
                                     log_url=None)
        D.reset_state_for_tests()
        P.reset_for_tests()  # a fresh process
        asyncio.run(D.janitor_tick(self.clock.t))
        self.assertTrue(self.db.row(self.tid)["log_url"])
        self.assertIn("training_log.txt", self.hub.commits[0][1])
        asyncio.run(D.janitor_tick(self.clock.t))
        self.assertNotIn(r2_client.log_key(self.tid, 1), self.r2.objects, "then the janitor removes it")

    def test_at_most_three_tries(self):
        self.db.row(self.tid).update(status="failed", terminated_at=self.clock.iso(), log_url=None)
        self.hub.fail_commit = 10
        for _ in range(5):
            P.schedule_failed_log(self.db.row(self.tid))
        self.assertEqual(P._LOG_TRIES[self.tid], 3)


class Hashes(unittest.TestCase):
    def test_git_blob_sha1(self):
        self.assertEqual(P.git_blob_sha1(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")
        self.assertEqual(P.git_blob_sha1(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")


if __name__ == "__main__":
    unittest.main()
