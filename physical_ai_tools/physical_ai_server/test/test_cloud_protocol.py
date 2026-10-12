"""Cloud-Inferenz wire protocol v1 (spec §2.1-2.6, Appendix C).

``cloud_protocol.py`` is shipped twice: inside the robot's package and as
``/root/cloud_protocol.py`` in the Modal image. It is stdlib + numpy only, so
this whole file runs in the deps-free CI suite. The JPEG helpers are checked
against real encoder output (cv2), which python-tests installs.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import random
import re

import numpy as np
import pytest

from physical_ai_server.inference import cloud_protocol as P

_SRC = Path(P.__file__).read_text(encoding='utf-8')

_POLICY = {'type': 'act', 'expected_cameras': ['gripper', 'scene'],
           'image_shapes': {'gripper': [3, 480, 640], 'scene': [3, 480, 640]},
           'trained_fps': 30, 'chunk_size': 100, 'n_action_steps': 15,
           'state_dim': 6, 'action_dim': 6, 'requires_task': False}
_LIMITS = {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7,
           'max_obs_per_s': 8.0, 'stall_end_s': 15.0, 'resend_after_s': 2.0, 'max_end_in_s': 1795.0}
_CID = 'ab' * 16
# Not a real JPEG: the codec never looks inside a camera segment (the server's SOF check does).
_JPEG_A = b'\xff\xd8' + bytes(range(256)) * 7 + b'\xff\xd9'
_JPEG_B = b'\xff\xd8' + bytes(reversed(range(256))) * 3 + b'\xff\xd9'


def _parse(frame):
    h, pl = P.decode(frame)
    return {'hello': P.parse_hello, 'ready': P.parse_ready, 'obs': P.parse_obs, 'chunk': P.parse_chunk,
            'error': P.parse_error, 'bye': P.parse_bye}[h['t']](h, pl)


def _raw(header, segments=b''):
    raw = json.dumps(header, ensure_ascii=False).encode('utf-8')
    return P.U32.pack(len(raw)) + raw + segments


def _hello(**over):
    kw = dict(conn_id=_CID, robot_type='omx_f', robot_profile='omx_full',
              cameras=[('gripper', 640, 480), ('scene', 640, 480)], state_dim=6, fps=30)
    kw.update(over)
    return P.build_hello(**kw)


# ---------------------------------------------------------------- round trips

def test_round_trip_every_message():
    assert _parse(_hello()) == {'v': 1, 'conn_id': _CID}
    h, _ = P.decode(_hello())
    assert h['cameras'] == [{'name': 'gripper', 'w': 640, 'h': 480}, {'name': 'scene', 'w': 640, 'h': 480}]
    assert h['state_dim'] == 6 and h['fps'] == 30.0 and h['robot_profile'] == 'omx_full'

    pol, lim = _parse(P.build_ready(server_sha='abc1234', policy=_POLICY, limits=_LIMITS))
    assert pol == _POLICY and lim == _LIMITS

    state = np.arange(6, dtype=np.float32) / 7
    seq, obs_e, cams, st, task = _parse(P.build_obs(3, 15, {'gripper': _JPEG_A, 'scene': _JPEG_B}, state, 'greif'))
    assert (seq, obs_e, task) == (3, 15, 'greif')
    assert cams == {'gripper': _JPEG_A, 'scene': _JPEG_B}
    assert st.dtype == np.dtype('<f4') and np.array_equal(st, state)
    assert _parse(P.build_obs(4, 20, {'gripper': _JPEG_A}, state))[4] is None

    a = np.random.default_rng(0).standard_normal((50, 6)).astype(np.float32)
    seq, obs_e, actions, gpu_ms = _parse(P.build_chunk(3, 15, a, gpu_ms=22.814, queue_ms=1, decode_ms=2.3))
    assert (seq, obs_e, gpu_ms) == (3, 15, 22.81)
    assert actions.dtype == np.dtype('<f4') and np.array_equal(actions, a)

    for code, retry in P.ERROR_CODES.items():
        assert _parse(P.build_error(code)) == (code, retry, None, None, None)
    assert _parse(P.build_error('ended', seq=7, reason='expired')) == ('ended', False, 7, 'expired', None)
    assert _parse(P.build_error('version', server_v=[1])) == ('version', False, None, None, (1,))

    assert _parse(P.build_bye('robot_stop', {'rtt_ms_p50': 110})) == ('robot_stop', {'rtt_ms_p50': 110})
    assert _parse(P.build_bye('stall')) == ('stall', None)


def test_obs_segments_in_header_order_then_state():
    state = np.array([1.5, -2.0, 3.25], dtype=np.float32)
    frame = P.build_obs(1, 2, {'scene': _JPEG_B, 'gripper': _JPEG_A}, state)
    h, pl = P.decode(frame)
    assert [c['name'] for c in h['cams']] == ['scene', 'gripper']
    assert [c['len'] for c in h['cams']] == [len(_JPEG_B), len(_JPEG_A)]
    raw = bytes(pl)
    assert raw[:len(_JPEG_B)] == _JPEG_B
    assert raw[len(_JPEG_B):len(_JPEG_B) + len(_JPEG_A)] == _JPEG_A
    assert raw[len(_JPEG_B) + len(_JPEG_A):] == state.astype('<f4').tobytes()
    assert len(raw) == len(_JPEG_A) + len(_JPEG_B) + 12
    # The frame is the u32 LE header length, the header, the segments, and nothing after them.
    (n,) = P.U32.unpack_from(frame, 0)
    assert frame[4:4 + n] == json.dumps(h, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    assert len(frame) == 4 + n + len(raw)


# ---------------------------------------------------------------- caps

def _obs_header(**over):
    h = {'t': 'obs', 'seq': 1, 'obs_e': 0, 'cams': [{'name': 'gripper', 'len': 4}], 'state_dim': 1}
    h.update(over)
    return h


def test_caps_refused():
    st4 = np.zeros(1, np.float32).tobytes()
    # The well-formed baseline parses, so each refusal below is the cap itself.
    assert _parse(_raw(_obs_header(), b'\xff\xd8\xff\xd9' + st4))[0] == 1

    refusals = [
        _raw(_obs_header(cams=[{'name': 'gripper', 'len': P.MAX_JPEG_BYTES + 1}])),          # JPEG > 512 KiB
        _raw(_obs_header(cams=[{'name': f'c{i}', 'len': 1} for i in range(P.MAX_CAMERAS + 1)])),
        _raw(_obs_header(cams=[])),
        _raw(_obs_header(state_dim=P.MAX_STATE_DIM + 1)),
        _raw(_obs_header(state_dim=0)),
        _raw(_obs_header(seq=P.MAX_SEQ + 1), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(seq=-1), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(obs_e=P.MAX_OBS_E + 1), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(task='x' * (P.MAX_TASK_CHARS + 1)), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(cams=[{'name': 'a' * 33, 'len': 4}]), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(cams=[{'name': 'gr ipper', 'len': 4}]), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(cams=[{'name': 'g', 'len': 2}, {'name': 'g', 'len': 2}]), b'\xff\xd8\xff\xd9' + st4),
        _raw(_obs_header(), b'\xff\xd8\xff\xd9' + st4 + b'x'),                                 # trailing byte
        _raw(_obs_header(), b'\xff\xd8\xff' + st4),                                            # short payload
        _raw({'t': 'chunk', 'seq': 1, 'obs_e': 0, 'n': P.MAX_CHUNK_N + 1, 'dim': 1},
             bytes(4 * (P.MAX_CHUNK_N + 1))),
        _raw({'t': 'chunk', 'seq': 1, 'obs_e': 0, 'n': 1, 'dim': P.MAX_ACTION_DIM + 1},
             bytes(4 * (P.MAX_ACTION_DIM + 1))),
        _raw({'t': 'chunk', 'seq': 1, 'obs_e': 0, 'n': 2, 'dim': 2}, bytes(12)),
        _raw({'t': 'hello', 'v': 1, 'conn_id': _CID, 'cameras': [{'name': 'a', 'w': 8193, 'h': 1}],
              'state_dim': 6, 'fps': 30}),
        _raw({'t': 'hello', 'v': 1, 'conn_id': _CID, 'cameras': [], 'state_dim': 6, 'fps': 256}),
        _raw({'t': 'hello', 'v': 1, 'conn_id': 'AB' * 16, 'cameras': [], 'state_dim': 6, 'fps': 30}),
    ]
    for i, frame in enumerate(refusals):
        with pytest.raises(P.ProtocolError):
            _parse(frame)
            pytest.fail(f'refusal {i} parsed')

    # The frame and header caps, in both directions.
    with pytest.raises(P.ProtocolError):
        P.encode({'t': 'bye', 'reason': 'x' * P.MAX_HEADER_BYTES})
    with pytest.raises(P.ProtocolError):
        P.encode({'t': 'obs'}, [bytes(P.MAX_FRAME_BYTES)])
    with pytest.raises(P.ProtocolError):
        P.decode(bytes(P.MAX_FRAME_BYTES + 1))
    with pytest.raises(P.ProtocolError):
        P.decode(P.U32.pack(P.MAX_HEADER_BYTES + 1) + b'{}')
    with pytest.raises(P.ProtocolError):
        P.decode(P.U32.pack(0) + b'{}')
    with pytest.raises(P.ProtocolError):
        P.decode(P.U32.pack(100) + b'{"t":"bye"}')                                             # H > buffer
    with pytest.raises(P.ProtocolError):
        P.build_chunk(1, 0, np.zeros((P.MAX_CHUNK_N + 1, 6), np.float32))
    with pytest.raises(P.ProtocolError):
        P.build_chunk(1, 0, np.zeros(6, np.float32))
    with pytest.raises(P.ProtocolError, match='frame_size'):     # four maximal JPEGs exceed 1.5 MiB
        P.build_obs(1, 0, {f'c{i}': bytes(P.MAX_JPEG_BYTES) for i in range(4)}, np.zeros(6, np.float32))
    # The task is cut to the cap by the builder, never sent longer.
    h, _ = P.decode(P.build_obs(1, 0, {'g': b'\xff\xd8'}, np.zeros(1, np.float32), 'y' * 2000))
    assert len(h['task']) == P.MAX_TASK_CHARS


def test_nan_refused_in_header_and_state():
    for literal in (b'NaN', b'Infinity', b'-Infinity'):
        raw = b'{"t":"chunk","seq":1,"obs_e":0,"n":1,"dim":1,"gpu_ms":' + literal + b'}'
        with pytest.raises(P.ProtocolError):
            P.decode(P.U32.pack(len(raw)) + raw + bytes(4))
    with pytest.raises(ValueError):
        P.encode({'t': 'chunk', 'gpu_ms': float('nan')})
    with pytest.raises(ValueError):
        P.build_chunk(1, 0, np.zeros((1, 1), np.float32), gpu_ms=float('inf'))
    for bad in (np.nan, np.inf, -np.inf):
        frame = P.build_obs(1, 0, {'gripper': _JPEG_A}, np.array([0.0, bad, 1.0], np.float32))
        with pytest.raises(P.ProtocolError):
            _parse(frame)
    # A non-finite ACTION is not the codec's verdict: parse_chunk hands it to the schedule.
    _, _, actions, _ = _parse(P.build_chunk(1, 0, np.array([[np.nan, 1.0]], np.float32)))
    assert np.isnan(actions[0, 0])


def test_parse_hello_refuses_a_token_in_the_body():
    h, pl = P.decode(_hello())
    assert P.parse_hello(h, pl)['conn_id'] == _CID
    for value in ('A' * 43, '', None, 1):
        with pytest.raises(P.ProtocolError, match='token_in_body'):
            P.parse_hello(dict(h, token=value), pl)
    assert 'token' not in h
    # A payload after a hello is refused too: the hello has no segments.
    with pytest.raises(P.ProtocolError):
        P.parse_hello(h, memoryview(b'x'))


def test_parse_error_validates_seq_reason_server_v():
    def err(**over):
        h = {'t': 'error', 'code': 'ended', 'retry': False}
        h.update(over)
        return h

    empty = memoryview(b'')
    assert P.parse_error(err(seq=0, reason='x', server_v=[1, 2]), empty) == ('ended', False, 0, 'x', (1, 2))
    bad = [
        err(seq=-1), err(seq=P.MAX_SEQ + 1), err(seq=True), err(seq=1.0), err(seq='1'), err(seq=[1]),
        err(reason=''), err(reason='x' * 65), err(reason=1), err(reason=['x']), err(reason={}),
        err(server_v=[]), err(server_v=[1] * 9), err(server_v=[True]), err(server_v=[1.0]), err(server_v=[-1]),
        err(server_v=[1001]), err(server_v=1), err(server_v='1'), err(server_v={'1': 1}), err(server_v=[[1]]),
        err(code='nope'), err(code=['ended']), err(code=None), err(retry=None), err(retry=0), err(t='bye'),
    ]
    for h in bad:
        with pytest.raises(P.ProtocolError):
            P.parse_error(h, empty)
            pytest.fail(repr(h))
    with pytest.raises(P.ProtocolError):
        P.parse_error(err(), memoryview(b'x'))
    # server_v is a tuple, so max() and dict lookups on it cannot raise.
    code, _, _, _, server_v = P.parse_error(err(code='version', server_v=[2, 1]), empty)
    assert code == 'version' and isinstance(server_v, tuple) and max(server_v) == 2


def test_error_codes_retry_flags_fixed():
    assert P.ERROR_CODES == {
        'bad_frame': False, 'version': False, 'auth': False, 'ended': False, 'replaced': False,
        'unavailable': False, 'model': False, 'model_type': False, 'cameras': False,
        'shape': False, 'state': False, 'rate': True, 'internal': True,
    }
    assert len(P.ERROR_CODES) == 13
    assert 'idle' not in P.ERROR_CODES and 'hello_timeout' not in P.ERROR_CODES
    for code, retry in P.ERROR_CODES.items():
        h, _ = P.decode(P.build_error(code))
        assert h['retry'] is retry
    for bad in ('idle', 'hello_timeout', '', None, 1, ['rate']):
        with pytest.raises(P.ProtocolError):
            P.build_error(bad)
    assert P.BYE_REASONS == frozenset({'robot_stop', 'robot_error', 'stall'})
    with pytest.raises(P.ProtocolError):
        P.build_bye('user_stop')


# ---------------------------------------------------------------- fuzz

_VALUES = [[], [1], {}, {'a': []}, None, True, False, 10 ** 400, -10 ** 400, -1, 1.5, 1e308, 'x', '',
           'ü' * 70, [[[]]], [{'name': []}], {'t': []}]
_PARSERS = {'obs': P.parse_obs, 'chunk': P.parse_chunk, 'ready': P.parse_ready, 'error': P.parse_error,
            'bye': P.parse_bye, 'hello': P.parse_hello}


def test_fuzz_only_protocol_error():
    """>= 60 000 inputs (byte mutations, type substitution of every header value, non-bytes): only
    ``ProtocolError`` ever escapes (R-2; the model is the architect's fuzz_v4)."""
    st = np.arange(6, dtype=np.float32)
    frames = [P.build_obs(3, 15, {'gripper': _JPEG_A[:2000]}, st),
              P.build_chunk(3, 15, np.zeros((50, 6), np.float32)),
              P.build_ready(server_sha='x', policy=_POLICY, limits=_LIMITS),
              _hello(cameras=[('gripper', 640, 480)]),
              P.build_error('ended', seq=1, reason='expired'), P.build_error('version', server_v=[1]),
              P.build_bye('robot_stop', {'a': 1})]
    decoded = [P.decode(f) for f in frames]
    rng = random.Random(7)
    escaped = {}
    count = 0

    def run(buf):
        nonlocal count
        count += 1
        try:
            h, pl = P.decode(buf)
            _PARSERS[h['t']](h, pl)
        except P.ProtocolError:
            pass
        except Exception as e:  # noqa: BLE001 - the point of the test
            key = f'{type(e).__name__}: {str(e)[:60]}'
            escaped[key] = escaped.get(key, 0) + 1

    for _ in range(30_000):
        k = rng.randrange(len(frames))
        h0, pl = decoded[k]
        h = json.loads(json.dumps(h0))
        for _ in range(rng.randint(1, 3)):          # type substitution on 1-3 keys, possibly nested
            target = h
            if rng.random() < 0.4:
                nested = [key for key, v in h.items() if isinstance(v, (dict, list)) and v]
                if nested:
                    target = h[rng.choice(nested)]
                    if isinstance(target, list):
                        target = target[0] if isinstance(target[0], dict) else h
            if isinstance(target, dict) and target:
                key = rng.choice(list(target.keys()) + ['t', 'reason', 'code', 'seq', 'server_v', 'retry'])
                target[key] = copy.deepcopy(rng.choice(_VALUES))
        try:
            raw = json.dumps(h, ensure_ascii=False).encode()
        except ValueError:
            raw = b''
        if 1 <= len(raw) <= P.MAX_HEADER_BYTES:
            run(P.U32.pack(len(raw)) + raw + bytes(pl))
        else:
            run(raw)
        b = bytearray(frames[k])                     # byte mutations
        for _ in range(rng.randint(1, 4)):
            pos = rng.randrange(min(len(b), 600))
            b[pos] = rng.randrange(256)
        if rng.random() < 0.1:
            b = b[:rng.randrange(len(b))]
        run(bytes(b))
    for bad in ['text frame', 123, None, ['x'], {'t': 'obs'}, 1.5, object(),
                bytearray(b'\x05\x00\x00\x00{"t":1}'), memoryview(b'\x00\x00\x00\x00'),
                memoryview(np.zeros(4, np.int32)), b'\x01\x00\x00\x00[' + b'[' * 100_000,
                P.U32.pack(4096) + b'[' * 4096]:          # nesting deep enough for a RecursionError
        run(bad)
    assert count >= 60_000
    assert escaped == {}


# ---------------------------------------------------------------- JPEG helpers

def _jpegs():
    cv2 = pytest.importorskip('cv2')
    rng = np.random.default_rng(3)
    img = (rng.random((480, 640, 3)) * 255).astype(np.uint8)
    out = {}
    out['baseline'] = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
    out['progressive'] = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_PROGRESSIVE, 1])[1].tobytes()
    out['optimized'] = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_OPTIMIZE, 1])[1].tobytes()
    out['grey_progressive'] = cv2.imencode(
        '.jpg', img[:, :, 0], [cv2.IMWRITE_JPEG_PROGRESSIVE, 1])[1].tobytes()
    out['png'] = cv2.imencode('.png', img)[1].tobytes()
    out['small'] = cv2.imencode('.jpg', img[:7, :13])[1].tobytes()
    return out


def test_jpeg_size_baseline_progressive_optimized_png_truncated():
    j = _jpegs()
    assert P.jpeg_size(j['baseline']) == (640, 480)
    assert P.jpeg_size(j['progressive']) == (640, 480)
    assert P.jpeg_size(j['optimized']) == (640, 480)
    assert P.jpeg_size(j['grey_progressive']) == (640, 480)
    assert P.jpeg_size(j['small']) == (13, 7)
    assert P.jpeg_size(memoryview(j['baseline'])) == (640, 480)
    assert P.jpeg_size(j['png']) is None
    # Truncated before the SOF: no size; truncated after it: the size is already known.
    sof = j['baseline'].index(b'\xff\xc0')
    assert P.jpeg_size(j['baseline'][:sof + 5]) is None
    assert P.jpeg_size(j['baseline'][:sof + 2 + 17]) == (640, 480)
    for junk in (b'', b'\xff', b'\xff\xd8', b'\xff\xd8\xff', b'\x00' * 64, b'\xff\xd8\x00\x00\x00\x00',
                 b'\xff\xd8\xff\xda\x00\x08' + bytes(8), b'\xff\xd8\xff\xd9\x00\x00',
                 b'\xff\xd8\xff\xc0\x00\x05\x08\x00\x01\x00',
                 b'\xff\xd8\xff\xc0\x00\x11\x08\x00\x00\x00\x00' + bytes(12)):
        assert P.jpeg_size(junk) is None, junk
    # A hand-made SOF2 for 8192 x 8192 reads without any decode.
    sof2 = b'\xff\xd8\xff\xc2\x00\x11\x08\x20\x00\x20\x00\x03' + bytes(9)
    assert P.jpeg_size(sof2) == (8192, 8192)


def test_jpeg_scans_baseline_1_progressive_10_cap_16():
    j = _jpegs()
    assert P.jpeg_scans(j['baseline']) == 1
    assert P.jpeg_scans(j['optimized']) == 1
    assert P.jpeg_scans(j['progressive']) == 10
    assert P.jpeg_scans(j['grey_progressive']) == 6
    assert P.jpeg_scans(b'\xff\xd8\xff\xd9') == 0
    assert P.jpeg_scans(memoryview(j['baseline'])) == 1
    assert P.MAX_JPEG_SCANS == 16
    assert P.jpeg_scans(b'\xff\xd8' + b'\xff\xda\x00\x02' * 17) == 17 > P.MAX_JPEG_SCANS


# ---------------------------------------------------------------- constants

def test_url_schemes():
    assert P.url(P.INFER_PATH) == P.CLOUD_BASE_URL + '/v1/infer'
    assert P.url(P.HELLO_PATH, 'http://127.0.0.1:8000/') == 'http://127.0.0.1:8000/v1/hello'
    assert P.url(P.BYE_PATH, 'https://example.test') == 'https://example.test/v1/bye'
    for bad in ('ws://x', 'wss://x', 'ftp://x', 'x', ''):
        with pytest.raises(ValueError):
            P.url(P.INFER_PATH, bad)
    assert (P.HEALTH_PATH, P.HELLO_PATH, P.INFER_PATH, P.BYE_PATH, P.PREFETCH_PATH) == (
        '/v1/health', '/v1/hello', '/v1/infer', '/v1/bye', '/v1/prefetch')
    assert P.CONTENT_TYPE == 'application/octet-stream'


def test_header_names_and_token_shapes():
    assert P.EDGE_AUTH_HEADER == 'Modal-Authorization'
    assert P.CONNECT_AUTH_HEADER == 'Authorization'
    assert P.MODAL_SESSION_HEADER == 'x-modal-server-session-id'
    assert P.CONN_HEADER == 'X-Conn-Id'

    ok_edge = ['a' * 16, 'x' * 4096, 'wk-AbC_123.~!$%&()*+,/:;<=>?@[]^`{|}', 'Z' * 64]
    for t in ok_edge:
        assert P.EDGE_TOKEN_RE.fullmatch(t), t
    bad_edge = ['a' * 15, 'x' * 4097, 'a' * 10 + ' ' + 'a' * 10, 'a' * 20 + '\r\n', 'a' * 20 + '\n',
                'a' * 10 + '\t' + 'a' * 10, 'a' * 20 + 'ü', 'a' * 20 + '\x7f', '']
    for t in bad_edge:
        assert not P.EDGE_TOKEN_RE.fullmatch(t), repr(t)

    for s in ('sd-1', 'sd-AbC_9-x', 'sd-' + 'a' * 128):
        assert P.MODAL_SESSION_ID_RE.fullmatch(s), s
    for s in ('sd-', 'xd-abc', 'sd-a b', 'sd-' + 'a' * 129, 'SD-abc', 'sd-ü', ' sd-abc', 'sd-abc\n'):
        assert not P.MODAL_SESSION_ID_RE.fullmatch(s), repr(s)

    import secrets
    assert P.TOKEN_RE.fullmatch(secrets.token_urlsafe(32))
    assert P.CONN_ID_RE.fullmatch(secrets.token_hex(16))
    assert not P.TOKEN_RE.fullmatch('A' * 42) and not P.TOKEN_RE.fullmatch('A' * 44)
    assert not P.CONN_ID_RE.fullmatch('AB' * 16)
    assert P.NAME_RE.fullmatch('gripper') and not P.NAME_RE.fullmatch('a' * 33)


def test_caps_are_the_spec_values():
    """Literal pins of every cap (spec §2.1; test_constant_pins.py demands a literal for each one a test uses)."""
    assert P.MAX_FRAME_BYTES == 1_572_864
    assert P.MAX_HEADER_BYTES == 4096
    assert P.MAX_JPEG_BYTES == 524_288
    assert P.MAX_CAMERAS == 4
    assert P.MAX_STATE_DIM == 32
    assert P.MAX_ACTION_DIM == 32
    assert P.MAX_CHUNK_N == 100
    assert P.MAX_SEQ == 9_007_199_254_740_992
    assert P.MAX_OBS_E == 2_147_483_647
    assert P.MAX_TASK_CHARS == 512
    assert P.MAX_JPEG_SCANS == 16


def test_cloud_base_url_is_the_server_host():
    assert P.CLOUD_BASE_URL == 'https://svendanilborodun--edubotics-inference-cloudinference.eu-west.modal.direct'
    assert P.PROTOCOL_VERSION == 1 and P.SERVER_ACCEPTS == (1,)


def test_no_physical_ai_server_import_and_no_edubotics_literal():
    tree = ast.parse(_SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, 'no relative import'
            imported.add((node.module or '').split('.')[0])
    assert imported == {'json', 're', 'struct', 'numpy'}
    # ci.yml env-forwarding-guard demands a compose forward for every such literal in the package.
    assert not re.search(r'EDUBOTICS_[A-Z0-9_]+', _SRC)


def test_no_websocket_anywhere():
    for needle in ('ws_url', 'WS_PATH', 'TRANSPORTS', '/v1/ws', 'ws_connect', 'WSMsgType'):
        assert needle not in _SRC, needle
    assert 'websocket' not in _SRC.lower()
    assert not hasattr(P, 'WS_PATH') and not hasattr(P, 'TRANSPORTS')
