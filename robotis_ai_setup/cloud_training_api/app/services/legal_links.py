# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The three legal links every surface shows (launch plan step 4, 2026-11-11).

The texts do not exist yet (the legal form is open), so their addresses are
configuration, not code: three Railway variables on the cloud API service,

    LEGAL_IMPRESSUM_URL            → ``legal_impressum_url``
    LEGAL_DATENSCHUTZ_URL          → ``legal_datenschutz_url``
    LEGAL_NUTZUNGSBEDINGUNGEN_URL  → ``legal_nutzungsbedingungen_url``

served as additive keys of the public ``GET /version`` (``app/routes/version.py``),
which the Windows GUI and the Orange Pi agent already fetch and every SPA can
read (``/version`` answers ``Access-Control-Allow-Origin: *``,
``app/edge.py::PublicReadCorsMiddleware``). A surface shows a link as soon as
its address is set and nothing while it is not; changing one needs a Railway
variable and the restart it triggers, never a rebuild.

An address must be an absolute ``https://`` URL with a host, no credentials,
only printable ASCII without whitespace, quotes, angle brackets, backslash or
backtick, and at most ``MAX_URL_CHARS`` characters. **An invalid one stops the
boot** (``validate_boot``, called from ``main.py``): the deploy then fails W2's
``/health`` gate while the previous one keeps serving, which is how a typo in
``ALLOWED_ORIGINS`` or the token key is caught too. A warning instead would
silently drop a legally required Impressum link that nobody notices. The
message names the variable, never the value. Empty or unset is valid: no link.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

# (Railway variable, /version key). Every client shows the links in this order,
# with its own German text („Impressum", „Datenschutz", „Nutzungsbedingungen"):
# React's features/legal/legalLinks.js and the GUI's update_checker.LEGAL_LINKS,
# both compared with this table by tests/test_legal_links_lockstep.py.
LEGAL_LINKS: tuple[tuple[str, str], ...] = (
    ("LEGAL_IMPRESSUM_URL", "legal_impressum_url"),
    ("LEGAL_DATENSCHUTZ_URL", "legal_datenschutz_url"),
    ("LEGAL_NUTZUNGSBEDINGUNGEN_URL", "legal_nutzungsbedingungen_url"),
)

MAX_URL_CHARS = 2048

# Printable ASCII (0x21-0x7E) minus the characters that could break out of an
# attribute, a header or a shell line: " ' < > \ ` and whitespace (excluded by
# the range). A non-ASCII host goes in as punycode, a path as %-escapes.
_ALLOWED_CHARS = re.compile(r"^[\x21-\x7e]+$")
_FORBIDDEN_CHARS = re.compile(r"[\"'<>\\`]")


def check_url(raw: str | None) -> str | None:
    """The address as served, or None for unset/empty. Raises ValueError (no
    value in the message) for anything that is not a clean https URL."""
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) > MAX_URL_CHARS:
        raise ValueError("longer than %d characters" % MAX_URL_CHARS)
    if not _ALLOWED_CHARS.match(value) or _FORBIDDEN_CHARS.search(value):
        raise ValueError("contains whitespace, a non-ASCII character, a quote, an angle bracket, a backslash or a backtick")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        raise ValueError("is not a parseable URL") from None
    if parts.scheme != "https":
        raise ValueError("is not an https:// URL")
    if not hostname:
        raise ValueError("has no host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("carries credentials")
    return value


def validate_boot() -> None:
    """Stop the boot on an invalid address (see the module docstring)."""
    for env_name, _key in LEGAL_LINKS:
        try:
            check_url(os.environ.get(env_name))
        except ValueError as exc:
            raise RuntimeError(
                f"{env_name} must be empty or an absolute https:// URL with a host "
                f"(no credentials, no whitespace or quotes, at most {MAX_URL_CHARS} "
                f"characters); it {exc}. Fix the Railway variable and redeploy."
            ) from None


def links() -> dict[str, str | None]:
    """The three ``/version`` keys, each the address or None. Read per request,
    like every other ``/version`` field; an address the boot would have refused
    (it cannot be set after the boot) is served as None, never as is."""
    out: dict[str, str | None] = {}
    for env_name, key in LEGAL_LINKS:
        try:
            out[key] = check_url(os.environ.get(env_name))
        except ValueError:
            out[key] = None
    return out
