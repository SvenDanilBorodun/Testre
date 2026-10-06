"""Daten 2.0: the sidecar's local library (spec §B4, §B6, §C1, §J.4).

Scan states in their order (``in_session`` first); reserved suffixes, hidden
siblings, records, markers and symlinks never listed; namespaces and ids
validated; the §J.4.1 entry fields; ``hint_episodes`` null until the hint worker
ran; summary/episode data/clip/thumb incl. the clip LRU and the remembered
``unplayable``; the memoised meta digest; and ``sync_map`` per id from hub views
— each ``unknown`` reason, ``complete``, a content decision, a listing that
fails mid-decision, and the head fast path making no tree call. T2-1: a
record-less dataset decided ``current`` (by ``sync_map`` or ``hubstate``) is
remembered by the background step — its record written once, after the reply,
then read on the fast path; nothing else is ever remembered.
"""

from __future__ import annotations

import json
import os
import time

import pytest

av = pytest.importorskip('av')
pytest.importorskip('pyarrow')
np = pytest.importorskip('numpy')

from daten_timeout import per_test_time_limit  # noqa: E402,F401 — V1-3: a hang fails within the limit

from physical_ai_server.daten import contract as C  # noqa: E402
from physical_ai_server.daten import library as L  # noqa: E402
from physical_ai_server.data_processing import dataset_sync as S  # noqa: E402
from physical_ai_server.data_processing import v3_surgery as V  # noqa: E402
from test_daten_hub_cache import FakeApi, make_hub  # noqa: E402
from test_v3_cut_episode import _unaligned  # noqa: E402
from test_v3_surgery_layout import build_dataset, default_motion  # noqa: E402

NS = 'lena-schmidt'
LENGTHS = (20, 21, 22)


def still_first(ep, n, joints):
    st, ac = default_motion(ep, n, joints)
    if ep == 0:
        ac[:] = ac[0]
        st[:] = ac[0]
    return st, ac


def _mutate_info(root, fn):
    p = root / 'meta' / 'info.json'
    info = json.loads(p.read_text())
    fn(info)
    p.write_text(json.dumps(info))


@pytest.fixture(scope='module')
def root(tmp_path_factory):
    base = tmp_path_factory.mktemp('daten_lib')
    ns = base / NS
    build_dataset(ns / 'omx_f_ok', lengths=LENGTHS, motion=still_first)
    build_dataset(ns / 'omx_f_old', lengths=(20,), codebase_version='v2.1')
    unsup = build_dataset(ns / 'omx_f_unsup', lengths=(20,))
    _mutate_info(unsup, lambda i: i.__setitem__('video_path', 'videos/{video_key}/{episode_index}.mp4'))
    empty = build_dataset(ns / 'omx_f_empty', lengths=(20,))
    _mutate_info(empty, lambda i: i.__setitem__('total_episodes', 0))
    (ns / 'omx_f_noinfo' / 'meta').mkdir(parents=True)
    build_dataset(ns / 'omx_f_session', lengths=(20,))
    S.session_marker_path(ns / 'omx_f_session').write_text('{}')
    _unaligned(build_dataset(ns / 'omx_f_unaligned', lengths=LENGTHS))
    # never listed
    build_dataset(ns / 'omx_f_ok.tmp_edit', lengths=(20,))
    (ns / 'omx_f_ok.bak_sync').mkdir()
    (ns / '.hidden').mkdir()
    (ns / '.omx_f_ok.lock').write_text('')
    S.write_record(ns / 'omx_f_ok', {'v': 1, 'repo_id': f'{NS}/omx_f_ok', 'display_name': 'Würfel legen',
                                     'hub_sha': None, 'private': True})
    (ns / 'omx_f_link').symlink_to(ns / 'omx_f_ok', target_is_directory=True)
    (base / 'outsider').mkdir()
    build_dataset(base / 'outsider' / 'omx_f_secret', lengths=(20,))
    (ns / 'omx_f_escape').symlink_to(base / 'outsider' / 'omx_f_secret', target_is_directory=True)
    return base


@pytest.fixture()
def lib(root, tmp_path):
    return L.Library(root=root, robot_type='omx_f', clip_tmp_dir=tmp_path / 'clips')


def by_id(entries):
    return {e['id']: e for e in entries}


def test_scan_states_in_their_order(lib):
    states = {e['id'].split('/')[1]: e['state'] for e in lib.scan([NS])}
    assert states == {'omx_f_ok': 'ok', 'omx_f_old': 'old_format', 'omx_f_unsup': 'unsupported',
                      'omx_f_empty': 'incomplete', 'omx_f_noinfo': 'incomplete',
                      'omx_f_session': 'in_session', 'omx_f_unaligned': 'ok'}


def test_a_marker_wins_over_every_other_state(root, lib):
    marker = S.session_marker_path(root / NS / 'omx_f_noinfo')
    marker.write_text('{}')
    try:
        assert by_id(lib.scan([NS]))[f'{NS}/omx_f_noinfo']['state'] == 'in_session'
    finally:
        marker.unlink()


def test_reserved_suffixes_hidden_files_records_and_symlinks_are_never_listed(lib):
    names = {e['name'] for e in lib.scan([NS])}
    assert not {n for n in names if n.endswith(C.RESERVED_SUFFIXES) or n.startswith('.')}
    assert 'omx_f_link' not in names and 'omx_f_escape' not in names
    assert not {n for n in names if n.endswith(('.json', '.lock'))}


def test_namespaces_and_ids_are_validated(lib):
    assert {e['ns'] for e in lib.scan([NS, '../outsider', '.hidden', '', 'a b', NS, 'outsider/x'])} == {NS}
    for bad in ('../outsider/omx_f_secret', f'{NS}/../x', f'{NS}/omx_f_ok.tmp_edit', '/etc/passwd',
                f'{NS}/omx_f_ok/meta', 'omx_f_ok', f'{NS}/.hidden', f'{NS}/omx_f_link', f'{NS}/omx_f_escape'):
        with pytest.raises(L.LibraryError) as e:
            lib.path_of(bad)
        assert e.value.code == 'invalid', bad
    with pytest.raises(L.LibraryError) as e:
        lib.path_of(f'{NS}/omx_f_nothing')
    assert e.value.code == 'not_found'
    assert [e['id'] for e in lib.scan([], ids=[f'{NS}/omx_f_ok', '../x', f'{NS}/omx_f_nothing'])] == \
        [f'{NS}/omx_f_ok']


def test_an_ok_entry_carries_the_contract_fields(lib, root):
    e = by_id(lib.scan([NS]))[f'{NS}/omx_f_ok']
    assert set(e) == {'id', 'ns', 'name', 'display_name', 'state', 'meta_digest', 'codebase_version',
                      'robot_type', 'fps', 'total_episodes', 'total_frames', 'duration_s', 'size_bytes',
                      'modified_at', 'cameras', 'joints', 'stat_names', 'tasks', 'hint_episodes', 'record'}
    assert (e['ns'], e['name'], e['display_name']) == (NS, 'omx_f_ok', 'Würfel legen')
    assert e['meta_digest'] == S.meta_digest(root / NS / 'omx_f_ok')
    assert (e['fps'], e['total_episodes'], e['total_frames']) == (30, 3, sum(LENGTHS))
    assert e['duration_s'] == round(sum(LENGTHS) / 30, 3)
    assert [c['name'] for c in e['cameras']] == ['gripper', 'scene']
    assert [c['index'] for c in e['cameras']] == [0, 1]
    assert e['joints']['action'][-1] == 'gripper_joint_1'
    assert 'q01' in e['stat_names']['action']
    assert e['tasks'] == ['Lege den gelben Würfel in die blaue Schale.']
    assert e['record'] == {'hub_sha': None, 'synced_at': None, 'source_repo': None, 'private': True,
                           'tag_ok': True}
    assert e['size_bytes'] > 0 and e['modified_at'].endswith('Z')
    json.dumps(e)                                              # JSON-serialisable as is


def test_hint_episodes_is_null_until_the_worker_ran(root, tmp_path):
    lib = L.Library(root=root, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    assert by_id(lib.scan([NS]))[f'{NS}/omx_f_ok']['hint_episodes'] is None
    lib.start_hint_worker()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = by_id(lib.scan([NS]))[f'{NS}/omx_f_ok']['hint_episodes']
        if value is not None:
            break
        time.sleep(0.05)
    hints = lib.hints(f'{NS}/omx_f_ok')
    assert value == sum(1 for h in hints if h) >= 1
    assert hints[0][0]['type'] == 'still'
    assert by_id(lib.scan([NS]))[f'{NS}/omx_f_unsup']['hint_episodes'] is None   # only ok datasets


def test_the_summary(lib):
    s = lib.summary(f'{NS}/omx_f_ok')
    assert s['v'] == 1 and s['algo'] == 'hints-v1' and s['total_episodes'] == 3
    assert [ep['length'] for ep in s['episodes']] == list(LENGTHS)
    for ep in s['episodes']:
        assert ep['frames'] == {'0': ep['length'], '1': ep['length']}
        assert ep['playable'] is True
        assert ep['bytes']['video'] > 0 and ep['bytes']['data_estimate'] > 0
        assert ep['duration_s'] == round(ep['length'] / 30, 3)
    assert s['episodes'][0]['hints'][0]['type'] == 'still'
    json.dumps(s)


def test_an_unaligned_dataset_is_listed_ok_with_unplayable_episodes(lib):
    s = lib.summary(f'{NS}/omx_f_unaligned')
    assert any(not ep['playable'] for ep in s['episodes'])


def test_media_routes_refuse_the_states_they_cannot_read(lib):
    for name, code in (('omx_f_session', 'in_session'), ('omx_f_unsup', 'unsupported'),
                       ('omx_f_empty', 'incomplete'), ('omx_f_old', 'unsupported')):
        for call in (lambda d: lib.summary(d), lambda d: lib.episode_data(d, 0),
                     lambda d: lib.clip(d, 0, 0), lambda d: lib.thumb(d)):
            with pytest.raises(L.LibraryError) as e:
                call(f'{NS}/{name}')
            assert e.value.code == code, name


def test_episode_data(lib):
    d = lib.episode_data(f'{NS}/omx_f_ok', 1)
    assert (d['i'], d['fps'], d['length'], d['unit']) == (1, 30, 21, 'rad')
    assert len(d['timestamp']) == len(d['state']) == len(d['action']) == 21
    assert len(d['state'][0]) == 6 and d['names']['state'][0] == 'joint1'
    assert all(round(x, 6) == x for row in d['action'] for x in row)
    for bad in (3, -1):
        with pytest.raises(L.LibraryError) as e:
            lib.episode_data(f'{NS}/omx_f_ok', bad)
        assert e.value.code == 'not_found'


def _frames(data, tmp_path):
    p = tmp_path / 'clip.mp4'
    p.write_bytes(data)
    c = av.open(str(p))
    try:
        return sum(1 for _ in c.decode(c.streams.video[0]))
    finally:
        c.close()


def test_the_clip_is_the_episode_and_is_served_from_the_cache(lib, tmp_path, monkeypatch):
    calls = []
    real = V.write_clip
    monkeypatch.setattr(V, 'write_clip', lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    data, etag = lib.clip(f'{NS}/omx_f_ok', 1, 1)
    assert data[4:8] == b'ftyp' and _frames(data, tmp_path) == 21
    assert etag == f'"{lib.meta_digest(lib.path_of(f"{NS}/omx_f_ok"))[:16]}-1-1"'
    again, _ = lib.clip(f'{NS}/omx_f_ok', 1, 1)
    assert again == data and len(calls) == 1
    assert list((tmp_path / 'clips').iterdir()) == []          # the temp file is gone
    for i, c in ((3, 0), (0, 2)):
        with pytest.raises(L.LibraryError) as e:
            lib.clip(f'{NS}/omx_f_ok', i, c)
        assert e.value.code == 'not_found'


def test_an_unplayable_clip_is_refused_once_and_remembered(lib, monkeypatch):
    s = lib.summary(f'{NS}/omx_f_unaligned')
    bad = next(ep['i'] for ep in s['episodes'] if not ep['playable'])
    calls = []
    real = V.write_clip
    monkeypatch.setattr(V, 'write_clip', lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    refused = None
    for c in range(len(s['cameras'])):
        try:
            lib.clip(f'{NS}/omx_f_unaligned', bad, c)
        except L.LibraryError as e:
            assert e.code == 'unplayable'
            refused = c
            break
    assert refused is not None
    n = len(calls)
    with pytest.raises(L.LibraryError) as e:
        lib.clip(f'{NS}/omx_f_unaligned', bad, refused)
    assert e.value.code == 'unplayable' and len(calls) == n        # remembered: no second cut


def test_the_clip_cache_is_bounded(root, tmp_path):
    lib = L.Library(root=root, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c', clip_cache_max=1)
    lib.clip(f'{NS}/omx_f_ok', 0, 0)
    lib.clip(f'{NS}/omx_f_ok', 1, 0)
    assert len(lib._clips) == 1                                # the newest stays, the bound holds
    assert lib.clip_cache_bytes() == len(next(iter(lib._clips.values())))


def test_the_thumbnail(lib, tmp_path):
    data, etag = lib.thumb(f'{NS}/omx_f_ok')
    assert data[:2] == b'\xff\xd8' and etag.endswith('-thumb"')
    p = tmp_path / 't.jpg'
    p.write_bytes(data)
    c = av.open(str(p))
    try:
        frame = next(c.decode(c.streams.video[0]))
        assert frame.width == L.THUMB_WIDTH
    finally:
        c.close()


def test_a_marker_after_caching_refuses_the_cached_media_too(root, lib):
    dataset = f'{NS}/omx_f_ok'
    lib.summary(dataset)
    lib.clip(dataset, 0, 0)
    lib.thumb(dataset)
    marker = S.session_marker_path(root / NS / 'omx_f_ok')
    marker.write_text('{}')
    try:
        for call in (lambda: lib.summary(dataset), lambda: lib.clip(dataset, 0, 0), lambda: lib.thumb(dataset)):
            with pytest.raises(L.LibraryError) as e:
                call()
            assert e.value.code == 'in_session'
    finally:
        marker.unlink()


def test_the_meta_digest_is_memoised_and_follows_an_edit(tmp_path, monkeypatch):
    ds = build_dataset(tmp_path / NS / 'omx_f_memo', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    calls = []
    real = S.meta_digest
    monkeypatch.setattr(S, 'meta_digest', lambda r: (calls.append(r), real(r))[1])
    d1 = lib.meta_digest(ds)
    assert lib.meta_digest(ds) == d1 and len(calls) == 1
    info = ds / 'meta' / 'info.json'
    info.write_text(info.read_text().replace('"fps": 30', '"fps": 30 '))
    os.utime(info, ns=(time.time_ns(), time.time_ns() + 10**9))
    d2 = lib.meta_digest(ds)
    assert len(calls) == 2 and d2 != d1


def test_hubstate_local_reads_info_json_alone(lib, root):
    local = lib.local_for_hubstate(f'{NS}/omx_f_session')
    assert local['total_episodes'] == 1 and local['duration_s'] == round(20 / 30, 3)
    assert local['modified_at'].endswith('Z')
    assert lib.local_for_hubstate(f'{NS}/omx_f_noinfo') is None


# ── sync per id ──────────────────────────────────────────────────────────────

def _mirror(api, repo, path, head='h1', drop=()):
    """A hub repo holding exactly the local files (the real pointer rule)."""
    files = {}
    for rel in sorted(S.local_files(path)):
        if rel in drop:
            continue
        p = path / rel
        if rel.endswith(S.LFS_EXT):
            sha = S.sha256_file(p)
            files[rel] = (p.stat().st_size, S.git_sha1_bytes(S.lfs_pointer(sha, p.stat().st_size)), sha)
        else:
            files[rel] = (p.stat().st_size, S.git_sha1_file(p), None)
    return api.add(repo, head=head, files=files)


def test_a_trailing_newline_is_never_a_valid_id():
    assert L.is_valid_id(f'{NS}/omx_f_a')
    assert not L.is_valid_id(f'{NS}/omx_f_a\n') and not L.is_valid_id(f'{NS}\n/omx_f_a')
    assert not L.valid_part('omx_f_a\n')


def test_sync_map_reasons_and_states(tmp_path):
    ns = tmp_path / NS
    a = build_dataset(ns / 'omx_f_a', lengths=(20, 21))
    build_dataset(ns / 'omx_f_b', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    local = lib.scan([NS])
    ids = [e['id'] for e in local]
    assert {k: (v['state'], v['reason']) for k, v in L.sync_map(lib, local, {}, {}).items()} == \
        {i: ('unknown', 'not_asked') for i in ids}
    assert {v['reason'] for v in L.sync_map(lib, local, {}, {}, default_view={'state': 'unreachable'}).values()} \
        == {'unreachable'}
    views = {ids[0]: {'state': 'absent', 'complete': False}, ids[1]: {'state': 'absent', 'complete': True}}
    out = L.sync_map(lib, local, views, {})
    assert (out[ids[0]]['state'], out[ids[0]]['reason']) == ('unknown', 'not_visible')
    assert (out[ids[1]]['state'], out[ids[1]]['reason']) == ('local', None)
    out = L.sync_map(lib, local, {}, {f'{NS}/omx_f_c': {'head': 'c1'}})
    assert out[f'{NS}/omx_f_c'] == {'state': 'online', 'reason': None, 'head': 'c1'}
    # V2-10: a hub-only card whose info.json could not be read just now
    out = L.sync_map(lib, local, {f'{NS}/omx_f_c': {'state': 'unreachable'}}, {f'{NS}/omx_f_c': {'head': 'c1'}})
    assert out[f'{NS}/omx_f_c'] == {'state': 'unknown', 'reason': 'unreachable', 'head': 'c1'}

    # a record-less dataset decided by content through the real HubReads
    api = FakeApi(account=NS)
    _mirror(api, f'{NS}/omx_f_a', a)
    hub, _ = make_hub(api)
    _, views, listed = hub.library_hub([NS], local)
    out = L.sync_map(lib, local, views, listed)
    assert out[f'{NS}/omx_f_a'] == {'state': 'current', 'reason': None, 'head': 'h1'}
    assert out[f'{NS}/omx_f_b'] == {'state': 'local', 'reason': None, 'head': None}   # complete: own ns
    video = next(r for r in S.local_files(a) if r.startswith('videos/'))
    _mirror(api, f'{NS}/omx_f_a', a, head='h2', drop=(video,))
    _, views, listed = hub.library_hub([NS], local)
    assert L.sync_map(lib, local, views, listed)[f'{NS}/omx_f_a']['state'] == 'changed'


def test_a_listing_failing_mid_decision_is_unreachable_never_dropped(tmp_path):
    a = build_dataset(tmp_path / NS / 'omx_f_a', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    local = lib.scan([NS])
    api = FakeApi(account=NS)
    _mirror(api, f'{NS}/omx_f_a', a)
    hub, _ = make_hub(api)
    _, views, listed = hub.library_hub([NS], local)
    api.fail['files'] = ConnectionError('gone')
    out = L.sync_map(lib, local, views, listed)
    assert out[f'{NS}/omx_f_a'] == {'state': 'unknown', 'reason': 'unreachable', 'head': None}


def test_a_synced_dataset_whose_record_holds_the_head_needs_no_tree_call(tmp_path):
    a = build_dataset(tmp_path / NS / 'omx_f_a', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    api = FakeApi(account=NS)
    repo = api.add(f'{NS}/omx_f_a', head='h7')
    S.write_record(a, {'v': 1, 'repo_id': f'{NS}/omx_f_a', 'hub_sha': 'h7', 'hub_trees': dict(repo['trees']),
                       'local_digest': S.meta_digest(a), 'tag_ok': True})
    local = lib.scan([NS])
    hub, _ = make_hub(api)
    _, views, listed = hub.library_hub([NS], local, records=lib.records(local))
    out = L.sync_map(lib, local, views, listed)
    assert out[f'{NS}/omx_f_a'] == {'state': 'current', 'reason': None, 'head': 'h7'}
    assert api.calls['tree'] == 0 and api.calls['files'] == 0
    assert by_id(local)[f'{NS}/omx_f_a']['record']['hub_sha'] == 'h7'


# ── T2-1: remember the state first ───────────────────────────────────────────

def _wait_until(pred, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _hub_view_of(lib, api, hub, local):
    _, views, listed = hub.library_hub([NS], local, records=lib.records(local))
    return L.sync_map(lib, local, views, listed)


def test_a_record_less_identical_dataset_is_remembered_after_the_reply(tmp_path):
    a = build_dataset(tmp_path / NS / 'omx_f_a', lengths=(20, 21))
    S.write_record(a, {'v': 1, 'repo_id': f'{NS}/omx_f_a', 'display_name': 'Würfel legen'})
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    api = FakeApi(account=NS)
    repo = _mirror(api, f'{NS}/omx_f_a', a)
    hub, _ = make_hub(api)
    local = lib.scan([NS])
    out = _hub_view_of(lib, api, hub, local)
    assert out[f'{NS}/omx_f_a']['state'] == 'current'
    assert S.read_record(a).get('hub_sha') is None, 'never inside the reply: the step runs in the background'
    assert lib.remember_pending() == 1
    lib.start_remember_worker()
    assert _wait_until(lambda: (S.read_record(a) or {}).get('hub_sha') == 'h1')
    rec = S.read_record(a)
    assert rec['hub_trees'] == dict(repo['trees'])
    assert rec['local_digest'] == S.meta_digest(a)
    assert rec['files'] == S.files_manifest(a)
    assert rec['display_name'] == 'Würfel legen'
    assert _wait_until(lambda: lib.remember_pending() == 0)
    # read back on the head fast path: no tree call, no recursive listing
    calls = (api.calls['tree'], api.calls['files'])
    local = lib.scan([NS])
    assert _hub_view_of(lib, api, hub, local)[f'{NS}/omx_f_a']['state'] == 'current'
    assert (api.calls['tree'], api.calls['files']) == calls
    assert lib.remember_pending() == 0, 'a synced dataset is never queued again'


def test_only_a_current_record_less_dataset_with_the_hub_asked_is_queued(tmp_path):
    ns = tmp_path / NS
    a = build_dataset(ns / 'omx_f_a', lengths=(20, 21))           # here = hub + a session: changed
    b = build_dataset(ns / 'omx_f_b', lengths=(20,))              # only here: local
    c = build_dataset(ns / 'omx_f_c', lengths=(20,))              # synced and current
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'cl')
    api = FakeApi(account=NS)
    video = next(r for r in S.local_files(a) if r.startswith('videos/'))
    _mirror(api, f'{NS}/omx_f_a', a, drop=(video,))
    repo_c = _mirror(api, f'{NS}/omx_f_c', c)
    S.write_record(c, {'v': 1, 'repo_id': f'{NS}/omx_f_c', 'hub_sha': 'h1', 'hub_trees': dict(repo_c['trees']),
                       'local_digest': S.meta_digest(c)})
    hub, _ = make_hub(api)
    local = lib.scan([NS])
    out = _hub_view_of(lib, api, hub, local)
    assert {k: v['state'] for k, v in out.items()} == {f'{NS}/omx_f_a': 'changed', f'{NS}/omx_f_b': 'local',
                                                       f'{NS}/omx_f_c': 'current'}
    assert L.sync_map(lib, local, {}, {})                          # the hub not asked: unknown
    assert lib.remember_pending() == 0
    assert S.read_record(b) is None and S.read_record(a) is None


def test_hubstate_remembers_too_and_a_differing_video_is_never_remembered(tmp_path):
    a = build_dataset(tmp_path / NS / 'omx_f_a', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    api = FakeApi(account=NS)
    _mirror(api, f'{NS}/omx_f_a', a)
    hub, _ = make_hub(api)
    video = a / next(r for r in S.local_files(a) if r.startswith('videos/'))
    data = bytearray(video.read_bytes())
    data[len(data) // 2] ^= 0xFF                                   # same size, one other byte
    video.write_bytes(bytes(data))
    assert hub.hubstate(lib, f'{NS}/omx_f_a')['sync']['state'] == 'current'   # videos by size (§C3)
    assert lib.remember_pending() == 1
    lib.start_remember_worker()
    assert _wait_until(lambda: lib.remember_pending() == 0)
    assert S.read_record(a) is None, 'not provably identical: nothing written'
    assert hub.hubstate(lib, f'{NS}/omx_f_a')['sync']['state'] == 'current'
    assert lib.remember_pending() == 0, 'a refusal is remembered for this state: not hashed again'
    data[len(data) // 2] ^= 0xFF                                   # the byte back: a file changed,
    video.write_bytes(bytes(data))                                 # so it is looked at again
    assert hub.hubstate(lib, f'{NS}/omx_f_a')['sync']['state'] == 'current'
    assert _wait_until(lambda: (S.read_record(a) or {}).get('hub_sha') == 'h1')


def test_the_sidecars_main_starts_the_remember_worker():
    import ast
    import inspect
    from physical_ai_server.daten import http_server
    calls = {n.func.attr for n in ast.walk(ast.parse(inspect.getsource(http_server.main)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {'start_hint_worker', 'start_remember_worker'} <= calls


def test_the_reply_never_depends_on_the_remember_step(tmp_path):
    """A listing that fails, a folder that vanished: the decision stands, nothing queued."""
    a = build_dataset(tmp_path / NS / 'omx_f_a', lengths=(20,))
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'c')
    view = {'state': 'present', 'head': 'h1', 'trees': {'data': 't'},
            'files': lambda: (_ for _ in ()).throw(ConnectionError('gone'))}
    assert lib.remember_if_current(f'{NS}/omx_f_a', a, None, view, 'current') is False
    view['files'] = lambda: {}
    assert lib.remember_if_current(f'{NS}/omx_f_a', tmp_path / NS / 'omx_f_gone', None, view, 'current') is False
    assert lib.remember_pending() == 0
