# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""One-time Orange Pi login tickets (SA-06, 2026-11-13).

Until now the Pi's login sent the browser's Supabase access token to the Pi
over plain HTTP (``POST /auth/session``, ``Authorization: Bearer …``), and the
agent asked the cloud's ``GET /me`` with it. Anyone listening on the school
network read a token that opens the student's whole cloud account for up to an
hour (``POST /me/hf-token/reveal`` included). Now the token never crosses the
LAN:

* the browser asks THIS API over HTTPS for a ticket (``POST /pi/tickets`` with
  its access token and the Pi's id, ``mint``): 256 random bits, valid for
  ``TICKET_TTL_S``, bound to that Pi;
* the browser hands only the ticket to the Pi, and the Pi redeems it here
  (``POST /pi/tickets/redeem``, ``redeem``): single-use, the Pi's id must match,
  and the answer is who the ticket belongs to (user id, role) and how long a
  session for it may live (``session_max_s``: the access token's remaining life
  at redeem time, at most ``SESSION_MAX_S``).

A ticket read off the wire is worth little: it dies on its first redeem (the
Pi's, normally within milliseconds) or after a minute, opens one Pi, and its
redeem answers no token. What an ACTIVE attacker on the LAN can still do (race
the Pi with a sniffed ticket, rewrite the page the Pi serves) is an accepted
residual (docs/KNOWN-ISSUES.md).

The store is in MEMORY (the API runs one uvicorn worker, CLAUDE.md Rule §6):
an API restart forgets every ticket, and during a blue-green deploy a ticket
minted on the old instance is unknown to the new one. The browser then simply
mints a new one (``features/piAuth/piAuth.js``). Stored under the sha256 of the
ticket, so a memory dump never shows a usable one; a lookup by that hash is a
dict lookup whose timing says nothing about the ticket. Pure stdlib and
thread-safe (the routes are plain ``def`` handlers in FastAPI's threadpool).
Nothing here logs a ticket.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Callable, Optional

# How long a ticket may wait for its redeem. The browser posts it to the Pi at
# once and the Pi redeems at once; a minute covers a slow school link.
TICKET_TTL_S = 60
# The longest robot session a ticket may open (the Pi's own SESSION_TTL_S).
SESSION_MAX_S = 15 * 60
# Bounded memory: a burst beyond this drops the OLDEST tickets (their owners
# simply mint again). A class of 30 needs a few dozen at any one time.
TICKETS_MAX = 8192

ROLES = frozenset({"student", "teacher", "admin"})

# The Pi's id: secrets.token_urlsafe(16) on the Pi (22 characters); anything of
# this alphabet and length is accepted, so a future Pi may choose a longer one.
PI_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
# secrets.token_urlsafe(32) → 43 characters of the URL-safe alphabet.
TICKET_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def valid_pi_id(value) -> bool:
    return isinstance(value, str) and PI_ID_RE.fullmatch(value) is not None


def valid_ticket(value) -> bool:
    return isinstance(value, str) and TICKET_RE.fullmatch(value) is not None


def _key(ticket: str) -> str:
    return hashlib.sha256(("edubotics-pi-ticket:" + ticket).encode("ascii")).hexdigest()


class TicketStore:
    """The in-memory ticket book. ``now`` is monotonic (expiry), ``wall`` is
    the epoch clock the access token's ``exp`` is measured in."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, max_tickets: int = TICKETS_MAX) -> None:
        self._now = now
        self._wall = wall
        self._max = int(max_tickets)
        self._lock = threading.Lock()
        self._tickets: "OrderedDict[str, dict]" = OrderedDict()

    def mint(self, *, user_id: str, role: str, pi_id: str, token_exp: Optional[float]) -> str:
        """A new ticket for ``user_id`` (``role``) at the Pi ``pi_id``.
        ``token_exp`` is the access token's ``exp`` (epoch seconds), or None
        when the token carries none (then a session gets SESSION_MAX_S)."""
        if role not in ROLES:
            raise ValueError("role")
        if not valid_pi_id(pi_id):
            raise ValueError("pi_id")
        ticket = secrets.token_urlsafe(32)
        now = self._now()
        record = {
            "user_id": str(user_id),
            "role": role,
            "pi_id": pi_id,
            "expires": now + TICKET_TTL_S,
            "token_exp": float(token_exp) if isinstance(token_exp, (int, float))
            and not isinstance(token_exp, bool) else None,
        }
        with self._lock:
            self._prune_locked(now)
            self._tickets[_key(ticket)] = record
            while len(self._tickets) > self._max:
                self._tickets.popitem(last=False)
        return ticket

    def redeem(self, ticket, pi_id) -> Optional[dict]:
        """``{"user_id", "role", "session_max_s"}`` for a live ticket of this
        Pi, else None. EVERY redeem attempt spends the ticket (a wrong Pi id
        included), so a ticket is never tried twice."""
        if not valid_ticket(ticket) or not valid_pi_id(pi_id):
            return None
        now = self._now()
        with self._lock:
            record = self._tickets.pop(_key(ticket), None)
        if record is None or record["expires"] <= now:
            return None
        if not hmac.compare_digest(record["pi_id"].encode("ascii"), pi_id.encode("ascii")):
            return None
        if record["token_exp"] is None:
            remaining = SESSION_MAX_S
        else:
            remaining = int(record["token_exp"] - self._wall())
            if remaining <= 0:
                # The access token ran out between mint and redeem: the browser
                # gets a new one from supabase-js and mints again.
                return None
        return {
            "user_id": record["user_id"],
            "role": record["role"],
            "session_max_s": min(SESSION_MAX_S, remaining),
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._tickets)

    def _prune_locked(self, now: float) -> None:
        # Every ticket has the same life and the book is in mint order, so the
        # expired ones are exactly a prefix.
        while self._tickets:
            first = next(iter(self._tickets.values()))
            if first["expires"] > now:
                break
            self._tickets.popitem(last=False)


STORE = TicketStore()
