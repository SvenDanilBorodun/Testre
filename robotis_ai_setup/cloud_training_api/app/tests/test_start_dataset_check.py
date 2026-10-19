"""POST /trainings/start's dataset check through the REAL preflight (spec §4.1 step 5).

The other start tests replace ``dataset_preflight.preflight_dataset``; here only
the Hub is fake (``dataset_preflight._hub``), so the twin, its classification
and the route's 400/502 split run as they do on Railway, on the DEFAULT legacy
path (every ACT start under ``TRAINING_DISPATCH=legacy``, every other policy
always) and on the managed one.

Pinned (fix round E): a Hub that could not be asked — an HTTP 429 or 5xx, a
network or transport error, a timeout (the module's own 60 s thread bound
included), a failure nobody classified — is 502 with the German retry sentence,
never 400; a dataset that is missing, private, gated, untagged, malformed or
incomplete is 400 with its German sentence; and no ``detail`` the route answers
ever carries an exception's text (every injected exception says
``SECRET-ENGLISH-MARKER``).

The exception classes are huggingface_hub 1.23's own, loaded from the installed
library's ``errors.py`` (it needs only httpx; the house stub may shadow the
package in sys.modules), else fakes with the same names and the same MRO shape.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_managed_start import StartBase, request  # noqa: F401  (StartBase: the house setup)

from fastapi import HTTPException  # noqa: E402
from app.services import dataset_preflight as P  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402

REAL_PREFLIGHT = P.preflight_dataset  # StartBase replaces it; these tests put it back
MARKER = "SECRET-ENGLISH-MARKER"
NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "gripper_joint_1"]


def _real_errors():
    """huggingface_hub/errors.py of the installed library, loaded by path, or None."""
    try:
        import httpx
    except ImportError:
        return None, None
    spec = importlib.machinery.PathFinder.find_spec("huggingface_hub")
    if spec is None or not spec.origin:
        return None, None
    path = os.path.join(os.path.dirname(spec.origin), "errors.py")
    if not os.path.isfile(path):
        return None, None
    loaded = importlib.util.spec_from_file_location("_edubotics_real_hf_errors", path)
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module, httpx


def _fake_errors():
    class HfHubHTTPError(OSError):
        def __init__(self, message, response=None):
            super().__init__(message)
            self.response = response

    class RepositoryNotFoundError(HfHubHTTPError):
        pass

    class GatedRepoError(RepositoryNotFoundError):
        pass

    class RevisionNotFoundError(HfHubHTTPError):
        pass

    class EntryNotFoundError(Exception):
        pass

    class RemoteEntryNotFoundError(HfHubHTTPError, EntryNotFoundError):
        pass

    class LocalEntryNotFoundError(FileNotFoundError, EntryNotFoundError):
        pass

    return SimpleNamespace(**{c.__name__: c for c in (
        HfHubHTTPError, RepositoryNotFoundError, GatedRepoError, RevisionNotFoundError, EntryNotFoundError,
        RemoteEntryNotFoundError, LocalEntryNotFoundError)})


E, HTTPX = _real_errors()
if E is None:  # pragma: no cover - CI installs huggingface_hub==1.23.0 and httpx
    E = _fake_errors()


def http(cls, status):
    """An instance of a huggingface_hub HTTP error class with that status, saying MARKER."""
    if HTTPX is not None:
        resp = HTTPX.Response(status, request=HTTPX.Request("GET", "https://hub.test/api/x"))
        return cls(f"{status} {MARKER}", response=resp)
    return cls(f"{status} {MARKER}", response=SimpleNamespace(status_code=status))


def local_from(cause):
    """What huggingface_hub 1.23's hf_hub_download raises when its HEAD failed and
    nothing is cached: LocalEntryNotFoundError FROM the real cause."""
    outer = E.LocalEntryNotFoundError(f"An error happened while trying to locate the file {MARKER}")
    outer.__cause__ = cause
    return outer


def connect_error():
    if HTTPX is not None:
        return HTTPX.ConnectError(f"[Errno 8] nodename nor servname provided {MARKER}")
    return ConnectionError(MARKER)


def read_timeout():
    if HTTPX is not None:
        return HTTPX.ReadTimeout(f"timed out {MARKER}")
    return TimeoutError(MARKER)


def good_info():
    return {
        "codebase_version": "v3.0", "fps": 30, "total_episodes": 3, "total_tasks": 1, "robot_type": "omx_f",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [6], "names": list(NAMES)},
            "action": {"dtype": "float32", "shape": [6], "names": list(NAMES)},
            "observation.images.gripper": {"dtype": "video", "shape": [480, 640, 3],
                                           "names": ["height", "width", "channels"]},
            "observation.images.scene": {"dtype": "video", "shape": [480, 640, 3],
                                         "names": ["height", "width", "channels"]},
        },
    }


def good_stats():
    return {f"{f}/{k}": [0.0] * 6 for f in ("observation.state", "action")
            for k in ("mean", "std", "min", "max", "q01", "q99")}


GOOD_FILES = [
    "meta/info.json", "meta/stats.json", "data/chunk-000/file-000.parquet", "meta/episodes/chunk-000/file-000.parquet",
    "videos/observation.images.gripper/chunk-000/file-000.mp4", "videos/observation.images.scene/chunk-000/file-000.mp4",
]


class FakeHub:
    """meta/info.json and meta/stats.json download as a dict, raw bytes, or an
    exception INSTANCE (raised); the file list is a list or an exception instance."""

    def __init__(self, tmp, info=None, stats=None, files=None):
        self.tmp, self.calls = tmp, []
        self.info = good_info() if info is None else info
        self.stats = good_stats() if stats is None else stats
        self.files = list(GOOD_FILES) if files is None else files

    def hf_hub_download(self, repo_id, filename, repo_type, revision, token):
        self.calls.append((filename, revision, token))
        value = self.info if filename == "meta/info.json" else self.stats
        if isinstance(value, BaseException):
            raise value
        path = os.path.join(self.tmp, filename.replace("/", "_"))
        with open(path, "wb") as fh:
            fh.write(value if isinstance(value, bytes) else json.dumps(value).encode())
        return path

    def HfApi(self, token=None):
        hub = self

        class _Api:
            def list_repo_files(self, repo_id, repo_type=None, revision=None):
                hub.calls.append(("list", revision, token))
                if isinstance(hub.files, BaseException):
                    raise hub.files
                return list(hub.files)

        return _Api()

    def namespace(self):
        return SimpleNamespace(
            HfApi=self.HfApi, hf_hub_download=self.hf_hub_download, HfHubHTTPError=E.HfHubHTTPError,
            RepositoryNotFoundError=E.RepositoryNotFoundError, RevisionNotFoundError=E.RevisionNotFoundError,
            GatedRepoError=E.GatedRepoError)


class _StuckThread:
    """A download thread that never finishes: the twin's 60 s bound fires at once."""

    def __init__(self, target=None, daemon=None):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


def transient_cases():
    return {
        "info 503 (LocalEntryNotFound from it)": {"info": local_from(http(E.HfHubHTTPError, 503))},
        "info 429 (LocalEntryNotFound from it)": {"info": local_from(http(E.HfHubHTTPError, 429))},
        "info DNS failure (LocalEntryNotFound from ConnectError)": {"info": local_from(connect_error())},
        "info 500 raised directly": {"info": http(E.HfHubHTTPError, 500)},
        "info unknown failure": {"info": RuntimeError(MARKER)},
        "stats 502 (LocalEntryNotFound from it)": {"stats": local_from(http(E.HfHubHTTPError, 502))},
        "stats DNS failure": {"stats": local_from(connect_error())},
        "file list 503": {"files": http(E.HfHubHTTPError, 503)},
        "file list read timeout": {"files": read_timeout()},
        "file list TimeoutError": {"files": TimeoutError(MARKER)},
        "file list connection reset": {"files": ConnectionResetError(MARKER)},
    }


def permanent_cases():
    """name -> (hub, the expected sentence as a thunk: resolved only by the test that compares it)."""
    n = "u/ds"
    return {
        "repo not found": ({"info": http(E.RepositoryNotFoundError, 404)}, lambda: P.not_found_sentence(n)),
        "gated": ({"info": http(E.GatedRepoError, 403)}, lambda: P.not_found_sentence(n)),
        "private, 401 raised directly": ({"info": http(E.HfHubHTTPError, 401)}, lambda: P.not_found_sentence(n)),
        "403 (LocalEntryNotFound from it)": ({"info": local_from(http(E.HfHubHTTPError, 403))},
                                             lambda: P.not_found_sentence(n)),
        "no v3.0 tag": ({"info": http(E.RevisionNotFoundError, 404)}, lambda: P.no_tag_sentence(n)),
        "no meta/info.json": ({"info": http(E.RemoteEntryNotFoundError, 404)}, lambda: P.info_missing_sentence(n)),
        "info.json not JSON": ({"info": b"{" + MARKER.encode()}, lambda: P.info_invalid_sentence(n)),
        "no meta/stats.json": ({"stats": http(E.RemoteEntryNotFoundError, 404)},
                               lambda: P.stats_unloadable_sentence(n)),
        "stats.json not JSON": ({"stats": b"[" + MARKER.encode()}, lambda: P.stats_unloadable_sentence(n)),
        "file list refused 400": ({"files": http(E.HfHubHTTPError, 400)}, lambda: P.hub_refused_sentence(n, 400)),
        "file list repo gone": ({"files": http(E.RepositoryNotFoundError, 404)}, lambda: P.not_found_sentence(n)),
    }


class _RealPreflight(StartBase):
    """StartBase with the real preflight and a fake Hub."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = FakeHub(self._tmp.name)
        extra = [patch.object(P, "preflight_dataset", REAL_PREFLIGHT), patch.object(P, "_hub", lambda: self.hub.namespace())]
        for cm in extra:
            cm.__enter__()
        self._cms += extra

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self._tmp.cleanup()

    async def answer(self, req=None, **hub):
        """The HTTPException the start answers (or None on success) for this Hub."""
        self.hub = FakeHub(self._tmp.name, **hub)
        try:
            await self.start(req)
        except HTTPException as exc:
            return exc
        return None


class LegacyPathDatasetCheck(_RealPreflight):
    """The DEFAULT path: TRAINING_DISPATCH=legacy (every start a legacy start)."""

    dispatch = "legacy"

    async def test_a_good_dataset_starts(self):
        self.assertIsNone(await self.answer())
        self.assertEqual(len(self.db.calls("start_training_safe")), 1)
        self.assertEqual(self.hub.calls[0], ("meta/info.json", "v3.0", "hf_platform"))

    async def test_a_hub_that_could_not_be_asked_is_502_with_the_retry_sentence(self):
        for name, hub in transient_cases().items():
            with self.subTest(case=name):
                exc = await self.answer(request(steps=40000 + len(self.hub.calls)), **hub)
                self.assertIsNotNone(exc, name)
                self.assertEqual((exc.status_code, exc.detail), (502, T.HF_UNREACHABLE_DE), name)
        self.assertEqual(self.db.calls("start_training_safe"), [], "no credit for a check that could not run")

    async def test_the_preflights_own_60s_bound_is_502(self):
        with patch.object(P, "threading", SimpleNamespace(Thread=_StuckThread)):
            exc = await self.answer()
        self.assertEqual((exc.status_code, exc.detail), (502, T.HF_UNREACHABLE_DE))

    async def test_a_dataset_that_cannot_be_trained_is_400_with_its_german_sentence(self):
        for name, (hub, sentence) in permanent_cases().items():
            with self.subTest(case=name):
                exc = await self.answer(request(steps=40000 + len(self.hub.calls)), **hub)
                self.assertIsNotNone(exc, name)
                self.assertEqual((exc.status_code, exc.detail), (400, sentence()), name)
        self.assertEqual(self.db.calls("start_training_safe"), [])

    async def test_no_detail_ever_carries_an_exceptions_text(self):
        cases = list(transient_cases().values()) + [hub for hub, _ in permanent_cases().values()]
        for i, hub in enumerate(cases):
            with self.subTest(case=i):
                exc = await self.answer(request(steps=30000 + i), **hub)
                self.assertNotIn(MARKER, exc.detail)
                self.assertNotRegex(exc.detail, r"(?i)error|errno|traceback|exception|timed out|\bnot found\b")

    async def test_the_content_refusals_keep_their_sentences(self):
        info = good_info()
        info["total_episodes"] = 0
        exc = await self.answer(info=info)
        self.assertEqual(exc.status_code, 400)
        self.assertIn("enthält keine Episoden", exc.detail)
        exc = await self.answer(request(steps=40001), files=[f for f in GOOD_FILES if not f.startswith("data/")])
        self.assertEqual(exc.status_code, 400)
        self.assertIn("ist unvollständig hochgeladen", exc.detail)

    async def test_a_library_value_error_is_never_shown(self):
        # A huggingface_hub ValueError (HFValidationError is one) escaping the
        # check is not this module's sentence: 502, its text in no detail.
        with patch.object(P, "preflight_dataset", lambda *a, **k: (_ for _ in ()).throw(ValueError(MARKER))):
            with self.assertRaises(HTTPException) as ctx:
                await self.start()
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (502, T.HF_UNREACHABLE_DE))

    async def test_another_policy_takes_the_same_path(self):
        exc = await self.answer(request(model_type="diffusion"), info=local_from(http(E.HfHubHTTPError, 503)))
        self.assertEqual((exc.status_code, exc.detail), (502, T.HF_UNREACHABLE_DE))


class ManagedPathDatasetCheck(_RealPreflight):
    dispatch = "managed"

    async def test_transient_502_and_permanent_400_on_the_managed_path(self):
        exc = await self.answer(info=local_from(http(E.HfHubHTTPError, 503)))
        self.assertEqual((exc.status_code, exc.detail), (502, T.HF_UNREACHABLE_DE))
        exc = await self.answer(request(steps=40001), info=http(E.RepositoryNotFoundError, 404))
        self.assertEqual((exc.status_code, exc.detail), (400, P.not_found_sentence("u/ds")))
        self.assertEqual(self.hub.calls[0][1:], (self.resolved, False), "anonymous, at the resolved commit")

    async def test_a_library_value_error_from_the_tag_resolution_is_never_shown(self):
        self.resolved = ValueError(MARKER)
        with self.assertRaises(HTTPException) as ctx:
            await self.start()
        self.assertEqual((ctx.exception.status_code, ctx.exception.detail), (502, T.HF_UNREACHABLE_DE))


if __name__ == "__main__":
    import unittest

    unittest.main()
