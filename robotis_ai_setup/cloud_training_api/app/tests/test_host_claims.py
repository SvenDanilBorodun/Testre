"""One host's word is never enough (owner Q-C, review 2 m3, spec §9.6).

``distinct_hosts``: two attempts ran on provably different hosts exactly when their
backend differs, or both GPU types are known and differ, or both data centres are known
and differ; an unknown field means „maybe the same". A first ``lerobot_error`` (S19) or
content refusal (S7) requeues the attempt with its marker; the same report from a
DISTINCT host is believed (S19 with the charge waiver; S7, or the content publish);
from a not-distinct one it requeues again. The publish-time S7 is the API's own
verdict and immediate. The API has no host key of its own (source fence).
"""

from __future__ import annotations

import json
import os
import re
import unittest

from app.tests.test_training_worker_routes import TOKEN, WorkerBase  # noqa: F401  (stubs installed there)

from app.routes import training_worker as route  # noqa: E402
from app.services import hf_publish as P  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import checkpoint_files, manifest_of  # noqa: E402
from app.tests.test_hf_publish import PublishBase  # noqa: E402

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DistinctHosts(unittest.TestCase):
    def test_the_matrix(self):
        a = {"backend": "runpod_community", "gpu_type": "NVIDIA RTX A5000", "data_center": "US-TX-3"}
        cases = [
            ({"backend": "runpod_secure"}, True),                                       # backend differs
            ({"backend": "modal", "gpu_type": None, "data_center": None}, True),
            ({"gpu_type": "NVIDIA L4"}, True),                                          # both GPUs known, differ
            ({"data_center": "EU-RO-1"}, True),                                         # both DCs known, differ
            ({}, False),                                                                # identical
            ({"gpu_type": None}, False),                                                # unknown GPU on one side
            ({"data_center": None}, False),                                             # unknown DC on one side
            ({"gpu_type": None, "data_center": None}, False),
        ]
        for change, want in cases:
            with self.subTest(change=change):
                b = {**a, **change}
                self.assertIs(D.distinct_hosts(a, b), want)
                self.assertIs(D.distinct_hosts(b, a), want, "symmetric")
        both_unknown = {"backend": "runpod_community", "gpu_type": None, "data_center": None}
        self.assertFalse(D.distinct_hosts(both_unknown, dict(both_unknown)))

    def test_two_modal_attempts_are_never_distinct(self):
        self.assertFalse(D.distinct_hosts({"backend": "modal"}, {"backend": "modal"}))
        self.assertFalse(D.distinct_hosts(None, {}))


class Claims(WorkerBase):
    def earlier(self, marker, n=1, host=None):
        """Attempt ``n`` (earlier) reported ``marker``; the current attempt is n + 1."""
        self.db.tables["training_attempts"] = [a for a in self.db.tables["training_attempts"]
                                               if a["training_id"] != self.tid]
        for k in range(1, n + 1):
            spec = dict(backend="runpod_community", gpu_type="NVIDIA RTX A5000", data_center="EU-RO-1")
            spec.update((host or {}).get(k, {}))
            self.db.add_attempt(self.tid, k, state="lost", end_reason=marker,
                                first_heartbeat_at=self.clock.iso(self.clock.t - 7200), **spec)
        cur = n + 1
        self.db.row(self.tid).update(current_attempt=cur, status="running", worker_token=TOKEN)
        self.att = self.db.add_attempt(self.tid, cur, state="running", backend="runpod_community", cloud_ref="pod1",
                                       gpu_type="NVIDIA RTX A5000", data_center="EU-RO-1", resume_from_step=0,
                                       first_heartbeat_at=self.clock.iso(self.clock.t - 1800))
        return str(cur)

    async def lerobot_error(self, attempt="1"):
        return await self.call("finish", {"outcome": "failed", "reason": "lerobot_error"}, attempt=attempt)

    async def test_a_first_lerobot_error_requeues_with_its_marker(self):
        self.assertEqual(await self.lerobot_error(), (200, {"status": "requeued"}))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("queued", None))
        self.assertEqual(self.att["end_reason"], route.MARK_LEROBOT_ERROR)
        self.assertEqual(self.db.calls("finish_training"), [])
        self.assertIn("pod1", self.rp.deletes())

    async def test_a_second_from_a_distinct_host_is_s19_without_a_charge(self):
        cur = self.earlier(route.MARK_LEROBOT_ERROR, host={1: {"data_center": "US-CA-2"}})
        self.assertEqual(await self.lerobot_error(cur), (200, {"status": "failed"}))
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S19))
        (call,) = self.db.calls("finish_training")
        self.assertEqual((call["p_message"], call["p_charge_waiver"]), (T.S19, "lerobot_error"))
        self.assertEqual(self.logs, [self.tid], "the failed log goes to the teacher")

    async def test_a_second_from_a_not_distinct_host_requeues_again(self):
        for host in ({}, {"gpu_type": None}, {"data_center": None}):
            with self.subTest(host=host):
                self.db.calls("finish_training").clear()
                cur = self.earlier(route.MARK_LEROBOT_ERROR, host={1: host})
                self.assertEqual(await self.lerobot_error(cur), (200, {"status": "requeued"}))
                self.assertEqual(self.db.row(self.tid)["status"], "queued")
                self.assertEqual([c for c in self.db.calls("finish_training") if c["p_training_id"] == self.tid], [])

    async def test_two_earlier_same_host_reports_still_need_a_distinct_one(self):
        # 1 and 2 on the same coarse host, 3 (now) the same again: never two distinct hosts
        cur = self.earlier(route.MARK_LEROBOT_ERROR, n=2)
        self.assertEqual(await self.lerobot_error(cur), (200, {"status": "requeued"}))
        # … one of the earlier reports on another backend makes it two
        cur = self.earlier(route.MARK_LEROBOT_ERROR, n=2, host={2: {"backend": "runpod_secure"}})
        self.assertEqual(await self.lerobot_error(cur), (200, {"status": "failed"}))

    async def test_another_marker_never_counts(self):
        cur = self.earlier("host_unfit:lerobot_crash", host={1: {"backend": "modal"}})
        self.assertEqual(await self.lerobot_error(cur), (200, {"status": "requeued"}))
        self.assertEqual(self.att["end_reason"], route.MARK_LEROBOT_ERROR)

    async def _poisoned_upload(self, attempt):
        files = checkpoint_files(self.db.row(self.tid), 2500)
        doc = json.loads(files["pretrained_model/train_config.json"])
        doc["policy"]["type"] = "groot"
        files["pretrained_model/train_config.json"] = json.dumps(doc).encode()
        self.r2.put_checkpoint(self.tid, int(attempt), 2500, files)
        self.db.add_checkpoint(self.tid, int(attempt), 2500, state="uploading", manifest=manifest_of(files))
        return await self.call("ckpt-done", {"step": 2500}, attempt=attempt)

    async def test_a_content_refusal_follows_the_same_rule(self):
        self.assertEqual((await self._poisoned_upload("1"))[1]["code"], "ckpt_refused")
        self.assertEqual((self.db.row(self.tid)["status"], self.att["end_reason"]), ("queued", route.MARK_CONTENT))
        cur = self.earlier(route.MARK_CONTENT)                       # not distinct → again
        await self._poisoned_upload(cur)
        self.assertEqual(self.db.row(self.tid)["status"], "queued")
        cur = self.earlier(route.MARK_CONTENT, n=2, host={1: {"backend": "runpod_secure"}})
        await self._poisoned_upload(cur)
        self.assertEqual((self.db.row(self.tid)["status"], self.db.row(self.tid)["error_message"]), ("failed", T.S7))

    def test_the_limit_is_two_hosts(self):
        self.assertEqual(route.HOST_CLAIM_LIMIT, 2)
        self.assertEqual(route.MARK_LEROBOT_ERROR, "host_unfit:lerobot_error_unconfirmed")
        self.assertEqual(route.MARK_CONTENT, "host_unfit:content_unconfirmed")
        for marker in (route.MARK_LEROBOT_ERROR, route.MARK_CONTENT):
            self.assertIn(marker, T.DATASET_CLASS_LOSSES)


class PublishTimeContentRule(PublishBase):
    def test_the_publish_time_s7_is_immediate(self):
        # the API's own validation of the final files is no host's word: no requeue, no count
        from app.services import r2_client

        rel = "pretrained_model/policy_preprocessor.json"
        doc = json.loads(self.files[rel])
        doc["steps"][0]["class"] = "os.system"
        data = json.dumps(doc).encode()
        self.r2.objects[r2_client.ckpt_key(self.tid, 1, 50000, rel)] = data
        self.ckpt["manifest"][rel] = manifest_of({rel: data})[rel]
        self.assertEqual(P.publish_sync(self.tid, 1), "refused")
        r = self.db.row(self.tid)
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S7))
        self.assertEqual(self.db.calls("requeue_training"), [])


class NoHostKey(unittest.TestCase):
    def test_the_api_carries_no_host_key_and_no_dropped_sentence(self):
        banned = re.compile(r"host_key|machineId|machine_id|START_RETRY_DE|STOP_STATE_CHANGED_DE")
        hits = []
        for root, _dirs, files in os.walk(APP):
            if os.sep + "tests" in root:
                continue
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8") as fh:
                        for i, line in enumerate(fh, 1):
                            if banned.search(line):
                                hits.append(f"{os.path.relpath(path, APP)}:{i}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
