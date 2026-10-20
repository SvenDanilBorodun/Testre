"""/me/export and /me/delete with managed trainings (spec §4.4, E16, FR-19).

The GDPR export names every trainings column except worker_token (the 20
columns 047 grants plus 049's seven) and lists, per managed training, where
each attempt ran — provider, cloud, data centre, GPU, start, end — never the
pod or call id. /me/delete cancels a managed row through the same
compare-and-set as Cancel and leaves a publishing one to finish; legacy rows
keep today's direct update.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unittest
from unittest.mock import AsyncMock, patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.routes import me as route  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.tests._managed_fakes import Clock, FakeDB, FakeModal, FakeRunpod  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations")
PROFILE = {"id": "u-1", "role": "student", "workgroup_id": None, "classroom_id": None}


def content(resp):
    return json.loads(resp.body) if hasattr(resp, "body") else resp.content


def _db():
    db = FakeDB(Clock())
    for t in ("workgroup_memberships", "workflows", "datasets", "workflow_versions", "tutorial_progress",
              "progress_entries", "users", "user_hf_credentials", "classrooms"):
        db.tables[t] = []
    db.tables["users"].append({"id": "u-1", "deletion_requested_at": None, "workgroup_id": None})
    return db


class Export(unittest.TestCase):
    def test_the_columns_are_every_column_but_the_token(self):
        cols = [c.strip() for c in route._EXPORT_TRAINING_COLUMNS.split(",")]
        self.assertNotIn("worker_token", cols)
        def code(name):
            with open(os.path.join(MIG, name), encoding="utf-8") as fh:
                return "\n".join(line.split("--", 1)[0] for line in fh.read().splitlines())

        m047 = re.search(r"GRANT SELECT \((.*?)\) ON public\.trainings", code("20261020120000_047_hide_worker_token.sql"), re.S)
        m049 = re.search(r"GRANT SELECT \((.*?)\)\s+ON public\.trainings", code("20261020130000_049_managed_training.sql"), re.S)
        m050 = re.search(r"GRANT SELECT \((.*?)\) ON public\.trainings", code("20261021120000_050_training_continuation.sql"), re.S)
        granted = ([c.strip() for c in m047.group(1).split(",")] + [c.strip() for c in m049.group(1).split(",")]
                   + [c.strip() for c in m050.group(1).split(",")])
        self.assertEqual(sorted(cols), sorted(granted))
        self.assertEqual(len(cols), 30)

    def test_the_export_carries_attempts_without_ids(self):
        db = _db()
        managed = db.add_training(user_id="u-1", status="succeeded", worker_token="should-not-leak", current_attempt=3)
        db.add_attempt(managed["id"], 1, backend="runpod_community", cloud_ref="pod-secret", data_center="EU-RO-1",
                       gpu_type="NVIDIA RTX A5000", started_at="2026-10-18T10:00:00+00:00",
                       ended_at="2026-10-18T11:00:00+00:00", state="lost")
        db.add_attempt(managed["id"], 2, backend="modal", cloud_ref="fc-secret", state="ended",
                       started_at="2026-10-18T11:05:00+00:00", ended_at="2026-10-18T12:00:00+00:00")
        db.add_attempt(managed["id"], 3, backend=None, state="ended")  # never placed: ran nowhere
        legacy = db.add_training(user_id="u-1", status="failed", managed=False, worker_token="x")
        db.add_training(user_id="someone-else", status="queued")
        from app.services import workgroups

        with patch.object(route, "get_supabase", lambda: db), patch.object(workgroups, "get_supabase", lambda: db):
            resp = asyncio.run(route.export_my_data(profile=dict(PROFILE)))
        bundle = content(resp)
        text = json.dumps(bundle)
        for secret in ("should-not-leak", "pod-secret", "fc-secret", "worker_token", "cloud_ref"):
            self.assertNotIn(secret, text)
        by_id = {t["id"]: t for t in bundle["trainings"]}
        self.assertEqual(set(by_id), {managed["id"], legacy["id"]})
        self.assertEqual(by_id[managed["id"]]["attempts"], [
            {"provider": "runpod", "cloud": "community", "data_center": "EU-RO-1",
             "requested_data_centers": [], "gpu_type": "NVIDIA RTX A5000",
             "started_at": "2026-10-18T10:00:00+00:00", "ended_at": "2026-10-18T11:00:00+00:00"},
            {"provider": "modal", "cloud": None, "data_center": None, "requested_data_centers": [], "gpu_type": None,
             "started_at": "2026-10-18T11:05:00+00:00", "ended_at": "2026-10-18T12:00:00+00:00"},
        ])
        self.assertEqual(by_id[legacy["id"]]["attempts"], [])
        self.assertEqual(resp.headers["Cache-Control"], "no-store")


class Delete(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        D.reset_state_for_tests()
        self.db = _db()
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        self.legacy_cancel = AsyncMock()
        self._cms = [patch.object(route, "get_supabase", lambda: self.db), patch.object(D, "_sb", lambda: self.db),
                     patch.object(route, "cancel_training_job", self.legacy_cancel), self.rp.installed(),
                     self.modal.installed()]
        for cm in self._cms:
            cm.__enter__()

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def test_managed_rows_go_through_the_cas(self):
        queued = self.db.add_training(user_id="u-1", status="queued")
        running = self.db.add_training(user_id="u-1", status="running", current_attempt=1)
        self.db.add_attempt(running["id"], 1, state="running", backend="runpod_secure", cloud_ref="pod-r")
        self.rp.add_pod(id="pod-r")
        publishing = self.db.add_training(user_id="u-1", status="running", current_attempt=1)
        self.db.add_attempt(publishing["id"], 1, state="ended", end_reason="succeeded", backend="modal", cloud_ref="fc")
        legacy = self.db.add_training(user_id="u-1", status="running", managed=False, cloud_job_id="fc-L")
        resp = await route.delete_my_account(profile=dict(PROFILE))
        body = resp if isinstance(resp, dict) else content(resp)
        self.assertEqual(sorted(body["canceled_trainings"]), sorted([queued["id"], running["id"], legacy["id"]]))
        self.assertEqual(self.db.row(queued["id"])["status"], "canceled")
        self.assertEqual(self.db.row(running["id"])["status"], "canceled")
        self.assertIn("pod-r", self.rp.deletes())
        self.assertEqual(self.db.row(publishing["id"])["status"], "running", "P0051: its publish finishes")
        self.assertEqual(self.db.row(legacy["id"])["status"], "canceled")
        self.assertEqual(self.db.row(legacy["id"])["error_message"], "Automatisch abgebrochen: Kontolöschung angefordert.")
        self.legacy_cancel.assert_awaited_once_with("fc-L")
        # no direct status write on a managed row
        for op in self.db.ops:
            if op[0] == "update" and op[1] == "trainings":
                ids = [v for k, c, v, _ in op[2] if c == "id"]
                self.assertNotIn(queued["id"], ids)
                self.assertNotIn(running["id"], ids)
        names = [n for n, _ in self.db.rpcs]
        self.assertEqual(names.count("cancel_request_training"), 3)
        self.assertEqual(names.count("complete_cancel_training"), 2)


if __name__ == "__main__":
    unittest.main()
