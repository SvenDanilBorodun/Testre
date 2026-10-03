#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The robot's one-slot, memory-only Hugging Face token (the 042 feature).

The student's personal token reaches the robot over `/register_hf_user` and
lives in ONE file on a tmpfs, `$HF_TOKEN_PATH` (compose mounts
`/run/edubotics-hf`). `huggingface_hub` re-reads that file on every call (and the
spawned upload child inherits the env var), so no consumer passes a token. The
node never reads the file back to a client: it publishes only a fingerprint and
three booleans on `/edubotics/hf_token_state`.

Stdlib only, so the schema, the file mechanics and the scrubber are testable
without ROS and loadable by path. The German sentences live in
`record_texts_de.py`; this module has none.

`fingerprint()` and `TOKEN_SHAPE` are twins of
`cloud_training_api/app/services/hf_credentials.py`
(`robotis_ai_setup/tests/test_hf_token_fp_lockstep.py` holds both to one table).
"""

from __future__ import annotations

import json
import logging
import hashlib
import os
import re
from typing import List, Optional

PATH_ENV = 'HF_TOKEN_PATH'
STATE_TOPIC = '/edubotics/hf_token_state'
SCHEMA_VERSION = 1
PUBLISH_PERIOD_S = 1.0
FP_DOMAIN = b'edubotics-hf-token-fp:'
# The temp file `write()` stages the new token in (same directory, so the final
# os.replace is atomic). `clear()` removes it too: a process killed between the
# fsync and the replace must not leave a token behind that a handover forgot.
TMP_NAME = '.token.tmp'

# fullmatch only: `$` would also accept a trailing newline.
TOKEN_SHAPE = re.compile(r'hf_[A-Za-z0-9_-]{16,256}')
# Unanchored, for scrubbing free text.
TOKEN_IN_TEXT = re.compile(r'hf_[A-Za-z0-9_-]{16,}')


def token_path(environ=None) -> Optional[str]:
    """The slot's absolute path, or None when this image does not take a
    personal token (the Jetson sets no HF_TOKEN_PATH)."""
    env = os.environ if environ is None else environ
    raw = (env.get(PATH_ENV) or '').strip()
    return raw if raw and os.path.isabs(raw) else None


def accepts(environ=None) -> bool:
    return token_path(environ) is not None


def valid_shape(token) -> bool:
    return isinstance(token, str) and TOKEN_SHAPE.fullmatch(token) is not None


def fingerprint(token: str) -> str:
    """First 16 hex chars of sha256("edubotics-hf-token-fp:" + token)."""
    return hashlib.sha256(FP_DOMAIN + token.encode('utf-8')).hexdigest()[:16]


def read(path: Optional[str] = None) -> Optional[str]:
    """The stored token if the slot holds a shape-valid one, else None. Never
    raises: a missing, empty, unreadable or garbage file is 'nothing stored'."""
    path = path or token_path()
    if not path:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            text = fh.read(1024)
    except (OSError, UnicodeDecodeError):
        return None
    token = text.replace('\r', '').replace('\n', '').strip()
    return token if valid_shape(token) else None


def write(token: str, path: Optional[str] = None) -> None:
    """Atomically replace the slot: temp file in the SAME directory (0600) ->
    fsync -> os.replace. A reader sees the old or the new token, never half of
    either. Raises ValueError on a bad shape, OSError on I/O trouble."""
    path = path or token_path()
    if not path:
        raise ValueError('no token slot')
    if not valid_shape(token):
        raise ValueError('bad token shape')
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = os.path.join(directory, TMP_NAME)
    try:
        # A leftover from a killed earlier write: never reuse its inode/mode.
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear(path: Optional[str] = None) -> bool:
    """Remove the slot (and a stale staging file). True when the SLOT itself was
    removed; absent is not an error, and a leftover `.token.tmp` alone is removed
    but answers False (there was no active token to take away)."""
    path = path or token_path()
    if not path:
        return False
    removed = False
    for candidate in (path, os.path.join(os.path.dirname(path), TMP_NAME)):
        try:
            os.unlink(candidate)
            removed = removed or candidate == path
        except FileNotFoundError:
            pass
    return removed


def state_payload(seq: int, busy: bool, path: Optional[str] = None, environ=None) -> dict:
    """The /edubotics/hf_token_state document, key order fixed."""
    if not accepts(environ):
        return {'v': SCHEMA_VERSION, 'seq': int(seq), 'accepts': False,
                'present': False, 'fp': None, 'busy': bool(busy)}
    token = read(path or token_path(environ))
    return {'v': SCHEMA_VERSION, 'seq': int(seq), 'accepts': True,
            'present': token is not None,
            'fp': fingerprint(token) if token else None, 'busy': bool(busy)}


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, separators=(',', ':'))


def purge_legacy(environ=None, home: Optional[str] = None) -> List[str]:
    """Delete the token files the old `huggingface-cli login` path wrote into the
    persistent `huggingface_cache` volume. Only on an image that takes a personal
    token (the Jetson keeps its classroom token untouched). Returns the removed
    paths; their content is never read or logged."""
    env = os.environ if environ is None else environ
    if not accepts(env):
        return []
    base = env.get('HF_HOME') or os.path.join(home or os.path.expanduser('~'), '.cache', 'huggingface')
    removed: List[str] = []
    for name in ('token', 'stored_tokens'):
        candidate = os.path.join(base, name)
        if os.path.abspath(candidate) == os.path.abspath(token_path(env) or ''):
            continue
        try:
            os.unlink(candidate)
            removed.append(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


REDACTED = 'hf_***'


def _scrub(text: str) -> str:
    return TOKEN_IN_TEXT.sub(REDACTED, text)


def _scrub_arg(arg):
    """One `%s` argument of a log call. A str is scrubbed; numbers, bools and
    None pass through untouched (so `%d` still formats). Any other object (an
    exception that carries the token in its message, a dict, bytes ...) is
    replaced by its scrubbed text ONLY when its text contains a token, so an
    innocent object keeps its own formatting."""
    if isinstance(arg, str):
        return _scrub(arg)
    if arg is None or isinstance(arg, (bool, int, float)):
        return arg
    try:
        text = str(arg)
    except Exception:  # noqa: BLE001 - an object whose __str__ raises is not our business
        return arg
    return _scrub(text) if TOKEN_IN_TEXT.search(text) else arg


class TokenScrubber(logging.Filter):
    """Replace any `hf_…` token in a record's message, arguments, traceback and
    stack info with `hf_***`.

    A logger-level filter only sees records logged DIRECTLY on that logger, so a
    child logger ('huggingface_hub.lfs') slips past it; `install_log_scrubber`
    therefore also attaches the same filter to the handlers, which every
    propagated record passes. Never raises, never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _scrub(record.msg)
            else:
                record.msg = _scrub_arg(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _scrub_arg(v) for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_scrub_arg(a) for a in record.args)
            # Format the traceback now (the Formatter caches it on the record),
            # so the exception's own message is scrubbed as well.
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _scrub(record.exc_text)
            if record.stack_info:
                record.stack_info = _scrub(record.stack_info)
        except Exception:  # noqa: BLE001 - a log filter must never raise
            pass
        return True


SCRUBBED_LOGGERS = ('huggingface_hub', 'huggingface_hub.utils._http', 'huggingface_hub.hf_api',
                    'huggingface_hub.file_download', 'huggingface_hub._upload_large_folder',
                    'httpx', 'httpcore', 'urllib3', 'requests')


def _attach(target, scrubber: TokenScrubber) -> None:
    if not any(isinstance(f, TokenScrubber) for f in target.filters):
        target.addFilter(scrubber)


def install_log_scrubber() -> None:
    """Idempotent. Attach one TokenScrubber to the named HF/HTTP loggers AND to
    every handler that exists now (the root's and the named loggers' own), so
    child loggers and propagated records are covered too. Call it again after
    anything that creates a new handler (`HfApiWorker.__init__` runs
    `logging.basicConfig`); a handler added later has no filter until then."""
    scrubber = TokenScrubber()
    loggers = [logging.getLogger(name) for name in SCRUBBED_LOGGERS]
    for logger in loggers:
        _attach(logger, scrubber)
    for holder in [logging.getLogger()] + loggers:
        for handler in list(holder.handlers):
            _attach(handler, scrubber)
