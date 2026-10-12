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

"""Cloud-Inferenz wire protocol v1: frame codec, message builders/validators, limits.

Shared, Modal-free: stdlib + numpy only. Shipped twice — inside physical_ai_server (the robot) and
as /root/cloud_protocol.py in the Modal image (the server). Imports nothing from physical_ai_server.
Error CODES travel on the wire; German sentences live on the robot (inference/cloud_texts_de.py).

A frame is ``u32 LE header length H | H bytes UTF-8 JSON header | segments``; the header names the
message type in ``t`` and declares every segment's length, nothing follows the segments, and the
whole frame is at most ``MAX_FRAME_BYTES``. Floats in segments are explicit little-endian float32.
Every parser checks a value's TYPE before any set/dict membership test or comparison, so the codec
raises only ``ProtocolError`` on hostile input (spec §2.1, R-2).
"""
import json
import re
import struct

import numpy as np

PROTOCOL_VERSION = 1
# The server accepts these hello versions (N and N-1 once v2 exists).
SERVER_ACCEPTS = (1,)
# @app.server host: <workspace>--<app>-<class lowercased>.<routing_region>.modal.direct (step 0 measured
# svendanilborodun--edubotics-inference-spike-eu-servereu.eu-west.modal.direct); prod checks it at deploy (§7.2).
CLOUD_BASE_URL = 'https://svendanilborodun--edubotics-inference-cloudinference.eu-west.modal.direct'

HEALTH_PATH = '/v1/health'
HELLO_PATH = '/v1/hello'
INFER_PATH = '/v1/infer'
BYE_PATH = '/v1/bye'
PREFETCH_PATH = '/v1/prefetch'      # the cloud API's model prefetch for a reservation (D31)
CONTENT_TYPE = 'application/octet-stream'
CONN_HEADER = 'X-Conn-Id'
# O7' (owner-answers-4): HTTPS ONLY, one request per observation to the session's own container.
# D24: Modal sessioned server. EVERY request carries the per-session EDGE token in EDGE_AUTH_HEADER as
# 'Bearer <token>' (Modal refuses others before any container starts) and the CONNECT token in
# CONNECT_AUTH_HEADER as 'Bearer <token>' (never in a body). The container trusts MODAL_SESSION_HEADER:
# Modal's proxy overwrites it with the authenticated session's id.
EDGE_AUTH_HEADER = 'Modal-Authorization'
CONNECT_AUTH_HEADER = 'Authorization'
MODAL_SESSION_HEADER = 'x-modal-server-session-id'
EDGE_TOKEN_RE = re.compile(r'[\x21-\x7e]{16,4096}')         # opaque Modal token: printable ASCII, no space
MODAL_SESSION_ID_RE = re.compile(r'sd-[A-Za-z0-9_-]{1,128}')   # == the SQL CHECK of 048

MAX_FRAME_BYTES = 1_572_864        # 1.5 MiB per request or response body
MAX_HEADER_BYTES = 4_096
MAX_JPEG_BYTES = 524_288           # 512 KiB per camera image
MAX_CAMERAS = 4
MAX_STATE_DIM = 32
MAX_ACTION_DIM = 32
MAX_CHUNK_N = 100
MAX_SEQ = 2 ** 53
MAX_OBS_E = 2 ** 31 - 1
MAX_TASK_CHARS = 512
MAX_JPEG_SCANS = 16                # F-17: baseline = 1 scan, libjpeg's default progressive script = 10

TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{43}')         # secrets.token_urlsafe(32)
CONN_ID_RE = re.compile(r'[0-9a-f]{32}')             # secrets.token_hex(16)
NAME_RE = re.compile(r'[A-Za-z0-9_-]{1,32}')

TYPES = frozenset({'hello', 'ready', 'obs', 'chunk', 'error', 'bye'})
BYE_REASONS = frozenset({'robot_stop', 'robot_error', 'stall'})
ERROR_CODES = {   # code -> retry flag the server always sends with it
    'bad_frame': False, 'version': False, 'auth': False, 'ended': False, 'replaced': False,
    'unavailable': False, 'model': False, 'model_type': False, 'cameras': False,
    'shape': False, 'state': False, 'rate': True, 'internal': True,
}
U32 = struct.Struct('<I')
F32 = np.dtype('<f4')


class ProtocolError(ValueError):
    """A frame that is not protocol v1. ``str(e)`` is an English code for logs, never shown."""


def _no_const(_):
    raise ValueError('NaN/Infinity')


def encode(header, segments=()):
    """One frame: the JSON header (compact, UTF-8, NaN/Infinity refused) and the segments after it.

    Raises ``ProtocolError`` when the header or the whole frame exceeds its cap.
    """
    raw = json.dumps(header, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    if not 1 <= len(raw) <= MAX_HEADER_BYTES:
        raise ProtocolError('header_size')
    frame = U32.pack(len(raw)) + raw + b''.join(bytes(s) for s in segments)
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError('frame_size')
    return frame


def decode(buf):
    """``(header dict, payload memoryview)`` of one frame; bytes-like input only.

    Every malformed input (wrong type, size, header length, JSON, NaN, unknown ``t``) raises
    ``ProtocolError`` and nothing else. The payload is returned unparsed: the message's own
    ``parse_*`` validates it against the header.
    """
    if not isinstance(buf, (bytes, bytearray, memoryview)):
        raise ProtocolError('frame_type')
    mv = memoryview(buf)
    if mv.ndim != 1 or mv.itemsize != 1:
        raise ProtocolError('frame_type')
    if not 4 <= len(mv) <= MAX_FRAME_BYTES:
        raise ProtocolError('frame_size')
    (n,) = U32.unpack_from(mv, 0)
    if not 1 <= n <= MAX_HEADER_BYTES or 4 + n > len(mv):
        raise ProtocolError('header_size')
    try:
        header = json.loads(bytes(mv[4:4 + n]).decode('utf-8'), parse_constant=_no_const)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ProtocolError('header_json') from e
    if not isinstance(header, dict) or type(header.get('t')) is not str or header['t'] not in TYPES:
        raise ProtocolError('header_type')
    return header, mv[4 + n:]


def _int(h, key, lo, hi):
    v = h.get(key)
    if type(v) is not int or not lo <= v <= hi:
        raise ProtocolError(key)
    return v


def _num(h, key, lo, hi, allow_none=False):
    v = h.get(key)
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ProtocolError(key)
    return float(v)


def _name(v):
    if type(v) is not str or not NAME_RE.fullmatch(v):
        raise ProtocolError('name')
    return v


# ---------------- hello ----------------
def build_hello(*, conn_id, robot_type, robot_profile, cameras, state_dim, fps):
    """The robot's ``hello`` (body of ``POST /v1/hello``); ``cameras`` = ``[(name, w, h), ...]``.

    It carries no token: both tokens travel only in headers.
    """
    return encode({'t': 'hello', 'v': PROTOCOL_VERSION, 'conn_id': conn_id,
                   'robot_type': robot_type, 'robot_profile': robot_profile,
                   'cameras': [{'name': n, 'w': int(w), 'h': int(hh)} for n, w, hh in cameras],
                   'state_dim': int(state_dim), 'fps': float(fps)})


def parse_hello(header, payload):
    """``{'v', 'conn_id'}`` of a valid ``hello``; a ``token`` key in the body is refused."""
    if header.get('t') != 'hello' or len(payload):
        raise ProtocolError('hello')
    v = header.get('v')
    if type(v) is not int:
        raise ProtocolError('v')
    conn_id = header.get('conn_id')
    if type(conn_id) is not str or not CONN_ID_RE.fullmatch(conn_id):
        raise ProtocolError('conn_id')
    if 'token' in header:          # a token never travels in a body (CONNECT_AUTH_HEADER only)
        raise ProtocolError('token_in_body')
    cams = header.get('cameras')
    if type(cams) is not list or len(cams) > MAX_CAMERAS:
        raise ProtocolError('cameras')
    for c in cams:
        if not isinstance(c, dict):
            raise ProtocolError('cameras')
        _name(c.get('name')); _int(c, 'w', 0, 8192); _int(c, 'h', 0, 8192)
    _int(header, 'state_dim', 1, MAX_STATE_DIM)
    _num(header, 'fps', 1, 255)
    return {'v': v, 'conn_id': conn_id}


# ---------------- ready ----------------
def build_ready(*, server_sha, policy, limits):
    """The server's answer to ``hello``: the policy summary and the served ``limits`` (§2.3)."""
    return encode({'t': 'ready', 'v': PROTOCOL_VERSION, 'server_sha': server_sha,
                   'policy': policy, 'limits': limits})


def parse_ready(header, payload):
    """``(policy, limits)`` of a valid ``ready``.

    The policy is validated here; the limits are returned as sent and clamped by the robot
    (``cloud_schedule.clamp_limits``), so a server value can never widen a robot ceiling.
    """
    if header.get('t') != 'ready' or len(payload):
        raise ProtocolError('ready')
    pol, lim = header.get('policy'), header.get('limits')
    if not isinstance(pol, dict) or not isinstance(lim, dict):
        raise ProtocolError('ready_shape')
    if pol.get('type') != 'act':
        raise ProtocolError('policy_type')
    cams = pol.get('expected_cameras')
    shapes = pol.get('image_shapes')
    if type(cams) is not list or not 1 <= len(cams) <= MAX_CAMERAS or not isinstance(shapes, dict):
        raise ProtocolError('policy_cameras')
    for c in cams:
        _name(c)
        s = shapes.get(c)
        if type(s) is not list or len(s) != 3 or s[0] != 3 or any(type(x) is not int or not 1 <= x <= 8192 for x in s):
            raise ProtocolError('image_shapes')
    _int(pol, 'state_dim', 1, MAX_STATE_DIM); _int(pol, 'action_dim', 1, MAX_ACTION_DIM)
    _int(pol, 'chunk_size', 1, 1000); _int(pol, 'n_action_steps', 1, 1000)
    _num(pol, 'trained_fps', 1, 255, allow_none=True)
    if type(pol.get('requires_task')) is not bool:
        raise ProtocolError('requires_task')
    return pol, lim     # limits are clamped by the robot (cloud_schedule.clamp_limits)


# ---------------- obs ----------------
def build_obs(seq, obs_e, cams, state, task=None):
    """cams: ordered {name: jpeg bytes}; state: float32 vector. Segments: JPEGs in header order, then state."""
    state = np.ascontiguousarray(state, dtype=F32)
    h = {'t': 'obs', 'seq': seq, 'obs_e': obs_e,
         'cams': [{'name': k, 'len': len(v)} for k, v in cams.items()],
         'state_dim': int(state.shape[0])}
    if task:
        h['task'] = task[:MAX_TASK_CHARS]
    return encode(h, [*cams.values(), state.tobytes()])


def parse_obs(header, payload):
    """``(seq, obs_e, {name: jpeg bytes}, state float32, task | None)`` of a valid ``obs``.

    The payload must be exactly the declared JPEG lengths plus ``4 * state_dim`` bytes; camera names
    are unique; the state is finite.
    """
    seq = _int(header, 'seq', 0, MAX_SEQ)
    obs_e = _int(header, 'obs_e', 0, MAX_OBS_E)
    cams = header.get('cams')
    if type(cams) is not list or not 1 <= len(cams) <= MAX_CAMERAS or not all(isinstance(c, dict) for c in cams):
        raise ProtocolError('cams')
    sd = _int(header, 'state_dim', 1, MAX_STATE_DIM)
    lens = [_int(c, 'len', 1, MAX_JPEG_BYTES) for c in cams]
    if len(payload) != sum(lens) + 4 * sd:
        raise ProtocolError('payload_length')
    task = header.get('task')
    if task is not None and (type(task) is not str or len(task) > MAX_TASK_CHARS):
        raise ProtocolError('task')
    out, off = {}, 0
    for c, n in zip(cams, lens):
        name = _name(c.get('name'))
        if name in out:
            raise ProtocolError('duplicate_camera')
        out[name] = bytes(payload[off:off + n]); off += n
    state = np.frombuffer(bytes(payload[off:off + 4 * sd]), dtype=F32).copy()
    if not np.all(np.isfinite(state)):
        raise ProtocolError('state_not_finite')
    return seq, obs_e, out, state, task


# ---------------- chunk ----------------
def build_chunk(seq, obs_e, actions, *, gpu_ms=0.0, queue_ms=0.0, decode_ms=0.0):
    """The server's answer to ``obs``: ``actions`` (n, dim) float32, row i = timestep ``obs_e + i``."""
    a = np.ascontiguousarray(actions, dtype=F32)
    if a.ndim != 2 or not 1 <= a.shape[0] <= MAX_CHUNK_N or not 1 <= a.shape[1] <= MAX_ACTION_DIM:
        raise ProtocolError('chunk_shape')
    return encode({'t': 'chunk', 'seq': seq, 'obs_e': obs_e, 'n': int(a.shape[0]), 'dim': int(a.shape[1]),
                   'gpu_ms': round(float(gpu_ms), 2), 'queue_ms': round(float(queue_ms), 2),
                   'decode_ms': round(float(decode_ms), 2)}, [a.tobytes()])


def parse_chunk(header, payload):
    """``(seq, obs_e, actions float32 (n, dim), gpu_ms | None)`` of a valid ``chunk``.

    Finiteness is NOT checked here: a non-finite row is the schedule's verdict (end, never clamp).
    """
    seq = _int(header, 'seq', 0, MAX_SEQ)
    obs_e = _int(header, 'obs_e', 0, MAX_OBS_E)
    n = _int(header, 'n', 1, MAX_CHUNK_N)
    dim = _int(header, 'dim', 1, MAX_ACTION_DIM)
    if len(payload) != 4 * n * dim:
        raise ProtocolError('payload_length')
    actions = np.frombuffer(bytes(payload), dtype=F32).reshape(n, dim).copy()
    gpu_ms = _num(header, 'gpu_ms', 0, 1e6, allow_none=True)
    return seq, obs_e, actions, gpu_ms   # finiteness is the schedule's verdict (END, never clamp)


# ---------------- error / bye ----------------
def build_error(code, *, seq=None, reason=None, server_v=None):
    """An ``error`` frame; ``retry`` is fixed by ``ERROR_CODES`` (the server never chooses it)."""
    if type(code) is not str or code not in ERROR_CODES:
        raise ProtocolError('error_code')
    h = {'t': 'error', 'code': code, 'retry': ERROR_CODES[code]}
    if seq is not None:
        h['seq'] = seq
    if reason is not None:
        h['reason'] = reason
    if server_v is not None:
        h['server_v'] = list(server_v)
    return encode(h)


def parse_error(header, payload):
    """``(code, retry, seq | None, reason str<=64 | None, server_v tuple[int]<=8 | None)``.

    ``server_v`` comes back as a tuple of small ints so the robot's ``max()`` and dict lookups
    cannot raise.
    """
    code = header.get('code')
    if (header.get('t') != 'error' or len(payload) or type(code) is not str or code not in ERROR_CODES
            or type(header.get('retry')) is not bool):
        raise ProtocolError('error')
    seq = header.get('seq')
    if seq is not None and (type(seq) is not int or not 0 <= seq <= MAX_SEQ):
        raise ProtocolError('error_seq')
    reason = header.get('reason')
    if reason is not None and (type(reason) is not str or not 1 <= len(reason) <= 64):
        raise ProtocolError('error_reason')
    server_v = header.get('server_v')
    if server_v is not None and (type(server_v) is not list or not 1 <= len(server_v) <= 8
                                 or any(type(x) is not int or not 0 <= x <= 1000 for x in server_v)):
        raise ProtocolError('error_server_v')
    return code, header['retry'], seq, reason, (tuple(server_v) if server_v is not None else None)


def build_bye(reason, stats=None):
    """The robot's ``bye`` (body of ``POST /v1/bye``) with one of ``BYE_REASONS``."""
    if type(reason) is not str or reason not in BYE_REASONS:
        raise ProtocolError('bye_reason')
    h = {'t': 'bye', 'reason': reason}
    if stats:
        h['stats'] = stats
    return encode(h)


def parse_bye(header, payload):
    """``(reason, stats dict | None)`` of a valid ``bye``."""
    reason = header.get('reason')
    if header.get('t') != 'bye' or len(payload) or type(reason) is not str or reason not in BYE_REASONS:
        raise ProtocolError('bye')
    stats = header.get('stats')
    return reason, (stats if isinstance(stats, dict) else None)


def jpeg_size(data):
    """(width, height) from the first SOFn marker of a JPEG, or None. Never decodes."""
    b = memoryview(data)
    if len(b) < 4 or b[0] != 0xFF or b[1] != 0xD8:
        return None
    i, n = 2, len(b)
    while i + 4 <= n:
        if b[i] != 0xFF:
            return None
        m = b[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0x01 or 0xD0 <= m <= 0xD8:
            i += 2
            continue
        if m in (0xD9, 0xDA):
            return None
        seglen = (b[i + 2] << 8) | b[i + 3]
        if seglen < 2 or i + 2 + seglen > n:
            return None
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if seglen < 7:
                return None
            h = (b[i + 5] << 8) | b[i + 6]
            w = (b[i + 7] << 8) | b[i + 8]
            return (w, h) if w and h else None
        i += 2 + seglen
    return None


def jpeg_scans(data):
    """Number of SOS markers (0xFFDA). Entropy-coded data stuffs every 0xFF (0xFF00) or uses RSTn, so an SOS
    cannot hide inside a scan; bytes inside APPn/COM segments can only OVER-count (a refusal of that frame)."""
    return bytes(data).count(b'\xff\xda')


def url(path, base=CLOUD_BASE_URL):
    """``base + path``; ``https://`` in production, ``http://`` for tests and the §10.4 run only."""
    if not (base.startswith('https://') or base.startswith('http://')):   # http:// for tests and §10.4 only
        raise ValueError('base url scheme')
    return base.rstrip('/') + path
