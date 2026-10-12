"""Every refusal of the observation pipeline (spec §3.3 steps 1-6, D25, D40) and a fuzz that never
reaches the engine. The policy here takes two 640x480 cameras, as a real EduBotics ACT does."""

from __future__ import annotations

import copy
import json
import random

import numpy as np
import pytest

from conftest import SUMMARY, FakeClock, FakeEngine, headers, hello_frame, jpeg
import cloud_protocol as P

W, H = 640, 480
POLICY = dict(SUMMARY, image_shapes={'gripper': [3, H, W], 'scene': [3, H, W]})


def _err(r):
    assert r.headers['content-type'] == P.CONTENT_TYPE
    return P.parse_error(*P.decode(r.content))


@pytest.fixture
def rig(server):
    clock = FakeClock()
    engine = FakeEngine(POLICY)
    client, backend, _, _ = server(engine=engine, clock=clock, ttl=1e9)
    client.__enter__()
    body = hello_frame(P, cameras=[('gripper', W, H), ('scene', W, H)])
    assert client.post(P.HELLO_PATH, content=body, headers=headers()).status_code == 200
    good = {'gripper': jpeg(W, H, seed=1), 'scene': jpeg(W, H, seed=2)}

    def post(frame):
        clock.advance(1.0)                                # keep the rate bucket out of the way
        return client.post(P.INFER_PATH, content=frame, headers=headers())
    yield post, engine, good
    client.__exit__(None, None, None)


class Spy:
    def __init__(self, monkeypatch):
        import cv2
        self.real = cv2.imdecode
        self.calls = 0
        monkeypatch.setattr(cv2, 'imdecode', self)

    def __call__(self, *a, **k):
        self.calls += 1
        return self.real(*a, **k)


def _seventeen_scans(data):
    """A valid JPEG whose COM segment carries 16 more SOS byte pairs: 17 by count, decodable, SOF intact."""
    com = b'\xff\xda' * 16
    return data[:2] + b'\xff\xfe' + (len(com) + 2).to_bytes(2, 'big') + com + data[2:]


def test_every_refusal(rig, monkeypatch):
    post, engine, good = rig
    state = np.arange(6, dtype=np.float32)
    spy = Spy(monkeypatch)
    cases = [
        (b'', 400, 'bad_frame'),
        (b'\x00\x01\x02', 400, 'bad_frame'),
        (P.build_bye('stall'), 400, 'bad_frame'),                                  # not an obs
        (hello_frame(P), 400, 'bad_frame'),
        (P.encode({'t': 'obs', 'seq': 1, 'obs_e': 0, 'cams': [], 'state_dim': 6}), 400, 'bad_frame'),
        (P.build_obs(1, 0, {'gripper': good['gripper']}, state), 422, 'cameras'),       # one missing
        (P.build_obs(1, 0, {**good, 'wrist': good['scene']}, state), 422, 'cameras'),   # one extra
        (P.build_obs(1, 0, {'gripper': good['gripper'], 'Scene': good['scene']}, state), 422, 'cameras'),
        (P.build_obs(1, 0, good, np.arange(7, dtype=np.float32)), 422, 'state'),
        (P.build_obs(1, 0, good, np.arange(5, dtype=np.float32)), 422, 'state'),
        (P.build_obs(1, 0, {**good, 'scene': jpeg(320, 240)}, state), 422, 'shape'),     # wrong size
        (P.build_obs(1, 0, {**good, 'scene': jpeg(H, W)}, state), 422, 'shape'),         # rotated
        (P.build_obs(1, 0, {**good, 'scene': b'not a jpeg at all'}, state), 422, 'shape'),
        (P.build_obs(1, 0, {**good, 'scene': _seventeen_scans(good['scene'])}, state), 422, 'shape'),
    ]
    for frame, status, code in cases:
        r = post(frame)
        assert r.status_code == status, (code, r.status_code)
        got = _err(r)
        assert got[0] == code
        if status == 422:
            assert got[2] == 1                               # the seq is echoed on a pipeline refusal
    assert spy.calls == 0 and engine.calls == []
    # A JPEG whose SOF is right but which does not decode (None or an exception): 'shape' after the decoder ran.
    import chunk_engine
    real = chunk_engine.decode_jpeg_rgb
    monkeypatch.setattr(chunk_engine, 'decode_jpeg_rgb', lambda data, w, h: None)
    r = post(P.build_obs(2, 0, good, state))
    assert r.status_code == 422 and _err(r) == ('shape', False, 2, None, None)

    def boom(data, w, h):
        raise MemoryError('decoder')
    monkeypatch.setattr(chunk_engine, 'decode_jpeg_rgb', boom)
    r = post(P.build_obs(2, 0, good, state))
    assert r.status_code == 422 and _err(r) == ('shape', False, 2, None, None)
    monkeypatch.setattr(chunk_engine, 'decode_jpeg_rgb', real)
    assert engine.calls == []
    assert P.jpeg_scans(_seventeen_scans(good['scene'])) == 17 and P.jpeg_size(_seventeen_scans(good['scene'])) == (W, H)
    # The well-formed observation is served.
    r = post(P.build_obs(3, 5, good, state))
    assert r.status_code == 200 and spy.calls >= 2


def test_the_8192_bomb_never_reaches_imdecode(rig, monkeypatch):
    import cv2
    post, engine, good = rig
    bomb = cv2.imencode('.jpg', np.full((8192, 8192, 3), 127, np.uint8),
                        [cv2.IMWRITE_JPEG_QUALITY, 1, cv2.IMWRITE_JPEG_OPTIMIZE, 1,
                         cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_420])[1].tobytes()
    assert len(bomb) <= P.MAX_JPEG_BYTES
    spy = Spy(monkeypatch)
    r = post(P.build_obs(4, 0, {**good, 'gripper': bomb}, np.zeros(6, np.float32)))
    assert r.status_code == 422 and _err(r)[0] == 'shape'
    assert spy.calls == 0 and engine.calls == []


def test_a_17_scan_640x480_jpeg_is_refused_before_decode(rig, monkeypatch):
    import cv2
    post, engine, good = rig
    progressive = cv2.imencode('.jpg', np.full((H, W, 3), 90, np.uint8), [cv2.IMWRITE_JPEG_PROGRESSIVE, 1])[1].tobytes()
    assert P.jpeg_scans(progressive) == 10
    seventeen = progressive[:2] + b'\xff\xfe\x00\x10' + b'\xff\xda' * 7 + progressive[2:]
    assert P.jpeg_scans(seventeen) == 17 and P.jpeg_size(seventeen) == (W, H)
    assert cv2.imdecode(np.frombuffer(seventeen, np.uint8), cv2.IMREAD_COLOR).shape == (H, W, 3)
    spy = Spy(monkeypatch)
    r = post(P.build_obs(5, 0, {**good, 'scene': seventeen}, np.zeros(6, np.float32)))
    assert r.status_code == 422 and _err(r) == ('shape', False, 5, None, None)
    assert spy.calls == 0 and engine.calls == []
    # Sixteen is still a JPEG the server takes (the cap admits libjpeg's progressive script).
    sixteen = progressive[:2] + b'\xff\xfe\x00\x0e' + b'\xff\xda' * 6 + progressive[2:]
    assert P.jpeg_scans(sixteen) == 16
    assert post(P.build_obs(6, 0, {**good, 'scene': sixteen}, np.zeros(6, np.float32))).status_code == 200


_VALUES = [[], [1], {}, None, True, False, 10 ** 400, -1, 1.5, 'x', '', [[[]]], [{'name': []}]]


def test_fuzz_never_reaches_the_engine_and_always_gets_an_error_frame(rig):
    post, engine, good = rig
    small = {'gripper': good['gripper'], 'scene': good['scene']}
    base = P.build_obs(7, 35, small, np.arange(6, dtype=np.float32))
    h0, pl0 = P.decode(base)
    rng = random.Random(11)
    served = errors = 0
    for i in range(300):
        if i % 2:
            h = copy.deepcopy(h0)
            for _ in range(rng.randint(1, 3)):
                cams = h.get('cams')
                target = h
                if rng.random() >= 0.6 and isinstance(cams, list) and cams and isinstance(cams[0], dict):
                    target = cams[rng.randrange(len(cams))] if all(isinstance(c, dict) for c in cams) else h
                key = rng.choice(list(target) + ['t', 'task'])
                target[key] = copy.deepcopy(rng.choice(_VALUES))
            raw = json.dumps(h).encode()
            frame = P.U32.pack(len(raw)) + raw + bytes(pl0)
        else:
            b = bytearray(base)
            for _ in range(rng.randint(1, 6)):
                pos = rng.randrange(min(len(b), 900))
                b[pos] = rng.randrange(256)
            frame = bytes(b)
        before = len(engine.calls)
        r = post(frame)
        if r.status_code == 200:
            served += 1                                       # the mutation left a valid observation
            assert len(engine.calls) == before + 1
            P.parse_chunk(*P.decode(r.content))
        else:
            errors += 1
            assert len(engine.calls) == before, 'a refused frame reached the engine'
            code = _err(r)[0]
            assert code in ('bad_frame', 'cameras', 'state', 'shape'), code
            assert r.status_code in (400, 422)
    assert served + errors == 300 and errors >= 100
