# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Orange Pi's robot login — sessions, the one-time setup code, the cloud check.

Before this module every browser↔robot transport on the Pi's port 80 was open
to anyone on the school network: rosbridge (drive the arm), the MJPEG camera
stream, the dataset sidecar and the agent's management API (factory reset
included). Now each of them needs a ROBOT SESSION, an opaque random cookie this
agent mints after an EduBotics login:

* ``POST /auth/session`` — the browser hands over its Supabase access token
  ONCE (``Authorization: Bearer …``); the agent asks the cloud API ``GET /me``
  with exactly that token, which proves the token and names the role, and then
  forgets it. Never stored, never logged, never forwarded anywhere else.
* the cookie (``COOKIE_NAME``) is 256 random bits, ``HttpOnly``,
  ``SameSite=Strict``, ``Path=/``, and short-lived (``SESSION_TTL_S``, capped by
  the token's own ``exp``); the SPA re-exchanges before it runs out.
* sessions live in memory only: an agent restart (a self-update, a reboot) logs
  everyone out, and the SPA silently exchanges again with its Supabase token.

A Pi that has never been set up is UNCLAIMED: no session can exist and every
robot route refuses, until a teacher or admin claims it with the one-time
setup code (``POST /auth/claim``). The code is stored only as a salted
PBKDF2-HMAC-SHA256 hash, compared in constant time, voided after
``CODE_MAX_FAILURES`` wrong attempts and spent by a successful claim; a factory
reset returns the Pi to unclaimed with a new code.

stdlib only, like the rest of the agent (``requirements.txt``). Nothing here
ever writes a token, a session id or a setup code into a log line: the only
place a code is shown is the response/terminal of whoever minted it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from typing import Callable, Optional

# ── wire constants (mirrored by physical_ai_manager/src/features/piAuth) ────

# The session cookie. The SPA never reads it (HttpOnly); it only needs the name
# to stay stable across agent versions.
COOKIE_NAME = "edubotics_pi_session"

# How long a session lives at most. The SPA re-exchanges at 60 % of what the
# exchange answered, on every Supabase token refresh and on any 401, so a
# student never sees the window close. Short on purpose (owner decision 4):
# there is no TLS, so a cookie read off the wire is worth at most this long.
SESSION_TTL_S = 15 * 60
# Floor for a session bound by a nearly expired token: the cloud just accepted
# the token, so it is valid; a session shorter than this would make the SPA
# re-exchange in a tight loop.
SESSION_MIN_TTL_S = 30
# Upper bound on live sessions (a class of 30 with a few tabs each is far
# below it); the oldest is dropped when it is reached, never an error.
MAX_SESSIONS = 512

# The setup code: 8 characters from an alphabet without 0/O, 1/I/L, shown as
# XXXX-XXXX (31^8 ≈ 2^39.6). Brute force is bounded three times over: a claim
# needs a teacher/admin login first, every attempt is rate limited per client,
# and CODE_MAX_FAILURES wrong attempts void the code.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
CODE_MAX_FAILURES = 20
# PBKDF2 so a stolen state file does not hand the 40-bit code over in minutes.
CODE_PBKDF2_ITERATIONS = 200_000

# Roles the cloud answers in GET /me. Any of them may use a claimed Pi (owner
# decision 2); only these may claim one (owner decision 3).
ROLES = frozenset({"student", "teacher", "admin"})
CLAIM_ROLES = frozenset({"teacher", "admin"})

# The cloud call made once per exchange/claim.
CLOUD_TIMEOUT_S = 8.0
# A Supabase access token is a JWT of a few hundred bytes; anything far larger
# is not one and is refused before any network call.
TOKEN_MAX_LEN = 8192
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
# secrets.token_urlsafe(32) → 43 characters of the URL-safe alphabet.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

STATE_VERSION = 1

# Where the claim state lives. Resolved at CALL time (never at import), so a
# test module can point it at a temp dir before it builds an AgentApp.
_DEFAULT_STATE_FILE = "/var/lib/edubotics/pi_auth.json"


def default_state_file() -> str:
    return os.environ.get("EDUBOTICS_PI_AUTH_FILE") or _DEFAULT_STATE_FILE


# ── small pure helpers ───────────────────────────────────────────────────────


def generate_code() -> str:
    """A fresh setup code (the bare 8 characters, no hyphen)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def format_code(code: str) -> str:
    """``ABCDEFGH`` → ``ABCD-EFGH`` (what a teacher reads and types)."""
    half = CODE_LEN // 2
    return f"{code[:half]}-{code[half:]}"


def normalize_code(raw) -> Optional[str]:
    """What the teacher typed → the bare code, or None when it cannot be one.

    Case, spaces and hyphens do not matter. A malformed entry is refused
    WITHOUT counting as a failed attempt: the shape is public, so refusing it
    tells an attacker nothing, and a typo must not burn the teacher's budget.
    """
    if not isinstance(raw, str):
        return None
    s = "".join(ch for ch in raw.upper() if ch not in " -\t")
    if len(s) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in s):
        return None
    return s


def hash_code(code: str, salt: bytes, iterations: int = CODE_PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, iterations).hex()


def bearer_token(header) -> Optional[str]:
    """The token of an ``Authorization: Bearer <jwt>`` header, or None.

    Shape-checked before any network call; the value itself is never echoed.
    """
    if not isinstance(header, str):
        return None
    if not header[:7].lower() == "bearer ":
        return None
    token = header[7:].strip()
    if not token or len(token) > TOKEN_MAX_LEN or not _JWT_RE.match(token):
        return None
    return token


def jwt_exp(token: str) -> Optional[int]:
    """The ``exp`` claim of a JWT, read WITHOUT verification.

    Only ever consulted after the cloud accepted the very same token, so the
    claim is the issuer's; it merely shortens a session, it can never lengthen
    one past SESSION_TTL_S.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp") if isinstance(data, dict) else None
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            return None
        return int(exp)
    except Exception:  # noqa: BLE001 — any malformed payload is just "no exp"
        return None


def session_ttl(token: str, wall: Callable[[], float] = time.time) -> int:
    """How long a session minted for ``token`` may live (seconds).

    SESSION_TTL_S, shortened to the token's remaining lifetime when that is
    plausible. A Pi with a skewed clock (exp already "past", or absurdly far
    out) keeps the fixed window instead of locking everybody out.
    """
    exp = jwt_exp(token)
    if exp is None:
        return SESSION_TTL_S
    remaining = exp - wall()
    if 0 < remaining < SESSION_TTL_S:
        return max(SESSION_MIN_TTL_S, int(remaining))
    return SESSION_TTL_S


def parse_session_cookie(header) -> Optional[str]:
    """The session id from a ``Cookie`` header, or None (shape-checked)."""
    if not isinstance(header, str) or not header:
        return None
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == COOKIE_NAME:
            value = value.strip().strip('"')
            return value if _SID_RE.match(value) else None
    return None


def session_cookie_header(sid: str, max_age: int) -> str:
    return (f"{COOKIE_NAME}={sid}; Max-Age={int(max_age)}; Path=/; "
            "HttpOnly; SameSite=Strict")


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"


def _sid_key(sid: str) -> str:
    """Sessions are stored under the HASH of their id: a memory dump or a debug
    print of the store never shows a usable cookie."""
    return hashlib.sha256(sid.encode("ascii")).hexdigest()


# ── the cloud check ──────────────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. A captive portal or a hijacked DNS name answers
    the /me call with a 3xx; following it would hand the student's token to
    that host (urllib copies ordinary headers onto the redirected request)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def verify_with_cloud(token: str, api_url: str, timeout: float = CLOUD_TIMEOUT_S,
                      opener=None) -> "tuple[str, Optional[str]]":
    """Ask the cloud API who ``token`` belongs to: ``("ok", role)``,
    ``("rejected", None)`` when the cloud judged the token, or
    ``("unreachable", None)`` when it could not be asked.

    One ``GET {api_url}/me`` with the token as a NON-redirected header. Nothing
    of the token, the URL's answer body or an exception text is logged or
    returned — the caller gets one of three words and a role.
    """
    open_ = opener.open if opener is not None else _OPENER.open
    req = urllib.request.Request(f"{api_url.rstrip('/')}/me", method="GET")
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with open_(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read(256 * 1024)
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0) or 0
        try:
            e.close()
        except Exception:  # noqa: BLE001
            pass
        # 401/403: the cloud judged the token. 404: a valid login without a
        # profile row — not an EduBotics account the Pi can serve.
        if code in (401, 403, 404):
            return "rejected", None
        return "unreachable", None
    except Exception:  # noqa: BLE001 — URLError, timeout, TLS, reset: "could not ask"
        return "unreachable", None
    if status != 200:
        return "unreachable", None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # A 200 that is not our JSON (a portal page) is not an answer.
        return "unreachable", None
    role = data.get("role") if isinstance(data, dict) else None
    if not isinstance(role, str) or role not in ROLES:
        return "rejected", None
    return "ok", role


# ── rate limiting ────────────────────────────────────────────────────────────


class RateLimiter:
    """Sliding-window limiter keyed by client. Bounded memory: at most
    ``max_keys`` clients are tracked; the least recently seen is dropped."""

    def __init__(self, limit: int, window_s: float, max_keys: int = 4096,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.limit = int(limit)
        self.window_s = float(window_s)
        self.max_keys = int(max_keys)
        self._now = now
        self._hits: "OrderedDict[str, deque]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque()
                self._hits[key] = q
                while len(self._hits) > self.max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


# ── the store ────────────────────────────────────────────────────────────────


class AuthStore:
    """Claim state (persisted) + live sessions (memory only).

    Thread-safe; every mutation holds ``_lock``. The PBKDF2 of a claim attempt
    is computed OUTSIDE the lock so a guess never stalls the session checks
    nginx makes for every camera frame request.
    """

    def __init__(self, path: Optional[str] = None,
                 now: Callable[[], float] = time.monotonic,
                 iterations: int = CODE_PBKDF2_ITERATIONS) -> None:
        self.path = path or default_state_file()
        self._now = now
        self._iterations = int(iterations)
        self._lock = threading.Lock()
        self._sessions: dict = {}
        self._state = self._empty_state()
        # True when the file existed but could not be read: the Pi then counts
        # as UNCLAIMED (fail closed — nothing opens, a new code is needed), and
        # the agent says so in German instead of silently forgetting the claim.
        self.load_failed = False
        self.load()

    # ── persistence ──────────────────────────────────────────────────────

    @staticmethod
    def _empty_state() -> dict:
        return {"v": STATE_VERSION, "claimed": False, "claimed_at": None,
                "claimed_role": None, "code": None}

    def load(self) -> None:
        state = self._empty_state()
        failed = False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = None
        except (OSError, ValueError):
            data = None
            failed = True
        if isinstance(data, dict):
            state["claimed"] = data.get("claimed") is True
            if isinstance(data.get("claimed_at"), str):
                state["claimed_at"] = data["claimed_at"]
            if data.get("claimed_role") in CLAIM_ROLES:
                state["claimed_role"] = data["claimed_role"]
            code = data.get("code")
            if (not state["claimed"] and isinstance(code, dict)
                    and isinstance(code.get("salt"), str)
                    and isinstance(code.get("hash"), str)
                    and isinstance(code.get("iterations"), int)
                    and code["iterations"] > 0):
                state["code"] = {
                    "salt": code["salt"], "hash": code["hash"],
                    "iterations": code["iterations"],
                    "failures": int(code.get("failures") or 0),
                    "void": code.get("void") is True,
                    "created_at": code.get("created_at") if isinstance(
                        code.get("created_at"), str) else None,
                }
        elif data is not None:
            failed = True
        with self._lock:
            self._state = state
            self.load_failed = failed

    def _persist_locked(self) -> None:
        """Atomic write (temp in the same dir, fsync, replace, fsync dir), 0600.
        Raises OSError on failure; the caller decides what the teacher reads."""
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".pi_auth.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    # ── claim state ──────────────────────────────────────────────────────

    @property
    def claimed(self) -> bool:
        with self._lock:
            return bool(self._state["claimed"])

    def code_status(self) -> str:
        """``none`` | ``active`` | ``void`` — for the claim screen's wording."""
        with self._lock:
            code = self._state["code"]
            if code is None:
                return "none"
            return "void" if code.get("void") else "active"

    def mint_code(self) -> str:
        """Replace the setup code with a fresh one and return it (bare).

        Only on an UNCLAIMED Pi (raises ``RuntimeError`` otherwise: a claimed Pi
        gets a new code only through a factory reset). Raises ``OSError`` when
        the state cannot be written — a code that is not on disk must never be
        shown, because it would not survive the next restart.
        """
        code = generate_code()
        salt = secrets.token_bytes(16)
        digest = hash_code(code, salt, self._iterations)
        with self._lock:
            if self._state["claimed"]:
                raise RuntimeError("claimed")
            previous = self._state["code"]
            self._state["code"] = {
                "salt": salt.hex(), "hash": digest, "iterations": self._iterations,
                "failures": 0, "void": False,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            try:
                self._persist_locked()
            except OSError:
                self._state["code"] = previous
                raise
        return code

    def try_claim(self, raw_code, role: str) -> str:
        """Check a setup code and claim on a match.

        Returns ``ok`` | ``malformed`` | ``wrong`` | ``void`` | ``no_code`` |
        ``claimed`` | ``write_failed``. ``wrong`` counts against the code's
        budget; the CODE_MAX_FAILURES-th wrong attempt voids it.
        """
        code = normalize_code(raw_code)
        if code is None:
            return "malformed"
        with self._lock:
            if self._state["claimed"]:
                return "claimed"
            rec = self._state["code"]
            if rec is None:
                return "no_code"
            if rec.get("void"):
                return "void"
            salt_hex, expected, iterations = rec["salt"], rec["hash"], rec["iterations"]
        try:
            candidate = hash_code(code, bytes.fromhex(salt_hex), iterations)
        except ValueError:
            candidate = ""
        match = bool(candidate) and hmac.compare_digest(candidate, expected)
        with self._lock:
            rec = self._state["code"]
            # The code was replaced (or the Pi claimed) while we hashed: judge
            # nothing against a record that is no longer the current one.
            if self._state["claimed"]:
                return "claimed"
            if rec is None or rec["salt"] != salt_hex:
                return "no_code" if rec is None else "wrong"
            if not match:
                rec["failures"] = int(rec.get("failures") or 0) + 1
                if rec["failures"] >= CODE_MAX_FAILURES:
                    rec["void"] = True
                try:
                    self._persist_locked()
                except OSError:
                    pass  # the in-memory count still binds this process
                return "void" if rec["void"] else "wrong"
            previous = dict(self._state)
            self._state["claimed"] = True
            self._state["claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._state["claimed_role"] = role if role in CLAIM_ROLES else None
            self._state["code"] = None  # spent
            try:
                self._persist_locked()
            except OSError:
                self._state = previous
                return "write_failed"
            return "ok"

    def reset_to_unclaimed(self) -> str:
        """Factory reset: revoke EVERY session, forget the claim, mint a new
        code and return it. Raises OSError when the new state cannot be written
        (the sessions are revoked regardless)."""
        with self._lock:
            self._sessions.clear()
            previous = dict(self._state)
            self._state = self._empty_state()
            try:
                self._persist_locked()
            except OSError:
                self._state = previous
                raise
        return self.mint_code()

    # ── sessions ─────────────────────────────────────────────────────────

    def create_session(self, role: str, ttl_s: int) -> str:
        sid = secrets.token_urlsafe(32)
        now = self._now()
        with self._lock:
            self._prune_locked(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["created"])
                del self._sessions[oldest]
            self._sessions[_sid_key(sid)] = {
                "role": role, "created": now, "expires": now + max(1, int(ttl_s)),
            }
        return sid

    def session(self, sid: Optional[str]) -> Optional[dict]:
        """The live session for ``sid`` (a copy with ``expires_in``), or None.
        An unclaimed Pi has no sessions by construction."""
        if not sid:
            return None
        now = self._now()
        with self._lock:
            rec = self._sessions.get(_sid_key(sid))
            if rec is None:
                return None
            if rec["expires"] <= now:
                del self._sessions[_sid_key(sid)]
                return None
            if not self._state["claimed"]:
                return None
            return {"role": rec["role"], "expires_in": int(rec["expires"] - now)}

    def revoke(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(_sid_key(sid), None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def session_count(self) -> int:
        with self._lock:
            self._prune_locked(self._now())
            return len(self._sessions)

    def _prune_locked(self, now: float) -> None:
        for key in [k for k, v in self._sessions.items() if v["expires"] <= now]:
            del self._sessions[key]
