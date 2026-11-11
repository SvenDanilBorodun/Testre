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
    "LEGAL_IMPRESSUM_URL",
    "LEGAL_DATENSCHUTZ_URL",
    "LEGAL_NUTZUNGSBEDINGUNGEN_URL",
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

    def test_an_explicit_release_asset_of_the_repo_wins(self):
        # The exact shape release.yml W6 sets (another tag than GUI_VERSION is
        # allowed: the operator may pin an asset by hand).
        for url in ("https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
                    "https://github.com/Owner/Repo/releases/download/v2.12.2/EduBotics_Setup.exe"):
            with self.subTest(url=url):
                body = _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="owner/repo", GUI_DOWNLOAD_URL=url)
                self.assertEqual(body["download_url"], url)


class TestSa01TheUpdateChannelIsPinnedToTheReleaseRepo(unittest.TestCase):
    """SA-01 (2026-11-13): GUI_DOWNLOAD_URL used to win verbatim, so the update
    could point at any host — the first updater read it from a PUBLIC Supabase
    Storage bucket every signed-in user could upload into. An override is now
    served only as a release asset of GUI_RELEASE_REPO on github.com."""

    DERIVED = "https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe"
    BAD_URLS = (
        "https://fnnbysrjkfugsqzwcksd.supabase.co/storage/v1/object/public/releases/EduBotics_Setup.exe",
        "https://example.test/custom.exe",
        "http://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com/other/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com/owner/repo-fork/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com.evil.test/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com@evil.test/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com:8443/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe",
        "https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe?x=1",
        "https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe#x",
        "https://github.com/owner/repo/releases/download/../../evil/EduBotics_Setup.exe",
        "https://github.com/owner/repo/releases/download/v2.12.3/x/EduBotics_Setup.exe",
        "https://github.com/owner/repo/archive/v2.12.3/EduBotics_Setup.exe",
        "https://github.com/owner/repo/releases/download/v2.12.3/",
        "javascript:alert(1)",
    )

    def setUp(self):
        ver._warned.clear()

    def test_any_other_url_is_ignored_and_the_derived_one_served(self):
        for url in self.BAD_URLS:
            with self.subTest(url=url):
                body = _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="owner/repo", GUI_DOWNLOAD_URL=url)
                self.assertEqual(body["download_url"], self.DERIVED)

    def test_without_a_valid_repo_nothing_is_served(self):
        url = "https://github.com/owner/repo/releases/download/v2.12.3/EduBotics_Setup.exe"
        for repo in ({}, {"GUI_RELEASE_REPO": "not a repo"},
                     {"GUI_RELEASE_REPO": "owner/.."}, {"GUI_RELEASE_REPO": "-owner/repo"},
                     {"GUI_RELEASE_REPO": "owner/repo/extra"},
                     {"GUI_RELEASE_REPO": "evil.test/x"}):
            with self.subTest(repo=repo):
                body = _call(GUI_VERSION="2.12.3", GUI_DOWNLOAD_URL=url, **repo)
                self.assertIsNone(body["download_url"])
                self.assertIsNone(body["pi_agent_download_url"])

    def test_one_warning_per_process_naming_no_value(self):
        bad = self.BAD_URLS[0]
        with self.assertLogs(ver.logger, level="WARNING") as logs:
            for _ in range(3):
                _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="owner/repo", GUI_DOWNLOAD_URL=bad)
            _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="evil.test/x")
            _call(GUI_VERSION="2.12.3", GUI_RELEASE_REPO="evil.test/x")
        self.assertEqual(len(logs.output), 2, logs.output)
        for line in logs.output:
            self.assertNotIn("supabase", line)
            self.assertNotIn("evil", line)

    def test_the_release_workflow_sets_exactly_the_accepted_shape(self):
        root = os.path.dirname(os.path.dirname(APP_PARENT))  # the repository root
        wf = open(os.path.join(root, ".github", "workflows", "release.yml"), encoding="utf-8").read()
        self.assertIn('DOWNLOAD_URL="https://github.com/${GIT_REPO}/releases/download/${TAG}/EduBotics_Setup.exe"', wf)
        self.assertIn('railway variable set "GUI_RELEASE_REPO=${GIT_REPO}"', wf)

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


_LEGAL_KEYS = ("legal_impressum_url", "legal_datenschutz_url", "legal_nutzungsbedingungen_url")


class TestVersionLegalLinks(unittest.TestCase):
    """Launch plan step 4 (2026-11-11): the three legal links, additive keys
    every surface reads (app/services/legal_links.py)."""

    def test_keys_are_always_present_and_null_when_unset(self):
        body = _call()
        for key in _LEGAL_KEYS:
            self.assertIn(key, body)
            self.assertIsNone(body[key])
        self.assertTrue(all(body[k] is None for k in _LEGAL_KEYS))

    def test_each_set_address_is_served_on_its_own_key(self):
        body = _call(
            LEGAL_IMPRESSUM_URL="https://edubotics.example/impressum",
            LEGAL_NUTZUNGSBEDINGUNGEN_URL=" https://edubotics.example/agb#schule ",
        )
        self.assertEqual(body["legal_impressum_url"], "https://edubotics.example/impressum")
        self.assertIsNone(body["legal_datenschutz_url"])
        self.assertEqual(body["legal_nutzungsbedingungen_url"], "https://edubotics.example/agb#schule")

    def test_an_address_the_boot_would_refuse_is_never_served(self):
        body = _call(LEGAL_DATENSCHUTZ_URL="http://edubotics.example/datenschutz")
        self.assertIsNone(body["legal_datenschutz_url"])
        body = _call(LEGAL_DATENSCHUTZ_URL="javascript:alert(1)")
        self.assertIsNone(body["legal_datenschutz_url"])

    def test_the_older_keys_are_unchanged(self):
        body = _call()
        self.assertEqual(
            set(body),
            {"version", "download_url", "installer_sha256", "commit", "pi_agent_download_url", "pi_agent_sha256",
             *_LEGAL_KEYS},
        )


if __name__ == "__main__":
    unittest.main()
