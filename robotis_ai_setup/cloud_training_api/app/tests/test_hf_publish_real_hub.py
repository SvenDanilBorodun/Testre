"""hf_publish._read_back against the REAL huggingface_hub (V2-3).

Every other test of the API stubs ``huggingface_hub``, so a release of the
library that renamed ``RepoFile.lfs`` / ``.blob_id`` or changed how
``get_paths_info`` parses the Hub's answer would keep the whole suite green
while every publish retried for 6 h and ended S8. Here the installed library
talks HTTP to a local server that answers ``POST /api/models/<repo>/paths-info/
main`` with a canned reply in the Hub's own shape (``oid`` = git blob sha1,
``lfs.oid`` = sha256, ``expand=True``'s ``lastCommit``).

The version is pinned: requirements.txt says ``huggingface_hub==X`` and the CI
``python-tests`` job installs the same X into its one shared venv (the server
image's version), so this test runs in CI against exactly what Railway
installs. A different installed version fails here rather than proving nothing.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.services import hf_publish as P  # noqa: E402

API_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = os.path.dirname(os.path.dirname(API_DIR))
REQUIREMENTS = os.path.join(API_DIR, "requirements.txt")
CI_YML = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
REPO = "EduBotics-Solutions/m-act-ds-abc"
BIG = bytes(range(256)) * 64          # an LFS file (the model)
SMALL = b'{"type": "act"}\n'          # a regular git blob (config.json)


def _pin(text: str, where: str) -> str:
    found = re.findall(r"huggingface_hub==([0-9][0-9A-Za-z.]*)", text)
    if len(set(found)) != 1:
        raise AssertionError(f"{where}: expected one huggingface_hub==X pin, found {found}")
    return found[0]


def _requirements_pin() -> str:
    with open(REQUIREMENTS, encoding="utf-8") as fh:
        return _pin(fh.read(), "requirements.txt")


def _ci_python_tests_pin() -> str:
    """The pin in ci.yml's python-tests job (up to the next job header)."""
    with open(CI_YML, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("\n  python-tests:\n")
    nxt = re.compile(r"\n  [a-z0-9-]+:\n").search(text, start + 1)
    return _pin(text[start: nxt.start() if nxt else len(text)], "ci.yml python-tests")


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class _Hub(BaseHTTPRequestHandler):
    """paths-info only; answers from the class-level ``reply``."""

    reply: list = []
    status = 200
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode()) if n else {}
        type(self).seen.append((self.path, form))
        body = json.dumps(type(self).reply if type(self).status == 200 else {"error": "down"}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _entry(path: str, data: bytes, *, lfs: bool, sha256: str | None = None, size: int | None = None,
           blob: str | None = None) -> dict:
    out = {"type": "file", "oid": blob or (git_blob_sha1(data) if not lfs else "f" * 40),
           "size": len(data) if size is None else size, "path": path,
           "lastCommit": {"id": "c" * 40, "title": "EduBotics: trained model", "date": "2026-10-18T12:00:00.000Z"}}
    if lfs:
        out["lfs"] = {"oid": sha256 or hashlib.sha256(data).hexdigest(),
                      "size": len(data) if size is None else size, "pointerSize": 134}
    return out


class ReadBackAgainstTheRealLibrary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The suite's stub sits in sys.modules; put the real package in its place
        # for this class only (huggingface_hub resolves its own submodules lazily
        # through sys.modules), and give the stub back afterwards.
        cls._saved = {k: sys.modules.pop(k) for k in list(sys.modules)
                      if k == "huggingface_hub" or k.startswith("huggingface_hub.")}
        cls.hub = None
        if importlib.util.find_spec("huggingface_hub") is not None:
            with patch.dict(os.environ, {"HF_HUB_OFFLINE": "0", "HF_HUB_DISABLE_TELEMETRY": "1"}):
                cls.hub = importlib.import_module("huggingface_hub")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for k in [k for k in sys.modules if k == "huggingface_hub" or k.startswith("huggingface_hub.")]:
            del sys.modules[k]
        sys.modules.update(cls._saved)

    def setUp(self):
        if self.hub is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("huggingface_hub is not installed in CI's python-tests venv")
            self.skipTest("huggingface_hub is not installed")
        _Hub.reply, _Hub.status, _Hub.seen = [], 200, []
        self.api = self.hub.HfApi(endpoint=self.endpoint, token="hf_" + "x" * 34)
        self.expected = {
            "model.safetensors": (hashlib.sha256(BIG).hexdigest(), len(BIG), git_blob_sha1(BIG)),
            "config.json": (hashlib.sha256(SMALL).hexdigest(), len(SMALL), git_blob_sha1(SMALL)),
        }

    def test_the_installed_version_is_the_pinned_one_and_ci_installs_it(self):
        self.assertEqual(self.hub.__version__, _requirements_pin())
        self.assertEqual(_ci_python_tests_pin(), _requirements_pin(),
                         "ci.yml's python-tests venv must install the API's pinned huggingface_hub")

    def test_a_matching_reply_passes_and_the_request_has_the_hubs_shape(self):
        _Hub.reply = [_entry("model.safetensors", BIG, lfs=True), _entry("config.json", SMALL, lfs=False)]
        P._read_back(self.api, REPO, self.expected)
        ((path, form),) = _Hub.seen
        self.assertEqual(path, f"/api/models/{REPO}/paths-info/main")
        self.assertEqual(sorted(form["paths"]), ["config.json", "model.safetensors"])
        self.assertEqual([v.lower() for v in form["expand"]], ["true"])

    def test_an_lfs_sha256_or_size_mismatch_is_transient(self):
        for kw in ({"sha256": "0" * 64}, {"size": len(BIG) + 1}):
            with self.subTest(kw=kw):
                _Hub.reply = [_entry("model.safetensors", BIG, lfs=True, **kw), _entry("config.json", SMALL, lfs=False)]
                with self.assertRaises(P.Transient):
                    P._read_back(self.api, REPO, self.expected)

    def test_a_git_blob_mismatch_is_transient(self):
        _Hub.reply = [_entry("model.safetensors", BIG, lfs=True), _entry("config.json", SMALL, lfs=False, blob="0" * 40)]
        with self.assertRaises(P.Transient):
            P._read_back(self.api, REPO, self.expected)

    def test_a_missing_path_is_transient(self):
        _Hub.reply = [_entry("model.safetensors", BIG, lfs=True)]
        with self.assertRaises(P.Transient):
            P._read_back(self.api, REPO, self.expected)

    def test_a_hub_error_is_transient(self):
        _Hub.status = 503
        with self.assertRaises(P.Transient):
            P._read_back(self.api, REPO, self.expected)


if __name__ == "__main__":
    unittest.main()
