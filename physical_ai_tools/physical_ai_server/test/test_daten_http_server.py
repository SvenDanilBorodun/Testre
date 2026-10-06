"""Daten 2.0: the sidecar over a real socket (spec §B3, §B5–§B8, §J.4).

Routing with a temp root: an unknown route is 404 JSON; a bad, an expired and a
wrong-scope token are 403 with their codes; a ``ds`` token reads only the
dataset it names (the URL carries none); ``..``/absolute/symlinked ids are
refused; no body ever contains the dataset root; every answer is JSON or media
with ``nosniff``. The pools: with both media slots busy a third media request
WAITS and is answered when a slot frees; the 17th waiter is ``503 overloaded``
with ``Retry-After``; a waiter past ``MEDIA_WAIT_S`` (patched) is 503; a hub
request is answered while both media slots are busy; N parallel GETs of one
thumbnail build it once and all end 200; connection 33 is 503; an idle socket
is closed after the (patched) timeout. An unaligned episode's clip is
``409 unplayable``; an ``unsupported`` dataset 409; ``hubstate`` of a crashed
(``in_session``) dataset answers 200 with ``local`` from its ``info.json`` and
of a dataset without a readable ``info.json`` with ``local: null`` (U-6).
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

pytest.importorskip('av')
pytest.importorskip('pyarrow')

from physical_ai_server.daten import contract as C  # noqa: E402
from physical_ai_server.daten import http_server as HS  # noqa: E402
from physical_ai_server.daten import library as L  # noqa: E402
from physical_ai_server.daten import link_tokens as LT  # noqa: E402
from physical_ai_server.data_processing import dataset_sync as S  # noqa: E402
from test_daten_hub_cache import FakeApi, make_hub  # noqa: E402
from test_v3_cut_episode import _unaligned  # noqa: E402
from test_v3_surgery_layout import build_dataset  # noqa: E402

NS = 'lena-schmidt'
SECRET = b'z' * 32
LENGTHS = (20, 21, 22)


@pytest.fixture(scope='module')
def root(tmp_path_factory):
    base = tmp_path_factory.mktemp('daten_http')
    ns = base / NS
    build_dataset(ns / 'omx_f_ok', lengths=LENGTHS)
    build_dataset(ns / 'omx_f_other', lengths=(20,), seed=3)
    _unaligned(build_dataset(ns / 'omx_f_unaligned', lengths=LENGTHS))
    unsup = build_dataset(ns / 'omx_f_unsup', lengths=(20,))
    info = json.loads((unsup / 'meta' / 'info.json').read_text())
    info['data_path'] = '/etc/{episode_index}.parquet'
    (unsup / 'meta' / 'info.json').write_text(json.dumps(info))
    build_dataset(ns / 'kaputt', lengths=(20, 20, 20, 20))
    S.session_marker_path(ns / 'kaputt').write_text('{}')
    (ns / 'omx_f_noinfo' / 'meta').mkdir(parents=True)
    (base / 'outsider').mkdir()
    build_dataset(base / 'outsider' / 'omx_f_secret', lengths=(20,))
    (ns / 'omx_f_link').symlink_to(base / 'outsider' / 'omx_f_secret', target_is_directory=True)
    return base


class Running:
    def __init__(self, root, tmp_path, max_connections=C.MAX_CONNECTIONS, media=None, hub_pool=None,
                 api=None):
        self.library = L.Library(root=root, robot_type='omx_f', clip_tmp_dir=tmp_path / 'clips')
        self.api = api or FakeApi(account=NS)
        self.hub, self.token_holder = make_hub(self.api)
        self.server = HS.make_server('127.0.0.1', 0, library=self.library, hub=self.hub, secret=SECRET,
                                     max_connections=max_connections, media=media, hub_pool=hub_pool)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                                       daemon=True)
        self.thread.start()
        self.bodies = []

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def get(self, path, headers=None, method='GET', timeout=30):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        try:
            conn.request(method, path, headers=headers or {})
            r = conn.getresponse()
            body = r.read()
            self.bodies.append(body)
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body
        finally:
            conn.close()


@pytest.fixture()
def srv(root, tmp_path):
    s = Running(root, tmp_path)
    yield s
    s.close()


def ds(dataset_id, **kw):
    return f'{C.API_PREFIX}/ds/{LT.mint(SECRET, LT.dataset_scope(dataset_id), **kw)}'


def lib_url(**kw):
    return f'{C.API_PREFIX}/lib/{LT.mint(SECRET, LT.LIB_SCOPE, **kw)}'


def as_json(resp):
    status, headers, body = resp
    assert headers['content-type'] == 'application/json; charset=utf-8'
    assert headers['x-content-type-options'] == 'nosniff'
    return status, json.loads(body)


# ── routing and tokens ───────────────────────────────────────────────────────

def test_unknown_routes_are_404_json(srv):
    for path in ('/', f'{C.API_PREFIX}/', f'{C.API_PREFIX}/lib/x', f'{ds(NS + "/omx_f_ok")}/nothing',
                 f'{ds(NS + "/omx_f_ok")}/episode/a/data', f'{ds(NS + "/omx_f_ok")}/episode/0/video/x.mp4'):
        assert as_json(srv.get(path)) == (404, {'error': 'not_found'}), path


def test_tokens_bad_expired_and_of_the_wrong_scope(srv):
    tok = LT.mint(SECRET, LT.dataset_scope(f'{NS}/omx_f_ok'))
    forged = LT.mint(b'x' * 32, LT.dataset_scope(f'{NS}/omx_f_ok'))
    cases = [
        (f'{C.API_PREFIX}/ds/garbage/summary', 'token_invalid'),
        (f'{C.API_PREFIX}/ds/{forged}/summary', 'token_invalid'),
        (f'{C.API_PREFIX}/ds/{tok[:-1]}A/summary', 'token_invalid'),
        (f'{ds(NS + "/omx_f_ok", now=time.time() - 4000)}/summary', 'token_expired'),
        (lib_url().replace('/lib/', '/ds/') + '/summary', 'scope'),                    # a lib token on a ds route
        (ds(NS + '/omx_f_ok').replace('/ds/', '/lib/') + '/library?ns=' + NS, 'scope'),  # and the reverse
    ]
    for path, code in cases:
        assert as_json(srv.get(path)) == (403, {'error': code}), path


def test_a_ds_token_reads_only_the_dataset_it_names(srv):
    status, body = as_json(srv.get(f'{ds(NS + "/omx_f_ok")}/summary'))
    assert status == 200 and body['id'] == f'{NS}/omx_f_ok' and body['total_episodes'] == 3
    status, body = as_json(srv.get(f'{ds(NS + "/omx_f_other")}/summary'))
    assert status == 200 and body['id'] == f'{NS}/omx_f_other' and body['total_episodes'] == 1


def test_traversal_absolute_and_symlinked_ids_are_refused(srv):
    for bad in ('../outsider/omx_f_secret', '/etc/passwd', f'{NS}/omx_f_link', f'{NS}/..', f'{NS}/omx_f_ok.tmp_edit'):
        for route in ('summary', 'hubstate', 'thumb.jpg', 'episode/0/data', 'episode/0/video/0.mp4'):
            status, body = as_json(srv.get(f'{ds(bad)}/{route}'))
            assert (status, body) == (400, {'error': 'invalid'}), (bad, route)
    status, body = as_json(srv.get(f'{lib_url()}/library?ns={NS},..,outsider/x&hub=0&ids=../outsider/omx_f_secret,{NS}/omx_f_ok'))
    assert status == 200 and [e['id'] for e in body['local']] == [f'{NS}/omx_f_ok']
    assert as_json(srv.get(f'{ds(NS + "/omx_f_missing")}/summary')) == (404, {'error': 'not_found'})


def test_the_library_route(srv):
    status, body = as_json(srv.get(f'{lib_url()}/library?ns={NS}&hub=0'))
    assert status == 200 and body['v'] == 1 and body['robot_type'] == 'omx_f'
    states = {e['name']: e['state'] for e in body['local']}
    assert states['kaputt'] == 'in_session' and states['omx_f_unsup'] == 'unsupported'
    assert 'omx_f_link' not in states
    assert body['hub']['state'] == 'skipped'
    assert set(body['sync']) == {e['id'] for e in body['local']}
    assert {v['reason'] for v in body['sync'].values()} == {'not_asked'}
    assert as_json(srv.get(f'{lib_url()}/library?ns={NS}&hub=yes')) == (400, {'error': 'invalid'})
    many = ','.join(f'{NS}/d{k}' for k in range(C.MAX_LINK_DATASETS + 1))
    assert as_json(srv.get(f'{lib_url()}/library?ids={many}')) == (400, {'error': 'invalid'})


def test_the_library_route_with_the_hub(srv):
    srv.api.add(f'{NS}/omx_f_online', head='o1')
    status, body = as_json(srv.get(f'{lib_url()}/library?ns={NS}&hub=1'))
    assert status == 200 and body['hub']['state'] == 'ok' and body['hub']['account'] == NS
    assert body['sync'][f'{NS}/omx_f_online'] == {'state': 'online', 'reason': None, 'head': 'o1'}
    assert body['sync'][f'{NS}/omx_f_ok'] == {'state': 'local', 'reason': None, 'head': None}
    srv.api.fail['list_datasets'] = ConnectionError('down')
    status, body = as_json(srv.get(f'{lib_url()}/library?ns={NS}&hub=1'))
    assert body['hub']['state'] == 'unreachable'
    assert {v['reason'] for v in body['sync'].values()} == {'unreachable'}
    srv.token_holder['token'] = None
    status, body = as_json(srv.get(f'{lib_url()}/library?ns={NS}&hub=1'))
    assert body['hub']['state'] == 'no_token'
    assert {v['reason'] for v in body['sync'].values()} == {'not_asked'}


def test_the_probe_route(srv):
    srv.api.add(f'{NS}/omx_f_found', head='f1')
    status, body = as_json(srv.get(f'{lib_url()}/hub/probe?repo={NS}/omx_f_found'))
    assert status == 200 and body['found'] is True and body['sha'] == 'f1' and body['refusal'] is None
    status, body = as_json(srv.get(f'{lib_url()}/hub/probe?repo=../x'))
    assert body['refusal'] == 'invalid'
    status, body = as_json(srv.get(f'{lib_url()}/hub/probe?repo={NS}/nothing'))
    assert body['refusal'] == 'not_found'


def test_media_and_refusals(srv):
    status, headers, body = srv.get(f'{ds(NS + "/omx_f_ok")}/episode/1/video/0.mp4')
    assert status == 200 and headers['content-type'] == 'video/mp4' and body[4:8] == b'ftyp'
    assert headers['x-content-type-options'] == 'nosniff' and headers['accept-ranges'] == 'bytes'
    status, headers, body = srv.get(f'{ds(NS + "/omx_f_ok")}/thumb.jpg')
    assert status == 200 and headers['content-type'] == 'image/jpeg' and body[:2] == b'\xff\xd8'
    status, body = as_json(srv.get(f'{ds(NS + "/omx_f_ok")}/episode/2/data'))
    assert status == 200 and body['length'] == 22
    summary = as_json(srv.get(f'{ds(NS + "/omx_f_unaligned")}/summary'))[1]
    bad = next(ep['i'] for ep in summary['episodes'] if not ep['playable'])
    refused = [c for c in range(len(summary['cameras']))
               if srv.get(f'{ds(NS + "/omx_f_unaligned")}/episode/{bad}/video/{c}.mp4')[0] == 409]
    assert refused
    assert as_json(srv.get(f'{ds(NS + "/omx_f_unaligned")}/episode/{bad}/video/{refused[0]}.mp4')) == \
        (409, {'error': 'unplayable'})
    for route in ('summary', 'thumb.jpg', 'episode/0/data', 'episode/0/video/0.mp4'):
        assert as_json(srv.get(f'{ds(NS + "/omx_f_unsup")}/{route}')) == (409, {'error': 'unsupported'}), route
        assert as_json(srv.get(f'{ds(NS + "/kaputt")}/{route}')) == (409, {'error': 'in_session'}), route
    assert as_json(srv.get(f'{ds(NS + "/omx_f_ok")}/episode/7/video/0.mp4')) == (404, {'error': 'not_found'})


def test_hubstate_answers_for_a_crashed_dataset_and_one_without_info(srv, root):
    status, body = as_json(srv.get(f'{ds(NS + "/kaputt")}/hubstate'))
    assert status == 200 and body['id'] == f'{NS}/kaputt'
    assert body['local']['total_episodes'] == 4 and body['local']['duration_s'] == round(80 / 30, 3)
    assert body['local']['modified_at'].endswith('Z')
    assert body['hub'] is None and body['new_repo_private'] is False
    assert body['sync'] == {'state': 'local', 'reason': None}     # own namespace, nothing on the hub
    status, body = as_json(srv.get(f'{ds(NS + "/omx_f_noinfo")}/hubstate'))
    assert status == 200 and body['local'] is None
    srv.token_holder['token'] = None
    status, body = as_json(srv.get(f'{ds(NS + "/kaputt")}/hubstate'))
    assert body['sync'] == {'state': 'unknown', 'reason': 'not_asked'}


def test_no_body_ever_contains_the_dataset_root(srv, root):
    srv.get(f'{lib_url()}/library?ns={NS}&hub=1')
    for name in ('omx_f_ok', 'kaputt', 'omx_f_unsup', 'omx_f_noinfo', 'omx_f_missing', '../x'):
        for route in ('summary', 'hubstate', 'thumb.jpg', 'episode/0/data', 'episode/0/video/0.mp4',
                      'episode/99/data'):
            srv.get(f'{ds(NS + "/" + name)}/{route}')
    srv.get(f'{C.API_PREFIX}/ds/garbage/summary')
    srv.get(f'{ds(NS + "/omx_f_ok")}/summary', method='POST')
    needle = str(root).encode()
    assert len(srv.bodies) > 30
    assert not [b for b in srv.bodies if needle in b or b'/tmp' in b or b'Traceback' in b]


# ── load limits (§B8) ────────────────────────────────────────────────────────

class Gate:
    """Holds every ``episode_data`` call until released."""

    def __init__(self, library):
        self.release = threading.Event()
        self.entered = 0
        self.lock = threading.Lock()
        real = library.episode_data

        def slow(dataset_id, i):
            with self.lock:
                self.entered += 1
            self.release.wait(30)
            return real(dataset_id, i)
        library.episode_data = slow

    def wait_entered(self, n, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.entered >= n:
                    return True
            time.sleep(0.01)
        return False


def _wait(pred, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _background(srv, path, out):
    t = threading.Thread(target=lambda: out.append(srv.get(path)), daemon=True)
    t.start()
    return t


def test_media_requests_wait_for_a_slot_and_the_17th_waiter_is_refused(root, tmp_path):
    srv = Running(root, tmp_path)
    try:
        gate = Gate(srv.library)
        url = f'{ds(NS + "/omx_f_ok")}/episode/0/data'
        out = []
        threads = [_background(srv, url, out) for _ in range(C.MEDIA_WORKERS)]
        assert gate.wait_entered(C.MEDIA_WORKERS)
        threads += [_background(srv, url, out) for _ in range(C.MEDIA_QUEUE_MAX)]
        assert _wait(lambda: srv.server.sidecar.media._waiting == C.MEDIA_QUEUE_MAX)
        status, headers, body = srv.get(url)                           # the 17th waiter
        assert (status, json.loads(body), headers['retry-after']) == (503, {'error': 'overloaded'}, '2')
        # a hub request is answered while both media slots are busy
        srv.api.add(f'{NS}/omx_f_found', head='f1')
        t0 = time.monotonic()
        assert srv.get(f'{lib_url()}/hub/probe?repo={NS}/omx_f_found')[0] == 200
        assert time.monotonic() - t0 < 5
        assert gate.entered == C.MEDIA_WORKERS                         # the waiters really wait
        gate.release.set()
        for t in threads:
            t.join(30)
        assert [r[0] for r in out] == [200] * (C.MEDIA_WORKERS + C.MEDIA_QUEUE_MAX)
    finally:
        srv.close()


def test_a_waiter_past_the_wait_bound_is_refused(root, tmp_path):
    srv = Running(root, tmp_path, media=HS.Pool(C.MEDIA_WORKERS, C.MEDIA_QUEUE_MAX, 0.3))
    try:
        gate = Gate(srv.library)
        url = f'{ds(NS + "/omx_f_ok")}/episode/0/data'
        threads = [_background(srv, url, []) for _ in range(C.MEDIA_WORKERS)]
        assert gate.wait_entered(C.MEDIA_WORKERS)
        t0 = time.monotonic()
        status, headers, _ = srv.get(url)
        assert (status, headers['retry-after']) == (503, '2')
        assert 0.25 <= time.monotonic() - t0 < 3
        gate.release.set()
        for t in threads:
            t.join(30)
    finally:
        srv.close()


def test_parallel_gets_of_one_thumbnail_build_it_once_and_all_succeed(root, tmp_path, monkeypatch):
    srv = Running(root, tmp_path)
    try:
        builds = []
        real = srv.library._build_thumb

        def counted(*a):
            builds.append(a)
            time.sleep(0.3)                                            # a slow first build
            return real(*a)
        monkeypatch.setattr(srv.library, '_build_thumb', counted)
        out = []
        threads = [_background(srv, f'{ds(NS + "/omx_f_ok")}/thumb.jpg', out) for _ in range(20)]
        for t in threads:
            t.join(30)
        assert [r[0] for r in out] == [200] * 20
        assert len(builds) == 1
    finally:
        srv.close()


# A SYN dropped from a full accept queue is retried by the client only after a
# full second (Linux); a connect that took this long was not accepted at once.
SYN_RETRY_S = 0.9


def test_connection_33_is_refused_and_an_idle_socket_is_closed(root, tmp_path, monkeypatch):
    monkeypatch.setattr(HS.Handler, 'timeout', 3.0)
    srv = Running(root, tmp_path)
    held = []
    try:
        # Every idle timer starts at its socket's accept, so none can end before
        # `timeout` after the first connect (measured from here, never from the
        # end of a connect phase whose length depends on the host).
        t0 = time.monotonic()
        for _ in range(C.MAX_CONNECTIONS):
            held.append(socket.create_connection(('127.0.0.1', srv.port), timeout=10))
        assert time.monotonic() - t0 < SYN_RETRY_S, 'a connect waited for a SYN retry'
        assert _wait(lambda: srv.server._connections._value == 0)
        status, headers, body = srv.get(f'{C.API_PREFIX}/health')
        assert (status, json.loads(body), headers['retry-after']) == (503, {'error': 'overloaded'}, '2')
        assert headers['x-content-type-options'] == 'nosniff'
        # the idle ones are closed after the socket timeout, freeing their slots
        held[0].settimeout(10)
        assert held[0].recv(1) == b''
        assert 2.9 <= time.monotonic() - t0 < 6
        assert _wait(lambda: srv.server._connections._value == C.MAX_CONNECTIONS, timeout=10)
        assert srv.get(f'{C.API_PREFIX}/health')[0] == 200
    finally:
        for s in held:
            s.close()
        srv.close()


def test_a_burst_of_parallel_connects_is_accepted_at_once(root, tmp_path):
    """V2-1: the listen backlog holds a burst of the cap's size. With
    socketserver's default (5) a page's 30 parallel thumbnails overflowed the
    accept queue on Linux and part of them waited a SYN retry (1 s or more)."""
    srv = Running(root, tmp_path)
    assert srv.server.request_queue_size == HS.listen_backlog() >= 2 * C.MAX_CONNECTIONS
    assert HS.listen_backlog(4) == HS.listen_backlog()                  # never below twice the contract's cap
    socks, times, errors = [], [], []
    lock = threading.Lock()
    barrier = threading.Barrier(C.MAX_CONNECTIONS)

    def connect():
        try:
            barrier.wait(10)
            t = time.monotonic()
            s = socket.create_connection(('127.0.0.1', srv.port), timeout=10)
            with lock:
                times.append(time.monotonic() - t)
                socks.append(s)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    try:
        threads = [threading.Thread(target=connect, daemon=True) for _ in range(C.MAX_CONNECTIONS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert errors == [] and len(times) == C.MAX_CONNECTIONS
        assert max(times) < SYN_RETRY_S, sorted(round(t, 3) for t in times)
    finally:
        for s in socks:
            s.close()
        srv.close()


def test_the_load_limits_are_the_specs():
    """§B8's numbers and the contract values the sidecar enforces, as literals."""
    assert C.HTTP_PORT == 8095
    assert C.MAX_CONNECTIONS == 32
    assert C.SOCKET_TIMEOUT_S == 30
    assert C.MEDIA_WORKERS == 2
    assert C.MEDIA_QUEUE_MAX == 16
    assert C.MEDIA_WAIT_S == 20
    assert C.HUB_WORKERS == 2
    assert C.HUB_QUEUE_MAX == 8
    assert C.HUB_WAIT_S == 20
    assert C.HUB_CACHE_MAX == 512
    assert C.HUB_CALL_TIMEOUT_S == 10
    assert C.MAX_LINK_DATASETS == 200
    assert C.TOKEN_TTL_S == 1800
    assert C.CLIP_CACHE_MAX_BYTES == 100663296
    assert C.DOWNLOAD_POLL_S == 0.5
    assert C.DOWNLOAD_STALL_S == 120
    assert C.DOWNLOAD_TIMEOUT_S == 21600
    assert C.EDIT_TIMEOUT_S == 3600
    assert C.JOB_KEEP_S == 600
    assert C.JOB_KEEP_MAX == 10
    assert L.THUMB_WIDTH == 320
