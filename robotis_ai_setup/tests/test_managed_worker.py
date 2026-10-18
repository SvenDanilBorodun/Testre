"""Managed-mode worker (spec §3.2, §4.7): run_managed against a fake API, a fake R2, a fake RPC
and a fake `lerobot_train` that writes LeRobot-shaped checkpoints in a real subprocess.

One test per row of §3.2's "Worker reactions" table (FR-2), plus the host check, the budget,
the E12 exit table, the resume staging and its refusals, FR-13/16/18, the lock hash, the
headers of every PUT, the legacy S16 path, entry.py's self-stop switch and the fresh-command
contract. Deps-free like the other training_handler tests (huggingface_hub and supabase are
stubbed); httpx is the CI python-tests job's pinned 0.28.1.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


def _ensure_stubs() -> None:
    if "huggingface_hub" not in sys.modules:
        m = types.ModuleType("huggingface_hub")

        class _HfApiStub:
            def __init__(self, *a, **kw):
                pass

        m.HfApi = _HfApiStub
        m.hf_hub_download = lambda *a, **kw: None
        m.login = lambda *a, **kw: None
        utils = types.ModuleType("huggingface_hub.utils")
        for name in ("HfHubHTTPError", "RepositoryNotFoundError", "RevisionNotFoundError"):
            setattr(utils, name, type(name, (Exception,), {}))
        sys.modules["huggingface_hub"] = m
        sys.modules["huggingface_hub.utils"] = utils
    if "supabase" not in sys.modules:
        m = types.ModuleType("supabase")
        m.create_client = lambda *a, **kw: None
        sys.modules["supabase"] = m


_ensure_stubs()

_HERE = Path(__file__).resolve().parent
_SETUP = _HERE.parent
for _p in (_SETUP / "modal_training", _SETUP / "runpod_training"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

import training_handler as th  # noqa: E402

MODEL = "EduBotics-Solutions/m"
DATASET = "EduBotics-Solutions/ds"
REVISION = "b1c3ecbae7f244acc039a3dbc255a00dad1372b9"
TOKEN = "11111111-2222-3333-4444-555555555555"
P0001 = Exception("{'code': 'P0001', 'message': 'Invalid worker token, training not found, or training already terminal'}")


# ---------------------------------------------------------------- the fake LeRobot
# These two functions are written VERBATIM into the fake lerobot_train script, so the test
# and the subprocess build the same train_config and the same checkpoint layout.
def make_cfg(opts):
    resume = opts.get("resume") == "true"
    return {
        "dataset": {"repo_id": opts.get("dataset.repo_id"), "revision": opts.get("dataset.revision")},
        "output_dir": opts["output_dir"], "steps": int(opts["steps"]), "save_freq": int(opts["save_freq"]),
        "seed": int(opts.get("seed", 1000)), "batch_size": int(opts.get("batch_size", 8)),
        "num_workers": int(opts.get("num_workers", 0)), "resume": resume, "checkpoint_path": None,
        "policy": {"type": "act", "device": opts.get("policy.device"), "pretrained_path": None},
    }


def write_ckpt(out, step, steps, cfg, resumed_from=None):
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    sid = f"{step:0{max(6, len(str(steps)))}d}"
    d = _Path(out) / "checkpoints" / sid
    pm, ts = d / "pretrained_model", d / "training_state"
    pm.mkdir(parents=True, exist_ok=True)
    ts.mkdir(parents=True, exist_ok=True)
    cfg = _json.loads(_json.dumps(cfg))
    if resumed_from is not None:
        cfg["resume"] = True
        cfg["checkpoint_path"] = f"{out}/checkpoints/{resumed_from}"
        cfg["policy"]["pretrained_path"] = f"{out}/checkpoints/{resumed_from}/pretrained_model"
    pre = {"name": "policy_preprocessor", "steps": [
        {"registry_name": "rename_observations_processor", "config": {"rename_map": {}}},
        {"registry_name": "to_batch_processor", "config": {}},
        {"registry_name": "device_processor", "config": {"device": "cpu"}},
        {"registry_name": "normalizer_processor", "config": {},
         "state_file": "policy_preprocessor_step_3_normalizer_processor.safetensors"}]}
    post = {"name": "policy_postprocessor", "steps": [
        {"registry_name": "unnormalizer_processor", "config": {},
         "state_file": "policy_postprocessor_step_0_unnormalizer_processor.safetensors"},
        {"registry_name": "device_processor", "config": {"device": "cpu"}}]}
    files = {
        pm / "config.json": _json.dumps(cfg["policy"]),
        pm / "model.safetensors": "M" * (1000 + step),
        pm / "train_config.json": _json.dumps(cfg, indent=4),
        pm / "policy_preprocessor.json": _json.dumps(pre),
        pm / "policy_preprocessor_step_3_normalizer_processor.safetensors": "N" * 64,
        pm / "policy_postprocessor.json": _json.dumps(post),
        pm / "policy_postprocessor_step_0_unnormalizer_processor.safetensors": "U" * 64,
        ts / "optimizer_param_groups.json": "[]",
        ts / "optimizer_state.safetensors": "O" * (2000 + step),
        ts / "rng_state.safetensors": "R" * 32,
        ts / "training_step.json": _json.dumps({"step": step}, indent=4),
    }
    for p, text in files.items():
        p.write_text(text)
    tmp = _Path(out) / "checkpoints" / ".last.tmp"
    if tmp.is_symlink():
        tmp.unlink()
    _os.symlink(sid, tmp)
    _os.replace(tmp, _Path(out) / "checkpoints" / "last")


FAKE_MAIN = '''
import json, os, sys, time
from pathlib import Path
opts = {}
for a in sys.argv[1:]:
    if a.startswith("--") and "=" in a:
        k, v = a[2:].split("=", 1)
        opts[k] = v
mode = json.loads(os.environ.get("FAKE_LR") or "{}")
rec = os.environ.get("FAKE_LR_RECORD")
if rec:
    with open(rec, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "env_has_token": "EDUBOTICS_WORKER_TOKEN" in os.environ}) + "\\n")
resumed_from = None
if opts.get("resume") == "true":
    cfg_path = Path(opts["config_path"])
    cfg = json.loads(cfg_path.read_text())
    out, steps, sf = cfg["output_dir"], int(cfg["steps"]), int(cfg["save_freq"])
    start = json.loads((cfg_path.parent.parent / "training_state" / "training_step.json").read_text())["step"]
    resumed_from = cfg_path.parent.parent.name
    cfg["num_workers"] = int(opts.get("num_workers", 0))
else:
    cfg = make_cfg(opts)
    out, steps, sf, start = opts["output_dir"], cfg["steps"], cfg["save_freq"], 0
print("INFO cfg.steps=%d (%d)" % (steps, steps), flush=True)
crash_at = mode.get("crash_at")
for step in range(start + 1, steps + 1):
    if crash_at is not None and step >= start + crash_at:
        sys.exit(int(mode.get("rc", 1)))
    time.sleep(float(mode.get("delay", 0.02)))
    print("INFO 2026-10-18 ot_train.py:451 step:%d smpl:%d ep:0 epch:0.07 loss:%.4f grdn:1.0" % (step, 8 * step, 1.0 / step), flush=True)
    if step % sf == 0 or step == steps:
        write_ckpt(out, step, steps, cfg, resumed_from)
sys.exit(int(mode.get("exit_rc", 0)))
'''


def parse_opts(args):
    opts = {}
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opts[k] = v
    return opts


def fake_regenerate(fresh_args):
    return make_cfg(parse_opts(fresh_args))


# ---------------------------------------------------------------- the fake API + R2
class World:
    """MockTransport handler: the worker API at https://api.test, R2 at https://r2.test."""

    def __init__(self, steps=6, save_freq=2, mirror_every=4, budget_s=3600, resume=None):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.puts: list[dict] = []
        self.gets: list[str] = []
        self.objects: dict[str, bytes] = {}
        self.script: dict[str, list] = {}
        self.put_script: list = []
        self.get_script: list = []
        self.steps, self.save_freq, self.mirror_every = steps, save_freq, mirror_every
        self.budget_s = budget_s
        self.resume = resume
        self.ready: set = set()
        self.auth_headers: list = []

    # --- scripting ---
    def on(self, action, *responses):
        self.script.setdefault(action, []).extend(responses)

    def spec(self):
        return {"dataset_name": DATASET, "model_name": MODEL, "model_type": "act",
                "training_params": {"seed": 1000, "batch_size": 8, "steps": self.steps, "log_freq": 1,
                                    "save_freq": self.save_freq, "mirror_every": self.mirror_every,
                                    "dataset_revision": REVISION},
                "total_steps": self.steps, "save_freq": self.save_freq, "mirror_every": self.mirror_every,
                "dataset_revision": REVISION, "output_dir": str(th._output_path(MODEL))}

    def start_answer(self):
        return {"spec": self.spec(), "budget_s": self.budget_s, "finished": False, "resume": self.resume}

    def default(self, action, body):
        if action == "start":
            return 200, self.start_answer()
        if action == "upload-url":
            prefix = "log" if body.get("kind") == "log" else f"ckpt/{body.get('step')}"
            urls, headers = {}, {}
            for rel, meta in body["files"].items():
                urls[rel] = f"https://r2.test/{prefix}/{rel}?X-Amz-Signature=sig"
                headers[rel] = {"Content-Type": "application/octet-stream",
                                "Content-Length": str(meta["size"]), "Content-MD5": meta["md5"]}
            return 200, {"urls": urls, "expires_in": 900, "headers": headers}
        if action == "ckpt-done":
            self.ready.add(body["step"])
            return 200, {"state": "ready"}
        if action == "ckpt-bad":
            return 200, {"state": "refused"}
        if action == "finish":
            return 200, {"status": "publishing" if body.get("outcome") == "succeeded" else
                         ("failed" if body.get("outcome") == "failed" else "requeued")}
        return 404, {"detail": "unknown", "code": "unknown"}

    def __call__(self, request: httpx.Request):
        host = request.url.host
        if host == "api.test":
            action = request.url.path.rsplit("/", 1)[1]
            body = json.loads(request.content or b"{}")
            with self.lock:
                self.calls.append((action, body))
                self.auth_headers.append(request.headers.get("authorization"))
                queue = self.script.get(action)
                responder = queue.pop(0) if queue else None
            if responder is None:
                status, payload = self.default(action, body)
                return httpx.Response(status, json=payload)
            if responder == "network":
                raise httpx.ConnectError("boom", request=request)
            if callable(responder):
                status, payload, *hdrs = responder(body)
            else:
                status, payload, *hdrs = responder
            if status == 200 and payload is None:
                status, payload = self.default(action, body)
            return httpx.Response(status, json=payload, headers=hdrs[0] if hdrs else None)
        if host == "r2.test":
            if request.method == "PUT":
                data = request.read()
                with self.lock:
                    resp = self.put_script.pop(0) if self.put_script else None
                    self.puts.append({"path": request.url.path, "headers": dict(request.headers),
                                      "body": data, "answer": resp})
                if resp == "network":
                    raise httpx.ReadTimeout("slow", request=request)
                if resp is not None:
                    return httpx.Response(resp)
                self.objects[request.url.path] = data
                return httpx.Response(200)
            if request.method == "GET":
                with self.lock:
                    self.gets.append(request.url.path)
                    resp = self.get_script.pop(0) if self.get_script else None
                if resp == "network":
                    raise httpx.ConnectError("reset", request=request)
                if resp is not None:
                    return httpx.Response(resp)
                data = self.objects.get(request.url.path)
                return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        return httpx.Response(599)

    # --- views ---
    def actions(self):
        return [a for a, _ in self.calls]

    def bodies(self, action):
        return [b for a, b in self.calls if a == action]

    def ckpt_asks(self):
        return [b["step"] for a, b in self.calls if a == "upload-url" and b.get("kind") == "ckpt"]

    def finish(self):
        f = self.bodies("finish")
        return f[-1] if f else None


class Rpc:
    def __init__(self):
        self.calls: list[dict] = []
        self.fail = None          # fail(kw, n) -> exception or None

    def __call__(self, supabase_url, supabase_anon_key, worker_token, training_id, **kw):
        n = len(self.calls)
        self.calls.append(kw)
        if self.fail is not None:
            exc = self.fail(kw, n)
            if exc is not None:
                raise exc


class ManagedTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="managed-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        lock = self.tmp / "training-lock.txt"
        lock.write_text("# test lock\nlerobot==0.5.1\n")
        self.lock_sha = hashlib.sha256(lock.read_bytes()).hexdigest()
        self.fake = self.tmp / "fake_lerobot_train.py"
        self.fake.write_text(inspect.getsource(make_cfg) + "\n" + textwrap.dedent(inspect.getsource(write_ckpt))
                             + FAKE_MAIN)
        self.record = self.tmp / "argv.jsonl"
        self.world = World()
        self.rpc = Rpc()
        self.lr_mode: dict = {}
        orig_spawn = th._spawn_lerobot

        def spawn(cmd, env):
            self.assertEqual(cmd[:3], [sys.executable, "-m", "lerobot.scripts.lerobot_train"])
            env = {**env, "FAKE_LR": json.dumps(self.lr_mode), "FAKE_LR_RECORD": str(self.record)}
            return orig_spawn([sys.executable, str(self.fake)] + cmd[3:], env)

        fast_sleep = lambda s: time.sleep(min(float(s), 0.01))  # noqa: E731
        patches = {
            "OUTPUT_DIR": self.tmp / "out",
            "LOCK_CANDIDATES": (lock,),
            "_HTTP_TRANSPORT": httpx.MockTransport(lambda r: self.world(r)),
            "_call_progress_rpc": self.rpc,
            "_spawn_lerobot": spawn,
            "_assert_training_host": lambda: None,
            "_regenerate_train_config": fake_regenerate,
            "_sleep": fast_sleep,
            "HEARTBEAT_INTERVAL_S": 0.05,
            "UPLOAD_TICK_S": 0.05,
            "START_RETRY_DELAYS_S": (0.01, 0.01),
            "START_RETRY_EVERY_S": 0.01,
            "START_RETRY_GIVEUP_S": 0.4,
            "CKPT_BAD_RETRY_DELAYS_S": (0.01, 0.01, 0.01),
            "FINAL_UPLOAD_WAIT_S": 8,
        }
        for name, value in patches.items():
            p = mock.patch.object(th, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"EDUBOTICS_BUILD": "test-build"})
        env.start()
        self.addCleanup(env.stop)

    def run_managed(self, cpu_budget=4, on_budget=None):
        return th.run_managed("https://api.test", "http://sb.test", "anon", 7, 2, TOKEN, cpu_budget,
                              on_budget=on_budget)

    def lerobot_argv(self):
        if not self.record.exists():
            return []
        return [json.loads(l) for l in self.record.read_text().splitlines()]

    def source_checkpoint(self, step, steps=None, cfg_opts=None):
        """A checkpoint the 'previous attempt' uploaded, served from R2 for a resume."""
        steps = steps or self.world.steps
        n = th._num_workers(4)
        opts = cfg_opts or parse_opts(th._fresh_args(self.world.spec(), n)[3:])
        src = self.tmp / "prev"
        write_ckpt(src, step, steps, make_cfg(opts))
        sid = th.step_id(step, steps)
        files = {}
        for rel in th.CHECKPOINT_RELPATHS:
            data = (src / "checkpoints" / sid / rel).read_bytes()
            path = f"/prev/{sid}/{rel}"
            self.world.objects[path] = data
            files[rel] = {"url": f"https://r2.test{path}", "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()}
        return {"step": step, "step_id": sid, "attempt_no": 1, "files": files}


# ======================================================================= happy paths
class HappyPath(ManagedTestBase):
    def test_fresh_run_call_order_and_final_publish(self):
        result = self.run_managed()
        self.assertEqual(result["outcome"], "succeeded")
        acts = self.world.actions()
        self.assertEqual(acts[0], "start")
        self.assertEqual(acts[-1], "finish")
        self.assertEqual(self.world.finish(), {"outcome": "succeeded"})
        # The first RPC is the process's one 'running' write at the resume step (0).
        self.assertEqual(self.rpc.calls[0], {"status": "running", "current_step": 0, "total_steps": 6})
        self.assertTrue(all(c.get("status") is None for c in self.rpc.calls[1:]),
                        "a heartbeat must never send status 'running' (it would reset step_advanced_at)")
        self.assertTrue(all("error_message" not in c and "log_url" not in c for c in self.rpc.calls))
        self.assertIn(6, self.world.ready)
        # The final upload names exactly the 7 pretrained_model files; earlier ones all 11.
        ckpt_bodies = [b for b in self.world.bodies("upload-url") if b["kind"] == "ckpt"]
        by_step = {b["step"]: b for b in ckpt_bodies}
        self.assertEqual(sorted(by_step[6]["files"]), sorted(th.FINAL_CHECKPOINT_RELPATHS))
        self.assertIn(4, by_step, "a multiple of mirror_every must be uploaded")
        self.assertEqual(sorted(by_step[4]["files"]), sorted(th.CHECKPOINT_RELPATHS))
        # The log goes up before finish.
        log_idx = next(i for i, (a, b) in enumerate(self.world.calls) if a == "upload-url" and b["kind"] == "log")
        self.assertLess(log_idx, len(self.world.calls) - 1)

    def test_lock_hash_and_build_in_start(self):
        self.run_managed()
        start = self.world.bodies("start")[0]
        self.assertEqual(start, {"lock_sha256": self.lock_sha, "build": "test-build"})

    def test_every_api_call_carries_the_bearer_token_and_no_put_does(self):
        self.run_managed()
        self.assertTrue(all(h == f"Bearer {TOKEN}" for h in self.world.auth_headers))
        self.assertTrue(self.world.puts)
        for put in self.world.puts:
            self.assertNotIn("authorization", {k.lower() for k in put["headers"]})

    def test_md5_and_returned_headers_on_every_put(self):
        self.run_managed()
        for put in self.world.puts:
            body = put["body"]
            h = {k.lower(): v for k, v in put["headers"].items()}
            self.assertEqual(h["content-type"], "application/octet-stream")
            self.assertEqual(h["content-length"], str(len(body)))
            self.assertEqual(h["content-md5"], base64.b64encode(hashlib.md5(body).digest()).decode())
            self.assertNotIn("transfer-encoding", h)
        for b in self.world.bodies("upload-url"):
            for rel, meta in b["files"].items():
                self.assertRegex(meta["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(len(base64.b64decode(meta["md5"])), 16)

    def test_lerobot_gets_the_fresh_command_and_no_worker_credentials(self):
        self.run_managed(cpu_budget=4)
        runs = self.lerobot_argv()
        self.assertEqual(len(runs), 1)
        argv = runs[0]["argv"]
        self.assertIn(f"--dataset.revision={REVISION}", argv)
        self.assertEqual(argv[-1], "--num_workers=3")
        self.assertFalse(runs[0]["env_has_token"])

    def test_on_budget_receives_the_start_budget(self):
        got = []
        self.world.budget_s = 1234
        self.run_managed(on_budget=got.append)
        self.assertEqual(got, [1234])

    def test_uploads_never_skip_a_mirror_multiple_and_old_checkpoints_are_pruned(self):
        # Checkpoints every step; only mirror multiples (4, 8) and the final (10) are must-uploads.
        self.world.steps, self.world.save_freq, self.world.mirror_every = 10, 1, 4
        self.lr_mode = {"delay": 0.003}
        with mock.patch.object(th, "UPLOAD_TICK_S", 0.4):
            self.run_managed()
        done = {b["step"] for b in self.world.bodies("ckpt-done")}
        self.assertTrue({4, 8, 10} <= done, done)
        present = sorted(p.name for p in (th._output_path(MODEL) / "checkpoints").iterdir()
                         if p.name.isdigit())
        self.assertEqual(present, ["000010"], "older checkpoints are pruned, the newest is kept")


# ======================================================================= §3.2 reaction table, row by row
class ReactionTable(ManagedTestBase):
    # --- any action / 401 ---
    def test_any_action_401_stops_start(self):
        self.world.on("start", (401, {"detail": "x", "code": "unauthorized"}))
        result = self.run_managed()
        self.assertEqual(result, {"outcome": "stopped"})
        self.assertEqual(self.world.actions(), ["start"])
        self.assertEqual(self.rpc.calls, [])

    def test_any_action_401_stops_the_uploader_and_kills_lerobot(self):
        self.lr_mode = {"delay": 0.2}
        self.world.steps = 20
        self.world.on("upload-url", (401, {"code": "unauthorized"}))
        t0 = time.monotonic()
        result = self.run_managed()
        self.assertEqual(result, {"outcome": "stopped"})
        self.assertLess(time.monotonic() - t0, 3.5, "LeRobot must be killed, not run to its end")
        self.assertNotIn("finish", self.world.actions())

    # --- start ---
    def test_start_409_budget_exhausted_stops(self):
        self.world.on("start", (409, {"code": "budget_exhausted"}))
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertEqual(self.world.actions(), ["start"])

    def test_start_409_wrong_image_stops(self):
        self.world.on("start", (409, {"code": "wrong_image"}))
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertEqual(self.world.actions(), ["start"])
        self.assertEqual(self.rpc.calls, [])

    def test_start_5xx_and_network_are_retried(self):
        self.world.on("start", (503, {}), "network", (500, {}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertEqual(self.world.actions()[:4], ["start"] * 4)

    def test_start_5xx_forever_gives_up_and_stops(self):
        self.world.on("start", *([(503, {})] * 1000))
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertNotIn("finish", self.world.actions())

    def test_start_unlisted_status_is_handled_as_5xx(self):
        self.world.on("start", (418, {}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")

    def test_429_waits_and_repeats_the_same_call(self):
        waits = []
        self.world.on("start", (429, {}, {"Retry-After": "7"}))
        with mock.patch.object(th, "START_RETRY_GIVEUP_S", 600), \
                mock.patch.object(th, "_sleep", side_effect=lambda s: (waits.append(s), time.sleep(0.001))):
            self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertEqual(waits[0], 7.0)
        self.assertEqual(self.world.actions()[:2], ["start", "start"])

    def test_429_retry_after_is_capped_at_60(self):
        self.assertEqual(th._retry_after_s("900"), 60.0)
        self.assertEqual(th._retry_after_s(None), 5.0)

    # --- upload-url ---
    def test_upload_url_200_puts_with_exactly_the_returned_headers(self):
        def custom(body):
            status, payload = self.world.default("upload-url", body)
            for rel in payload["headers"]:
                payload["headers"][rel]["X-Test-Echo"] = "yes"
            return 200, payload
        self.world.on("upload-url", custom)
        self.run_managed()
        self.assertEqual(self.world.puts[0]["headers"].get("x-test-echo"), "yes")

    def test_upload_url_409_already_ready_marks_uploaded(self):
        self.world.mirror_every = 2
        self.world.on("upload-url", (409, {"code": "already_ready"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first_step = self.world.ckpt_asks()[0]
        self.assertNotIn(first_step, {b["step"] for b in self.world.bodies("ckpt-done")})
        self.assertEqual(self.world.ckpt_asks().count(first_step), 1)

    def test_upload_url_409_already_refused_skips_the_step(self):
        self.world.mirror_every = 2
        self.world.on("upload-url", (409, {"code": "already_refused"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first_step = self.world.ckpt_asks()[0]
        self.assertNotIn(first_step, {b["step"] for b in self.world.bodies("ckpt-done")})
        self.assertEqual(self.world.ckpt_asks().count(first_step), 1)

    def test_upload_url_422_skips_the_step(self):
        self.world.mirror_every = 2          # every checkpoint is a must-upload
        self.world.on("upload-url", (422, {"code": "bad_manifest"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first_step = self.world.ckpt_asks()[0]
        self.assertEqual(self.world.ckpt_asks().count(first_step), 1, "a 422 step is never asked again")
        self.assertNotIn(first_step, self.world.ready)

    def test_upload_url_5xx_and_network_retry_on_the_next_tick(self):
        self.world.mirror_every = 2          # every checkpoint is a must-upload
        self.world.on("upload-url", (502, {}), "network")
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first_step = self.world.ckpt_asks()[0]
        self.assertGreaterEqual(self.world.ckpt_asks().count(first_step), 3)
        self.assertIn(first_step, self.world.ready)

    # --- PUT ---
    def test_put_403_asks_upload_url_again(self):
        self.world.mirror_every = 2
        self.world.put_script = [403]
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first = self.world.ckpt_asks()[0]
        self.assertEqual(self.world.ckpt_asks()[:2], [first, first])
        self.assertIn(first, self.world.ready)

    def test_put_403_at_most_three_reasks_per_tick(self):
        self.world.mirror_every = 2
        self.world.put_script = [403] * 4
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first = self.world.ckpt_asks()[0]
        self.assertEqual(self.world.ckpt_asks()[:4], [first] * 4, "1 ask + 3 re-asks in the first tick")
        # The 4th 403 ends the tick: the 5th PUT (the next tick's) succeeds and the step is ready.
        self.assertIn(first, self.world.ready)

    def test_put_5xx_and_network_three_tries_per_file(self):
        self.world.mirror_every = 2
        self.world.put_script = [500, "network"]
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first_path = self.world.puts[0]["path"]
        self.assertEqual([p["path"] for p in self.world.puts[:3]], [first_path] * 3)

    def test_put_failing_three_times_waits_for_the_next_tick(self):
        self.world.mirror_every = 2
        self.world.put_script = [500, 500, 500]
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first = self.world.ckpt_asks()[0]
        self.assertGreaterEqual(self.world.ckpt_asks().count(first), 2, "asked again on the next tick")
        self.assertIn(first, self.world.ready)

    # --- ckpt-done ---
    def test_ckpt_done_200_marks_uploaded(self):
        self.run_managed()
        self.assertEqual(sorted(self.world.ready), sorted({b["step"] for b in self.world.bodies("ckpt-done")}))

    def test_ckpt_done_409_not_uploading_skips(self):
        self.world.mirror_every = 2
        self.world.on("ckpt-done", (409, {"code": "not_uploading"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first = self.world.bodies("ckpt-done")[0]["step"]
        self.assertEqual([b["step"] for b in self.world.bodies("ckpt-done")].count(first), 1)

    def test_ckpt_done_422_skips_and_training_continues(self):
        self.world.on("ckpt-done", (422, {"code": "ckpt_refused"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")

    def test_ckpt_done_422_then_p0001_on_the_next_heartbeat_stops(self):
        self.world.steps = 30
        self.lr_mode = {"delay": 0.1}
        refused = threading.Event()

        def refuse(body):
            refused.set()
            return 422, {"code": "ckpt_refused"}
        self.world.on("ckpt-done", refuse)
        self.rpc.fail = lambda kw, n: P0001 if refused.is_set() else None
        result = self.run_managed()
        self.assertEqual(result, {"outcome": "stopped"})
        self.assertNotIn("finish", self.world.actions())

    def test_ckpt_done_5xx_calls_ckpt_done_again_without_reputting(self):
        self.world.mirror_every = 2
        failed = []

        def fail_first_step_twice(body):
            if len(failed) < 2 and (not failed or body["step"] == failed[0]):
                failed.append(body["step"])
                return 503, {}
            return 200, None
        self.world.on("ckpt-done", *([fail_first_step_twice] * 100))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        first = failed[0]
        self.assertEqual([b["step"] for b in self.world.bodies("ckpt-done")].count(first), 3)
        self.assertEqual(self.world.ckpt_asks().count(first), 1, "ckpt-done is repeated, the PUTs are not")
        self.assertIn(first, self.world.ready)

    # --- finish ---
    def test_finish_200_returns(self):
        self.assertEqual(self.run_managed()["api"], "publishing")

    def test_finish_409_no_final_checkpoint_stops_without_retry(self):
        self.world.on("finish", (409, {"code": "no_final_checkpoint"}))
        self.assertEqual(self.run_managed()["api"], th._STOP)
        self.assertEqual(self.world.actions().count("finish"), 1)

    def test_finish_5xx_is_retried(self):
        self.world.on("finish", (500, {}), "network")
        self.assertEqual(self.run_managed()["api"], "publishing")
        self.assertEqual(self.world.actions().count("finish"), 3)

    def test_finish_5xx_forever_gives_up(self):
        self.world.on("finish", *([(500, {})] * 1000))
        self.assertEqual(self.run_managed()["api"], th._STOP)

    # --- RPC ---
    def test_rpc_p0001_stops_and_kills_lerobot(self):
        self.world.steps = 30
        self.lr_mode = {"delay": 0.1}
        self.rpc.fail = lambda kw, n: P0001 if n >= 2 else None
        t0 = time.monotonic()
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertNotIn("finish", self.world.actions())

    def test_first_running_write_transport_error_is_retried(self):
        self.rpc.fail = lambda kw, n: ConnectionError("down") if n < 3 else None
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertEqual([c.get("status") for c in self.rpc.calls[:4]], ["running"] * 4)

    def test_first_running_write_gives_up_after_the_start_class_window(self):
        self.rpc.fail = lambda kw, n: ConnectionError("down")
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertEqual(self.lerobot_argv(), [])

    def test_first_running_write_p0001_stops(self):
        self.rpc.fail = lambda kw, n: P0001
        self.assertEqual(self.run_managed(), {"outcome": "stopped"})
        self.assertEqual(len(self.rpc.calls), 1)

    def test_later_heartbeat_errors_never_stop(self):
        self.lr_mode = {"delay": 0.05}
        self.rpc.fail = lambda kw, n: ConnectionError("blip") if n >= 1 else None
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertGreater(len(self.rpc.calls), 2)


# ======================================================================= ckpt-bad rows + resume staging
class ResumeAndCkptBad(ManagedTestBase):
    def setUp(self):
        super().setUp()
        self.world.steps, self.world.save_freq, self.world.mirror_every = 8, 2, 4

    def test_resume_happy_path(self):
        self.world.resume = self.source_checkpoint(4)
        result = self.run_managed()
        self.assertEqual(result["outcome"], "succeeded")
        argv = self.lerobot_argv()[0]["argv"]
        ckpt = th._output_path(MODEL) / "checkpoints" / "000004"
        self.assertEqual(argv, ["--resume=true",
                                f"--config_path={ckpt}/pretrained_model/train_config.json",
                                "--num_workers=3"])
        self.assertEqual(self.rpc.calls[0]["status"], "running")
        self.assertEqual(self.rpc.calls[0]["current_step"], 4)
        uploaded = {b["step"] for b in self.world.bodies("upload-url") if b["kind"] == "ckpt"}
        self.assertTrue(uploaded and min(uploaded) > 4, "never re-upload the step it resumed from")

    def test_resume_overwrites_train_config_with_the_regenerated_one(self):
        self.world.resume = self.source_checkpoint(4)
        seen = {}
        orig = th._compare_and_overwrite_train_config

        def spy(stage, ref):
            orig(stage, ref)
            seen["text"] = (stage / "pretrained_model" / "train_config.json").read_text()
            seen["ref"] = ref
        with mock.patch.object(th, "_compare_and_overwrite_train_config", spy):
            self.run_managed()
        self.assertEqual(seen["text"], json.dumps(seen["ref"], indent=4))

    def _bad(self, mutate, reason, verdicts=("kept",)):
        self.world.resume = self.source_checkpoint(4)
        mutate(self.world.resume)
        for v in verdicts:
            self.world.on("ckpt-bad", (200, {"state": v}))
        result = self.run_managed()
        self.assertEqual(self.world.bodies("ckpt-bad")[0], {"step": 4, "reason": reason})
        return result

    def _rewrite(self, resume, rel, text):
        data = text.encode()
        path = resume["files"][rel]["url"].replace("https://r2.test", "")
        self.world.objects[path] = data
        resume["files"][rel].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def test_sha256_mismatch_is_ckpt_bad_sha256(self):
        def m(resume):
            path = resume["files"]["pretrained_model/model.safetensors"]["url"].replace("https://r2.test", "")
            data = bytearray(self.world.objects[path])
            data[0] ^= 1
            self.world.objects[path] = bytes(data)
        r = self._bad(m, "sha256")
        self.assertEqual(self.world.finish()["outcome"], "host_unfit")
        self.assertEqual(self.world.finish()["reason"], "download")
        self.assertEqual(r["reason"], "download")

    def test_too_long_body_is_ckpt_bad_sha256(self):
        def m(resume):
            path = resume["files"]["training_state/rng_state.safetensors"]["url"].replace("https://r2.test", "")
            self.world.objects[path] += b"extra"
        self._bad(m, "sha256")

    def test_processor_class_is_ckpt_bad_steps(self):
        def m(resume):
            rel = "pretrained_model/policy_preprocessor.json"
            path = resume["files"][rel]["url"].replace("https://r2.test", "")
            doc = json.loads(self.world.objects[path])
            doc["steps"][0]["class"] = "os.system"
            self._rewrite(resume, rel, json.dumps(doc))
        self._bad(m, "steps")

    def test_wrong_state_file_is_ckpt_bad_steps(self):
        def m(resume):
            rel = "pretrained_model/policy_postprocessor.json"
            path = resume["files"][rel]["url"].replace("https://r2.test", "")
            doc = json.loads(self.world.objects[path])
            doc["steps"][0]["state_file"] = "../evil.safetensors"
            self._rewrite(resume, rel, json.dumps(doc))
        self._bad(m, "steps")

    def test_training_step_mismatch_is_ckpt_bad_training_step(self):
        self._bad(lambda r: self._rewrite(r, "training_state/training_step.json", '{"step": 5}'),
                  "training_step")

    def test_train_config_mismatch_is_ckpt_bad_train_config(self):
        def m(resume):
            rel = "pretrained_model/train_config.json"
            path = resume["files"][rel]["url"].replace("https://r2.test", "")
            doc = json.loads(self.world.objects[path])
            doc["policy"]["type"] = "groot"
            self._rewrite(resume, rel, json.dumps(doc))
        self._bad(m, "train_config")

    def test_train_config_ignores_exactly_the_four_resume_keys(self):
        def m(resume):
            rel = "pretrained_model/train_config.json"
            path = resume["files"][rel]["url"].replace("https://r2.test", "")
            doc = json.loads(self.world.objects[path])
            doc.update(resume=True, num_workers=9, checkpoint_path="/x")
            doc["policy"]["pretrained_path"] = "/x/pretrained_model"
            self._rewrite(resume, rel, json.dumps(doc))
        self.world.resume = self.source_checkpoint(4)
        m(self.world.resume)
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertNotIn("ckpt-bad", self.world.actions())

    def test_ckpt_bad_refused_starts_again_fresh(self):
        def m(resume):
            self._rewrite(resume, "training_state/training_step.json", '{"step": 99}')
        self.world.resume = self.source_checkpoint(4)
        m(self.world.resume)
        answers = [self.world.start_answer(), {**self.world.start_answer(), "resume": None}]
        self.world.on("start", *[(200, a) for a in answers])
        self.world.on("ckpt-bad", (200, {"state": "refused"}))
        self.assertEqual(self.run_managed()["outcome"], "succeeded")
        self.assertEqual(self.world.actions()[:3], ["start", "ckpt-bad", "start"])
        self.assertFalse(any(p.name.startswith(".stage-") for p in th.OUTPUT_DIR.iterdir()),
                         "the stage is deleted after a refusal")
        self.assertEqual(len([c for c in self.rpc.calls if c.get("status") == "running"]), 1,
                         "one first running write per process")

    def test_ckpt_bad_refused_START_LOOP_MAX_times_finishes_host_unfit_download(self):
        self.world.resume = self.source_checkpoint(4)
        self._rewrite(self.world.resume, "training_state/training_step.json", '{"step": 99}')
        self.world.on("ckpt-bad", *([(200, {"state": "refused"})] * 10))
        result = self.run_managed()
        self.assertEqual(self.world.actions().count("start"), th.START_LOOP_MAX)
        self.assertEqual(self.world.finish()["reason"], "download")
        self.assertEqual(result["reason"], "download")

    def test_ckpt_bad_kept_finishes_host_unfit_download(self):
        self._bad(lambda r: self._rewrite(r, "training_state/training_step.json", '{"step": 1}'),
                  "training_step", verdicts=("kept",))
        self.assertEqual(self.world.finish()["outcome"], "host_unfit")
        self.assertEqual(self.world.finish()["reason"], "download")

    def test_ckpt_bad_ignored_finishes_host_unfit_download(self):
        self._bad(lambda r: self._rewrite(r, "training_state/training_step.json", '{"step": 1}'),
                  "training_step", verdicts=("ignored",))
        self.assertEqual(self.world.finish()["reason"], "download")

    def test_ckpt_bad_5xx_three_retries_then_host_unfit_download(self):
        self.world.resume = self.source_checkpoint(4)
        self._rewrite(self.world.resume, "training_state/training_step.json", '{"step": 1}')
        self.world.on("ckpt-bad", (500, {}), "network", (502, {}), (503, {}))
        self.run_managed()
        self.assertEqual(self.world.actions().count("ckpt-bad"), 4, "the call, then 3 retries (5, 15, 45 s)")
        self.assertEqual(self.world.finish()["reason"], "download")

    def test_ckpt_bad_sha256_uses_the_long_read_timeout(self):
        seen = []
        orig = th._ManagedRun.api

        def spy(self_, action, body, **kw):
            if action == "ckpt-bad":
                seen.append(kw.get("read_timeout"))
            return orig(self_, action, body, **kw)

        def m(resume):
            path = resume["files"]["pretrained_model/model.safetensors"]["url"].replace("https://r2.test", "")
            self.world.objects[path] = b"X" * len(self.world.objects[path])
        with mock.patch.object(th._ManagedRun, "api", spy):
            self._bad(m, "sha256")
        self.assertEqual(seen, [th.CKPT_BAD_SHA256_READ_TIMEOUT_S])

    def test_download_failure_is_host_unfit_download_without_ckpt_bad(self):
        self.world.resume = self.source_checkpoint(4)
        self.world.get_script = [500, "network", 503]
        result = self.run_managed()
        self.assertNotIn("ckpt-bad", self.world.actions())
        self.assertEqual(self.world.finish()["outcome"], "host_unfit")
        self.assertEqual(self.world.finish()["reason"], "download")
        self.assertEqual(result["reason"], "download")

    def test_a_transient_download_error_is_retried(self):
        self.world.resume = self.source_checkpoint(4)
        self.world.get_script = ["network"]
        self.assertEqual(self.run_managed()["outcome"], "succeeded")

    def test_exception_before_lerobot_is_host_unfit_download_without_a_crash_count(self):
        self.world.resume = self.source_checkpoint(4)
        with mock.patch.object(th, "_regenerate_train_config", side_effect=OSError("hf down")):
            result = self.run_managed()
        self.assertNotIn("ckpt-bad", self.world.actions())
        self.assertEqual(self.world.finish()["reason"], "download")
        self.assertEqual(result["reason"], "download")
        self.assertEqual(self.lerobot_argv(), [])

    def test_exception_on_a_fresh_start_before_lerobot_is_host_unfit_download(self):
        with mock.patch.object(th, "_build_training_command", side_effect=OSError("disk full")):
            self.run_managed()
        self.assertEqual(self.world.finish(), {"outcome": "host_unfit", "reason": "download",
                                               "detail": "OSError"})

    def test_a_link_in_the_stage_is_file_list(self):
        stage = self.tmp / "stage"
        src = self.tmp / "src"
        write_ckpt(src, 2, 8, make_cfg(parse_opts(th._fresh_args(self.world.spec(), 3)[3:])))
        shutil.copytree(src / "checkpoints" / "000002", stage)
        (stage / "training_state" / "rng_state.safetensors").unlink()
        os.symlink("/etc/passwd", stage / "training_state" / "rng_state.safetensors")
        with self.assertRaises(th._CkptBad) as cm:
            th._validate_resume_dir(stage, 2)
        self.assertEqual(cm.exception.reason, "file_list")

    def test_an_extra_file_in_the_stage_is_file_list(self):
        stage = self.tmp / "stage"
        src = self.tmp / "src"
        write_ckpt(src, 2, 8, make_cfg(parse_opts(th._fresh_args(self.world.spec(), 3)[3:])))
        shutil.copytree(src / "checkpoints" / "000002", stage)
        (stage / "pretrained_model" / "extra.py").write_text("x")
        with self.assertRaises(th._CkptBad) as cm:
            th._validate_resume_dir(stage, 2)
        self.assertEqual(cm.exception.reason, "file_list")


# ======================================================================= host check, budget, E12
class HostCheck(ManagedTestBase):
    def test_every_host_check_reason_reaches_finish(self):
        for reason in ("no_cuda", "gpu_capability", "gpu_memory", "matmul", "disk"):
            with self.subTest(reason=reason):
                self.world.calls.clear()

                def unfit(reason=reason):
                    raise th.HostUnfit(reason, "measured")
                with mock.patch.object(th, "_assert_training_host", unfit):
                    result = self.run_managed()
                self.assertEqual(self.world.actions(), ["finish"])
                self.assertEqual(self.world.finish(), {"outcome": "host_unfit", "reason": reason,
                                                       "detail": "measured"})
                self.assertEqual(result["reason"], reason)


class _FakeCuda:
    def __init__(self, available=True, cap=(8, 6), memory=24 * 10 ** 9, finite=True, raise_matmul=False):
        self.available, self.cap, self.memory, self.finite, self.raise_matmul = \
            available, cap, memory, finite, raise_matmul

    def module(self):
        cuda = types.SimpleNamespace(
            is_available=lambda: self.available,
            get_device_capability=lambda i: self.cap,
            get_device_properties=lambda i: types.SimpleNamespace(name="Fake GPU", total_memory=self.memory),
            synchronize=lambda i=None: None,
        )
        finite = self.finite
        raise_matmul = self.raise_matmul

        class T:
            def __matmul__(self, other):
                if raise_matmul:
                    raise RuntimeError("CUDA error")
                return T()

            def all(self):
                return types.SimpleNamespace(item=lambda: finite)

        return types.SimpleNamespace(cuda=cuda, float32="f32", randn=lambda *a, **k: T(),
                                     isfinite=lambda t: T())


class AssertTrainingHost(unittest.TestCase):
    def check(self, fake, free=100 * 1024 ** 3):
        with mock.patch.dict(sys.modules, {"torch": fake.module()}), \
                mock.patch.object(th.shutil, "disk_usage", return_value=types.SimpleNamespace(free=free)):
            th._assert_training_host()

    def reason(self, fake, **kw):
        with self.assertRaises(th.HostUnfit) as cm:
            self.check(fake, **kw)
        return cm.exception.reason

    def test_a_good_host_passes(self):
        self.check(_FakeCuda())
        self.check(_FakeCuda(cap=(8, 0)))
        self.check(_FakeCuda(cap=(9, 0)))

    def test_no_cuda(self):
        self.assertEqual(self.reason(_FakeCuda(available=False)), "no_cuda")

    def test_capability_outside_8_0_to_9_0(self):
        self.assertEqual(self.reason(_FakeCuda(cap=(7, 5))), "gpu_capability")
        self.assertEqual(self.reason(_FakeCuda(cap=(12, 0))), "gpu_capability")

    def test_memory_below_9_5_gb(self):
        self.assertEqual(self.reason(_FakeCuda(memory=8 * 1024 ** 3)), "gpu_memory")

    def test_matmul_non_finite_or_failing(self):
        self.assertEqual(self.reason(_FakeCuda(finite=False)), "matmul")
        self.assertEqual(self.reason(_FakeCuda(raise_matmul=True)), "matmul")

    def test_disk_below_30_gib(self):
        self.assertEqual(self.reason(_FakeCuda(), free=29 * 1024 ** 3), "disk")


class BudgetAndExit(ManagedTestBase):
    def test_budget_kills_lerobot_and_finishes_failed_time_cap(self):
        self.world.steps = 200
        self.world.budget_s = 1
        self.lr_mode = {"delay": 0.1}
        t0 = time.monotonic()
        result = self.run_managed()
        self.assertLess(time.monotonic() - t0, 6)
        self.assertEqual(self.world.finish(), {"outcome": "failed", "reason": "time_cap"})
        self.assertEqual(result["reason"], "time_cap")
        log_idx = [i for i, (a, b) in enumerate(self.world.calls) if a == "upload-url" and b["kind"] == "log"]
        finish_idx = self.world.actions().index("finish")
        self.assertTrue(log_idx and log_idx[0] < finish_idx, "the log goes up before finish")

    def test_crash_after_progress_is_lerobot_crash(self):
        self.lr_mode = {"crash_at": 3}
        self.run_managed()
        self.assertEqual(self.world.finish(), {"outcome": "host_unfit", "reason": "lerobot_crash",
                                               "detail": "returncode=1"})

    def test_crash_before_a_step_on_a_fresh_start_is_lerobot_error(self):
        self.lr_mode = {"crash_at": 1}
        self.run_managed()
        self.assertEqual(self.world.finish()["outcome"], "failed")
        self.assertEqual(self.world.finish()["reason"], "lerobot_error")

    def test_crash_before_a_new_step_on_a_resume_is_resume_crash(self):
        self.world.steps, self.world.save_freq, self.world.mirror_every = 8, 2, 4
        self.world.resume = self.source_checkpoint(4)
        self.lr_mode = {"crash_at": 1}
        self.run_managed()
        self.assertEqual(self.world.finish()["outcome"], "host_unfit")
        self.assertEqual(self.world.finish()["reason"], "resume_crash")

    def test_crash_after_new_steps_on_a_resume_is_lerobot_crash(self):
        self.world.steps, self.world.save_freq, self.world.mirror_every = 8, 2, 4
        self.world.resume = self.source_checkpoint(4)
        self.lr_mode = {"crash_at": 3}
        self.run_managed()
        self.assertEqual(self.world.finish()["reason"], "lerobot_crash")

    def test_the_final_upload_wait_is_bounded(self):
        self.world.steps, self.world.save_freq, self.world.mirror_every = 4, 2, 2

        def final_never(body):
            if body.get("kind") == "ckpt" and body.get("step") == 4:
                return 503, {}
            return 200, None
        self.world.on("upload-url", *([final_never] * 10000))
        self.world.on("finish", (409, {"code": "no_final_checkpoint"}))
        with mock.patch.object(th, "FINAL_UPLOAD_WAIT_S", 1.0):
            t0 = time.monotonic()
            self.run_managed()
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5)
        self.assertEqual(self.world.finish(), {"outcome": "succeeded"})

    def test_heartbeat_runs_while_waiting_for_the_final_upload(self):
        self.world.steps, self.world.save_freq, self.world.mirror_every = 4, 2, 2
        gate = threading.Event()

        def slow_final(body):
            if body.get("kind") == "ckpt" and body.get("step") == 4 and not gate.is_set():
                return 503, {}
            return 200, None
        self.world.on("upload-url", *([slow_final] * 10000))
        threading.Timer(1.0, gate.set).start()
        before = None

        def count():
            nonlocal before
            before = len(self.rpc.calls)
        threading.Timer(0.6, count).start()
        self.run_managed()
        self.assertGreater(len(self.rpc.calls), before)


# ======================================================================= the commands, constants, signals
class Commands(ManagedTestBase):
    def test_fresh_command_equals_legacy_builder_plus_revision_and_num_workers(self):
        spec = self.world.spec()
        legacy = th._build_training_command(DATASET, "act", MODEL, spec["training_params"])
        self.assertEqual(th._fresh_args(spec, 5), legacy + [f"--dataset.revision={REVISION}", "--num_workers=5"])
        unpinned = {**spec, "dataset_revision": None}
        self.assertEqual(th._fresh_args(unpinned, 5), legacy + ["--num_workers=5"])
        self.assertIn("--policy.device=cuda", legacy)
        self.assertIn("--save_freq=2", legacy)

    def test_num_workers_rule(self):
        self.assertEqual(th._num_workers(1), 1)
        self.assertEqual(th._num_workers(2), 1)
        self.assertEqual(th._num_workers(8), 7)
        self.assertEqual(th._num_workers(64), th.NUM_WORKERS_MAX)

    def test_step_id_rule(self):
        self.assertEqual(th.step_id(2500, 50000), "002500")
        self.assertEqual(th.step_id(2500, 1234567), "0002500")
        self.assertEqual(th.step_id(20, 20), "000020")

    def test_checkpoint_relpaths(self):
        self.assertEqual(len(th.CHECKPOINT_RELPATHS), 11)
        self.assertEqual(len(th.FINAL_CHECKPOINT_RELPATHS), 7)
        self.assertTrue(all(r.startswith("pretrained_model/") for r in th.FINAL_CHECKPOINT_RELPATHS))
        self.assertEqual(th.ACT_PRE_STEPS, ("rename_observations_processor", "to_batch_processor",
                                            "device_processor", "normalizer_processor"))
        self.assertEqual(th.ACT_POST_STEPS, ("unnormalizer_processor", "device_processor"))

    def test_no_save_or_mirror_cadence_of_its_own(self):
        names = {n.upper() for n in vars(th)}
        self.assertFalse({"SAVE_FREQ", "MIRROR_EVERY"} & names)

    def test_sigterm_kills_a_managed_lerobot_without_a_db_write(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        with mock.patch.object(th, "_managed_proc", proc), mock.patch.object(th, "_current_job", None), \
                mock.patch.object(th, "_update_supabase_status") as write:
            with self.assertRaises(SystemExit):
                th._on_shutdown(15, None)
        self.assertIsNotNone(proc.poll())
        write.assert_not_called()


class LegacyHostCheck(unittest.TestCase):
    def test_legacy_host_check_failure_writes_S16(self):
        env = {"SUPABASE_URL": "http://sb.test", "SUPABASE_ANON_KEY": "anon"}
        writes = []
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(th, "_preflight_dataset", return_value={"fps": 30}), \
                mock.patch.object(th, "_assert_training_host", side_effect=th.HostUnfit("no_cuda", "x")), \
                mock.patch.object(th, "_update_status_with_retry",
                                  side_effect=lambda *a, **k: writes.append(a) or True), \
                mock.patch.object(th, "_update_supabase_status") as running:
            result = th.run_training("u/d", "u/m", "pi0", {}, 5, TOKEN)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(writes[0][4:6], ("failed", th.LEGACY_HOST_UNFIT_DE))
        running.assert_not_called()
        self.assertEqual(th.LEGACY_HOST_UNFIT_DE,
                         "Der Cloud-GPU-Server ist nicht nutzbar (GPU oder Speicherplatz fehlt). "
                         "Das Training wurde nicht gestartet – das Trainingsguthaben wurde erstattet.")


class EntrySelfStop(unittest.TestCase):
    """entry.py: after run_managed, sleep, best-effort self-stop, then EXIT_AFTER_FAILED_SELF_STOP."""

    def setUp(self):
        import entry
        self.entry = entry
        self.env = {"EDUBOTICS_TRAINING_ID": "7", "EDUBOTICS_ATTEMPT": "2", "EDUBOTICS_WORKER_TOKEN": TOKEN,
                    "EDUBOTICS_API_URL": "https://api.test", "SUPABASE_URL": "http://sb.test",
                    "SUPABASE_ANON_KEY": "anon", "RUNPOD_POD_ID": "pod1", "RUNPOD_API_KEY": "k",
                    "RUNPOD_CPU_COUNT": "6"}

    def _main(self, exit_flag, stop_ok):
        sleeps = []

        class Forever(Exception):
            pass

        def fake_sleep(s):
            sleeps.append(s)
            if s == 3600:
                raise Forever()
        got = {}

        def fake_run(*a, **kw):
            got.update(args=a, kw=kw)
            return {"outcome": "succeeded"}
        with mock.patch.object(self.entry, "EXIT_AFTER_FAILED_SELF_STOP", exit_flag), \
                mock.patch.object(self.entry, "_sleep", fake_sleep), \
                mock.patch.object(self.entry, "self_stop", return_value=stop_ok) as stop, \
                mock.patch.object(th, "run_managed", fake_run), \
                mock.patch.object(self.entry.Watchdog, "start", lambda w: w):
            try:
                rc = self.entry.main(self.env)
            except Forever:
                rc = "sleeps forever"
        return rc, sleeps, stop, got

    def test_false_sleeps_forever_after_a_failed_self_stop(self):
        rc, sleeps, stop, got = self._main(False, False)
        self.assertEqual(rc, "sleeps forever")
        self.assertEqual(sleeps[0], self.entry.POST_RUN_SLEEP_S)
        stop.assert_called_once()
        self.assertEqual(got["kw"]["cpu_budget"], 6)
        self.assertEqual(got["args"], ("https://api.test", "http://sb.test", "anon", 7, 2, TOKEN))

    def test_true_exits_0_after_a_failed_self_stop(self):
        rc, _, stop, _ = self._main(True, False)
        self.assertEqual(rc, 0)
        stop.assert_called_once()

    def test_a_successful_self_stop_waits_for_runpod(self):
        rc, _, _, _ = self._main(True, True)
        self.assertEqual(rc, "sleeps forever")

    def test_self_stop_posts_the_stop_action_and_never_prints_the_key(self):
        import io
        import urllib.request
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        def fake_open(req, timeout):
            seen.append((req.full_url, req.get_method(), req.data, req.headers.get("Authorization")))
            return Resp()
        out = io.StringIO()
        with mock.patch.object(urllib.request, "urlopen", fake_open), mock.patch("sys.stdout", out):
            ok = self.entry.self_stop({**self.env, "RUNPOD_API_BASE": "http://fake:8765"})
        self.assertTrue(ok)
        self.assertEqual(seen[0][:3], ("http://fake:8765/v2/pods/pod1/action", "POST", b'{"action": "stop"}'))
        self.assertEqual(seen[0][3], "Bearer k")
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn("Bearer", out.getvalue())

    def test_watchdog_rearm_uses_budget_plus_grace(self):
        fired = []
        w = self.entry.Watchdog(10_000, on_fire=lambda: fired.append(1))
        w.rearm(100)
        self.assertAlmostEqual(w.deadline() - time.monotonic(), 100 + self.entry.WATCHDOG_GRACE_S, delta=2)

    def test_watchdog_fires_at_its_deadline(self):
        fired = threading.Event()
        self.entry.Watchdog(0.05, on_fire=fired.set).start()
        self.assertTrue(fired.wait(2))


if __name__ == "__main__":
    unittest.main()
