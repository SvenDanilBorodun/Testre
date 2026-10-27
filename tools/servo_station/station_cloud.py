"""The Servo-Station's cloud upload (spec S9, §5A „Cloud").

The bench PC never holds a service-role key. The station signs in as an ADMIN
account over Supabase Auth's password grant (``<username>@edubotics.local``,
the cloud API's synthetic-email scheme) and calls migration 052's
``submit_servo_calibration_record`` RPC with that JWT; the RPC refuses every
non-admin with 42501. The access and refresh tokens live in this object's
memory only; the password is used for the one sign-in request and kept
nowhere. Nothing here logs, prints or puts into an exception text a password,
a token or a request body.

Stdlib only (``urllib``). Redirects are never followed: a redirect would carry
the ``Authorization`` header to wherever it points.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from collections import namedtuple

import station_texts_de as T

log = logging.getLogger('servo_station.cloud')
log.addHandler(logging.NullHandler())

# Lockstep with cloud_training_api/app/services/usernames.py (a test reads both).
SYNTHETIC_EMAIL_DOMAIN = 'edubotics.local'
USERNAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{2,31}$')
_URL_RE = re.compile(r'^https://[A-Za-z0-9.-]+(:[0-9]+)?$|^http://(localhost|127\.0\.0\.1)(:[0-9]+)?$')
RPC_PATH = '/rest/v1/rpc/submit_servo_calibration_record'
TIMEOUT_S = 10
REFRESH_MARGIN_S = 30

_MESSAGES = {
    'auth_failed': T.CLOUD_AUTH_FAILED,
    'bad_username': T.BAD_USERNAME,
    'not_admin': T.CLOUD_NOT_ADMIN,
    'not_deployed': T.CLOUD_NOT_DEPLOYED,
    'unreachable': T.CLOUD_UNREACHABLE,
    'config_missing': T.CLOUD_CONFIG_MISSING,
    'bad_url': T.CLOUD_BAD_URL,
}


class CloudError(Exception):
    """``kind`` ∈ auth_failed | bad_username | not_admin | rejected |
    not_deployed | unreachable | config_missing | bad_url; plus the HTTP status
    and PostgREST ``code`` when there was an answer. Never carries a body."""

    def __init__(self, kind, *, status=None, code=None):
        super().__init__(f'cloud {kind} (status {status}, code {code})')
        self.kind = kind
        self.status = status
        self.code = code

    @property
    def message_de(self):
        if self.kind == 'rejected':
            return T.CLOUD_REJECTED.format(code=self.code or self.status)
        return _MESSAGES[self.kind]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def default_opener(request, timeout):
    return _OPENER.open(request, timeout=timeout)


def check_url(url):
    """The Supabase project URL, without a trailing slash; https only (plain
    http only to localhost for a local stack)."""
    url = (url or '').strip().rstrip('/')
    if not url:
        raise CloudError('config_missing')
    if not _URL_RE.match(url):
        raise CloudError('bad_url')
    return url


def _classify(status, code):
    if code == '42501' or status == 403:
        return 'not_admin'
    if code == 'PGRST202' or status == 404:
        return 'not_deployed'
    if status == 401:
        return 'auth_failed'
    if status == 400:
        return 'rejected'
    return 'unreachable'


def _code(payload):
    return payload.get('code') if isinstance(payload, dict) and isinstance(payload.get('code'), str) else None


class CloudSession:
    """One admin sign-in against one Supabase project."""

    def __init__(self, supabase_url, anon_key, opener=default_opener, clock=time.time):
        self._url = check_url(supabase_url)
        self._anon_key = (anon_key or '').strip()
        if not self._anon_key:
            raise CloudError('config_missing')
        self._opener = opener
        self._clock = clock
        self._access = None
        self._refresh_token = None
        self._expires_at = 0.0
        self.username = None
        self.user_id = None

    def __repr__(self):
        return f'CloudSession(url={self._url!r}, signed_in={self.signed_in}, user={self.username!r})'

    @property
    def signed_in(self):
        return self._access is not None

    def sign_out(self):
        self._access = self._refresh_token = None
        self._expires_at = 0.0
        self.username = self.user_id = None

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _post(self, path, body, bearer=None):
        headers = {'apikey': self._anon_key, 'Content-Type': 'application/json',
                   'Accept': 'application/json'}
        if bearer:
            headers['Authorization'] = f'Bearer {bearer}'
        request = urllib.request.Request(self._url + path, data=json.dumps(body).encode('utf-8'),
                                         headers=headers, method='POST')
        try:
            with self._opener(request, timeout=TIMEOUT_S) as response:
                status = getattr(response, 'status', None) or response.getcode()
                raw = response.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001 — an unreadable error body is no body
                raw = b''
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
            log.warning('cloud unreachable (%s)', type(e).__name__)
            raise CloudError('unreachable') from None
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        return status, payload

    def _take_tokens(self, payload):
        if not isinstance(payload, dict) or not payload.get('access_token'):
            raise CloudError('auth_failed')
        self._access = payload['access_token']
        self._refresh_token = payload.get('refresh_token')
        self._expires_at = self._clock() + float(payload.get('expires_in') or 3600)
        user = payload.get('user') if isinstance(payload.get('user'), dict) else {}
        self.user_id = user.get('id')

    # ── the API ──────────────────────────────────────────────────────────────
    def sign_in(self, username, password):
        """Password grant for ``<username>@edubotics.local``. The password is
        sent once and never stored."""
        name = (username or '').strip().lower()
        if not USERNAME_RE.match(name):
            raise CloudError('bad_username')
        self.sign_out()
        status, payload = self._post('/auth/v1/token?grant_type=password',
                                     {'email': f'{name}@{SYNTHETIC_EMAIL_DOMAIN}', 'password': password})
        if not 200 <= status < 300:
            log.info('sign-in refused (status %s)', status)
            raise CloudError('auth_failed' if status in (400, 401) else 'unreachable', status=status)
        self._take_tokens(payload)
        self.username = name
        log.info('signed in as %s', name)

    def _refresh(self):
        if not self._refresh_token:
            self.sign_out()
            raise CloudError('auth_failed')
        status, payload = self._post('/auth/v1/token?grant_type=refresh_token',
                                     {'refresh_token': self._refresh_token})
        if not 200 <= status < 300:
            log.info('token refresh refused (status %s)', status)
            self.sign_out()
            raise CloudError('auth_failed' if status in (400, 401) else 'unreachable', status=status)
        self._take_tokens(payload)

    def submit(self, record):
        """One record through the RPC. A 401 gets exactly one token refresh and
        one retry; everything else maps to a :class:`CloudError` kind."""
        if not self.signed_in:
            raise CloudError('auth_failed')
        if self._clock() >= self._expires_at - REFRESH_MARGIN_S:
            self._refresh()
        status, payload = self._post(RPC_PATH, {'p_record': record}, bearer=self._access)
        if status == 401 and _code(payload) != '42501':
            self._refresh()
            status, payload = self._post(RPC_PATH, {'p_record': record}, bearer=self._access)
        if 200 <= status < 300:
            return payload
        raise CloudError(_classify(status, _code(payload)), status=status, code=_code(payload))


UploadResult = namedtuple('UploadResult', 'uploaded rejected stopped')


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def upload_pending(session, store, now=_utc_now):
    """Upload every record without a matching uploaded or rejected receipt. A
    database refusal (400) writes a rejected receipt and the record is not sent
    again until it is re-calibrated; any other failure stops the round (the
    rest would fail alike) and leaves the records pending."""
    uploaded, rejected = 0, []
    for record in store.pending_records():
        serial, checksum = record['servo_serial'], record.get('checksum')
        try:
            response = session.submit(record)
        except CloudError as e:
            if e.kind == 'rejected':
                code = e.code or str(e.status)
                store.write_rejected_receipt(serial, checksum, code, now())
                rejected.append((serial, code))
                log.info('upload of %s rejected (%s)', serial, code)
                continue
            log.info('upload stopped: %s', e.kind)
            return UploadResult(uploaded, rejected, e)
        store.write_uploaded_receipt(serial, checksum, response, now())
        uploaded += 1
    return UploadResult(uploaded, rejected, None)
