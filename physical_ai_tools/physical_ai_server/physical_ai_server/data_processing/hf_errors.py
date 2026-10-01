#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cause-aware Hugging Face errors (Aufnahme 2.0 round 5, spec §7.5, R5-4b).

``classify_hf_error(exc)`` answers ``'auth' | 'network' | 'busy' | 'server' |
None`` by walking the exception and its ``__cause__``/``__context__`` chain (at
most 8 deep), so a failure the hub wraps (``HfHubHTTPError`` around
``httpx.HTTPStatusError``, ``httpx.ConnectError`` around ``socket.gaierror``) is
judged by what really happened. ``hf_error_sentence_de(exc)`` maps the answer to
the German sentence (``record_texts_de.HF_ERROR_SENTENCES_DE``); None keeps the
caller's own generic sentence.

The library classes are recognised by name and module, never imported: the
classifier must work in the deps-free test suite and must not pull httpx or
huggingface_hub into a caller that only formats an error. The shapes, measured
against huggingface_hub 1.x in the server image::

    server closes the socket  httpx.RemoteProtocolError <- httpcore.RemoteProtocolError
    port closed               httpx.ConnectError <- httpcore.ConnectError <- ConnectionRefusedError
    DNS failure               httpx.ConnectError <- httpcore.ConnectError <- socket.gaierror
    401 / 403 / 429 / 503     HfHubHTTPError (.response.status_code) <- httpx.HTTPStatusError
    no token stored           huggingface_hub.errors.LocalTokenNotFoundError
    HF_HUB_OFFLINE=1          huggingface_hub.errors.OfflineModeIsEnabled (a ConnectionError)
    plain requests.get        requests.exceptions.ConnectionError <- urllib3 … <- ConnectionRefusedError
"""

from __future__ import annotations

import importlib.util
import os
import socket
from typing import Optional

MAX_CHAIN_DEPTH = 8

# (top-level module, class name) anywhere in an exception's MRO.
_AUTH_CLASSES = {('huggingface_hub', 'LocalTokenNotFoundError')}
_NETWORK_CLASSES = {
    ('httpx', 'TransportError'),
    ('httpcore', 'ConnectError'),
    ('httpcore', 'ReadError'),
    ('httpcore', 'WriteError'),
    ('httpcore', 'RemoteProtocolError'),
    ('httpcore', 'TimeoutException'),
    ('requests', 'ConnectionError'),
    ('requests', 'Timeout'),
    ('huggingface_hub', 'OfflineModeIsEnabled'),
}

# The long-standing auth substrings (DataManager._classify_hf_failure_de), then
# network ones; judged on the exception text when no class or status decided.
_AUTH_MARKERS = (
    '401',
    'unauthorized',
    'authentication',
    'authenticated',
    'invalid user token',
    'invalid token',
    'huggingfacehub_token',
    'token is required',
)
_NETWORK_MARKERS = (
    'connection',
    'timed out',
    'name resolution',
    'server disconnected',
)


def _mro_keys(exc: BaseException):
    for cls in type(exc).__mro__:
        yield ((getattr(cls, '__module__', '') or '').split('.')[0], cls.__name__)


def _status_code(exc: BaseException) -> Optional[int]:
    response = getattr(exc, 'response', None)
    code = getattr(response, 'status_code', None)
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    return None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and len(seen) < MAX_CHAIN_DEPTH and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _classify_one(exc: BaseException) -> Optional[str]:
    keys = set(_mro_keys(exc))
    if keys & _AUTH_CLASSES:
        return 'auth'
    code = _status_code(exc)
    if code is not None:
        if code in (401, 403):
            return 'auth'
        if code == 429:
            return 'busy'
        if code >= 500:
            return 'server'
    if keys & _NETWORK_CLASSES:
        return 'network'
    if isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror)):
        return 'network'
    return None


def classify_hf_error(exc: BaseException) -> Optional[str]:
    """'auth' | 'network' | 'busy' | 'server' | None for a Hugging Face failure."""
    if exc is None:
        return None
    chain = list(_chain(exc))
    for link in chain:
        kind = _classify_one(link)
        if kind is not None:
            return kind
    text = ' '.join(str(link) for link in chain).lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return 'auth'
    if any(marker in text for marker in _NETWORK_MARKERS):
        return 'network'
    return None


def _load_texts():
    try:
        from physical_ai_server.data_processing import record_texts_de
        return record_texts_de
    except ImportError:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'record_texts_de.py')
        spec = importlib.util.spec_from_file_location('_edubotics_record_texts_de', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def hf_error_sentence_de(exc: BaseException) -> Optional[str]:
    """The German sentence for ``exc``'s cause, or None (unclassified)."""
    kind = classify_hf_error(exc)
    if kind is None:
        return None
    return _load_texts().HF_ERROR_SENTENCES_DE[kind]
