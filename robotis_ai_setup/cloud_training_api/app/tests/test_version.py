"""Tests for app.routes.version — the /version endpoint consumed by BOTH the
Windows GUI's update_checker and the Orange Pi agent's update_checker.

Locks in two contracts:

  * The pre-existing .exe fields (version / download_url / installer_sha256 /
    commit) still behave: explicit-null when unset, derived-from-repo when set,
    malformed sha rejected.
  * The NEW, OPTIONAL Orange Pi fields (deploy plan §7) use the EXACT keys the
    agent reads — ``pi_agent_download_url`` + ``pi_agent_sha256`` — derived the
    same way as the .exe pair (release repo + version + the fixed asset name
    ``edubotics-pi-agent.tar.gz``, and PI_AGENT_SHA256 with empty-on-failure /
    never-stale semantics). A drift in either key or the asset name would
    silently break every Pi's self-update, so it is pinned here.

Follows the house pattern: stub ``fastapi`` in sys.modules (a pass-through
APIRouter) so the route module imports without the real dependency. The route
body is pure ``os.environ`` + string derivation — no supabase/pydantic — so
nothing else needs stubbing. The async handler is driven via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from unittest import mock


def _ensure_stubs() -> None:
    import app  # noqa: F401
    import app.routes  # noqa: F401

    if "fastapi" not in sys.modules:
        m = types.ModuleType("fastapi")

        class _Router:
            def __init__(self, *a, **k):
                pass

            def _deco(self, *a, **k):
                def d(fn):
                    return fn

                return d

            get = post = patch = delete = put = _deco

        class _HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail or "")
                self.status_code = status_code
                self.detail = detail

        m.APIRouter = _Router
        m.HTTPException = _HTTPException
        m.Depends = lambda *a, **k: None
        m.Query = lambda *a, **k: None
        m.Path = lambda *a, **k: None
        m.Body = lambda *a, **k: None
        m.Header = lambda *a, **k: None
        sys.modules["fastapi"] = m


HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

_ensure_stubs()

from app.routes import version as ver  # noqa: E402


# Every env var the /version handler reads; each test starts from a clean slate
# so a real Railway env leaking into CI can't skew a "should be null" assertion.
_VERSION_ENV_KEYS = (
    "GUI_VERSION",
    "GUI_DOWNLOAD_URL",
    "GUI_RELEASE_REPO",
    "GUI_INSTALLER_SHA256",
    "PI_AGENT_SHA256",
    "RAILWAY_GIT_COMMIT_SHA",
    "BUILD_COMMIT",
)

_GOOD_SHA = "a" * 64
_OTHER_SHA = "b" * 64


def _call(**env):
    """Invoke the async /version handler with EXACTLY the given env for the
    version-relevant keys (all others cleared). Restores os.environ on exit."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for k in _VERSION_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return asyncio.run(ver.get_latest_version())


class TestVersionExeFields(unittest.TestCase):
    def test_all_unset_returns_explicit_nulls(self):
        body = _call()
        self.assertIsNone(body["version"])
        self.assertIsNone(body["download_url"])
        self.assertIsNone(body["installer_sha256"])
        self.assertEqual(body["commit"], "unknown")

    def test_download_url_derives_from_repo_and_version(self):
        body = _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="owner/repo")
        self.assertEqual(body["version"], "2.12.3")
        self.assertEqual(
            body["download_url"],
            "https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        )

    def test_explicit_download_url_wins(self):
        body = _call(
            GUI_VERSION="2.12.3",
            GUI_RELEASE_REPO="owner/repo",
            GUI_DOWNLOAD_URL="https://example.test/custom.exe",
        )
        self.assertEqual(body["download_url"], "https://example.test/custom.exe")

    def test_installer_sha_malformed_is_none(self):
        self.assertIsNone(_call(GUI_INSTALLER_SHA256="not-a-hash")["installer_sha256"])
        self.assertIsNone(_call(GUI_INSTALLER_SHA256="abc")["installer_sha256"])

    def test_installer_sha_valid_is_lowercased(self):
        body = _call(GUI_INSTALLER_SHA256=_GOOD_SHA.upper())
        self.assertEqual(body["installer_sha256"], _GOOD_SHA)

    def test_commit_prefers_railway_sha(self):
        body = _call(RAILWAY_GIT_COMMIT_SHA="deadbeef", BUILD_COMMIT="fallback")
        self.assertEqual(body["commit"], "deadbeef")


class TestVersionPiAgentFields(unittest.TestCase):
    def test_keys_are_always_present(self):
        # The agent reads these EXACT keys; they must exist on every response
        # (as None when unconfigured), never be conditionally omitted.
        body = _call()
        self.assertIn("pi_agent_download_url", body)
        self.assertIn("pi_agent_sha256", body)
        self.assertIsNone(body["pi_agent_download_url"])
        self.assertIsNone(body["pi_agent_sha256"])

    def test_download_url_derives_with_fixed_asset_name(self):
        body = _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="owner/repo")
        self.assertEqual(
            body["pi_agent_download_url"],
            "https://github.com/owner/repo/releases/download/"
            "v2.12.3/edubotics-pi-agent.tar.gz",
        )

    def test_asset_name_matches_agent_constant(self):
        # Byte-for-byte contract with pi_agent/update_checker.py::AGENT_ASSET_NAME.
        self.assertEqual(ver._PI_AGENT_ASSET, "edubotics-pi-agent.tar.gz")

    def test_download_url_none_without_repo(self):
        # Version set but repo unset → cannot derive → None (no explicit
        # override env exists for the tarball, by design).
        self.assertIsNone(_call(GUI_VERSION="2.12.3")["pi_agent_download_url"])

    def test_download_url_none_without_version(self):
        self.assertIsNone(_call(GUI_RELEASE_REPO="owner/repo")["pi_agent_download_url"])

    def test_sha_valid_lowercased(self):
        body = _call(PI_AGENT_SHA256=_GOOD_SHA.upper())
        self.assertEqual(body["pi_agent_sha256"], _GOOD_SHA)

    def test_sha_malformed_is_none(self):
        self.assertIsNone(_call(PI_AGENT_SHA256="nope")["pi_agent_sha256"])
        self.assertIsNone(_call(PI_AGENT_SHA256=_GOOD_SHA + "extra")["pi_agent_sha256"])

    def test_pi_sha_independent_of_installer_sha(self):
        # W6 sets both vars separately; a stale one must not leak into the other.
        body = _call(GUI_INSTALLER_SHA256=_GOOD_SHA, PI_AGENT_SHA256=_OTHER_SHA)
        self.assertEqual(body["installer_sha256"], _GOOD_SHA)
        self.assertEqual(body["pi_agent_sha256"], _OTHER_SHA)

    def test_full_release_shape(self):
        # The exact env W6 sets on a real tag release → both surfaces advertised.
        body = _call(
            GUI_VERSION="2.13.0",
            GUI_RELEASE_REPO="svendanilborodun/edubotics",
            GUI_INSTALLER_SHA256=_GOOD_SHA,
            PI_AGENT_SHA256=_OTHER_SHA,
            RAILWAY_GIT_COMMIT_SHA="cafe1234",
        )
        self.assertEqual(body["version"], "2.13.0")
        self.assertTrue(body["download_url"].endswith("/v2.13.0/EduBotics_Setup.exe"))
        self.assertTrue(
            body["pi_agent_download_url"].endswith("/v2.13.0/edubotics-pi-agent.tar.gz")
        )
        self.assertEqual(body["installer_sha256"], _GOOD_SHA)
        self.assertEqual(body["pi_agent_sha256"], _OTHER_SHA)
        self.assertEqual(body["commit"], "cafe1234")


if __name__ == "__main__":
    unittest.main()
