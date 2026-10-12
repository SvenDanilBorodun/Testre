"""Cloud-Inferenz session backend: the container's view of a session row (spec §3.5, §4.2).

``SupabaseSessionBackend`` calls the four anon RPCs of migration 048 with the public anon key;
the connect token AND the Modal session id that Modal's proxy stamped on the request are the
credential (a row answers ``invalid`` unless both match). ``StaticSessionBackend`` is the dev
variant's and the tests' stand-in: static tokens, one model, the session id ignored; its ``from_env``
refuses to start unless the image's baked variant is ``dev`` (V1-5).

Every RPC sends ALL of its arguments, ``p_stats`` as null when there are none (PostgREST answers
404 / PGRST202 for an omitted one). A transport failure — HTTP >= 400, a timeout, the network,
a non-JSON or unexpected answer — is ``SessionDenied('unavailable')``; only ``invalid``,
``ended`` and ``replaced`` are verdicts about the session (D42, H-3). Logs carry the RPC name and
an exception TYPE, never a token, a header or a body.
"""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
import sys
import time

import httpx

_HERE = Path(__file__).resolve().parent
# NEVER `_HERE.parents[1]` unguarded: in the container this file is /root/session_backend.py and
# Path('/root').parents has ONE entry -> IndexError at import (the main.py `_safe_parent` trap).
_ROOT = _HERE.parents[1] if len(_HERE.parents) > 1 else None
_SHARED = _ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'inference' if _ROOT else None
if _SHARED is not None and _SHARED.is_dir() and str(_SHARED) not in sys.path:   # a checkout; container: /root
    sys.path.insert(0, str(_SHARED))
import cloud_protocol as P       # noqa: E402

log = logging.getLogger('edubotics.inference.session')

RPC_TIMEOUT_S = 5.0
DENIED_CODES = frozenset({'auth', 'ended', 'replaced', 'unavailable'})
VERDICT_CODES = frozenset({'auth', 'ended', 'replaced'})     # a check answering these ends the session
STATIC_MAX_SESSION_S = 3600.0        # V1-5: the dev soak §10.3(f) runs 35 min on one session
DEV_DERIVED_SESSIONS = 16            # V1-5: up to 17 robots on the dev app (§10.3(b)/(h) measure 4-15)
_DEV_TOKEN_LABEL = b'edubotics-inference-dev-session-'
_REASON_RE = re.compile(r'[a-z_]{1,64}')                   # an end reason fit for an error frame (≤ 64 chars)


@dataclass(frozen=True)
class Grant:
    session_id: str
    model_repo: str
    model_revision: str
    policy_type: str
    max_end_in_s: float | None


class SessionDenied(Exception):
    """``code`` in {'auth', 'ended', 'replaced', 'unavailable'}; ``reason`` = the DB end reason (with 'ended')."""

    def __init__(self, code, reason=None):
        super().__init__(code)
        self.code = code if code in DENIED_CODES else 'unavailable'
        self.reason = reason if type(reason) is str and _REASON_RE.fullmatch(reason) else None


def _session_id_ok(modal_session_id):
    return type(modal_session_id) is str and bool(P.MODAL_SESSION_ID_RE.fullmatch(modal_session_id))


def _number(v):
    return not isinstance(v, bool) and isinstance(v, (int, float))


def _verdict(data):
    """Raise the SessionDenied an answer that is not a grant means."""
    status = data.get('status')
    if status == 'invalid':
        raise SessionDenied('auth')
    if status == 'ended':
        raise SessionDenied('ended', data.get('end_reason'))
    if status == 'replaced':
        raise SessionDenied('replaced')
    raise SessionDenied('unavailable')


def _grant(data, *, with_clock):
    fields = (data.get('session_id'), data.get('model_repo'), data.get('model_revision'), data.get('policy_type'))
    if not all(type(f) is str for f in fields):
        raise SessionDenied('unavailable')
    max_end = data.get('max_end_in_s')
    if with_clock:
        if not _number(max_end):
            raise SessionDenied('unavailable')
        max_end = float(max_end)
    else:
        max_end = None
    return Grant(*fields, max_end)


class SupabaseSessionBackend:
    """The four anon RPCs over PostgREST (``POST {SUPABASE_URL}/rest/v1/rpc/<name>``)."""

    def __init__(self, url, anon_key, *, timeout=RPC_TIMEOUT_S, transport=None):
        self._url = url.rstrip('/')
        self._key = anon_key
        self._timeout = timeout
        self._transport = transport
        self._client = None

    @classmethod
    def from_env(cls):
        return cls(os.environ['SUPABASE_URL'], os.environ['SUPABASE_ANON_KEY'])

    def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _rpc(self, name, body):
        headers = {'apikey': self._key, 'Authorization': f'Bearer {self._key}', 'Content-Type': 'application/json'}
        try:
            r = await self._http().post(f'{self._url}/rest/v1/rpc/{name}', json=body, headers=headers)
        except Exception as e:  # noqa: BLE001 - any transport failure is 'unavailable'
            log.warning('session rpc %s failed: %s', name, type(e).__name__)
            raise SessionDenied('unavailable') from None
        if r.status_code >= 400:
            log.warning('session rpc %s answered HTTP %d', name, r.status_code)
            raise SessionDenied('unavailable')
        try:
            data = r.json()
        except ValueError:
            log.warning('session rpc %s answered no JSON', name)
            raise SessionDenied('unavailable') from None
        if not isinstance(data, dict):
            log.warning('session rpc %s answered no object', name)
            raise SessionDenied('unavailable')
        return data

    async def peek(self, token, modal_session_id):
        """The reservation's model; no state change (``reserved`` or ``active`` is a grant)."""
        if not _session_id_ok(modal_session_id):
            raise SessionDenied('auth')
        data = await self._rpc('inference_session_peek', {'p_token': token, 'p_modal_session_id': modal_session_id})
        if data.get('status') in ('reserved', 'active'):
            return _grant(data, with_clock=False)
        _verdict(data)

    async def connect(self, token, modal_session_id, conn_id):
        """Bind the run to ``conn_id`` (the clock starts once); idempotent for the same conn_id."""
        if not _session_id_ok(modal_session_id):
            raise SessionDenied('auth')
        data = await self._rpc('inference_session_connect', {'p_token': token, 'p_modal_session_id': modal_session_id,
                                                             'p_conn_id': conn_id})
        if data.get('status') == 'active':
            return _grant(data, with_clock=True)
        _verdict(data)

    async def heartbeat(self, token, modal_session_id, conn_id, stats):
        """The server heartbeat; returns ``max_end_in_s``."""
        if not _session_id_ok(modal_session_id):
            raise SessionDenied('auth')
        data = await self._rpc('inference_session_heartbeat', {
            'p_token': token, 'p_modal_session_id': modal_session_id, 'p_conn_id': conn_id,
            'p_stats': stats if stats else None})
        if data.get('status') == 'active':
            if not _number(data.get('max_end_in_s')):
                raise SessionDenied('unavailable')
            return float(data['max_end_in_s'])
        _verdict(data)

    async def end(self, token, modal_session_id, conn_id, reason, stats):
        """End the run with ``reason``. Never raises."""
        if not _session_id_ok(modal_session_id):
            return None
        try:
            await self._rpc('inference_session_end', {
                'p_token': token, 'p_modal_session_id': modal_session_id, 'p_conn_id': conn_id,
                'p_reason': reason, 'p_stats': stats if stats else None})
        except Exception as e:  # noqa: BLE001 - end never raises
            log.warning('session end failed: %s', type(e).__name__)
        return None


def dev_session_token(master, i):
    """The ``i``-th extra connect token of the dev app (1..DEV_DERIVED_SESSIONS), derived from the dev
    secret's ``DEV_SESSION_TOKEN``: unpadded urlsafe base64 of HMAC-SHA256(master, label + i), 43
    characters like ``secrets.token_urlsafe(32)``. A measurement script that holds the secret computes
    the same tokens; nobody else can (V1-5)."""
    if type(i) is not int or not 1 <= i <= DEV_DERIVED_SESSIONS:
        raise ValueError('dev session index out of range')
    digest = hmac.new(master.encode('utf-8'), _DEV_TOKEN_LABEL + str(i).encode('ascii'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


class _StaticRun:
    """One static token's session: its connection, its clock, its last end."""
    __slots__ = ('token', 'session_id', 'conn_id', 'started', 'ended')

    def __init__(self, token, session_id):
        self.token = token.encode('utf-8')
        self.session_id = session_id
        self.conn_id = None
        self.started = None
        self.ended = None           # (conn_id, reason) of the last ended run


class StaticSessionBackend:
    """Tests and the dev variant only: static tokens, one model; the Modal session id is IGNORED.

    Each token is its own session (its own run, clock and end), so several robots can share the
    dev app's containers (V1-5): ``token`` (session ``session_id``) and ``extra_tokens``
    (``session_id-1``, ``-2``, ...).
    """

    def __init__(self, token, model_repo, model_revision, *, max_session_s=STATIC_MAX_SESSION_S,
                 policy_type='act', session_id='static', clock=time.monotonic, extra_tokens=()):
        self._repo = model_repo
        self._revision = model_revision
        self._max_session_s = float(max_session_s)
        self._policy_type = policy_type
        self._clock = clock
        self._runs = [_StaticRun(token, session_id)]
        self._runs += [_StaticRun(t, f'{session_id}-{i}') for i, t in enumerate(extra_tokens, start=1)]

    @classmethod
    def from_env(cls):
        """The dev variant's backend; refuses to start in any other image (the prod image bakes
        ``EDUBOTICS_INFERENCE_VARIANT=prod``). The messages never carry the token."""
        if os.environ.get('EDUBOTICS_INFERENCE_VARIANT') != 'dev':
            raise RuntimeError('the static session backend runs only in the dev variant')
        master = os.environ['DEV_SESSION_TOKEN']
        if not P.TOKEN_RE.fullmatch(master):
            raise RuntimeError('DEV_SESSION_TOKEN is not a well-formed connect token')
        extra = [dev_session_token(master, i) for i in range(1, DEV_DERIVED_SESSIONS + 1)]
        return cls(master, os.environ['DEV_MODEL_REPO'], os.environ['DEV_MODEL_REVISION'], extra_tokens=extra)

    def _run(self, token):
        if type(token) is not str:
            raise SessionDenied('auth')
        given = token.encode('utf-8')
        found = None
        for run in self._runs:                       # every token compared, constant time each
            if hmac.compare_digest(given, run.token):
                found = run
        if found is None:
            raise SessionDenied('auth')
        return found

    def _grant(self, run, max_end_in_s):
        return Grant(run.session_id, self._repo, self._revision, self._policy_type, max_end_in_s)

    def _remaining(self, run):
        return max(0.0, self._max_session_s - (self._clock() - run.started))

    def ended(self, token):
        """``(conn_id, reason)`` of the token's last ended run, or None (tests and dev measurements)."""
        try:
            return self._run(token).ended
        except SessionDenied:
            return None

    async def peek(self, token, modal_session_id):
        return self._grant(self._run(token), None)

    async def connect(self, token, modal_session_id, conn_id):
        run = self._run(token)
        if conn_id != run.conn_id or run.ended is not None:    # a new run (or a take-over) starts the clock
            run.started = self._clock()
            run.ended = None
        run.conn_id = conn_id
        return self._grant(run, self._remaining(run))

    async def heartbeat(self, token, modal_session_id, conn_id, stats):
        run = self._run(token)
        if run.ended is not None and run.ended[0] == conn_id:
            raise SessionDenied('ended', run.ended[1])
        if conn_id != run.conn_id:
            raise SessionDenied('replaced')
        remaining = self._remaining(run)
        if remaining <= 0:
            run.ended = (conn_id, 'expired')
            raise SessionDenied('ended', 'expired')
        return remaining

    async def end(self, token, modal_session_id, conn_id, reason, stats):
        try:
            run = self._run(token)
        except SessionDenied:
            return None
        if conn_id == run.conn_id and run.ended is None:
            run.ended = (conn_id, reason)
        return None
