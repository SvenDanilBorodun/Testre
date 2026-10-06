"""Daten 2.0: the sidecar's hub cache (spec §B8, S-3).

Everything derived from a head — the top-level tree view, ``info.json`` /
``stats.json`` at that head, the recursive file listing — is cached in ONE LRU
keyed ``(repo_id, head)`` with no TTL; ``whoami`` per token fingerprint; the
namespace listing is asked on every ``hub=1`` request. A second library request
for an unchanged head makes no tree call; a moved head does; the LRU evicts
past its size; a clock that jumps ten years changes nothing; a synced dataset
whose record holds the current head needs no tree call at all; two identical
concurrent requests share one listing.

``FakeApi`` (a counting stand-in for ``HfApi`` with the call shapes the sidecar
uses) and ``CountingHubReads`` are shared with ``test_daten_library.py`` and
``test_daten_http_server.py``.
"""

from __future__ import annotations

import ast
import collections
import datetime
from pathlib import Path
import threading
import time
import types

import pytest

# hub_reads imports v3_surgery (PyAV, pyarrow, numpy) at module level; a Python
# without them skips this module (and the two that share its fakes) instead of
# aborting the whole collection (C-1).
pytest.importorskip('av')
pytest.importorskip('pyarrow')
pytest.importorskip('numpy')

from physical_ai_server.daten import contract as C  # noqa: E402
from physical_ai_server.daten import hub_reads as HR  # noqa: E402

TOKEN_A = 'hf_' + 'a' * 34
TOKEN_B = 'hf_' + 'b' * 34
INFO_V3 = {'codebase_version': 'v3.0', 'robot_type': 'omx_f', 'fps': 30, 'total_episodes': 4,
           'total_frames': 2400, 'data_path': 'data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet',
           'video_path': 'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4',
           'features': {'observation.images.scene': {'dtype': 'video',
                                                     'info': {'video.width': 64, 'video.height': 48}},
                        'observation.state': {'dtype': 'float32', 'names': ['joint1']},
                        'action': {'dtype': 'float32', 'names': ['joint1']}}}
STATS = {'action': {'mean': [0], 'min': [0]}, 'observation.state': {'mean': [0]}}


class RepositoryNotFoundError(Exception):
    """Named like huggingface_hub's: ``hub_reads`` classifies by class name."""


class HttpStatusError(Exception):
    def __init__(self, status):
        super().__init__(f'HTTP {status}')
        self.response = types.SimpleNamespace(status_code=status)


class FakeApi:
    """Repos ``{id: {head, private, trees, title, files, info, stats}}``; every
    call counted in ``calls``. ``fail`` maps a call name to an exception."""

    def __init__(self, account='lena-schmidt'):
        self.account = account
        self.repos = {}
        self.calls = collections.Counter()
        self.fail = {}
        self.gate = None               # an Event list_datasets waits on (single-flight test)

    def add(self, repo, head='h1', private=True, files=None, info=None, title='Upload',
            trees=None):
        files = files if files is not None else {'data/chunk-000/file-000.parquet': (100, 'b1', None),
                                                 'meta/info.json': (50, 'b2', None)}
        self.repos[repo] = {'head': head, 'private': private, 'title': title, 'files': files,
                            'info': dict(info or INFO_V3), 'stats': dict(STATS),
                            'trees': trees or {d: f'{d}-{repo}-{head}' for d in ('data', 'meta', 'videos')}}
        return self.repos[repo]

    def move(self, repo, head):
        r = self.repos[repo]
        r['head'] = head
        r['trees'] = {d: f'{d}-{repo}-{head}' for d in ('data', 'meta', 'videos')}

    def _check(self, name, repo=None):
        self.calls[name] += 1
        if name in self.fail:
            raise self.fail[name]
        if repo is not None and repo not in self.repos:
            raise RepositoryNotFoundError(repo)

    def whoami(self):
        self._check('whoami')
        return {'name': self.account, 'orgs': []}

    def list_datasets(self, author=None):
        self._check('list_datasets')
        if self.gate is not None:
            self.gate.wait(5)
        return [types.SimpleNamespace(id=k, sha=v['head'], private=v['private'],
                                      last_modified=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))
                for k, v in sorted(self.repos.items()) if k.split('/')[0] == author]

    def dataset_info(self, repo, revision=None, files_metadata=False):
        self._check('dataset_info', repo)
        r = self.repos[repo]
        return types.SimpleNamespace(
            id=repo, sha=r['head'], private=r['private'],
            last_modified=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc),
            siblings=[types.SimpleNamespace(rfilename=p, size=f[0]) for p, f in r['files'].items()])

    def list_repo_tree(self, repo, repo_type=None, revision=None, expand=False, recursive=False):
        self._check('files' if recursive else 'tree', repo)
        r = self.repos[repo]
        assert revision == r['head'], 'the sidecar reads AT a head'
        if recursive:
            return [types.SimpleNamespace(path=p, size=s, blob_id=b,
                                          lfs=types.SimpleNamespace(sha256=l) if l else None)
                    for p, (s, b, l) in r['files'].items()]
        lc = types.SimpleNamespace(oid=r['head'], title=r['title'],
                                   date=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))
        return [types.SimpleNamespace(path=d, tree_id=t, last_commit=lc) for d, t in r['trees'].items() if t]

    def get_json(self, repo, head, path):
        self._check('json', repo)
        r = self.repos[repo]
        assert head == r['head']
        return r['info'] if path == 'meta/info.json' else r['stats']


class CountingHubReads(HR.HubReads):
    """The real HubReads; only the one bounded GET goes to the fake."""

    def _json_at(self, api, repo, head, path, token):
        return api.get_json(repo, head, path)


def make_hub(api, token=TOKEN_A, **kw):
    holder = {'token': token}
    hub = CountingHubReads(robot_type='omx_f', api_factory=lambda t: api, token_reader=lambda: holder['token'],
                           **kw)
    return hub, holder


def entry(dataset_id, digest='d1'):
    return {'id': dataset_id, 'meta_digest': digest}


@pytest.fixture()
def api():
    a = FakeApi()
    a.add('lena-schmidt/omx_f_local')
    a.add('lena-schmidt/omx_f_online', head='o1')
    return a


def test_a_second_request_for_an_unchanged_head_makes_no_tree_call(api):
    hub, _ = make_hub(api)
    local = [entry('lena-schmidt/omx_f_local')]
    h1, views1, _ = hub.library_hub(['lena-schmidt'], local)
    assert h1['state'] == 'ok'
    views1['lena-schmidt/omx_f_local']['files']()             # a record-less content decision
    first = dict(api.calls)
    assert first['tree'] == 1                                  # the local repo's view; a card reads no tree
    assert first['files'] == 2                                 # the content decision + the card's size
    assert first['json'] == 2                                  # the card's info.json + stats.json
    h2, views2, _ = hub.library_hub(['lena-schmidt'], local)
    views2['lena-schmidt/omx_f_local']['files']()
    assert api.calls['tree'] == first['tree']
    assert api.calls['files'] == first['files']
    assert api.calls['json'] == first['json']
    assert api.calls['whoami'] == 1
    assert api.calls['list_datasets'] == 2                     # the listing is asked every time
    assert [e['id'] for e in h2['entries']] == [e['id'] for e in h1['entries']]


def test_a_moved_head_is_read_again(api):
    hub, _ = make_hub(api)
    local = [entry('lena-schmidt/omx_f_local')]
    hub.library_hub(['lena-schmidt'], local)
    before = api.calls['tree']
    api.move('lena-schmidt/omx_f_local', 'h2')
    _, views, _ = hub.library_hub(['lena-schmidt'], local)
    assert api.calls['tree'] == before + 1
    assert views['lena-schmidt/omx_f_local']['head'] == 'h2'
    assert views['lena-schmidt/omx_f_local']['trees']['data'] == 'data-lena-schmidt/omx_f_local-h2'


def test_the_lru_evicts_past_its_size(api):
    hub, _ = make_hub(api, cache_max=4)
    heads = [f'x{k}' for k in range(5)]
    for h in heads:
        api.move('lena-schmidt/omx_f_local', h)
        hub.tree(api, 'lena-schmidt/omx_f_local', h)
    assert hub.cache_size() == 4
    assert api.calls['tree'] == 5
    hub.tree(api, 'lena-schmidt/omx_f_local', heads[-1])       # the newest is cached
    assert api.calls['tree'] == 5
    api.move('lena-schmidt/omx_f_local', heads[0])
    hub.tree(api, 'lena-schmidt/omx_f_local', heads[0])        # the oldest was evicted
    assert api.calls['tree'] == 6


def test_the_default_size_is_the_contract_s():
    hub = HR.HubReads()
    assert hub.cache_max == C.HUB_CACHE_MAX == 512


def test_nothing_is_time_based(api, monkeypatch):
    hub, _ = make_hub(api)
    local = [entry('lena-schmidt/omx_f_local')]
    hub.library_hub(['lena-schmidt'], local)
    calls = dict(api.calls)
    ten_years = 10 * 365 * 86400
    real_time, real_mono = time.time, time.monotonic
    monkeypatch.setattr(time, 'time', lambda: real_time() + ten_years)
    monkeypatch.setattr(time, 'monotonic', lambda: real_mono() + ten_years)
    hub.library_hub(['lena-schmidt'], local)
    assert api.calls['tree'] == calls['tree']
    assert api.calls['json'] == calls['json']
    assert api.calls['whoami'] == calls['whoami']
    tree = ast.parse(Path(HR.__file__).read_text(encoding='utf-8'))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert 'time' not in imported
    assert 'ttl' not in Path(HR.__file__).read_text(encoding='utf-8').lower().replace('no ttl', '')


def test_whoami_is_cached_per_token_fingerprint(api):
    hub, holder = make_hub(api)
    hub.library_hub(['lena-schmidt'], [])
    hub.library_hub(['lena-schmidt'], [])
    assert api.calls['whoami'] == 1
    holder['token'] = TOKEN_B
    hub.library_hub(['lena-schmidt'], [])
    assert api.calls['whoami'] == 2


def test_a_record_holding_the_current_head_needs_no_tree_call(api):
    hub, _ = make_hub(api)
    repo = 'lena-schmidt/omx_f_local'
    record = {'hub_sha': 'h1', 'hub_trees': dict(api.repos[repo]['trees'])}
    _, views, _ = hub.library_hub(['lena-schmidt'], [entry(repo)], records={repo: record})
    assert api.calls['tree'] == 0
    assert views[repo]['trees'] == record['hub_trees']
    api.move(repo, 'h2')                                       # the hub moved past the record
    hub.library_hub(['lena-schmidt'], [entry(repo)], records={repo: record})
    assert api.calls['tree'] == 1


def test_two_identical_concurrent_requests_share_one_listing(api):
    hub, _ = make_hub(api)
    api.gate = threading.Event()
    out = []
    threads = [threading.Thread(target=lambda: out.append(hub.library_hub(['lena-schmidt'], [])))
               for _ in range(2)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while api.calls['list_datasets'] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)                                            # the second request is waiting by now
    api.gate.set()
    for t in threads:
        t.join(5)
    assert api.calls['list_datasets'] == 1
    assert len(out) == 2 and out[0][0]['entries'] == out[1][0]['entries']


def test_the_states_of_a_failed_ask(api):
    hub, holder = make_hub(api)
    holder['token'] = None
    assert hub.library_hub(['lena-schmidt'], [])[0]['state'] == 'no_token'
    assert api.calls == collections.Counter()
    assert hub.library_hub(['lena-schmidt'], [], hub_asked=False)[0]['state'] == 'skipped'
    holder['token'] = TOKEN_A
    api.fail['whoami'] = HttpStatusError(401)
    assert hub.library_hub(['lena-schmidt'], [])[0]['state'] == 'auth'
    del api.fail['whoami']
    api.fail['list_datasets'] = ConnectionError('no route')
    h, views, listed = hub.library_hub(['lena-schmidt'], [entry('lena-schmidt/omx_f_local')])
    assert (h['state'], views, listed) == ('unreachable', {}, {})
    assert h['token_fp'] is not None


ONLINE = 'lena-schmidt/omx_f_online'


@pytest.mark.parametrize('error', [HttpStatusError(503), HttpStatusError(500), HttpStatusError(429),
                                   TimeoutError('read timed out'), ConnectionError('no route')],
                         ids=['503', '500', '429', 'timeout', 'network'])
def test_a_transient_failure_reading_a_card_keeps_it_unknown_and_retries(api, error):
    """V2-10: one 5xx / timeout / network failure while reading an online
    card's info.json proves nothing about the repo: the card stays (numbers
    unknown, its view `unreachable` → sync `unknown/unreachable`), it is NOT
    counted as hidden, nothing failed is cached, and the next load reads it."""
    hub, _ = make_hub(api)
    api.fail['json'] = error
    h, views, listed = hub.library_hub(['lena-schmidt'], [entry('lena-schmidt/omx_f_local')])
    assert h['state'] == 'ok' and h['hidden_count'] == 0
    card = next(e for e in h['entries'] if e['id'] == ONLINE)
    assert (card['head'], card['total_episodes'], card['fps'], card['cameras']) == ('o1', None, None, None)
    assert card['size_bytes'] == 150                          # the listing worked: its size stays
    assert views[ONLINE] == {'state': 'unreachable'} and ONLINE in listed
    del api.fail['json']
    h, views, listed = hub.library_hub(['lena-schmidt'], [entry('lena-schmidt/omx_f_local')])
    card = next(e for e in h['entries'] if e['id'] == ONLINE)
    assert (card['total_episodes'], card['fps']) == (4, 30)
    assert ONLINE not in views


@pytest.mark.parametrize('error', [HttpStatusError(404), ValueError('not JSON'), HttpStatusError(403),
                                   RepositoryNotFoundError('gone')], ids=['404', 'not_json', '403', 'not_found'])
def test_an_unreadable_card_is_still_hidden(api, error):
    hub, _ = make_hub(api)
    api.fail['json'] = error
    h, views, listed = hub.library_hub(['lena-schmidt'], [entry('lena-schmidt/omx_f_local')])
    assert h['state'] == 'ok' and h['hidden_count'] == 1
    assert ONLINE not in [e['id'] for e in h['entries']] and ONLINE not in listed and ONLINE not in views


def test_another_robots_dataset_is_hidden_and_a_transient_probe_is_unreachable(api):
    hub, _ = make_hub(api)
    api.add('lena-schmidt/so100_x', head='s1', info=dict(INFO_V3, robot_type='so100_follower'))
    h, _, listed = hub.library_hub(['lena-schmidt'], [])
    assert h['hidden_count'] == 1 and 'lena-schmidt/so100_x' not in listed
    hub, _ = make_hub(api)                                    # nothing cached yet
    api.fail['json'] = HttpStatusError(503)
    assert hub.probe(ONLINE)['refusal'] == 'unreachable'     # never `unsupported` for a 503
    del api.fail['json']
    assert hub.probe(ONLINE)['found'] is True                # the failure was not cached


def test_transient_is_the_network_a_5xx_or_a_429_only():
    assert all(HR.transient(e) for e in (HttpStatusError(502), HttpStatusError(429), TimeoutError('t'),
                                         ConnectionError('c')))
    nf = RepositoryNotFoundError('x')
    nf.response = types.SimpleNamespace(status_code=503)
    assert not any(HR.transient(e) for e in (nf, HttpStatusError(404), HttpStatusError(401), ValueError('json')))


def test_a_token_that_changes_during_the_ask_is_token_changed(api):
    hub, holder = make_hub(api)
    real = api.list_datasets

    def swap(author=None):
        holder['token'] = TOKEN_B
        return real(author)
    api.list_datasets = swap
    assert hub.library_hub(['lena-schmidt'], [])[0]['state'] == 'token_changed'


def test_a_not_found_is_classified_before_auth():
    err = RepositoryNotFoundError('x')
    err.response = types.SimpleNamespace(status_code=401)      # anonymous 401 on a private repo (R-12)
    assert HR.classify(err) == 'not_found'
    assert HR.classify(HttpStatusError(403)) == 'auth'
    assert HR.classify(ConnectionError('x')) == 'unreachable'
