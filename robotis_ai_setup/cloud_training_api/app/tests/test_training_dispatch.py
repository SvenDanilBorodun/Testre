"""training_dispatch: settings, dispatcher, placement guard, sweep, reaper, HF check (spec §3.6, §3.8, §4.2, §4.3).

Driven against ``_managed_fakes``: a FakeDB that follows migration 049's
functions statement by statement, the RunPod REST surface as an
``httpx.MockTransport``, an in-memory R2 and a fake Modal client; one Clock for
the code and the fakes. Every RunPod error answer, every guard verdict, every
loss rule, the outage clock (SR-4), the reaper's candidate rules and the HF
write check are pinned here.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
import unittest
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import hf_publish  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import (  # noqa: E402
    Clock, FakeDB, FakeModal, FakeR2, FakeRunpod, PgError, catalog_entry,
)

GOOD_IMAGE = "ghcr.io/svendanilborodun/edubotics-trainer:2.32.0@sha256:" + "a" * 64
BASE_ENV = {
    "TRAINING_DISPATCH": "managed", "SUPABASE_URL": "https://sb.test", "SUPABASE_ANON_KEY": "anon-key",
    "TRAINING_WORKER_API_URL": "https://api.test", "R2_ENDPOINT_URL": "https://r2.test", "R2_ACCESS_KEY_ID": "k",
    "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "b", "HF_TOKEN": "hf_" + "x" * 30, "RUNPOD_API_KEY": "rp-key",
    "RUNPOD_TRAINER_IMAGE": GOOD_IMAGE,
}


def settings(**env):
    e = dict(BASE_ENV)
    e.update(env)
    return D.load_settings({k: v for k, v in e.items() if v is not None})


class SettingsTests(unittest.TestCase):
    def test_the_default_ladder(self):
        s = settings()
        self.assertEqual([r["rung"] for r in s.ladder], ["runpod_community", "runpod_secure", "modal"])
        self.assertEqual(len(s.rung_gpus("runpod_community")), 13)
        self.assertEqual(len(s.rung_gpus("runpod_secure")), 7)
        self.assertNotIn("NVIDIA GeForce RTX 3070", [g["id"] for g in s.rung_gpus("runpod_community")])
        self.assertEqual(s.missing, ())

    def test_ladder_validation(self):
        bad = ['{"rung":"modal"}', "[]", '[{"rung":"aws"},{"rung":"modal"}]', '[{"rung":"runpod_secure","gpus":[]},{"rung":"modal"}]',
               '[{"rung":"modal"},{"rung":"runpod_secure","gpus":[{"id":"x","min_vcpu":8}]}]',
               '[{"rung":"runpod_secure","gpus":[{"id":"x","min_vcpu":8}]}]',
               '[{"rung":"runpod_secure","gpus":[{"id":"x","min_vcpu":0}]},{"rung":"modal"}]',
               '[{"rung":"runpod_secure","gpus":[{"id":"x","min_vcpu":33}]},{"rung":"modal"}]',
               '[{"rung":"runpod_secure","gpus":[{"id":"","min_vcpu":8}]},{"rung":"modal"}]',
               '[{"rung":"runpod_secure","gpus":[{"id":"x","min_vcpu":8,"cloud":"x"}]},{"rung":"modal"}]',
               '[{"rung":"modal"},{"rung":"modal"}]', '[{"rung":"modal","gpus":[]}]', "not json"]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(D.SettingsError):
                settings(RUNPOD_LADDER=raw)
        s = settings(RUNPOD_LADDER='[{"rung":"modal"}]')
        self.assertFalse(s.has_runpod_rung)
        self.assertNotIn("RUNPOD_API_KEY", settings(RUNPOD_LADDER='[{"rung":"modal"}]', RUNPOD_API_KEY=None).missing)

    def test_the_cadence_lock(self):
        with self.assertRaises(D.SettingsError):
            settings(TRAINING_SAVE_FREQ="20")
        with self.assertRaises(D.SettingsError):
            settings(TRAINING_MIRROR_EVERY="40")
        s = settings(TRAINING_SAVE_FREQ="20", TRAINING_MIRROR_EVERY="40", TRAINING_E2E="1")
        self.assertEqual((s.save_freq, s.mirror_every, s.e2e), (20, 40, True))
        with self.assertRaises(D.SettingsError):
            settings(TRAINING_SAVE_FREQ="20", TRAINING_MIRROR_EVERY="50", TRAINING_E2E="1")
        self.assertEqual((settings().save_freq, settings().mirror_every), (2500, 10000))

    def test_e2e_logs_a_warning_on_every_boot(self):
        env = dict(BASE_ENV, TRAINING_SAVE_FREQ="20", TRAINING_MIRROR_EVERY="40", TRAINING_E2E="1")
        with self.assertLogs("app.services.training_dispatch", level="WARNING") as logs:
            D.validate_boot(env)
        self.assertTrue(any("TRAINING_E2E=1" in m for m in logs.output))
        D.validate_boot(dict(BASE_ENV, TRAINING_DISPATCH="legacy"))

    def test_the_cap_ceiling_while_the_ladder_ends_in_modal(self):
        self.assertEqual(D.MANAGED_CAP_CEILING_S, 23520)
        self.assertEqual(settings(MANAGED_ACT_CAP_S="21600").cap_s, 21600)
        with self.assertRaises(D.SettingsError):
            settings(MANAGED_ACT_CAP_S="23521")

    def test_the_image_must_be_digest_pinned(self):
        for bad in ("ghcr.io/svendanilborodun/edubotics-trainer:latest", "docker.io/x/y@sha256:" + "a" * 64,
                    "ghcr.io/svendanilborodun/edubotics-trainer@sha256:" + "A" * 64):
            with self.subTest(image=bad), self.assertRaises(D.SettingsError):
                settings(RUNPOD_TRAINER_IMAGE=bad)
        settings(RUNPOD_TRAINER_IMAGE="ghcr.io/svendanilborodun/edubotics-trainer@sha256:" + "0" * 64)

    def test_prefix_and_worker_url(self):
        for bad in ("Edu", "ab", "a" * 41, "edu_train"):
            with self.subTest(prefix=bad), self.assertRaises(D.SettingsError):
                settings(RUNPOD_POD_PREFIX=bad)
        with self.assertRaises(D.SettingsError):
            settings(TRAINING_WORKER_API_URL="http://api.test")

    def test_missing_variables_stop_only_a_managed_boot_and_name_no_value(self):
        env = dict(BASE_ENV, R2_BUCKET=None, HF_TOKEN=None)
        env = {k: v for k, v in env.items() if v is not None}
        with self.assertRaises(RuntimeError) as ctx:
            D.validate_boot(env)
        self.assertIn("R2_BUCKET", str(ctx.exception))
        self.assertIn("HF_TOKEN", str(ctx.exception))
        self.assertNotIn("anon-key", str(ctx.exception))
        D.validate_boot(dict(env, TRAINING_DISPATCH="legacy"))  # legacy: logged, not fatal
        with self.assertRaises(RuntimeError):
            D.validate_boot(dict(BASE_ENV, RUNPOD_LADDER="[]", TRAINING_DISPATCH="legacy"))  # malformed: always fatal

    def test_runpod_variables_are_required_only_with_a_runpod_rung(self):
        env = {k: v for k, v in BASE_ENV.items() if k not in ("RUNPOD_API_KEY", "RUNPOD_TRAINER_IMAGE")}
        self.assertEqual(set(D.load_settings(env).missing), {"RUNPOD_API_KEY", "RUNPOD_TRAINER_IMAGE"})
        self.assertEqual(D.load_settings(dict(env, RUNPOD_LADDER='[{"rung":"modal"}]')).missing, ())

    def test_the_spec_constants(self):
        for name, value in (("SAVE_FREQ", 2500), ("MIRROR_EVERY", 10000), ("MAX_ATTEMPTS", 4), ("WALL_CAP_S", 43200),
                            ("HEARTBEAT_LOST_S", 300), ("STALL_S", 1500), ("PLACING_STALE_S", 600),
                            ("PLACEMENT_GIVEUP_S", 1800), ("RUNPOD_GIVEUP_S", 120), ("MAX_CREATES_PER_TICK", 6),
                            ("CREATE_400_CACHE_S", 300), ("PUBLISH_RETRY_S", 21600), ("WATCHDOG_GRACE_S", 1200),
                            ("FINAL_UPLOAD_WAIT_S", 1080), ("RESUME_CRASH_LIMIT", 2), ("MIRROR_LEASE_S", 900)):
            self.assertEqual(getattr(D, name), value, name)
        self.assertEqual(D.FINAL_UPLOAD_WAIT_S, D.WATCHDOG_GRACE_S - 120)


class Base(unittest.IsolatedAsyncioTestCase):
    """Fakes installed; STATE fresh; the HF check passed; publish scheduling recorded."""

    env: dict = {}

    async def asyncSetUp(self):
        D.reset_state_for_tests()
        hf_publish.reset_for_tests()
        self.clock = Clock()
        self.db = FakeDB(self.clock)
        self.r2 = FakeR2()
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        self.s = settings(**self.env)
        D.STATE.hf_check = True
        D.STATE.last_failed_round_at = self.clock.t - 10_000
        self.published = []
        self.logs_sleep = []
        self._stack = []
        for cm in (patch.object(D, "_now", self.clock), patch.object(D, "_sb", lambda: self.db),
                   patch.object(D, "SETTINGS", self.s), patch.object(D, "_sleep", self._fake_sleep),
                   patch.object(hf_publish, "schedule_publish", lambda tid, n: self.published.append((tid, n)) or True),
                   patch.object(hf_publish, "schedule_failed_log", lambda row: True),
                   self.r2.installed(), self.rp.installed(), self.modal.installed()):
            cm.__enter__()
            self._stack.append(cm)

    async def asyncTearDown(self):
        for cm in reversed(self._stack):
            cm.__exit__(None, None, None)

    async def _fake_sleep(self, s):
        self.logs_sleep.append(s)

    def stock(self, cloud="COMMUNITY", gpus=("NVIDIA RTX A5000",), dcs=("EU-RO-1",)):
        self.rp.catalog[cloud] = [catalog_entry(g, dcs) for g in gpus]

    def queued(self, **kw):
        return self.db.add_training(status="queued", **kw)


class DispatcherPlacement(Base):
    async def test_a_community_pod_with_exactly_the_spec_body(self):
        self.stock(dcs=("EU-RO-1", "EU-SE-1"))
        row = self.queued()
        out = await D.dispatch_tick()
        self.assertEqual(out["placed"], 1)
        (create,) = self.rp.creates()
        body = create[3]
        a = self.db.attempt(row["id"], 1)
        self.assertEqual(body["name"], f"edubotics-train-{row['id']}-a1")
        self.assertEqual(body["image"], GOOD_IMAGE)
        self.assertEqual(body["cloud"], "COMMUNITY")
        self.assertEqual(body["gpu"], {"id": "NVIDIA RTX A5000", "count": 1, "minCudaVersion": "12.6",
                                       "minVcpuCountPerGpu": 8, "minRamPerGpu": 24})
        self.assertEqual(body["dataCenterIds"], ["EU-RO-1", "EU-SE-1"])
        self.assertEqual(a["allowed_dcs"], ["EU-RO-1", "EU-SE-1"])  # FR-8: persisted before the create
        self.assertEqual((body["disk"], body["ports"], body["startSsh"], body["startJupyter"], body["globalNetworking"]),
                         (50, [], False, False, False))
        self.assertEqual(set(body["env"]), {"EDUBOTICS_TRAINING_ID", "EDUBOTICS_ATTEMPT", "EDUBOTICS_WORKER_TOKEN",
                                            "EDUBOTICS_API_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY"})
        self.assertEqual(body["env"]["EDUBOTICS_WORKER_TOKEN"], self.db.row(row["id"])["worker_token"])
        self.assertEqual(body["env"]["EDUBOTICS_API_URL"], "https://api.test")
        self.assertEqual((a["state"], a["backend"], a["gpu_type"]), ("starting", "runpod_community", "NVIDIA RTX A5000"))
        self.assertEqual(a["cloud_ref"], list(self.rp.pods)[0])
        catalog_call = [c for c in self.rp.calls if c[1] == "/v2/catalog/gpus"][0]
        self.assertEqual(catalog_call[2]["include"], "AVAILABILITY")
        self.assertEqual(catalog_call[2]["product"], "POD")
        self.assertEqual(catalog_call[2]["minCudaVersion"], "12.6")
        self.assertEqual(len(catalog_call[2]["countryCodes"].split(",")), 30)

    async def test_the_allowed_dcs_write_precedes_the_create(self):
        self.stock()
        self.queued()
        order = []
        real_create = self.rp.handler

        def handler(request):
            if request.method == "POST":
                order.append(("create", [o for o in self.db.ops if o[0] == "update" and o[1] == "training_attempts"
                                         and "allowed_dcs" in (o[3] or {})] != []))
            return real_create(request)

        import httpx

        from app.services import runpod_client
        runpod_client.set_transport_for_tests(httpx.MockTransport(handler))
        await D.dispatch_tick()
        self.assertEqual(order, [("create", True)])

    async def test_attempt_four_goes_to_modal_only(self):
        self.stock()
        row = self.queued(current_attempt=3)
        for n in (1, 2, 3):
            self.db.add_attempt(row["id"], n, state="lost", backend="runpod_secure")
        await D.dispatch_tick()
        self.assertEqual(self.rp.creates(), [])
        self.assertEqual(len(self.modal.spawned), 1)
        self.assertEqual(self.db.attempt(row["id"], 4)["backend"], "modal")

    async def test_community_is_skipped_after_two_community_losses(self):
        self.stock("COMMUNITY")
        self.stock("SECURE")
        row = self.queued(current_attempt=2)
        self.db.add_attempt(row["id"], 1, state="lost", backend="runpod_community")
        self.db.add_attempt(row["id"], 2, state="lost", backend="runpod_community")
        await D.dispatch_tick()
        (create,) = self.rp.creates()
        self.assertEqual(create[3]["cloud"], "SECURE")

    async def test_secure_dcs_are_cut_to_the_gdpr_list(self):
        self.s = settings(RUNPOD_LADDER='[{"rung":"runpod_secure","gpus":[{"id":"NVIDIA RTX A5000","min_vcpu":8}]},'
                                        '{"rung":"modal"}]')
        D.SETTINGS = self.s
        self.stock("SECURE", dcs=("EU-RO-1", "EU-CZ-1", "US-KS-2"))
        self.queued()
        with patch.object(D, "SETTINGS", self.s):
            await D.dispatch_tick(self.s)
        (create,) = self.rp.creates()
        self.assertEqual(create[3]["dataCenterIds"], ["EU-RO-1"])

    async def test_nothing_placed_releases_and_costs_no_attempt(self):
        row = self.queued()  # no stock anywhere
        out = await D.dispatch_tick()
        self.assertEqual(out["released"], 1)
        r = self.db.row(row["id"])
        self.assertEqual((r["current_attempt"], r["worker_token"]), (0, None))
        self.assertIsNotNone(r["unplaced_since"])
        self.assertEqual(self.db.tables["training_attempts"], [])
        self.assertEqual(self.modal.spawned, [], "RunPod is usable: Modal waits RUNPOD_GIVEUP_S")

    async def test_modal_after_two_minutes_of_runpod_retries(self):
        row = self.queued()
        await D.dispatch_tick()
        self.clock.advance(60)
        await D.dispatch_tick()
        self.assertEqual(self.modal.spawned, [])
        self.clock.advance(61)
        await D.dispatch_tick()
        self.assertEqual(len(self.modal.spawned), 1)
        self.assertEqual(self.db.attempt(row["id"], 1)["backend"], "modal")
        self.assertIsNone(self.db.row(row["id"])["unplaced_since"])  # attach cleared it

    async def test_s18_after_thirty_minutes_unplaced(self):
        row = self.queued()
        self.modal.spawn_raises = RuntimeError("no modal either")
        await D.dispatch_tick()
        self.clock.advance(D.PLACEMENT_GIVEUP_S)
        await D.dispatch_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["error_message"]), ("failed", T.S18))

    async def test_the_modal_call_is_the_frozen_contract(self):
        from app.services import modal_client

        self.s = settings(RUNPOD_LADDER='[{"rung":"modal"}]')
        row = self.queued()
        await D.dispatch_tick(self.s)
        (kw,) = self.modal.spawned
        self.assertEqual(tuple(kw), ("training_id", "attempt", "worker_token", "api_url", "supabase_url",
                                     "supabase_anon_key"))
        self.assertEqual(kw["api_url"], "https://api.test")
        self.assertEqual(kw["supabase_url"], "https://sb.test")
        self.assertEqual(kw["supabase_anon_key"], "anon-key")
        self.assertEqual(kw["worker_token"], self.db.row(row["id"])["worker_token"])

    def test_the_real_modal_client_spawns_exactly_those_keywords(self):
        import ast
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "modal_client.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_managed_training_job")
        self.assertEqual([a.arg for a in fn.args.kwonlyargs], ["training_id", "attempt", "worker_token", "api_url",
                                                               "supabase_url", "supabase_anon_key"])
        assign = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", "") == "MANAGED_SPAWN_KWARGS")
        self.assertEqual(ast.literal_eval(assign.value), ("training_id", "attempt", "worker_token", "api_url",
                                                          "supabase_url", "supabase_anon_key"))

    async def test_a_modal_spawn_failure_releases(self):
        self.s = settings(RUNPOD_LADDER='[{"rung":"modal"}]')
        self.modal.spawn_raises = RuntimeError("app not deployed")
        row = self.queued()
        out = await D.dispatch_tick(self.s)
        self.assertEqual(out["released"], 1)
        self.assertEqual(self.db.row(row["id"])["current_attempt"], 0)

    async def test_attach_conflict_deletes_the_pod(self):
        self.stock()
        row = self.queued()
        real = self.db._rpc_attach_training_attempt
        self.db._rpc_attach_training_attempt = lambda **kw: (_ for _ in ()).throw(PgError("P0049"))
        await D.dispatch_tick()
        self.db._rpc_attach_training_attempt = real
        self.assertEqual(len(self.rp.deletes()), 1)
        self.assertEqual(self.rp.pods, {})
        self.assertEqual(self.db.row(row["id"])["current_attempt"], 1, "a lost attach is not released here")

    async def test_attach_exception_deletes_the_pod(self):
        self.stock()
        self.queued()
        self.db.failures["rpc:attach_training_attempt"] = 1
        await D.dispatch_tick()
        self.assertEqual(len(self.rp.deletes()), 1)

    async def test_modal_attach_conflict_cancels_the_call(self):
        self.s = settings(RUNPOD_LADDER='[{"rung":"modal"}]')
        self.queued()
        self.db._rpc_attach_training_attempt = lambda **kw: (_ for _ in ()).throw(PgError("P0049"))
        await D.dispatch_tick(self.s)
        self.assertEqual(self.modal.canceled, ["fc-1"])

    async def test_two_instances_claim_one_row_once(self):
        self.stock()
        row = self.queued()
        await asyncio.gather(D.dispatch_tick(), D.dispatch_tick())
        self.assertEqual(len(self.rp.creates()), 1)
        self.assertEqual(len([a for a in self.db.tables["training_attempts"] if a["training_id"] == row["id"]]), 1)
        self.assertEqual(len(self.db.calls("claim_training_attempt")), 2)

    async def test_max_runpod_pods(self):
        self.stock()
        D.STATE.pod_count = self.s.max_runpod_pods
        self.queued()
        await D.dispatch_tick()
        self.assertEqual(self.rp.creates(), [])

    async def test_a_failed_boot_probe_blocks_runpod(self):
        self.stock()
        self.rp.probe_status = 403
        self.assertIs(await D.runpod_probe_tick(), False)
        self.queued()
        await D.dispatch_tick()
        self.assertEqual(self.rp.creates(), [])
        self.rp.probe_status = 200
        self.assertIs(await D.runpod_probe_tick(), True)

    async def test_finished_rows_end_and_publish(self):
        row = self.queued(current_attempt=1)
        self.db.add_attempt(row["id"], 1, state="lost", backend="runpod_community")
        self.db.add_checkpoint(row["id"], 1, 50000, state="ready", manifest={})
        await D.dispatch_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["status_note"]), ("running", T.S3))
        self.assertEqual(self.published, [(row["id"], 1)])

    async def test_a_finished_lost_attempt_is_not_counted_twice(self):
        # Fix round 2026-10-19: the requeue that lost attempt 1 counted its 900 s already;
        # ending it now (a ready final) adds nothing, on the API's side and in 049.
        row = self.queued(current_attempt=1, train_seconds_used=900)
        self.db.add_attempt(row["id"], 1, state="lost", backend="runpod_community",
                            first_heartbeat_at=self.clock.iso(self.clock.t - 900))
        self.db.add_checkpoint(row["id"], 1, 50000, state="ready", manifest={})
        out = await D.dispatch_tick()
        self.assertEqual(out["finished"], 1)
        (call,) = self.db.calls("end_training_attempt")
        self.assertEqual(call["p_train_seconds"], 0)
        self.assertEqual(self.db.row(row["id"])["train_seconds_used"], 900)
        a = self.db.attempt(row["id"], 1)
        self.assertEqual((a["state"], a["end_reason"]), ("ended", "succeeded"))

    async def test_the_fake_end_follows_049_and_skips_only_a_counted_attempt(self):
        # The FakeDB's end_training_attempt as 049's: a lost attempt adds nothing, a running one its seconds.
        lost = self.queued(current_attempt=1, train_seconds_used=300)
        self.db.add_attempt(lost["id"], 1, state="lost")
        running = self.db.add_training(status="running", current_attempt=1, train_seconds_used=300)
        self.db.add_attempt(running["id"], 1, state="running")
        for r in (lost, running):
            self.db.rpc("end_training_attempt", {"p_training_id": r["id"], "p_attempt": 1, "p_reason": "succeeded",
                                                 "p_note": T.S3, "p_train_seconds": 300}).execute()
        self.assertEqual((self.db.row(lost["id"])["train_seconds_used"],
                          self.db.row(running["id"])["train_seconds_used"]), (300, 600))


class DispatcherBudgets(Base):
    async def test_the_attempt_cap(self):
        row = self.queued(current_attempt=4)
        self.db.add_attempt(row["id"], 4, state="lost", backend="modal")
        await D.dispatch_tick()
        self.assertEqual(self.db.row(row["id"])["error_message"], T.S4)

    async def test_the_training_time_cap_uses_the_setting(self):
        self.s = settings(MANAGED_ACT_CAP_S="21600")
        row = self.queued(train_seconds_used=21600 - 300)
        await D.dispatch_tick(self.s)
        msg = self.db.row(row["id"])["error_message"]
        self.assertEqual(msg, T.s5(21600))
        self.assertIn("6 Stunden", msg)
        self.assertEqual(T.s5(14400), T.S5.format(hours="4"))
        self.assertEqual(T.hours_text(5400), "1,5")

    async def test_the_wall_cap(self):
        row = self.queued(requested_at=self.clock.iso(self.clock.t - (D.WALL_CAP_S - 300)))
        await D.dispatch_tick()
        self.assertEqual(self.db.row(row["id"])["error_message"], T.S6)

    async def test_twelve_blocked_rows_are_all_marked_and_all_end_with_s18(self):
        D.STATE.hf_check = "rejected"
        self.stock()
        rows = [self.queued() for _ in range(12)]
        await D.dispatch_tick()
        self.assertTrue(all(self.db.row(r["id"])["unplaced_since"] for r in rows))
        self.assertEqual(self.db.calls("claim_training_attempt"), [])
        self.assertEqual(self.rp.creates(), [])
        self.clock.advance(D.PLACEMENT_GIVEUP_S)
        await D.dispatch_tick()
        self.assertEqual({self.db.row(r["id"])["error_message"] for r in rows}, {T.S18})

    async def test_budgets_run_while_placement_is_blocked(self):
        D.STATE.hf_check = False
        row = self.queued(train_seconds_used=14400)
        await D.dispatch_tick()
        self.assertEqual(self.db.row(row["id"])["status"], "failed")

    async def test_the_unplaced_write_is_filtered(self):
        D.STATE.hf_check = "no_token"
        row = self.queued()
        await D.dispatch_tick()
        (op,) = [o for o in self.db.ops if o[0] == "update" and o[1] == "trainings" and "unplaced_since" in o[3]]
        filters = {(k, c, str(v)) for k, c, v, _ in op[2]}
        self.assertIn(("eq", "managed", "True"), filters)
        self.assertIn(("eq", "status", "queued"), filters)
        self.assertIn(("eq", "current_attempt", "0"), filters)
        self.assertIn(("is", "unplaced_since", "null"), filters)
        first = self.db.row(row["id"])["unplaced_since"]
        self.clock.advance(30)
        await D.dispatch_tick()
        self.assertEqual(self.db.row(row["id"])["unplaced_since"], first, "never moved forward")

    async def test_unconfigured_vars_block_placement_too(self):
        s = settings(SUPABASE_ANON_KEY=None)
        self.assertTrue(D.placement_blocked(s))
        self.assertFalse(D.placement_blocked(self.s))
        for verdict in (None, "unreachable", "bad_reply", True):
            D.STATE.hf_check = verdict
            self.assertFalse(D.placement_blocked(self.s), verdict)
        for verdict in (False, "rejected", "no_token"):
            D.STATE.hf_check = verdict
            self.assertTrue(D.placement_blocked(self.s), verdict)


class RunpodErrorTable(Base):
    """§3.6 call 3's per-candidate rules, FR-14, TR-8 n7."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stock(gpus=("NVIDIA RTX A5000", "NVIDIA GeForce RTX 3080"), dcs=("EU-RO-1",))

    async def test_400_goes_to_the_next_candidate_and_is_cached(self):
        self.rp.create_answers = [(400, {"detail": "bad gpu"}, None)]
        self.queued()
        await D.dispatch_tick()
        creates = self.rp.creates()
        self.assertEqual([c[3]["gpu"]["id"] for c in creates], ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 3080"])
        self.assertIn(("COMMUNITY", "NVIDIA RTX A5000", "EU-RO-1"), D.STATE.create_400)
        row2 = self.queued()
        await D.dispatch_tick()
        self.assertEqual(self.rp.creates()[-1][3]["gpu"]["id"], "NVIDIA GeForce RTX 3080", "the 400 combo is skipped")

    async def test_every_candidate_400_logs_error_with_the_detail(self):
        self.rp.create_answers = [(400, {"detail": "imageRef ghcr.io/x@sha256:00 unresolvable"}, None)] * 2
        self.s = settings(RUNPOD_LADDER='[{"rung":"runpod_community","gpus":[{"id":"NVIDIA RTX A5000","min_vcpu":8},'
                                        '{"id":"NVIDIA GeForce RTX 3080","min_vcpu":8}]},{"rung":"modal"}]')
        self.queued()
        with self.assertLogs("app.services.training_dispatch", level="ERROR") as logs:
            await D.dispatch_tick(self.s)
        self.assertTrue(any("400" in m and "imageRef ghcr.io/x@sha256:00 unresolvable" in m for m in logs.output))

    async def test_every_candidate_403_logs_error_once_per_process(self):
        self.rp.create_answers = [(403, None, None)] * 4
        self.s = settings(RUNPOD_LADDER='[{"rung":"runpod_community","gpus":[{"id":"NVIDIA RTX A5000","min_vcpu":8},'
                                        '{"id":"NVIDIA GeForce RTX 3080","min_vcpu":8}]},{"rung":"modal"}]')
        self.queued()
        with self.assertLogs("app.services.training_dispatch", level="ERROR") as logs:
            await D.dispatch_tick(self.s)
        self.assertEqual(sum("pods:write" in m for m in logs.output), 1)
        self.queued()
        with self.assertLogs("app.services.training_dispatch", level="INFO") as logs2:
            await D.dispatch_tick(self.s)
        self.assertFalse(any("pods:write" in m for m in logs2.output))

    async def test_402_stops_runpod_for_sixty_seconds(self):
        self.rp.create_answers = [(402, None, None)]
        row = self.queued()
        await D.dispatch_tick()
        self.assertEqual(len(self.rp.creates()), 1)
        self.assertGreater(D.STATE.runpod_402_until, self.clock.t)
        self.assertEqual(self.modal.spawned, [], "this tick's rungs were chosen while RunPod was usable")
        self.assertEqual(self.db.row(row["id"])["current_attempt"], 0, "released, no attempt consumed")
        self.clock.advance(D.DISPATCH_INTERVAL_S)
        await D.dispatch_tick()
        self.assertEqual(len(self.rp.creates()), 1, "no RunPod inside the 60 s back-off")
        self.assertEqual(len(self.modal.spawned), 1, "RunPod not usable: Modal without the 2-min wait")
        self.assertEqual(self.db.attempt(row["id"], 1)["backend"], "modal")
        self.clock.advance(D.NO_BALANCE_BACKOFF_S)
        self.assertTrue(D._runpod_usable(self.clock.t, self.s))

    async def test_422_and_other_4xx_are_errors_and_end_runpod_for_the_tick(self):
        for status in (422, 404, 413, 418):
            with self.subTest(status=status):
                D.reset_state_for_tests()
                D.STATE.hf_check = True
                D.STATE.last_failed_round_at = self.clock.t - 10_000
                self.rp.calls.clear()
                self.db.tables["trainings"].clear()
                self.db.tables["training_attempts"].clear()
                self.rp.create_answers = [(status, None, None)]
                self.queued()
                self.queued()
                with self.assertLogs("app.services.training_dispatch", level="ERROR"):
                    await D.dispatch_tick()
                self.assertEqual(len(self.rp.creates()), 1, "no RunPod for the rest of the tick")

    async def test_429_waits_retry_after_and_retries_the_same_candidate(self):
        self.rp.create_answers = [(429, None, {"Retry-After": "7"}), (429, None, {"Retry-After": "99"})]
        self.queued()
        await D.dispatch_tick()
        ids = [c[3]["gpu"]["id"] for c in self.rp.creates()]
        self.assertEqual(ids, ["NVIDIA RTX A5000"] * 3)
        self.assertEqual(self.logs_sleep[:2], [7.0, 30.0])

    async def test_5xx_and_network_retry_after_2_then_4_seconds_then_next(self):
        self.rp.create_answers = [(502, None, None), ("network", None, None), (503, None, None)]
        self.queued()
        await D.dispatch_tick()
        ids = [c[3]["gpu"]["id"] for c in self.rp.creates()]
        self.assertEqual(ids, ["NVIDIA RTX A5000"] * 3 + ["NVIDIA GeForce RTX 3080"])
        self.assertEqual(self.logs_sleep[:2], [2, 4])

    async def test_at_most_six_creates_per_tick_and_the_rest_wait_unmarked(self):
        self.rp.create_answers = [(403, None, None)] * 20  # 403 is not cached: every row tries both cards
        rows = [self.queued() for _ in range(5)]
        await D.dispatch_tick()
        self.assertEqual(len(self.rp.creates()), D.MAX_CREATES_PER_TICK)
        self.assertEqual(len(self.db.calls("claim_training_attempt")), 3)
        untouched = [r for r in rows if not any(c["p_training_id"] == r["id"]
                                                for c in self.db.calls("claim_training_attempt"))]
        self.assertTrue(untouched)
        for r in untouched:
            self.assertIsNone(self.db.row(r["id"])["unplaced_since"], "TR-8 n6: not marked")


class PlacementGuard(Base):
    def _attempt(self, backend, allowed=("EU-RO-1",)):
        row = self.db.add_training(status="running", current_attempt=1)
        a = self.db.add_attempt(row["id"], 1, state="running", backend=backend, cloud_ref="pod1",
                                allowed_dcs=list(allowed))
        return row, a

    def test_secure_is_strict(self):
        _, a = self._attempt("runpod_secure")
        self.assertEqual(D.judge_guard(a, {"dataCenterId": "EU-CZ-1", "status": "RUNNING"}, None)[0], "refuse")
        self.assertEqual(D.judge_guard(a, {"dataCenterId": None, "status": "RUNNING"}, None)[0], "refuse")
        self.assertEqual(D.judge_guard(a, {"dataCenterId": None, "status": "STARTING"}, None), ("ok", False))
        self.assertEqual(D.judge_guard(a, {"dataCenterId": "EU-RO-1", "status": "RUNNING", "cudaVersion": "12.8"},
                                       None), ("ok", True))

    def test_community_refuses_only_a_known_outside_dc(self):
        _, a = self._attempt("runpod_community")
        table = self.rp.regions
        self.assertEqual(D.judge_guard(a, {"dataCenterId": "US-KS-2", "cudaVersion": "12.8"}, table)[0], "refuse")
        with self.assertLogs("app.services.training_dispatch", level="WARNING") as logs:
            self.assertEqual(D.judge_guard(a, {"dataCenterId": None, "cudaVersion": "12.8"}, table), ("ok", False))
            self.assertEqual(D.judge_guard(a, {"dataCenterId": "EU-CZ-1", "cudaVersion": "12.8"}, table), ("ok", True))
            self.assertEqual(D.judge_guard(a, {"dataCenterId": "ZZ-NEW-1", "cudaVersion": "12.8"}, table), ("ok", True))
            self.assertEqual(D.judge_guard(a, {"dataCenterId": "XX-1", "cudaVersion": "12.8"}, table), ("ok", True))
        self.assertTrue(any("no data centre" in m for m in logs.output))
        self.assertTrue(any("EU-CZ-1" in m for m in logs.output))
        # the table could not be read: no verdict
        self.assertEqual(D.judge_guard(a, {"dataCenterId": "US-KS-2", "cudaVersion": "12.8"}, None), ("ok", False))

    def test_cuda(self):
        _, a = self._attempt("runpod_secure")
        pod = {"dataCenterId": "EU-RO-1", "status": "RUNNING"}
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion="12.4"), None)[0], "refuse")
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion="11.8"), None)[0], "refuse")
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion="12.10"), None), ("ok", True))
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion=None), None), ("ok", False))
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion="13.0"), None), ("ok", True))
        self.assertEqual(D.judge_guard(a, dict(pod, cudaVersion="cuda"), None), ("ok", False))

    async def test_a_refusal_deletes_requeues_and_skips_the_community_dc(self):
        row, a = self._attempt("runpod_community")
        self.db.row(row["id"])["worker_token"] = "t"
        self.rp.add_pod(id="pod1", dataCenterId="US-KS-2", cudaVersion="12.8")
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "placement_guard")
        self.assertIn("pod1", self.rp.deletes())
        self.assertIn("US-KS-2", D.STATE.dc_skip)

    async def test_the_guard_is_rejudged_until_a_verdict_and_after_a_restart(self):
        row, a = self._attempt("runpod_community")
        pod = self.rp.add_pod(id="pod1", dataCenterId=None, cudaVersion=None, status="RUNNING")
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        await D.sweep_tick()
        self.assertNotIn((row["id"], 1), D.STATE.guard_final)
        pod.update(dataCenterId="EU-RO-1", cudaVersion="12.8")
        D.reset_state_for_tests()  # an API restart: the verdict comes from the persisted allowed_dcs
        D.STATE.hf_check = True
        D.STATE.last_failed_round_at = self.clock.t - 10_000
        await D.sweep_tick()
        self.assertIn((row["id"], 1), D.STATE.guard_final)
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.assertEqual(self.db.attempt(row["id"], 1)["data_center"], "EU-RO-1")

    async def test_a_known_outside_id_reported_later_is_refused_that_round(self):
        row, a = self._attempt("runpod_community")
        pod = self.rp.add_pod(id="pod1", dataCenterId=None, cudaVersion="12.8", status="RUNNING")
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        await D.sweep_tick()
        pod["dataCenterId"] = "CA-MTL-1"
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "placement_guard")

    async def test_an_unreadable_table_is_no_verdict(self):
        row, a = self._attempt("runpod_community")
        self.rp.add_pod(id="pod1", dataCenterId="US-KS-2", cudaVersion="12.8", status="RUNNING")
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        self.rp.dc_status = 500
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.rp.dc_status = 200
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "placement_guard")


class SweepLosses(Base):
    def _running(self, backend="runpod_community", **kw):
        row = self.db.add_training(status="running", current_attempt=1, worker_token="tok",
                                   last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso(), **kw)
        a = self.db.add_attempt(row["id"], 1, state="running", backend=backend, cloud_ref="pod1",
                                first_heartbeat_at=self.clock.iso(self.clock.t - 600), started_at=self.clock.iso(),
                                allowed_dcs=["EU-RO-1"], data_center="EU-RO-1")
        if backend in D.RUNPOD_RUNGS:
            self.rp.add_pod(id="pod1", dataCenterId="EU-RO-1", cudaVersion="12.8", status="RUNNING")
        else:
            a["cloud_ref"] = "fc-1"
        return row, a

    async def test_pod_gone_needs_two_rounds_thirty_seconds_apart(self):
        row, _ = self._running()
        self.db.add_checkpoint(row["id"], 1, 2500, state="ready", manifest={})
        self.rp.pods.clear()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.clock.advance(29)
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.clock.advance(1)
        await D.sweep_tick()
        r = self.db.row(row["id"])
        self.assertEqual((self.db.attempt(row["id"], 1)["end_reason"], r["status"], r["current_step"]),
                         ("pod_gone", "queued", 2500))
        self.assertEqual(r["status_note"], T.s1(2500))
        self.assertIn("2.500", r["status_note"])
        self.assertEqual(r["train_seconds_used"], 630)  # now − first_heartbeat_at

    async def test_a_requeue_keeps_the_loss_of_the_last_point_it_keeps(self):
        # V2-6: the requeue rolls the step back to 2500; the loss shown is 2000's, not 3000's.
        row, _ = self._running(loss_history=[{"s": 2000, "l": 0.4, "t": 1}, {"s": 3000, "l": 0.2, "t": 2}],
                               current_step=3000, current_loss=0.2)
        self.db.add_checkpoint(row["id"], 1, 2500, state="ready", manifest={})
        self.rp.pods.clear()
        await D.sweep_tick()
        self.clock.advance(30)
        await D.sweep_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["current_step"], r["current_loss"]), ("queued", 2500, 0.4))
        self.assertEqual([e["s"] for e in r["loss_history"]], [2000])

    async def test_an_exited_pod_is_gone_and_without_a_checkpoint_the_note_is_s2(self):
        row, _ = self._running()
        self.rp.pods["pod1"]["status"] = "EXITED"
        await D.sweep_tick()
        self.clock.advance(30)
        await D.sweep_tick()
        self.assertEqual(self.db.row(row["id"])["status_note"], T.S2)

    async def test_a_db_outage_skips_the_round_and_moves_the_clock(self):
        row, _ = self._running()
        # SR-4 / F15: Supabase down for 6 min. Every round fails (and moves the
        # clock); the worker's own heartbeats fail as well.
        for _ in range(12):
            self.db.failures["select:trainings"] = 1
            await D.sweep_tick()
            self.assertEqual(D.STATE.last_failed_round_at, self.clock.t)
            self.clock.advance(30)
        # back: last_progress_at is 6 min old, but t0 = max(…, last_failed_round_at)
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        for _ in range(3):
            self.clock.advance(30)
            await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.assertEqual(self.db.row(row["id"])["status"], "running")

    async def test_the_heartbeat_rule_counts_from_the_last_failed_db_round(self):
        # SR-4 (V1-4 Q17): the worker's heartbeats failed with the database, so
        # last_progress_at is old; t0 = max(last_progress_at, last_failed_round_at)
        # keeps two good rounds right after the outage from calling it lost.
        row, _ = self._running()
        self.db.row(row["id"])["last_progress_at"] = self.clock.iso(self.clock.t - D.HEARTBEAT_LOST_S - 100)
        D.STATE.last_failed_round_at = self.clock.t - 10
        await D.sweep_tick()
        self.clock.advance(30)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.assertEqual(self.db.row(row["id"])["status"], "running")
        self.assertNotIn((row["id"], 1), D.STATE.heartbeat_miss)
        # … and the rule still fires once the outage is HEARTBEAT_LOST_S behind.
        self.clock.advance(D.HEARTBEAT_LOST_S)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.clock.advance(30)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "heartbeat_lost")

    async def test_heartbeat_lost_needs_two_consecutive_rounds(self):
        row, _ = self._running()
        self.clock.advance(D.HEARTBEAT_LOST_S + 1)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "heartbeat_lost")

    async def test_a_heartbeat_in_between_resets_the_count(self):
        row, _ = self._running()
        self.clock.advance(D.HEARTBEAT_LOST_S + 1)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.db.row(row["id"])["last_progress_at"] = self.clock.iso()
        await D.sweep_tick()
        self.clock.advance(D.HEARTBEAT_LOST_S + 1)
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso()
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")

    async def test_stalled(self):
        row, _ = self._running()
        self.db.row(row["id"])["step_advanced_at"] = self.clock.iso(self.clock.t - D.STALL_S - 1)
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "stalled")

    async def test_start_timeout_with_the_outage_clock(self):
        row = self.db.add_training(status="queued", current_attempt=1, worker_token="tok")
        self.db.add_attempt(row["id"], 1, state="starting", backend="runpod_community", cloud_ref="pod1",
                            started_at=self.clock.iso(self.clock.t - 1201), data_center="EU-SE-1")
        self.rp.add_pod(id="pod1", status="STARTING")
        D.STATE.last_failed_round_at = self.clock.t - 100  # FR-16: max(started_at, last_failed_round_at)
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "starting")
        D.STATE.last_failed_round_at = self.clock.t - 10_000
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "start_timeout")
        self.assertIn("EU-SE-1", D.STATE.dc_skip)
        self.assertIn("pod1", self.rp.deletes())

    async def test_a_stale_placing_attempt_is_released(self):
        row = self.db.add_training(status="queued", current_attempt=1, worker_token="tok")
        self.db.add_attempt(row["id"], 1, state="placing", created_at=self.clock.iso(self.clock.t - 601))
        D.STATE.last_failed_round_at = self.clock.t - 300
        await D.sweep_tick()
        self.assertEqual(self.db.row(row["id"])["current_attempt"], 1)
        D.STATE.last_failed_round_at = self.clock.t - 10_000
        await D.sweep_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["current_attempt"], r["worker_token"]), (0, None))

    async def test_modal_verdicts_survive_a_runpod_list_failure(self):
        row, _ = self._running("modal")
        self.rp.list_status = 500
        self.modal.status["fc-1"] = "FAILED"
        await D.sweep_tick()
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "modal_ended")
        self.assertIn("fc-1", self.modal.canceled)

    async def test_a_runpod_list_failure_judges_no_pod(self):
        row, _ = self._running()
        self.rp.list_status = 500
        for _ in range(3):
            await D.sweep_tick()
            self.clock.advance(31)
        self.assertEqual(self.db.attempt(row["id"], 1)["state"], "running")

    async def test_the_training_time_cap_has_the_watchdog_grace(self):
        row, a = self._running(train_seconds_used=14400 - 600)
        self.clock.advance(D.WATCHDOG_GRACE_S - 1)
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        await D.sweep_tick()
        self.assertEqual(self.db.row(row["id"])["status"], "running")
        self.clock.advance(2)
        self.db.row(row["id"]).update(last_progress_at=self.clock.iso(), step_advanced_at=self.clock.iso())
        await D.sweep_tick()
        self.assertEqual((self.db.row(row["id"])["status"], self.db.row(row["id"])["error_message"]),
                         ("failed", T.s5(14400)))
        self.assertIn("pod1", self.rp.deletes())

    async def test_a_final_ready_after_the_cap_publishes_instead_of_s5(self):
        row, a = self._running(train_seconds_used=14400)
        self.clock.advance(600 + D.WATCHDOG_GRACE_S)
        self.db.add_checkpoint(row["id"], 1, 50000, state="ready", manifest={})
        await D.sweep_tick()
        r = self.db.row(row["id"])
        self.assertEqual((r["status"], r["status_note"]), ("running", T.S3))
        self.assertEqual(self.published, [(row["id"], 1)])

    async def test_the_wall_cap(self):
        row, _ = self._running(requested_at=self.clock.iso(self.clock.t - D.WALL_CAP_S - D.WATCHDOG_GRACE_S - 1))
        await D.sweep_tick()
        self.assertEqual(self.db.row(row["id"])["error_message"], T.S6)

    async def test_a_loss_with_a_ready_final_publishes(self):
        row, _ = self._running()
        self.db.add_checkpoint(row["id"], 1, 50000, state="ready", manifest={})
        self.rp.pods.clear()
        await D.sweep_tick()
        self.assertEqual(self.published, [(row["id"], 1)])  # the ready-final rule fires first

    async def test_publishing_rows_recover(self):
        row = self.db.add_training(status="running", current_attempt=1)
        self.db.add_attempt(row["id"], 1, state="ended", end_reason="succeeded")
        with patch.object(hf_publish, "recover_publish") as rec:
            async def _r(*a):
                rec.called_with = a
                return "scheduled"
            rec.side_effect = _r
            await D.sweep_tick()
        self.assertTrue(rec.called)


class Janitor(Base):
    async def test_terminal_rows_lose_every_object_except_a_live_mirror(self):
        from app.services import r2_client

        row = self.db.add_training(status="succeeded", current_attempt=1, terminated_at=self.clock.iso(),
                                   log_url="https://hf/x")
        self.db.add_attempt(row["id"], 1, state="ended")
        for step, state in ((2500, "ready"), (5000, "refused"), (7500, "uploading")):
            self.db.add_checkpoint(row["id"], 1, step, state=state, manifest={"pretrained_model/config.json": {}})
            self.r2.objects[r2_client.ckpt_key(row["id"], 1, step, "pretrained_model/config.json")] = b"x"
        self.db.add_checkpoint(row["id"], 1, 10000, state="ready", manifest={"pretrained_model/config.json": {}},
                               mirror_claimed_at=self.clock.iso())
        self.r2.objects[r2_client.ckpt_key(row["id"], 1, 10000, "pretrained_model/config.json")] = b"x"
        self.r2.objects[r2_client.log_key(row["id"], 1)] = b"log"
        await D.janitor_tick(self.clock.t)
        states = {c["step"]: c["state"] for c in self.db.tables["training_checkpoints"]}
        self.assertEqual(states, {2500: "deleted", 5000: "deleted", 7500: "deleted", 10000: "ready"})
        self.assertNotIn(row["id"], D.STATE.janitor_done)  # the live claim keeps it open
        self.clock.advance(D.MIRROR_LEASE_S + 1)
        await D.janitor_tick(self.clock.t)
        self.assertIn(row["id"], D.STATE.janitor_done)
        self.assertEqual(self.r2.objects, {})

    async def test_a_failed_rows_log_waits_for_its_upload(self):
        from app.services import r2_client

        row = self.db.add_training(status="failed", current_attempt=1, terminated_at=self.clock.iso(), log_url=None)
        self.r2.objects[r2_client.log_key(row["id"], 1)] = b"log"
        scheduled = []
        with patch.object(hf_publish, "schedule_failed_log", lambda r: scheduled.append(r["id"]) or True):
            await D.janitor_tick(self.clock.t)
        self.assertEqual(scheduled, [row["id"]])
        self.assertIn(r2_client.log_key(row["id"], 1), self.r2.objects)
        self.clock.advance(D.PUBLISH_RETRY_S + 1)
        await D.janitor_tick(self.clock.t)
        self.assertNotIn(r2_client.log_key(row["id"], 1), self.r2.objects)

    async def test_retention_keeps_two_and_pending_mirrors(self):
        row = self.db.add_training(status="running", current_attempt=1)
        for step in (2500, 5000, 7500, 10000, 12500, 15000):
            self.db.add_checkpoint(row["id"], 1, step, state="ready", manifest={"a": {}})
        await asyncio.to_thread(D.apply_retention, row["id"], self.clock.t)
        states = {c["step"]: c["state"] for c in self.db.tables["training_checkpoints"]}
        self.assertEqual(states, {2500: "deleted", 5000: "deleted", 7500: "deleted", 10000: "ready",
                                  12500: "ready", 15000: "ready"})


class Reaper(Base):
    async def test_the_candidate_rules_and_foreign_pods(self):
        row = self.db.add_training(status="running", current_attempt=2)
        self.db.add_attempt(row["id"], 1, state="lost", backend="runpod_community", cloud_ref="old")
        self.db.add_attempt(row["id"], 2, state="running", backend="runpod_secure", cloud_ref="cur")
        pfx = self.s.pod_prefix
        self.rp.add_pod(id="cur", name=f"{pfx}-{row['id']}-a2")
        self.rp.add_pod(id="old", name="renamed-by-host")                       # (b) by DB id
        self.rp.add_pod(id="stray", name=f"{pfx}-999-a1")                       # (a) by name
        self.rp.add_pod(id="img", name="x", image="ghcr.io/svendanilborodun/edubotics-trainer:1@sha256:" + "b" * 64,
                        env={"EDUBOTICS_API_URL": "https://api.test"})           # (c) by image + env
        self.rp.add_pod(id="otherenv", name="y", image="ghcr.io/svendanilborodun/edubotics-trainer:1",
                        env={"EDUBOTICS_API_URL": "https://e2e.test"})          # another environment
        self.rp.add_pod(id="foreign", name="someone-elses-pod", image="runpod/pytorch")
        self.rp.add_pod(id="otherprefix", name=f"edubotics-e2e-train-{row['id']}-a2")
        out = await D.reaper_tick()
        self.assertEqual(sorted(self.rp.deletes()), ["img", "old", "stray"])
        self.assertEqual(out["kept"], 1)
        self.assertIn("foreign", self.rp.pods)
        self.assertIn("otherenv", self.rp.pods)
        self.assertIn("otherprefix", self.rp.pods)

    async def test_the_db_id_rule_sees_every_row_past_postgrests_max_rows(self):
        # V1-7: PostgREST answers at most max_rows rows; the reaper pages, so a
        # renamed pod (rule (b) alone) is found however many attempts precede it.
        self.db.max_rows = 2
        for i in range(5):
            row = self.db.add_training(status="failed", current_attempt=1)
            self.db.add_attempt(row["id"], 1, state="ended", backend="runpod_community", cloud_ref=f"old{i}")
            self.rp.add_pod(id=f"old{i}", name=f"renamed-{i}", createdAt=self.clock.iso())
        await D.reaper_tick()
        self.assertEqual(sorted(self.rp.deletes()), [f"old{i}" for i in range(5)])

    async def test_the_db_id_rule_reads_only_attempts_young_enough_to_own_a_pod(self):
        old = self.db.add_training(status="failed", current_attempt=1)
        window = self.s.cap_s + self.s.start_deadline_s + D.REAPER_GRACE_S + D.PLACING_STALE_S + D.REAPER_GRACE_S
        self.db.add_attempt(old["id"], 1, state="ended", backend="runpod_secure", cloud_ref="ancient",
                            created_at=self.clock.iso(self.clock.t - window - 1))
        young = self.db.add_training(status="failed", current_attempt=1)
        self.db.add_attempt(young["id"], 1, state="ended", backend="runpod_secure", cloud_ref="recent",
                            created_at=self.clock.iso(self.clock.t - window + 60))
        self.rp.add_pod(id="ancient", name="renamed-a")
        self.rp.add_pod(id="recent", name="renamed-r")
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), ["recent"])
        reads = [f for op, table, f, _ in self.db.ops if op == "select" and table == "training_attempts"
                 and any(k == "is" and c == "cloud_ref" for k, c, _v, _n in f)]
        self.assertTrue(reads)
        self.assertTrue(all(("gte", "created_at", self.clock.iso(self.clock.t - window), False) in f for f in reads))

    async def test_a_young_placing_pod_is_kept_by_its_name(self):
        row = self.db.add_training(status="queued", current_attempt=1)
        self.db.add_attempt(row["id"], 1, state="placing")
        self.rp.add_pod(id="p", name=f"{self.s.pod_prefix}-{row['id']}-a1", createdAt=self.clock.iso())
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), [])
        self.clock.advance(D.PLACING_STALE_S + 1)
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), ["p"])

    async def test_an_attached_pod_past_its_age_is_deleted(self):
        row = self.db.add_training(status="running", current_attempt=1)
        self.db.add_attempt(row["id"], 1, state="running", backend="runpod_community", cloud_ref="p")
        self.rp.add_pod(id="p", name="x", createdAt=self.clock.iso(self.clock.t - (14400 + 1200 + 1800) - 1))
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), ["p"])

    async def test_unlock_only_when_locked_or_400_409(self):
        self.rp.add_pod(id="L", name=f"{self.s.pod_prefix}-1-a1", locked=True)
        await D.reaper_tick()
        self.assertEqual(self.rp.patches(), [("L", {"locked": False})])
        self.assertNotIn("L", self.rp.pods)
        self.rp.calls.clear()
        self.rp.add_pod(id="M", name=f"{self.s.pod_prefix}-1-a2")
        self.rp.delete_answers["M"] = [409]
        await D.reaper_tick()
        self.assertEqual(self.rp.patches(), [("M", {"locked": False})])
        self.assertNotIn("M", self.rp.pods)
        self.rp.calls.clear()
        self.rp.add_pod(id="N", name=f"{self.s.pod_prefix}-1-a3")
        self.rp.delete_answers["N"] = [400]
        await D.reaper_tick()
        self.assertEqual(self.rp.patches(), [("N", {"locked": False})])
        self.assertNotIn("N", self.rp.pods)

    async def test_no_unlock_after_401_403_429_and_survivors_are_errors(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                D.reset_state_for_tests()
                self.rp.calls.clear()
                pid = f"S{status}"
                self.rp.add_pod(id=pid, name=f"{self.s.pod_prefix}-7-a{status}")
                self.rp.delete_answers[pid] = [status] * 10
                for _ in range(D.REAPER_SURVIVOR_ROUNDS + 1):
                    await D.reaper_tick()
                self.assertEqual(self.rp.patches(), [])
                self.assertIn(pid, D.STATE.delete_retry)
                with self.assertLogs("app.services.training_dispatch", level="ERROR") as logs:
                    await D.reaper_tick()
                self.assertTrue(any("survives" in m for m in logs.output))
                self.rp.pods.pop(pid)

    async def test_the_retry_set_is_a_candidate(self):
        D.STATE.delete_retry["r"] = 0
        self.rp.add_pod(id="r", name="renamed", image="other")
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), ["r"])
        self.assertNotIn("r", D.STATE.delete_retry)

    async def test_a_database_failure_deletes_nothing(self):
        self.rp.add_pod(id="s", name=f"{self.s.pod_prefix}-1-a1")
        self.db.failures["select:training_attempts"] = 1
        await D.reaper_tick()
        self.assertEqual(self.rp.deletes(), [])


class HfWriteCheck(Base):
    def test_org_repo_write(self):
        org = "EduBotics-Solutions"
        classic = lambda role, orgs: {"name": "bot", "auth": {"accessToken": {"role": role}}, "orgs": orgs}  # noqa: E731
        self.assertIs(D.org_repo_write(classic("write", [{"name": org, "roleInOrg": "write"}])), True)
        self.assertIs(D.org_repo_write(classic("god", [{"name": org.lower(), "roleInOrg": "admin"}])), True)
        self.assertIs(D.org_repo_write(classic("write", [{"name": org, "roleInOrg": "contributor"}])), True)
        self.assertIs(D.org_repo_write(classic("write", [{"name": org, "roleInOrg": "read"}])), False)
        self.assertIs(D.org_repo_write(classic("write", [{"name": "other", "roleInOrg": "admin"}])), False)
        self.assertIs(D.org_repo_write(classic("read", [{"name": org, "roleInOrg": "admin"}])), False)
        fg = lambda scoped: {"name": "bot", "auth": {"accessToken": {"role": "fineGrained",  # noqa: E731
                                                                     "fineGrained": {"scoped": scoped}}}}
        self.assertIs(D.org_repo_write(fg([{"entity": {"type": "org", "name": org}, "permissions": ["repo.write"]}])), True)
        self.assertIs(D.org_repo_write(fg([{"entity": {"type": "org", "name": org},
                                            "permissions": ["repo.content.write"]}])), True)
        self.assertIs(D.org_repo_write(fg([{"entity": {"type": "org", "name": org}, "permissions": ["repo.content.read"]}])), False)
        self.assertIs(D.org_repo_write(fg([{"entity": {"type": "user", "name": "bot"}, "permissions": ["repo.write"]}])), False)
        self.assertIsNone(D.org_repo_write(fg(["weird"])))
        self.assertIsNone(D.org_repo_write({"name": "bot", "auth": {"accessToken": {"role": "fineGrained"}}}))
        self.assertIsNone(D.org_repo_write({"name": "bot"}))

    async def test_the_outcomes_of_the_check(self):
        from app.services import hf_credentials

        info_ok = {"name": "bot", "auth": {"accessToken": {"role": "write"}},
                   "orgs": [{"name": "EduBotics-Solutions", "roleInOrg": "write"}]}
        cases = ((lambda t: info_ok, True, False),
                 (lambda t: dict(info_ok, orgs=[]), False, True),
                 (lambda t: (_ for _ in ()).throw(hf_credentials.HfCredentialError("rejected")), "rejected", True),
                 (lambda t: (_ for _ in ()).throw(hf_credentials.HfCredentialError("unreachable")), "unreachable", False),
                 (lambda t: (_ for _ in ()).throw(hf_credentials.HfCredentialError("bad_reply")), "bad_reply", False))
        for body, verdict, blocked in cases:
            with self.subTest(verdict=verdict), patch.object(D, "_hf_whoami_body", body):
                self.assertEqual(await D.hf_check_tick(self.s), verdict)
                self.assertEqual(D.placement_blocked(self.s), blocked)
        self.assertEqual(await D.hf_check_tick(settings(HF_TOKEN=None)), "no_token")
        self.assertTrue(D.placement_blocked(self.s))

    def test_a_hung_whoami_is_bounded(self):
        import httpx

        from app.services import hf_credentials

        release = threading.Event()

        class _Hung:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **k):
                release.wait(5)
                raise httpx.ConnectError("late")

        with patch.object(httpx, "Client", _Hung), patch.object(D, "HF_CHECK_TIMEOUT_S", 0.3):
            t0 = time.monotonic()
            with self.assertRaises(hf_credentials.HfCredentialError) as ctx:
                D._hf_whoami_body("hf_x")
            elapsed = time.monotonic() - t0
        release.set()
        self.assertEqual(ctx.exception.code, "unreachable")
        self.assertLess(elapsed, 2.0)
        self.assertEqual(D.HF_CHECK_TIMEOUT_S, 10)


class Supervisor(Base):
    async def test_legacy_with_a_leftover_managed_row_starts_the_loops(self):
        started = []
        s = settings(TRAINING_DISPATCH="legacy")
        self.db.add_training(status="running", managed=True)
        with patch.object(D, "SETTINGS", s), patch.object(D, "start_loops", lambda: started.append(1)):
            await asyncio.wait_for(D.managed_supervisor(), 2)
        self.assertEqual(started, [1])

    async def test_starting_the_loops_with_missing_variables_logs_an_error(self):
        s = settings(TRAINING_DISPATCH="legacy", R2_BUCKET=None)
        created = []
        with patch.object(D, "SETTINGS", s), patch.object(asyncio, "create_task", lambda c: created.append(c.close()) or _Done()):
            with self.assertLogs("app.services.training_dispatch", level="ERROR") as logs:
                self.assertTrue(D.start_loops())
        self.assertTrue(any("R2_BUCKET" in m for m in logs.output))
        self.assertEqual(len(created), 5)
        self.assertFalse(D.start_loops(), "once per process")

    async def test_legacy_without_managed_rows_waits(self):
        s = settings(TRAINING_DISPATCH="legacy")
        self.db.add_training(status="running", managed=False)
        with patch.object(D, "SETTINGS", s), patch.object(D, "start_loops", lambda: self.fail("started")):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(D.managed_supervisor(), 0.2)


class _Done:
    def add_done_callback(self, cb):
        pass


class Helpers(unittest.TestCase):
    def test_budget_for_subtracts_the_current_attempts_own_time(self):
        now = 1_800_000_000.0
        row = {"train_seconds_used": 3600, "requested_at": Clock(now - 7200).iso()}
        att = {"first_heartbeat_at": Clock(now - 1800).iso()}
        s = settings()
        self.assertEqual(D.budget_for(row, att, now, s), (14400 - 3600 - 1800, "cap"))
        self.assertEqual(D.budget_for(row, {"first_heartbeat_at": None}, now, s), (14400 - 3600, "cap"))
        old = {"train_seconds_used": 0, "requested_at": Clock(now - 40000).iso()}
        self.assertEqual(D.budget_for(old, {}, now, s), (43200 - 40000, "wall"))

    def test_s1_formats_the_step(self):
        self.assertEqual(T.s1(12500), T.S1.format(step="12.500"))
        self.assertEqual(T.loss_note(0), T.S2)
        self.assertEqual(T.format_step(50000), "50.000")

    def test_resume_step_ignores_refused_and_the_final(self):
        ck = [{"step": 2500, "state": "ready"}, {"step": 5000, "state": "refused"}, {"step": 7500, "state": "deleted"},
              {"step": 50000, "state": "ready"}]
        self.assertEqual(D.resume_step_from(ck, 50000), 2500)


if __name__ == "__main__":
    unittest.main()
