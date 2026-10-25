"""modal_app.py's two variants (spec §3.8, §4.9), deps-free: the app is imported under a
recording fake `modal` module (the real SDK is exercised by ci.yml::modal-import-validate).

prod = `edubotics-training`: train, train_vla (legacy, the Modal secret), train_managed (no
secret), smoke_test. dev = `edubotics-training-dev`: train_managed + smoke_test, and NO secret
is referenced anywhere. train_managed's signature is the frozen contract with the cloud API
(impl-notes WP-A 14), its timeout is 7 h (FR-9), and smoke_test imports the VLA modules (FR-20).
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_MODAL_DIR = Path(__file__).resolve().parents[1] / "modal_training"
MODAL_APP = _MODAL_DIR / "modal_app.py"


class _Image:
    def __init__(self, calls):
        self.calls = calls

    @classmethod
    def from_registry(cls, *a, **k):
        return cls([("from_registry", a, k)])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def method(*a, **k):
            return _Image(self.calls + [(name, a, k)])
        return method


def _fake_modal(record):
    m = types.ModuleType("modal")

    class Secret:
        @staticmethod
        def from_name(name):
            record["secrets"].append(name)
            return ("secret", name)

    class App:
        def __init__(self, name):
            record["app"] = name
            self.functions = record["functions"]

        def function(self, **kw):
            def deco(fn):
                record["functions"][fn.__name__] = {"kw": kw, "fn": fn}
                return fn
            return deco

    m.App, m.Image, m.Secret = App, _Image, Secret
    return m


def load(variant):
    record = {"app": None, "functions": {}, "secrets": []}
    env = {} if variant is None else {"EDUBOTICS_TRAINING_VARIANT": variant}
    with mock.patch.dict(sys.modules, {"modal": _fake_modal(record)}), \
            mock.patch.dict(os.environ, env, clear=False):
        if variant is None:
            os.environ.pop("EDUBOTICS_TRAINING_VARIANT", None)
        spec = importlib.util.spec_from_file_location(f"modal_app_{variant}", MODAL_APP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod, record


class Variants(unittest.TestCase):
    def test_prod(self):
        mod, rec = load("prod")
        self.assertEqual(rec["app"], "edubotics-training")
        self.assertEqual(set(rec["functions"]), {"train", "train_vla", "train_managed", "smoke_test"})
        self.assertEqual(rec["secrets"], ["edubotics-training-secrets"])
        for name in ("train", "train_vla", "smoke_test"):
            self.assertEqual(rec["functions"][name]["kw"]["secrets"], [("secret", "edubotics-training-secrets")])
        self.assertEqual(rec["functions"]["train"]["kw"]["gpu"], "L4")
        self.assertEqual(rec["functions"]["train_vla"]["kw"]["gpu"], "A100-80GB")
        self.assertEqual(rec["functions"]["train_vla"]["kw"]["timeout"], 11 * 3600)

    def test_the_default_variant_is_prod(self):
        _, rec = load(None)
        self.assertEqual(rec["app"], "edubotics-training")

    def test_dev_has_only_train_managed_and_smoke_test_and_no_secret_anywhere(self):
        _, rec = load("dev")
        self.assertEqual(rec["app"], "edubotics-training-dev")
        self.assertEqual(set(rec["functions"]), {"train_managed", "smoke_test"})
        self.assertEqual(rec["secrets"], [], "dev references no Modal secret")
        self.assertEqual(rec["functions"]["smoke_test"]["kw"].get("secrets"), [])

    def test_an_unknown_variant_refuses_to_import(self):
        with self.assertRaises(RuntimeError):
            load("staging")

    def test_train_managed_shape(self):
        for variant in ("prod", "dev"):
            with self.subTest(variant=variant):
                mod, rec = load(variant)
                kw = rec["functions"]["train_managed"]["kw"]
                self.assertEqual(kw["gpu"], "L4")
                self.assertEqual(kw["cpu"], 8)
                self.assertEqual(kw["timeout"], 7 * 3600, "FR-9: the budget, not Modal, ends an attempt")
                self.assertEqual(kw["min_containers"], 0)
                self.assertNotIn("secrets", kw, "train_managed gets no Modal secret (§1 item 11)")
                self.assertIs(kw["image"], mod.image)

    def test_train_managed_signature_is_the_frozen_contract(self):
        mod, rec = load("prod")
        params = list(inspect.signature(rec["functions"]["train_managed"]["fn"]).parameters)
        self.assertEqual(params, ["training_id", "attempt", "worker_token", "api_url",
                                  "supabase_url", "supabase_anon_key"])

    def test_train_managed_calls_run_managed_with_cpu_budget_8(self):
        mod, rec = load("dev")
        got = {}
        fake_th = types.ModuleType("training_handler")
        fake_th.run_managed = lambda *a, **k: got.update(args=a, kw=k) or {"outcome": "x"}
        with mock.patch.dict(sys.modules, {"training_handler": fake_th}):
            out = rec["functions"]["train_managed"]["fn"](
                training_id=7, attempt=2, worker_token="t", api_url="https://api",
                supabase_url="https://sb", supabase_anon_key="anon")
        self.assertEqual(out, {"outcome": "x"})
        self.assertEqual(got["args"], ("https://api", "https://sb", "anon", 7, 2, "t"))
        self.assertEqual(got["kw"], {"cpu_budget": 8})

    def test_the_image_is_the_lock_based_one(self):
        for variant in ("prod", "dev"):
            with self.subTest(variant=variant):
                mod, _ = load(variant)
                calls = mod.image.calls
                names = [c[0] for c in calls]
                self.assertEqual(names, ["from_registry", "apt_install", "add_local_file", "add_local_file",
                                         "run_commands", "env", "add_local_python_source"])
                self.assertEqual(calls[0][1:], (("nvidia/cuda:12.6.1-devel-ubuntu22.04",), {"add_python": "3.12"}))
                self.assertEqual(calls[1][1], ("git", "ffmpeg", "clang", "build-essential"))
                self.assertEqual(calls[2][1], (_MODAL_DIR.resolve() / "training-lock.txt", "/root/training-lock.txt"))
                self.assertEqual(calls[3][1], (_MODAL_DIR.resolve() / "check_training_lock.py",
                                               "/root/check_training_lock.py"))
                self.assertTrue(calls[2][2].get("copy") and calls[3][2].get("copy"))
                self.assertEqual(calls[4][1], (mod.INSTALL_LOCK, "python /root/check_training_lock.py"))
                self.assertEqual(calls[5][1], ({"PYTHONUNBUFFERED": "1", "EDUBOTICS_TRAINING_VARIANT": variant},))
                self.assertEqual(calls[6][1], ("training_handler",))

    def test_smoke_test_checks_the_lock_and_imports_the_vla_modules(self):
        tree = ast.parse(MODAL_APP.read_text(encoding="utf-8"))
        smoke = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "smoke_test")
        imported = {a.name for n in ast.walk(smoke) if isinstance(n, ast.Import) for a in n.names}
        self.assertTrue({"lerobot.policies.pi0.modeling_pi0", "lerobot.policies.smolvla.modeling_smolvla",
                         "transformers", "torch"} <= imported, imported)
        self.assertIn("CHECK_IN_IMAGE", ast.unparse(smoke))


if __name__ == "__main__":
    unittest.main()
