"""services/inference_sessions.py — the Cloud-Inferenz control plane's pure half.

No FastAPI, no Modal: settings and their clamps, the connect token, the
edge-token envelope (AES-256-GCM under an HKDF sub-key of
EDUBOTICS_HF_TOKEN_KEY, D32), the training visibility ladder, the model
resolver against the Hugging Face Hub, and the thin RPC wrappers with their
error mapping. `cryptography` must be importable (CI installs it for 042);
`httpx` is faked per test.

Fixtures are deliberately synthetic and low-entropy (gitleaks scans history).
"""

from __future__ import annotations

import ast
import base64
import hashlib
import logging
import os
import re
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import hf_credentials as hc  # noqa: E402
from app.services import inference_sessions as svc  # noqa: E402
from app.tests.test_inference_migration_sql import END_REASONS, MIGRATION, end_reason_check  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SID = "0a1b2c3d-0000-4000-8000-00000000abcd"
OTHER_SID = "0a1b2c3d-0000-4000-8000-00000000abce"
EDGE = "edge-" + "x" * 40  # a synthetic edge token: printable, 16..4096 characters
SEQ_A = bytes(range(32))
SEQ_B = bytes(range(0x20, 0x40))


class _Keys:
    def __init__(self, current=SEQ_A, previous=None):
        self.values = {hc.KEY_ENV: base64.b64encode(current).decode()} if current else {}
        if previous is not None:
            self.values[hc.PREVIOUS_KEY_ENV] = base64.b64encode(previous).decode()

    def __enter__(self):
        self._p = patch.dict(os.environ, self.values)
        self._p.start()
        if hc.KEY_ENV not in self.values:
            os.environ.pop(hc.KEY_ENV, None)
        if hc.PREVIOUS_KEY_ENV not in self.values:
            os.environ.pop(hc.PREVIOUS_KEY_ENV, None)
        hc._reset_for_tests()
        return self

    def __exit__(self, *exc):
        self._p.stop()
        hc._reset_for_tests()


# ------------------------------------------------------------------- settings


class TestSettings(unittest.TestCase):
    def test_session_max_clamped_to_3300(self) -> None:
        # R-18: a 35-minute HTTPS run on one session is unproved; never above 3300 s.
        env = {"INFERENCE_SESSION_MAX_S": "7200"}
        self.assertEqual(svc._env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, env), 3300)
        self.assertEqual(svc._env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, {}), 1800)
        self.assertEqual(svc._env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, {"INFERENCE_SESSION_MAX_S": "5"}), 60)
        self.assertEqual(svc._env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, {"INFERENCE_SESSION_MAX_S": "x"}), 1800)
        self.assertEqual(svc._env_int("INFERENCE_SESSION_MAX_S", 1800, 60, 3300, {"INFERENCE_SESSION_MAX_S": " "}), 1800)
        self.assertEqual(svc._settings(env)["SESSION_MAX_S"], 3300)
        self.assertEqual(svc._settings({"INFERENCE_SESSION_MAX_S": "3301"})["SESSION_MAX_S"], 3300)
        self.assertEqual(svc._settings({"INFERENCE_SESSION_MAX_S": "3000"})["SESSION_MAX_S"], 3000)

    def test_the_defaults(self) -> None:
        self.assertEqual(
            (svc.SESSION_MAX_S, svc.DAILY_CAP_S, svc.GLOBAL_CAP, svc.RESERVE_S, svc.EDGE_IDLE_S),
            (1800, 7200, 60, 300, 600),
        )
        self.assertEqual(svc._settings({}), {
            "SESSION_MAX_S": 1800, "DAILY_CAP_S": 7200, "GLOBAL_CAP": 60, "RESERVE_S": 300, "EDGE_IDLE_S": 600,
        })
        self.assertEqual(svc._settings({"INFERENCE_GLOBAL_SESSION_CAP": "0", "INFERENCE_RESERVE_S": "5"}),
                         {"SESSION_MAX_S": 1800, "DAILY_CAP_S": 7200, "GLOBAL_CAP": 1, "RESERVE_S": 30,
                          "EDGE_IDLE_S": 600})

    def test_edge_idle_never_below_reserve_plus_120(self) -> None:
        # D34: a reservation must outlive its window on Modal's side.
        self.assertEqual(
            svc._settings({"INFERENCE_RESERVE_S": "900", "INFERENCE_EDGE_IDLE_S": "120"})["EDGE_IDLE_S"], 1020)
        self.assertEqual(svc._settings({"INFERENCE_RESERVE_S": "300"})["EDGE_IDLE_S"], 600)
        self.assertEqual(svc._settings({"INFERENCE_EDGE_IDLE_S": "5000"})["EDGE_IDLE_S"], 5000)
        self.assertEqual(svc._settings({"INFERENCE_EDGE_IDLE_S": "99999"})["EDGE_IDLE_S"], 7200)
        self.assertGreaterEqual(svc.EDGE_IDLE_S, svc.RESERVE_S + 120)


# ---------------------------------------------------------------- the tokens


class TestTokens(unittest.TestCase):
    def test_new_token_is_43_urlsafe_characters_and_its_sha256(self) -> None:
        seen = set()
        for _ in range(50):
            token, digest = svc.new_token()
            self.assertRegex(token, r"\A[A-Za-z0-9_-]{43}\Z")
            self.assertEqual(digest, hashlib.sha256(token.encode("utf-8")).hexdigest())
            seen.add(token)
        self.assertEqual(len(seen), 50)

    def test_the_regexes_equal_the_sql_checks(self) -> None:
        with open(MIGRATION, encoding="utf-8") as fh:
            sql = fh.read()
        self.assertIn(f"modal_session_id ~ '^{svc.MODAL_SESSION_ID_RE.pattern}$'", sql)
        self.assertIn(f"model_repo ~ '^{svc.MODEL_REPO_RE.pattern}$'", sql)
        self.assertEqual(svc.EDGE_TOKEN_RE.pattern, r"[\x21-\x7e]{16,4096}")


class TestEdgeEnvelope(unittest.TestCase):
    def test_edge_envelope_round_trip_rotation_and_domain_separation(self) -> None:
        with _Keys(current=SEQ_A):
            env = svc.edge_seal(SID, EDGE)
            self.assertRegex(env, r"\Ae1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{24,}\Z")
            self.assertLessEqual(len(env), 6000)
            self.assertNotIn(EDGE, env)
            self.assertNotIn(base64.urlsafe_b64encode(EDGE.encode()).decode()[:20], env)
            self.assertEqual(svc.edge_open(SID, env), EDGE)
            self.assertEqual(svc.edge_open(SID.upper(), env), EDGE)  # the AAD is the lower-case id
            self.assertIsNone(svc.edge_open(OTHER_SID, env))
            # a fresh nonce each time
            self.assertNotEqual(env, svc.edge_seal(SID, EDGE))
            # domain separation: the same bytes as an HF-token envelope never open
            swapped = "v1." + env[len("e1."):]
            with self.assertRaises(hc.HfCredentialError) as ctx:
                hc.decrypt(SID, swapped)
            self.assertEqual(ctx.exception.code, "undecryptable")
            # an HF-token envelope is no edge envelope either
            hf_env = hc.encrypt(SID, "hf_" + "a" * 34)
            self.assertIsNone(svc.edge_open(SID, "e1." + hf_env[len("v1."):]))
        # rotation: the old key moved to _PREVIOUS still opens it
        with _Keys(current=SEQ_B, previous=SEQ_A):
            self.assertEqual(svc.edge_open(SID, env), EDGE)
            self.assertNotEqual(svc.edge_seal(SID, EDGE).split(".")[1], env.split(".")[1])
        # the previous key gone: None, never an exception
        with _Keys(current=SEQ_B):
            self.assertIsNone(svc.edge_open(SID, env))
        with _Keys(current=None):
            self.assertIsNone(svc.edge_open(SID, env))

    def test_garbage_envelopes_are_none(self) -> None:
        with _Keys():
            good = svc.edge_seal(SID, EDGE)
            tampered = good[:-2] + ("A" if good[-2] != "A" else "B") + good[-1]
            for bad in (None, 5, b"e1.x", "", "e1", "e1..", "v1.00000000.AAAA", good + "=", tampered,
                        "e1.zzzzzzzz." + good.split(".")[2], "e1." + good.split(".")[1] + ".!!!!",
                        "e1." + good.split(".")[1] + ".AAAA", good.replace("e1.", "e2.", 1)):
                self.assertIsNone(svc.edge_open(SID, bad), repr(bad)[:40])
            for bad_sid in (None, 5, ""):
                self.assertIsNone(svc.edge_open(bad_sid, good))

    def test_seal_refuses_without_a_key(self) -> None:
        with _Keys(current=None):
            with self.assertRaises(hc.HfCredentialError):
                svc.edge_seal(SID, EDGE)

    def test_cryptography_is_imported_inside_the_edge_functions(self) -> None:
        # G-5: `from app.routes import inference` must import in CI jobs that do
        # not install cryptography (modal-import-validate).
        path = os.path.join(os.path.dirname(HERE), "services", "inference_sessions.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                self.assertFalse(name.startswith("cryptography"), name)
                self.assertNotIn(name, ("httpx", "fastapi", "modal"))
        inner = {
            fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
            for n in ast.walk(fn)
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("cryptography")
        }
        self.assertTrue({"edge_seal", "edge_open"} <= inner, inner)


# ------------------------------------------------------- visibility + summary


class _TrainingsDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def table(self, name):
        outer = self
        outer.calls.append(("table", name))

        class _Q:
            def __init__(self):
                self.filters = []

            def select(self, cols, *a, **k):
                outer.calls.append(("select", cols))
                return self

            def eq(self, col, val):
                outer.calls.append(("eq", col, val))
                self.filters.append((col, val))
                return self

            def execute(self):
                return SimpleNamespace(data=[r for r in outer.rows if all(r.get(c) == v for c, v in self.filters)])

        return _Q()


class TestVisibleTraining(unittest.TestCase):
    ROWS = [
        {"id": 1, "user_id": "u-me", "workgroup_id": None, "status": "succeeded"},
        {"id": 2, "user_id": "u-sib", "workgroup_id": "wg-a", "status": "succeeded"},
        {"id": 3, "user_id": "u-other", "workgroup_id": "wg-b", "status": "succeeded"},
        {"id": 4, "user_id": "u-other", "workgroup_id": None, "status": "succeeded"},
    ]

    def _visible(self, training_id, groups=("wg-a",)):
        db = _TrainingsDB(self.ROWS)
        with patch("app.services.supabase_client.get_supabase", return_value=db), \
                patch("app.services.workgroups.resolve_visible_workgroup_ids", return_value=list(groups)):
            return svc.visible_training({"id": "u-me", "role": "student"}, training_id), db

    def test_own_and_group_rows_are_visible(self) -> None:
        self.assertEqual(self._visible(1)[0]["id"], 1)
        self.assertEqual(self._visible(2)[0]["id"], 2)

    def test_other_rows_and_unknown_ids_are_none(self) -> None:
        self.assertIsNone(self._visible(3)[0])
        self.assertIsNone(self._visible(4)[0])
        self.assertIsNone(self._visible(99)[0])
        self.assertIsNone(self._visible(2, groups=())[0])

    def test_the_query_is_by_id_only(self) -> None:
        _, db = self._visible(2)
        self.assertEqual([c for c in db.calls if c[0] == "eq"], [("eq", "id", 2)])
        self.assertIn(("table", "trainings"), db.calls)


CONFIG = {
    "type": "act", "n_obs_steps": 1,
    "input_features": {
        "observation.images.gripper": {"type": "VISUAL", "shape": [3, 480, 640]},
        "observation.images.scene": {"type": "VISUAL", "shape": [3, 480, 640]},
        "observation.state": {"type": "STATE", "shape": [6]},
    },
    "output_features": {"action": {"type": "ACTION", "shape": [6]}},
    "use_peft": False, "chunk_size": 100, "n_action_steps": 15, "temporal_ensemble_coeff": None,
}
META = {"dataset_repo_id": "u/d", "dataset_fps": 30, "policy_type": "act"}


def _cfg(**over):
    c = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CONFIG.items()}
    c["input_features"] = {k: dict(v) for k, v in CONFIG["input_features"].items()}
    c.update(over)
    return c


class TestSummaryFromConfig(unittest.TestCase):
    def test_the_summary_of_a_real_edubotics_act(self) -> None:
        self.assertEqual(svc.summary_from_config(CONFIG, META), {
            "type": "act",
            "expected_cameras": ["gripper", "scene"],
            "image_shapes": {"gripper": [3, 480, 640], "scene": [3, 480, 640]},
            "trained_fps": 30,
            "chunk_size": 100,
            "n_action_steps": 15,
            "state_dim": 6,
            "action_dim": 6,
            "requires_task": False,
        })

    def test_trained_fps_follows_read_policy_summary(self) -> None:
        for meta, want in ((None, None), ({}, None), ({"dataset_fps": True}, None), ({"dataset_fps": "30"}, None),
                           ({"dataset_fps": 0}, None), ({"dataset_fps": -1}, None), ({"dataset_fps": 29.97}, 29.97),
                           ({"dataset_fps": float("nan")}, None), ([1], None)):
            self.assertEqual(svc.summary_from_config(CONFIG, meta)["trained_fps"], want, meta)

    def test_not_plain_act_is_not_act(self) -> None:
        for bad in (_cfg(type="diffusion"), _cfg(type=None), _cfg(temporal_ensemble_coeff=0.01),
                    _cfg(n_obs_steps=2), _cfg(use_peft=True)):
            with self.assertRaises(svc.ModelResolveError) as ctx:
                svc.summary_from_config(bad, META)
            self.assertEqual(ctx.exception.code, "not_act")

    def test_a_broken_config_is_invalid(self) -> None:
        no_state = _cfg()
        del no_state["input_features"]["observation.state"]
        bad_shape = _cfg()
        bad_shape["input_features"]["observation.images.scene"] = {"type": "VISUAL", "shape": [480, 640, 3]}
        bad_name = _cfg()
        bad_name["input_features"]["observation.images.a b"] = {"type": "VISUAL", "shape": [3, 4, 4]}
        no_cam = _cfg()
        no_cam["input_features"] = {"observation.state": {"type": "STATE", "shape": [6]}}
        five = _cfg()
        for i in range(5):
            five["input_features"][f"observation.images.c{i}"] = {"type": "VISUAL", "shape": [3, 8, 8]}
        for bad in (None, [], "x", _cfg(input_features=None), _cfg(output_features={}), no_state, bad_shape,
                    bad_name, no_cam, five, _cfg(chunk_size=0), _cfg(chunk_size="100"), _cfg(n_action_steps=True),
                    _cfg(output_features={"action": {"type": "ACTION", "shape": [40]}})):
            with self.assertRaises(svc.ModelResolveError, msg=repr(bad)[:60]) as ctx:
                svc.summary_from_config(bad, META)
            self.assertIn(ctx.exception.code, ("invalid", "not_act"))


# ------------------------------------------------------------ resolve_model


REPO = "EduBotics-Solutions/vfbdfveg-act-omx_f_kjkkj-bfs6egcrlv"
SHA = "6477f6ca9a53ea62317f95d65b05ab7fd32db59a"


def _fake_httpx(routes, *, error=None, seen=None):
    """`routes`: {url_suffix: (status, bytes_or_obj)}; unknown -> 404."""
    import json as _json

    mod = types.ModuleType("httpx")
    seen = seen if seen is not None else {}
    seen.setdefault("urls", [])

    class _Resp:
        def __init__(self, status, body):
            self.status_code = status
            raw = body if isinstance(body, (bytes, bytearray)) else _json.dumps(body).encode()
            self._raw = bytes(raw)
            self.headers = {"content-length": str(len(self._raw))}

        def iter_bytes(self):
            for i in range(0, len(self._raw), 4096):
                yield self._raw[i:i + 4096]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Client:
        def __init__(self, timeout=None, follow_redirects=None, **_kw):
            seen["timeout"] = timeout
            seen["follow_redirects"] = follow_redirects

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, **_kw):
            seen["urls"].append(url)
            seen.setdefault("headers", []).append(dict(headers or {}))
            if error is not None:
                raise error
            for suffix, (status, body) in routes.items():
                if url.endswith(suffix):
                    return _Resp(status, body)
            return _Resp(404, b"{}")

    mod.Client = Client
    mod.seen = seen
    return mod


def _hub(config=CONFIG, meta=META, info=None):
    return {
        f"/api/models/{REPO}/revision/main": (200, info if info is not None else {"sha": SHA, "id": REPO}),
        f"/{REPO}/resolve/{SHA}/config.json": (200, config),
        f"/{REPO}/resolve/{SHA}/edubotics_model_meta.json": (200, meta) if meta is not None else (404, b""),
    }


class TestResolveModel(unittest.TestCase):
    def setUp(self) -> None:
        svc._reset_model_cache_for_tests()

    def _resolve(self, fake, env=None):
        with patch.dict(sys.modules, {"httpx": fake}), patch.dict(os.environ, env or {}, clear=False):
            if env is None:
                os.environ.pop("HF_TOKEN", None)
            return svc.resolve_model(REPO)

    def test_the_three_requests_and_the_summary(self) -> None:
        fake = _fake_httpx(_hub())
        out = self._resolve(fake)
        self.assertEqual(out["model_repo"], REPO)
        self.assertEqual(out["model_revision"], SHA)
        self.assertEqual(out["expected_cameras"], ["gripper", "scene"])
        self.assertEqual(out["trained_fps"], 30)
        self.assertEqual(fake.seen["urls"], [
            f"https://huggingface.co/api/models/{REPO}/revision/main",
            f"https://huggingface.co/{REPO}/resolve/{SHA}/config.json",
            f"https://huggingface.co/{REPO}/resolve/{SHA}/edubotics_model_meta.json",
        ])
        self.assertEqual(fake.seen["timeout"], svc.HF_TIMEOUT_S)
        self.assertIs(fake.seen["follow_redirects"], True)
        self.assertTrue(all("Authorization" not in h for h in fake.seen["headers"]))

    def test_the_platform_token_is_sent_when_set(self) -> None:
        fake = _fake_httpx(_hub())
        self._resolve(fake, env={"HF_TOKEN": "hf_" + "p" * 30})
        self.assertTrue(all(h.get("Authorization") == "Bearer hf_" + "p" * 30 for h in fake.seen["headers"]))

    def test_no_meta_means_no_fps(self) -> None:
        self.assertIsNone(self._resolve(_fake_httpx(_hub(meta=None)))["trained_fps"])

    def test_cached_for_60_s_per_repo(self) -> None:
        fake = _fake_httpx(_hub())
        clock = [1000.0]
        with patch.object(svc.time, "monotonic", lambda: clock[0]):
            self._resolve(fake)
            self._resolve(fake)
            self.assertEqual(len(fake.seen["urls"]), 3)
            clock[0] += 61
            self._resolve(fake)
            self.assertEqual(len(fake.seen["urls"]), 6)

    def test_failures_are_not_cached(self) -> None:
        fake = _fake_httpx({}, error=OSError("network down"))
        with self.assertRaises(svc.ModelResolveError):
            self._resolve(fake)
        self.assertEqual(self._resolve(_fake_httpx(_hub()))["model_revision"], SHA)

    def test_the_refusals(self) -> None:
        cases = [
            (_fake_httpx({}, error=OSError("boom")), "unreachable"),
            (_fake_httpx({f"/api/models/{REPO}/revision/main": (404, {})}), "not_found"),
            (_fake_httpx({f"/api/models/{REPO}/revision/main": (401, {})}), "not_found"),
            (_fake_httpx({f"/api/models/{REPO}/revision/main": (403, {})}), "not_found"),
            (_fake_httpx({f"/api/models/{REPO}/revision/main": (503, {})}), "unreachable"),
            (_fake_httpx({f"/api/models/{REPO}/revision/main": (429, {})}), "unreachable"),
            (_fake_httpx(_hub(info={"sha": "nope"})), "invalid"),
            (_fake_httpx(_hub(info={"id": REPO})), "invalid"),
            (_fake_httpx({**_hub(), f"/{REPO}/resolve/{SHA}/config.json": (404, b"")}), "invalid"),
            (_fake_httpx({**_hub(), f"/{REPO}/resolve/{SHA}/config.json": (200, b"{not json")}), "invalid"),
            (_fake_httpx({**_hub(), f"/{REPO}/resolve/{SHA}/config.json": (200, b"x" * (svc.CONFIG_MAX_BYTES + 1))}),
             "invalid"),
            (_fake_httpx({**_hub(), f"/{REPO}/resolve/{SHA}/config.json": (500, b"")}), "unreachable"),
            (_fake_httpx(_hub(config=_cfg(type="pi0"))), "not_act"),
        ]
        for fake, code in cases:
            svc._reset_model_cache_for_tests()
            with self.assertRaises(svc.ModelResolveError) as ctx:
                self._resolve(fake)
            self.assertEqual(ctx.exception.code, code)

    def test_a_bad_repo_name_never_reaches_the_network(self) -> None:
        fake = _fake_httpx(_hub())
        for bad in ("someone/else", "EduBotics-Solutions/../x", "EduBotics-Solutions/a b", None):
            with patch.dict(sys.modules, {"httpx": fake}):
                with self.assertRaises(svc.ModelResolveError):
                    svc.resolve_model(bad)
        self.assertEqual(fake.seen["urls"], [])

    def test_logs_name_no_body_and_no_token(self) -> None:
        fake = _fake_httpx({}, error=OSError("secret-ish detail Bearer hf_zzzzzzzzzzzzzzzzzz"))
        with self.assertLogs(svc.logger, level="WARNING") as logs:
            with self.assertRaises(svc.ModelResolveError):
                self._resolve(fake, env={"HF_TOKEN": "hf_" + "q" * 30})
        text = "\n".join(logs.output)
        self.assertNotIn("hf_", text)
        self.assertIn("OSError", text)


# ---------------------------------------------------------- the RPC wrappers


class _RpcDB:
    def __init__(self, answers=None, errors=None):
        self.answers = answers or {}
        self.errors = errors or {}
        self.calls = []

    def rpc(self, name, args):
        outer = self
        outer.calls.append((name, dict(args)))

        class _R:
            def execute(self):
                if name in outer.errors:
                    raise outer.errors[name]
                return SimpleNamespace(data=outer.answers.get(name))

        return _R()


class _ApiError(Exception):
    """A PostgREST error as supabase-py raises it: the code in str() and .code."""

    def __init__(self, code, message="x"):
        super().__init__({"code": code, "message": message})
        self.code = code


PROFILE = {"id": "u-me", "role": "student", "classroom_id": "c-1", "workgroup_id": None}
TRAINING = {"id": 7, "user_id": "u-me", "status": "succeeded", "model_type": "act",
            "model_name": REPO}
SUMMARY = {**svc.summary_from_config(CONFIG, META), "model_repo": REPO, "model_revision": SHA}


class TestRpcWrappers(unittest.TestCase):
    def test_reserve_sends_only_the_hash_and_the_caps(self) -> None:
        token, digest = svc.new_token()
        db = _RpcDB({"create_inference_session_safe": {"session_id": SID, "session_s": 1800,
                                                       "remaining_today_s": 7200, "reserve_until": "t"}})
        out = svc.create_reservation(db, PROFILE, TRAINING, SUMMARY, digest)
        self.assertEqual(out["session_id"], SID)
        (name, args), = db.calls
        self.assertEqual(name, "create_inference_session_safe")
        self.assertEqual(args, {
            "p_user_id": "u-me", "p_training_id": 7, "p_model_repo": REPO, "p_model_revision": SHA,
            "p_policy_type": "act", "p_token_hash": digest, "p_max_session_s": svc.SESSION_MAX_S,
            "p_daily_cap_s": svc.DAILY_CAP_S, "p_global_cap": svc.GLOBAL_CAP, "p_reserve_s": svc.RESERVE_S,
        })
        self.assertNotIn(token, repr(db.calls))

    def test_p0048_p0049_p0002_and_others(self) -> None:
        for code, exc in (("P0048", svc.DailyCapReached), ("P0049", svc.CloudBusy), ("P0002", svc.NotFound),
                          ("22023", svc.SessionStoreError), ("XX000", svc.SessionStoreError)):
            db = _RpcDB(errors={"create_inference_session_safe": _ApiError(code)})
            with self.assertRaises(exc, msg=code):
                svc.create_reservation(db, PROFILE, TRAINING, SUMMARY, "0" * 64)
        db = _RpcDB(errors={"create_inference_session_safe": RuntimeError("connection reset")})
        with self.assertRaises(svc.SessionStoreError):
            svc.create_reservation(db, PROFILE, TRAINING, SUMMARY, "0" * 64)
        # a code carried only in the text (the house convention elsewhere)
        db = _RpcDB(errors={"create_inference_session_safe": RuntimeError("{'code': 'P0049'}")})
        with self.assertRaises(svc.CloudBusy):
            svc.create_reservation(db, PROFILE, TRAINING, SUMMARY, "0" * 64)
        # a malformed answer is a store error, never a KeyError
        db = _RpcDB({"create_inference_session_safe": None})
        with self.assertRaises(svc.SessionStoreError):
            svc.create_reservation(db, PROFILE, TRAINING, SUMMARY, "0" * 64)

    def test_the_other_rpcs_name_their_exact_arguments(self) -> None:
        db = _RpcDB({
            "attach_inference_edge": {"status": "reserved"},
            "arm_inference_session": {"status": "armed", "edge_token_enc": "e1.x", "session_s": 60},
            "end_inference_session": {"status": "ended", "end_reason": "user_stop", "active_seconds": 0},
            "heartbeat_inference_session_client": {"status": "reserved", "end_reason": None, "max_end_in_s": None},
            "inference_usage": {"used_today_s": 10, "remaining_today_s": 7190, "live_session_id": None},
        })
        self.assertEqual(svc.attach_edge(db, SID, "sd-abc", "e1.00000000." + "A" * 24)["status"], "reserved")
        self.assertEqual(svc.arm(db, "u-me", SID, "f" * 64)["status"], "armed")
        self.assertEqual(svc.end_session(db, "u-me", SID)["end_reason"], "user_stop")
        svc.end_session(db, "u-me", SID, "edge_failed")
        self.assertIsNone(svc.client_heartbeat(db, "u-me", SID)["max_end_in_s"])
        self.assertEqual(svc.usage(db, "u-me")["used_today_s"], 10)
        self.assertEqual(db.calls, [
            ("attach_inference_edge", {"p_session_id": SID, "p_modal_session_id": "sd-abc",
                                       "p_edge_token_enc": "e1.00000000." + "A" * 24}),
            ("arm_inference_session", {"p_session_id": SID, "p_user_id": "u-me", "p_token_hash": "f" * 64}),
            ("end_inference_session", {"p_session_id": SID, "p_user_id": "u-me", "p_reason": "user_stop"}),
            ("end_inference_session", {"p_session_id": SID, "p_user_id": "u-me", "p_reason": "edge_failed"}),
            ("heartbeat_inference_session_client", {"p_session_id": SID, "p_user_id": "u-me"}),
            ("inference_usage", {"p_user_id": "u-me", "p_daily_cap_s": svc.DAILY_CAP_S}),
        ])

    def test_p0002_is_not_found_for_every_caller_scoped_rpc(self) -> None:
        for name, call in (("arm_inference_session", lambda db: svc.arm(db, "u", SID, "0" * 64)),
                           ("end_inference_session", lambda db: svc.end_session(db, "u", SID)),
                           ("heartbeat_inference_session_client", lambda db: svc.client_heartbeat(db, "u", SID))):
            with self.assertRaises(svc.NotFound, msg=name):
                call(_RpcDB(errors={name: _ApiError("P0002", "Sitzung nicht gefunden")}))
            with self.assertRaises(svc.SessionStoreError, msg=name):
                call(_RpcDB(errors={name: RuntimeError("network")}))


class TestEndReasonMaps(unittest.TestCase):
    def test_end_reason_maps_cover_the_sql_check(self) -> None:
        with open(MIGRATION, encoding="utf-8") as fh:
            sql = fh.read()
        reasons = end_reason_check(sql)
        self.assertEqual(reasons, END_REASONS)
        self.assertEqual(set(svc.END_REASON_DE), reasons)
        self.assertEqual(set(svc.EXIT_REASON), reasons)
        self.assertTrue(set(svc.EXIT_REASON.values()) <= {"finished", "error", "stopped"})

    def test_the_table_of_spec_4_3(self) -> None:
        self.assertEqual(svc.EXIT_REASON, {
            "user_stop": "stopped", "robot_stop": "stopped", "superseded": "stopped", "client_gone": "stopped",
            "never_connected": "stopped", "expired": "finished", "robot_error": "error", "stall": "error",
            "server_error": "error", "server_gone": "error", "edge_failed": "error",
        })
        self.assertEqual(svc.END_REASON_DE["user_stop"], "")
        self.assertEqual(svc.END_REASON_DE["robot_stop"], "")
        for reason, text in svc.END_REASON_DE.items():
            if reason not in ("user_stop", "robot_stop"):
                self.assertTrue(text.endswith("."), reason)
                self.assertFalse(re.search(r"\b(ae|oe|ue)\w*", text) and not re.search("[äöüß]", text), text)
        self.assertEqual(svc.END_REASON_DE["client_gone"], "Die Inferenz-Seite war nicht mehr geöffnet.")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
