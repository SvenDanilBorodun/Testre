"""Unit tests for the GUI self-update reliability hardening.

Covers the two fixes that close the self-updater's edge-case strand paths the
2026-06-08 review surfaced:
  1. `_parse_version` is tolerant (a non-numeric / pre-release tag no longer
     silently disables ALL updates; short forms pad instead of mis-comparing).
  2. `check_for_update` HEAD-pre-checks the installer asset so a 404 asset can
     never open the forced, non-closable update modal (no mass lockout).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gui.app import update_checker  # noqa: E402


class TestParseVersion(unittest.TestCase):
    def test_plain_numeric(self):
        self.assertEqual(update_checker._parse_version("2.8.1"), (2, 8, 1))

    def test_v_prefix(self):
        self.assertEqual(update_checker._parse_version("v2.8.1"), (2, 8, 1))

    def test_prerelease_suffix_does_not_raise(self):
        # The regression: previously int('1-rc1') raised → caller returned None
        # → "GUI ist aktuell" forever. Now it compares by the numeric triplet.
        self.assertEqual(update_checker._parse_version("2.8.1-rc1"), (2, 8, 1))
        self.assertEqual(update_checker._parse_version("v2.8.1+build7"), (2, 8, 1))

    def test_short_form_padded(self):
        self.assertEqual(update_checker._parse_version("2.8"), (2, 8, 0))
        # (2,8) vs (2,8,0) used to compare False; padding fixes it.
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
        self.assertGreater(P("2.8.1"), P("2.8.0"))
        self.assertFalse(P("2.8.0") > P("2.8.0"))
        self.assertFalse(P("2.8.0") > P("2.9.0"))


def _cm(body: bytes = b""):
    """A context-manager mock mimicking urlopen()'s response."""
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class TestCheckForUpdate(unittest.TestCase):
    API = "https://api.test"
    URL = "https://github.com/o/r/releases/download/v2.9.0/EduBotics_Setup.exe"

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
        payload = {"version": "2.9.0", "download_url": self.URL}
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            r = update_checker.check_for_update("2.8.0", self.API)
        self.assertEqual(r, {"version": "2.9.0", "download_url": self.URL})

    def test_newer_but_asset_404_returns_none(self):
        # The lockout fix: a missing asset must NOT open the forced modal.
        payload = {"version": "2.9.0", "download_url": self.URL}
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, 404)):
            r = update_checker.check_for_update("2.8.0", self.API)
        self.assertIsNone(r)

    def test_newer_with_head_network_error_still_updates(self):
        # Can't prove absence (network/405) -> proceed (download+skip handle it).
        payload = {"version": "2.9.0", "download_url": self.URL}
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "neterr")):
            r = update_checker.check_for_update("2.8.0", self.API)
        self.assertIsNotNone(r)

    def test_prerelease_remote_tag_now_compares(self):
        # Previously raised inside check_for_update -> None (silent no-update).
        payload = {"version": "2.9.0-rc1", "download_url": self.URL}
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            r = update_checker.check_for_update("2.8.0", self.API)
        self.assertIsNotNone(r)

    def test_not_newer_returns_none(self):
        payload = {"version": "2.8.0", "download_url": self.URL}
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch(payload, "ok")):
            self.assertIsNone(update_checker.check_for_update("2.8.0", self.API))

    def test_missing_fields_returns_none(self):
        with patch("gui.app.update_checker.urllib.request.urlopen",
                   side_effect=self._dispatch({"version": "", "download_url": ""}, "ok")):
            self.assertIsNone(update_checker.check_for_update("2.8.0", self.API))


if __name__ == "__main__":
    unittest.main()
