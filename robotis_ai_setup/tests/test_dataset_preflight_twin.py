"""The cloud API's dataset preflight is a twin of the Modal worker's (spec §6 WP-A).

``cloud_training_api/app/services/dataset_preflight.py`` copies the worker's
``_stat_value``, ``_validate_stats`` and ``_preflight_dataset``
(``modal_training/training_handler.py``) so a dataset the worker would refuse
after a GPU was paid for is refused at ``POST /trainings/start``, before any
credit or GPU. Both run over ONE fake-hub table here; for every case the
verdicts are equal — both pass with the same info, or both refuse — and the
twin's German sentence, transliterated back (ä→ae, ö→oe, ü→ue, ß→ss, Ä→Ae,
Ö→Oe, Ü→Ue), is the worker's sentence character for character.

Both modules are loaded the way they run: the worker with ``huggingface_hub``
and ``supabase`` stubbed when they are not installed (its module top imports
them), the twin BY PATH (stdlib at its top; an ``app`` package of the GUI
could shadow the cloud API's). The fake hub is installed into both: the
worker's module globals, the twin's ``_hub()``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_TWIN = os.path.join(_ROOT, "cloud_training_api", "app", "services", "dataset_preflight.py")
_MODAL = os.path.join(_ROOT, "modal_training")

TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})


def _stub_worker_deps() -> None:
    if "huggingface_hub" not in sys.modules:
        m = types.ModuleType("huggingface_hub")
        m.HfApi = lambda *a, **k: None
        m.hf_hub_download = lambda *a, **k: None
        m.login = lambda *a, **k: None
        u = types.ModuleType("huggingface_hub.utils")

        class HfHubHTTPError(Exception):
            pass

        class RepositoryNotFoundError(HfHubHTTPError):
            pass

        class RevisionNotFoundError(HfHubHTTPError):
            pass

        u.HfHubHTTPError = HfHubHTTPError
        u.RepositoryNotFoundError = RepositoryNotFoundError
        u.RevisionNotFoundError = RevisionNotFoundError
        sys.modules["huggingface_hub"] = m
        sys.modules["huggingface_hub.utils"] = u
    if "supabase" not in sys.modules:
        m = types.ModuleType("supabase")
        m.create_client = lambda *a, **k: None
        sys.modules["supabase"] = m


_stub_worker_deps()
if _MODAL not in sys.path:
    sys.path.insert(0, _MODAL)
import training_handler as worker  # noqa: E402

_spec = importlib.util.spec_from_file_location("_edubotics_api_dataset_preflight", _TWIN)
twin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(twin)


def _make_error(cls):
    """An instance of the worker's error class (stub or real huggingface_hub)."""
    try:
        return cls("boom")
    except TypeError:  # real huggingface_hub: response is a required keyword
        import httpx

        return cls("boom", response=httpx.Response(500, request=httpx.Request("GET", "https://hub.test/x")))


NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_joint_1"]


def good_info(**over):
    info = {
        "codebase_version": "v3.0", "fps": 30, "total_episodes": 3, "total_tasks": 1, "robot_type": "omx_f",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [6], "names": list(NAMES)},
            "action": {"dtype": "float32", "shape": [6], "names": list(NAMES)},
            "observation.images.gripper": {"dtype": "video", "shape": [48, 64, 3],
                                           "names": ["height", "width", "channels"]},
            "observation.images.scene": {"dtype": "video", "shape": [48, 64, 3],
                                         "names": ["height", "width", "channels"]},
        },
    }
    info.update(over)
    return info


def good_stats(n=6, nested=False):
    if nested:
        return {f: {k: [0.0] * n for k in ("mean", "std", "min", "max", "q01", "q99")}
                for f in ("observation.state", "action")}
    return {f"{f}/{k}": [0.0] * n for f in ("observation.state", "action")
            for k in ("mean", "std", "min", "max", "q01", "q99")}


GOOD_FILES = [
    "meta/info.json", "meta/stats.json", "data/chunk-000/file-000.parquet", "meta/episodes/chunk-000/file-000.parquet",
    "videos/observation.images.gripper/chunk-000/file-000.mp4", "videos/observation.images.scene/chunk-000/file-000.mp4",
]


class FakeHub:
    """One dataset: what meta/info.json and meta/stats.json download as (an object,
    raw bytes, or an exception class), and the file list (a list or an exception class)."""

    def __init__(self, tmp, info=None, stats=None, files=None, model_type="act"):
        self.tmp = tmp
        self.info = good_info() if info is None else info
        self.stats = good_stats() if stats is None else stats
        self.files = list(GOOD_FILES) if files is None else files
        self.model_type = model_type
        self.calls = []

    def hf_hub_download(self, repo_id, filename, repo_type, revision, token):
        self.calls.append((filename, revision, token))
        value = self.info if filename == "meta/info.json" else self.stats
        if isinstance(value, type) and issubclass(value, BaseException):
            raise _make_error(value)
        path = os.path.join(self.tmp, filename.replace("/", "_"))
        with open(path, "wb") as fh:
            fh.write(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def HfApi(self, token=None):
        hub = self

        class _Api:
            def list_repo_files(self, repo_id, repo_type=None, revision=None):
                hub.calls.append(("list", revision, token))
                if isinstance(hub.files, type) and issubclass(hub.files, BaseException):
                    raise _make_error(hub.files)
                return list(hub.files)

        return _Api()


def _cases():
    E = worker
    no_state = good_info()
    no_state["features"].pop("observation.state")
    names_str = good_info()
    names_str["features"]["observation.state"]["names"] = "joints"
    three = good_info()
    for k in ("observation.state", "action"):
        three["features"][k]["names"] = NAMES[:3]
    differ = good_info()
    differ["features"]["action"]["names"] = list(reversed(NAMES))
    no_cam = good_info()
    for k in ("observation.images.gripper", "observation.images.scene"):
        no_cam["features"].pop(k)
    short_stats = good_stats()
    short_stats.pop("action/std")
    return {
        "good act dataset": {},
        "good, nested stats": {"stats": good_stats(nested=True)},
        "good, act without tasks": {"info": good_info(total_tasks=0)},
        "repo not found": {"info": E.RepositoryNotFoundError},
        "no v3.0 tag": {"info": E.RevisionNotFoundError},
        "hub http error": {"info": E.HfHubHTTPError},
        "other download error": {"info": RuntimeError},
        "info not json": {"info": b"{not json"},
        "no codebase_version": {"info": good_info(codebase_version=None)},
        "codebase v2.1": {"info": good_info(codebase_version="v2.1")},
        "fps 0": {"info": good_info(fps=0)},
        "no episodes": {"info": good_info(total_episodes=0)},
        "no observation.state": {"info": no_state},
        "names not a list": {"info": names_str},
        "three joints": {"info": three},
        "names differ": {"info": differ},
        "no camera": {"info": no_cam},
        "stats download fails": {"stats": E.HfHubHTTPError},
        "stats not json": {"stats": b"[1,"},
        "stats not an object": {"stats": [1, 2]},
        "stats incomplete": {"stats": short_stats},
        "stats wrong length": {"stats": good_stats(n=5)},
        "file list fails": {"files": RuntimeError},
        "no data parquet": {"files": [f for f in GOOD_FILES if not f.startswith("data/")]},
        "a camera without videos": {"files": [f for f in GOOD_FILES if "scene" not in f]},
        "no episode metadata": {"files": [f for f in GOOD_FILES if not f.startswith("meta/episodes/")]},
        "pi0 without tasks": {"info": good_info(total_tasks=0), "model_type": "pi0"},
        "smolvla with tasks": {"model_type": "smolvla"},
    }


def _run_worker(hub):
    saved = (worker.hf_hub_download, worker.HfApi)
    worker.hf_hub_download, worker.HfApi = hub.hf_hub_download, hub.HfApi
    try:
        return "ok", worker._preflight_dataset("u/ds", "tok", hub.model_type)
    except ValueError as exc:
        return "refused", str(exc)
    finally:
        worker.hf_hub_download, worker.HfApi = saved


def _run_twin(hub, revision="v3.0", token="tok"):
    saved = twin._hub
    twin._hub = lambda: SimpleNamespace(
        HfApi=hub.HfApi, hf_hub_download=hub.hf_hub_download, HfHubHTTPError=worker.HfHubHTTPError,
        RepositoryNotFoundError=worker.RepositoryNotFoundError, RevisionNotFoundError=worker.RevisionNotFoundError,
        GatedRepoError=worker.RepositoryNotFoundError)
    try:
        return "ok", twin._preflight_dataset("u/ds", token, hub.model_type, revision=revision)
    except ValueError as exc:
        return "refused", str(exc)
    finally:
        twin._hub = saved


class PreflightTwin(unittest.TestCase):
    def test_every_case_has_equal_verdicts_and_the_same_sentence(self):
        cases = _cases()
        self.assertGreaterEqual(len(cases), 12)
        refused = 0
        for name, spec in cases.items():
            with tempfile.TemporaryDirectory() as tmp, self.subTest(case=name):
                w = _run_worker(FakeHub(tmp, **spec))
                t = _run_twin(FakeHub(tmp, **spec))
                self.assertEqual(w[0], t[0], (name, w, t))
                if w[0] == "refused":
                    refused += 1
                    self.assertEqual(t[1].translate(TRANSLIT), w[1])
                else:
                    self.assertEqual(t[1], w[1])
                    self.assertIn("features", t[1])
        self.assertGreaterEqual(refused, 20)

    def test_the_twins_sentences_use_real_umlauts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for spec, word in (({"info": good_info(fps=0)}, "gültige"),
                               ({"info": good_info(total_episodes=0)}, "enthält"),
                               ({"stats": good_stats(n=5)}, "unvollständige")):
                verdict, text = _run_twin(FakeHub(tmp, **spec))
                self.assertEqual(verdict, "refused")
                self.assertIn(word, text)
                self.assertNotRegex(text, r"(?i)gueltig|enthaelt|unvollstaendig")

    def test_token_and_revision_are_passed_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = FakeHub(tmp)
            sha = "b1c3ecbae7f244acc039a3dbc255a00dad1372b9"
            verdict, _ = _run_twin(hub, revision=sha, token=False)
            self.assertEqual(verdict, "ok")
            self.assertEqual(hub.calls, [("meta/info.json", sha, False), ("meta/stats.json", sha, False),
                                         ("list", sha, False)])

    def test_the_same_constants(self):
        for name in ("EXPECTED_CODEBASE_VERSION", "MIN_JOINTS", "MAX_JOINTS", "_REQUIRED_STATS_KEYS",
                     "_LANGUAGE_CONDITIONED_POLICIES"):
            self.assertEqual(getattr(twin, name), getattr(worker, name), name)

    def test_the_twin_imports_nothing_heavy_at_its_top(self):
        import ast

        with open(_TWIN, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        mods = {(n.module if isinstance(n, ast.ImportFrom) else n.names[0].name).split(".")[0] for n in top}
        self.assertLessEqual(mods, {"__future__", "json", "logging", "re", "threading", "types"})


if __name__ == "__main__":
    unittest.main()
