"""Unit tests for the pi-agent self-update checker.

Mirrors the GUI's ``tests/test_update_checker.py`` shape but exercises the
agent-tarball surface: the OPTIONAL ``pi_agent_download_url`` / ``pi_agent_sha256``
fields (absent → "no agent update available", never a crash), the HEAD asset
pre-check, and the Content-Length + SHA-256 download gates.

Deps-free: every ``urlopen`` is mocked. Runs on Linux CI + macOS dev hosts.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]  # robotis_ai_setup
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pi_agent import update_checker  # noqa: E402


class TestParseVersion(unittest.TestCase):
    def test_plain_numeric(self):
        self.assertEqual(update_checker._parse_version("2.8.1"), (2, 8, 1))

    def test_v_prefix(self):
        self.assertEqual(update_checker._parse_version("v2.8.1"), (2, 8, 1))

    def test_prerelease_suffix_does_not_raise(self):
        self.assertEqual(update_checker._parse_version("2.8.1-rc1"), (2, 8, 1))
        self.assertEqual(update_checker._parse_version("v2.8.1+build7"), (2, 8, 1))

    def test_short_form_padded(self):
        self.assertEqual(update_checker._parse_version("2.8"), (2, 8, 0))
        self.assertEqual(
            update_checker._parse_version("2.8"),
            update_checker._parse_version("2.8.0"),
        )

    def test_empty_and_garbage(self):
        self.assertEqual(update_checker._parse_version(""), (0, 0, 0))
        self.assertEqual(update_checker._parse_version("latest"), (0, 0, 0))
        self.assertEqual(update_checker._parse_version(None), (0, 0, 0))

    def test_ordering(self):
        P = update_checker._parse_version
        self.assertGreater(P("2.9.0"), P("2.8.5"))
        self.assertFalse(P("2.8.0") > P("2.8.0"))


def _cm(body: bytes = b""):
    """A context-manager mock mimicking urlopen()'s response."""
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class TestCheckForAgentUpdate(unittest.TestCase):
    API = "https://api.test"
    URL = "https://github.com/o/r/releases/download/v2.9.0/edubotics-pi-agent.tar.gz"

    def _dispatch(self, version_payload, asset):
        """Return a urlopen side_effect: GET /version -> payload; HEAD -> asset
        ('ok' | 404 | 'neterr')."""
        def fake(req, timeout=None):
            if req.get_method() == "HEAD":
                if asset == 404:
                    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
                if asset == "neterr":
                    raise urllib.error.URLError("offline")
                return _cm()
            return _cm(json.dumps(version_payload).encode())
        return fake

    def test_newer_with_asset_present_returns_update(self):
        payload = {"version": "2.9.0", "pi_agent_download_url": self.URL,
                   "pi_agent_sha256": "ab" * 32}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            r = update_checker.check_for_agent_update("2.8.0", self.API)
        self.assertEqual(r, {"version": "2.9.0", "download_url": self.URL,
                             "sha256": "ab" * 32})

    def test_missing_pi_agent_fields_returns_none(self):
        # Pre-P4 deploy: /version has version + the .exe fields but NOT the Pi
        # ones. Must be treated as "no agent update available", not a crash.
        payload = {
            "version": "2.9.0",
            "download_url": "https://github.com/o/r/releases/download/v2.9.0/EduBotics_Setup.exe",
            "installer_sha256": "cd" * 32,
        }
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            self.assertIsNone(update_checker.check_for_agent_update("2.8.0", self.API))

    def test_newer_but_asset_404_returns_none(self):
        payload = {"version": "2.9.0", "pi_agent_download_url": self.URL}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, 404)):
            self.assertIsNone(update_checker.check_for_agent_update("2.8.0", self.API))

    def test_newer_with_head_network_error_still_updates(self):
        payload = {"version": "2.9.0", "pi_agent_download_url": self.URL}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "neterr")):
            self.assertIsNotNone(update_checker.check_for_agent_update("2.8.0", self.API))

    def test_prerelease_remote_tag_now_compares(self):
        payload = {"version": "2.9.0-rc1", "pi_agent_download_url": self.URL}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            self.assertIsNotNone(update_checker.check_for_agent_update("2.8.0", self.API))

    def test_not_newer_returns_none(self):
        payload = {"version": "2.8.0", "pi_agent_download_url": self.URL}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            self.assertIsNone(update_checker.check_for_agent_update("2.8.0", self.API))

    def test_sha256_absent_is_empty_string(self):
        payload = {"version": "2.9.0", "pi_agent_download_url": self.URL}
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            r = update_checker.check_for_agent_update("2.8.0", self.API)
        self.assertEqual(r["sha256"], "")

    def test_network_error_on_version_returns_none(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("dns down")
        with patch("pi_agent.update_checker.urllib.request.urlopen", side_effect=boom):
            self.assertIsNone(update_checker.check_for_agent_update("2.8.0", self.API))


class _FakeResp:
    """Mimics urlopen()'s response for download_agent_tarball."""
    def __init__(self, body: bytes, content_length=None):
        self._body = body
        self._pos = 0
        cl = len(body) if content_length is None else content_length
        self.headers = {"Content-Length": str(cl)}

    def read(self, n):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDownloadAgentTarball(unittest.TestCase):
    import hashlib as _hashlib
    BODY = b"pretend-this-is-the-agent-tarball-bytes" * 1000
    GOOD = _hashlib.sha256(BODY).hexdigest()

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dl(self, content_length=None, expected=None):
        with patch("pi_agent.update_checker.urllib.request.urlopen",
                   return_value=_FakeResp(self.BODY, content_length)):
            return update_checker.download_agent_tarball(
                "http://x/edubotics-pi-agent.tar.gz", dest_dir=self.tmp,
                expected_sha256=expected,
            )

    def test_good_download_no_hash(self):
        path = self._dl()
        self.assertTrue(path and os.path.isfile(path))
        self.assertTrue(path.endswith("edubotics-pi-agent.tar.gz"))

    def test_good_download_matching_hash(self):
        path = self._dl(expected=self.GOOD)
        self.assertTrue(path and os.path.isfile(path))

    def test_mismatched_hash_rejected_and_cleaned(self):
        path = self._dl(expected="00" * 32)
        self.assertIsNone(path)
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "edubotics-pi-agent.tar.gz")))

    def test_truncated_download_rejected(self):
        path = self._dl(content_length=len(self.BODY) + 5000)
        self.assertIsNone(path)
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "edubotics-pi-agent.tar.gz")))


class TestCleanupStaleTarballs(unittest.TestCase):
    def test_removes_old_and_keeps_fresh(self):
        import tempfile
        import time
        tmp = tempfile.mkdtemp()
        old = os.path.join(tmp, "edubotics-pi-agent.tar.gz")
        fresh = os.path.join(tmp, "edubotics-pi-agent-2.tar.gz")
        for p in (old, fresh):
            with open(p, "wb") as f:
                f.write(b"x")
        # Age the "old" file well past the cutoff.
        stale = time.time() - 48 * 3600
        os.utime(old, (stale, stale))
        with patch("pi_agent.update_checker.tempfile.gettempdir", return_value=tmp):
            removed = update_checker.cleanup_stale_tarballs(max_age_hours=24)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.isfile(old))
        self.assertTrue(os.path.isfile(fresh))
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
