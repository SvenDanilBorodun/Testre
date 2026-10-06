"""Daten 2.0 — the ONE download process, ``daten/download_worker.py`` (spec §E3,
S-2), against the Appendix K fake hub, each run a real subprocess.

The fake hub is the harness's (``robotis_ai_setup/tests/test_hub_sync_upload.py``
holds its two source files verbatim, spec Appendix K): huggingface_hub's own
client on a patched transport whose server rules are the real hub's. The
datasets are real v3.0 trees (``test_v3_surgery_layout.build_dataset``).
LeRobot exists only in the image, so a small wrapper stands in for its final
load check (a ``meta/NOLOAD`` file makes it fail) and, for two cases, narrows
the free disk or flips one transferred byte; the worker's code is unchanged.

Proven: a token fingerprint mismatch → ``token_changed`` before any request; the
worker's own watch — the slot changed with nobody polling, its parent killed —
ends it with the tmp removed (G-14, P29); ``replace`` without a digest →
``invalid``, with another digest → ``stale``, before any request (T-1 c);
``new``/``replace``/``sync``/``copy`` with their records (``hub_sha``,
``hub_trees``, ``files``; a copy's ``source`` and the source's visibility);
``replace`` over a crashed dataset removes its marker (H-1); old format / other
robot / unsupported / broken refused with the tmp removed and the target
untouched (one wrong byte → ``broken``, P28); the disk refusal carries
``free``/``need``; ``keep`` (the local gate first, no swap, no record) and
``base`` (data/+meta/, videos linked when the hashes match, fetched otherwise);
``HF_HUB_DISABLE_XET`` set by the supervisor. And the supervisor: a check slower
than the stall time survives on its heartbeats (H-10); a kill between the
swap's renames is recovered at once (H-9); a kill after the next record and
before the first rename keeps the old content, record and marker, with no
leftover (U-1, P35).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

pytest.importorskip('huggingface_hub')
pytest.importorskip('httpx')
pytest.importorskip('av')
pytest.importorskip('pyarrow')

from physical_ai_server.daten import node_service as NS  # noqa: E402
from physical_ai_server.data_processing import dataset_sync as S  # noqa: E402
from physical_ai_server.data_processing import hf_token_store  # noqa: E402
from physical_ai_server.data_processing import hub_sync as HS  # noqa: E402
from test_v3_surgery_layout import build_dataset  # noqa: E402

PKG_PARENT = Path(__file__).resolve().parents[1]
HUB_TESTS = PKG_PARENT.parents[1] / 'robotis_ai_setup' / 'tests' / 'test_hub_sync_upload.py'
LENA = 'hf_harnesslenaAAAAAAAAAAAAAAAAAA'
TOKENS = {LENA: {'name': 'lena-schmidt', 'orgs': []},
          'hf_harnessotherAAAAAAAAAAAAAAAAA': {'name': 'someone-else', 'orgs': []}}

WRAPPER = textwrap.dedent('''
    """TEST-ONLY: the download worker with LeRobot's final load check stubbed
    (LeRobot is image-only) and two optional narrowings; the worker is unchanged."""
    import os, pathlib, sys, types
    def _load(repo_id, root=None, **kw):
        if (pathlib.Path(root) / 'meta' / 'NOLOAD').exists():
            raise RuntimeError('does not load')
    mods = {n: types.ModuleType(n) for n in ('lerobot', 'lerobot.datasets', 'lerobot.datasets.lerobot_dataset')}
    mods['lerobot.datasets.lerobot_dataset'].LeRobotDataset = _load
    sys.modules.update(mods)
    from physical_ai_server.daten import download_worker as W
    from physical_ai_server.data_processing import hub_sync as H
    if os.environ.get('D2_FREE_BYTES'):
        H._disk_free = lambda p: int(os.environ['D2_FREE_BYTES'])
    if os.environ.get('D2_FLIP'):
        _real = H._verify_against_listing
        def _flip(tmp, listing, progress=None):
            f = pathlib.Path(tmp) / os.environ['D2_FLIP']
            b = bytearray(f.read_bytes()); b[len(b) // 2] ^= 0xFF; f.write_bytes(bytes(b))
            return _real(tmp, listing, progress)
        H._verify_against_listing = _flip
    sys.exit(W.main())
''')


def _load_harness():
    spec = importlib.util.spec_from_file_location('_d2_hub_harness', str(HUB_TESTS))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp('d2_dl')
    harness = _load_harness()
    fakehub = harness.write_fakehub(base / 'fakehub')
    hub_root = base / 'hub'
    hub_root.mkdir()
    (base / 'wrap').mkdir()
    (base / 'wrap' / 'd2_dl_wrapper.py').write_text(WRAPPER)
    saved = os.environ.get('FAKEHUB_ROOT')
    os.environ['FAKEHUB_ROOT'] = str(hub_root)
    sys.path.insert(0, str(fakehub))
    try:
        sys.modules.pop('fakehub_store', None)
        import fakehub_store as FS
    finally:
        sys.path.remove(str(fakehub))
    yield types.SimpleNamespace(base=base, fakehub=fakehub, hub_root=hub_root, wrap=base / 'wrap', FS=FS)
    if saved is None:
        os.environ.pop('FAKEHUB_ROOT', None)
    else:
        os.environ['FAKEHUB_ROOT'] = saved


@pytest.fixture()
def hub(env, tmp_path):
    for d in ('repos', 'blobs', 'cache'):
        shutil.rmtree(env.hub_root / d, ignore_errors=True)
    for f in ('audit.log', 'faults.json'):
        (env.hub_root / f).unlink(missing_ok=True)
    (env.hub_root / 'tokens.json').write_text(json.dumps(TOKENS))
    token_file = tmp_path / 'token'
    token_file.write_text(LENA)
    root = tmp_path / 'datasets'
    (root / 'lena-schmidt').mkdir(parents=True)
    return types.SimpleNamespace(env=env, FS=env.FS, token_file=token_file, root=root, tmp=tmp_path,
                                 audit=lambda: [json.loads(x) for x in (env.hub_root / 'audit.log').read_text()
                                                .splitlines()] if (env.hub_root / 'audit.log').exists() else [])


def child_env(hub, **extra):
    e = dict(os.environ)
    for k in ('HF_TOKEN', 'HF_HUB_OFFLINE', 'HF_ENDPOINT'):
        e.pop(k, None)
    e.update({'PYTHONPATH': os.pathsep.join([str(hub.env.fakehub), str(PKG_PARENT), str(hub.env.wrap)]),
              'FAKEHUB_ROOT': str(hub.env.hub_root), 'HF_TOKEN_PATH': str(hub.token_file),
              'HF_HUB_DISABLE_TELEMETRY': '1', 'HF_HOME': str(hub.tmp / 'hf_home'), 'HF_HUB_DISABLE_XET': '1'})
    e.update({k: str(v) for k, v in extra.items()})
    return e


def request(hub, repo, target, mode, revision=None, **kw):
    req = {'repo_id': repo, 'revision': revision, 'target_dir': str(target), 'mode': mode,
           'display_name': kw.pop('display_name', None), 'robot_type': kw.pop('robot_type', 'omx_f'),
           'token_fp': kw.pop('token_fp', hf_token_store.fingerprint(LENA)), 'private': None,
           'meta_digest': kw.pop('meta_digest', None)}
    assert not kw
    return req


def run_worker(hub, req, timeout=120, **extra):
    r = subprocess.run([sys.executable, '-m', 'd2_dl_wrapper'], input=json.dumps(req) + '\n', text=True,
                       capture_output=True, env=child_env(hub, **extra), timeout=timeout)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith('DL_RESULT::')]
    assert len(lines) == 1, (r.stdout[-2000:], r.stderr[-3000:])
    return json.loads(lines[0][len('DL_RESULT::'):])


def seed(hub, name, tmp_dir, lengths=(20, 21), repo=None, private=False, **build):
    src = build_dataset(Path(tmp_dir) / 'src' / name, lengths=lengths, **build)
    return hub.FS.put_tree(repo or f'lena-schmidt/{name}', src, private=private, title='seed'), src


def no_request_answers(hub):
    """Every hub call from now on fails with a 500: a refusal whose code is not
    a network class was decided before any request."""
    (hub.env.hub_root / 'faults.json').write_text(json.dumps([{'op': '*', 'kind': '500', 'times': None}]))


def tree_digest(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in sorted(Path(path).rglob('*')) if p.is_file()}


# ── the token and the request ────────────────────────────────────────────────

def test_a_foreign_token_fingerprint_is_refused_before_any_request(hub, tmp_path):
    seed(hub, 'omx_f_a', tmp_path)
    no_request_answers(hub)
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', hub.root / 'lena-schmidt/omx_f_a', 'new',
                                token_fp='0' * 16))
    assert r == {'ok': False, 'code': 'token_changed'}
    assert not (hub.root / 'lena-schmidt/omx_f_a').exists()


def test_replace_needs_the_digest_and_refuses_a_stale_one_before_any_request(hub, tmp_path):
    head, src = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    shutil.copytree(src, target)
    before = tree_digest(target)
    no_request_answers(hub)
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'replace', head))
    assert r['code'] == 'invalid'
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'replace', head, meta_digest='0' * 64))
    assert r['code'] == 'stale'
    assert tree_digest(target) == before
    assert not HS.tmp_path_of(target, 'replace').exists()


# ── the modes ────────────────────────────────────────────────────────────────

def test_new_writes_the_record_with_head_trees_and_files(hub, tmp_path):
    head, src = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'new', head, display_name='Würfel'))
    assert r['ok'] and r['revision'] == head and r['xet_disabled'] is True
    rec = S.read_record(target)
    assert rec['hub_sha'] == head and rec['display_name'] == 'Würfel'
    assert rec['hub_trees'] == r['trees'] and all(rec['hub_trees'][d] for d in S.SYNC_DIRS)
    assert set(rec['files']) == {p for p in tree_digest(target) if p.startswith(S.EPISODE_TOP)}
    assert rec['local_digest'] == S.meta_digest(target)
    assert not HS.tmp_path_of(target, 'new').exists()
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'new', head))
    assert r['code'] == 'exists'


def test_sync_resolves_mains_head_and_replace_over_a_crash_removes_the_marker(hub, tmp_path):
    head1, src = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    shutil.copytree(src, target)
    S.write_record(target, {'v': 1, 'repo_id': 'lena-schmidt/omx_f_a', 'display_name': 'Mein Name', 'private': True})
    head2, _ = seed(hub, 'omx_f_a', tmp_path / 'v2', lengths=(20, 21, 22))
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'sync', None))
    assert r['ok'] and r['revision'] == head2
    rec = S.read_record(target)
    assert (rec['hub_sha'], rec['display_name'], rec['private']) == (head2, 'Mein Name', True)
    S.session_marker_path(target).write_text('{}')                  # a stop/crash left it behind
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'replace', head1,
                                meta_digest=S.meta_digest(target)))
    assert r['ok']
    assert not S.session_marker_path(target).exists()
    assert json.loads((target / 'meta/info.json').read_text())['total_episodes'] == 2


def test_a_copy_remembers_its_source_and_its_visibility(hub, tmp_path):
    head, _ = seed(hub, 'omx_f_demo', tmp_path, repo='lena-schmidt/omx_f_demo', private=True)
    target = hub.root / 'lena-schmidt/omx_f_kopie'
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_demo', target, 'copy', head, display_name='Kopie'))
    assert r['ok']
    rec = S.read_record(target)
    assert rec['source'] == {'repo_id': 'lena-schmidt/omx_f_demo', 'sha': head}
    assert (rec['repo_id'], rec['private'], rec['display_name']) == ('lena-schmidt/omx_f_kopie', True, 'Kopie')
    assert 'hub_sha' not in rec


@pytest.mark.parametrize('case', ['old_format', 'other_robot', 'unsupported', 'noload', 'flip'])
def test_each_refusal_removes_the_tmp_and_leaves_the_target(hub, tmp_path, case):
    build = {'old_format': {'codebase_version': 'v2.1'}, 'other_robot': {'robot_type': 'koch'}}.get(case, {})
    head, src = seed(hub, 'omx_f_a', tmp_path, **build)
    if case in ('unsupported', 'noload'):
        info = json.loads((src / 'meta/info.json').read_text())
        if case == 'unsupported':
            info['video_path'] = 'videos/{video_key}/{episode_index}.mp4'
        (src / 'meta/info.json').write_text(json.dumps(info))
        if case == 'noload':
            (src / 'meta/NOLOAD').write_text('x')
        head = hub.FS.put_tree('lena-schmidt/omx_f_a', src, private=False, title='odd')
    target = hub.root / 'lena-schmidt/omx_f_a'
    shutil.copytree(src, target)
    (target / 'meta/NOLOAD').unlink(missing_ok=True)
    before = tree_digest(target)
    extra = {'D2_FLIP': 'data/chunk-000/file-000.parquet'} if case == 'flip' else {}
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'replace', head,
                                meta_digest=S.meta_digest(target)), **extra)
    assert r['code'] == {'old_format': 'old_format', 'other_robot': 'other_robot', 'unsupported': 'unsupported',
                         'noload': 'broken', 'flip': 'broken'}[case]
    assert tree_digest(target) == before
    assert not HS.tmp_path_of(target, 'replace').exists()
    assert not Path(f'{target}.bak_sync').exists()


def test_the_disk_refusal_carries_free_and_need(hub, tmp_path):
    head, _ = seed(hub, 'omx_f_a', tmp_path)
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', hub.root / 'lena-schmidt/omx_f_a', 'new', head),
                   D2_FREE_BYTES=3_000_000_100)
    assert r['code'] == 'disk' and r['free'] == 3_000_000_100 and r['need'] > 100


def test_keep_runs_the_local_gate_first_and_swaps_nothing(hub, tmp_path):
    head, src = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    shutil.copytree(src, target)
    S.session_marker_path(target).write_text('{}')
    no_request_answers(hub)
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'keep', head))
    assert r['code'] == 'in_session'
    (hub.env.hub_root / 'faults.json').unlink()
    S.session_marker_path(target).unlink()
    S.write_record(target, {'v': 1, 'repo_id': 'lena-schmidt/omx_f_a', 'display_name': 'X'})
    before = tree_digest(target)
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'keep', head))
    assert r['ok'] and r['trees']
    assert tree_digest(target) == before
    assert S.read_record(target) == {'v': 1, 'repo_id': 'lena-schmidt/omx_f_a', 'display_name': 'X'}
    assert (HS.tmp_path_of(target, 'keep') / 'meta/info.json').is_file()


def test_base_links_the_videos_it_already_has_and_fetches_the_rest(hub, tmp_path):
    base, src = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    shutil.copytree(src, target)
    head, _ = seed(hub, 'omx_f_a', tmp_path / 'v2', lengths=(20, 21, 22), seed=5)
    assert run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'keep', head))['ok']
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'base', base))
    assert r['ok'] and r['fetched'] == 0
    bdir = HS.tmp_path_of(target, 'base')
    vids = sorted(p.relative_to(bdir).as_posix() for p in bdir.rglob('*.mp4'))
    assert vids and all((bdir / v).read_bytes() == (target / v).read_bytes() for v in vids)
    assert any(p.name == 'info.json' for p in bdir.rglob('*'))
    shutil.rmtree(bdir)
    victim = target / vids[0]
    victim.write_bytes(victim.read_bytes()[:-1] + b'\x00')                    # this copy's video differs now
    shutil.rmtree(HS.tmp_path_of(target, 'keep'))
    r = run_worker(hub, request(hub, 'lena-schmidt/omx_f_a', target, 'base', base))
    assert r['ok'] and r['fetched'] == 1


# ── the worker's own watch (G-14, P29) ───────────────────────────────────────

SLOW = {'FAKEHUB_DELAY_S': 3}          # dataset_info 3 s, then 0.3 s per file: the tmp exists mid-download


def test_a_changed_slot_ends_the_worker_with_nobody_polling(hub, tmp_path):
    head, _ = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    p = subprocess.Popen([sys.executable, '-m', 'd2_dl_wrapper'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=child_env(hub, **SLOW), start_new_session=True)
    p.stdin.write(json.dumps(request(hub, 'lena-schmidt/omx_f_a', target, 'new', head)) + '\n')
    p.stdin.close()
    tmp = HS.tmp_path_of(target, 'new')
    deadline = time.monotonic() + 30
    while not tmp.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tmp.exists()
    hub.token_file.write_text('hf_harnessotherAAAAAAAAAAAAAAAAA')
    t0 = time.monotonic()
    p.wait(timeout=10)
    assert time.monotonic() - t0 < 2.0
    out = p.stdout.read()
    assert 'DL_RESULT::{"ok": false, "code": "token_changed"}' in out
    assert not tmp.exists() and not target.exists()


def test_a_dead_parent_ends_the_worker(hub, tmp_path):
    head, _ = seed(hub, 'omx_f_a', tmp_path)
    target = hub.root / 'lena-schmidt/omx_f_a'
    parent = textwrap.dedent(f'''
        import json, subprocess, sys, time
        p = subprocess.Popen([sys.executable, '-m', 'd2_dl_wrapper'], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
        p.stdin.write({json.dumps(json.dumps(request(hub, 'lena-schmidt/omx_f_a', target, 'new', head)))} + '\\n')
        p.stdin.close()
        print(p.pid, flush=True)
        time.sleep(120)
    ''')
    sup = subprocess.Popen([sys.executable, '-c', parent], stdout=subprocess.PIPE, text=True,
                           env=child_env(hub, **SLOW))
    pid = int(sup.stdout.readline())
    tmp = HS.tmp_path_of(target, 'new')
    deadline = time.monotonic() + 30
    while not tmp.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tmp.exists()
    sup.kill()
    sup.wait()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail('the orphaned worker kept running')
    assert time.monotonic() - t0 < 2.0
    assert not tmp.exists()


# ── the supervisor (DatenService's DownloadProcess) ──────────────────────────

def _service(hub, token=LENA):
    node = types.SimpleNamespace(on_recording=False, data_manager=None, hf_api_worker=None, robot_type='omx_f',
                                 get_logger=lambda: types.SimpleNamespace(info=lambda *a: None))
    svc = NS.DatenService(node, root=hub.root, ros=False, start_threads=False, token_reader=lambda: token,
                          secret=b's' * 32)
    svc.download_poll_s = 0.05
    return svc


@pytest.fixture()
def worker_env(hub, monkeypatch):
    for k, v in child_env(hub).items():
        if k != 'HF_HUB_DISABLE_XET':
            monkeypatch.setenv(k, v)
    monkeypatch.delenv('HF_HUB_DISABLE_XET', raising=False)
    return hub


def test_the_supervisor_sets_xet_off_and_a_real_download_succeeds(worker_env, tmp_path, monkeypatch):
    hub = worker_env
    head, _ = seed(hub, 'omx_f_a', tmp_path)
    monkeypatch.setattr(NS, 'DOWNLOAD_WORKER_MODULE', 'd2_dl_wrapper')
    svc = _service(hub)
    seen = []
    r = NS.DownloadProcess(svc, request(hub, 'lena-schmidt/omx_f_a', hub.root / 'lena-schmidt/omx_f_a', 'new', head),
                           on_progress=lambda d, t: seen.append((d, t))).run(threading.Event())
    assert r['ok'] and r['xet_disabled'] is True
    assert seen and seen[-1][1] > 0


STUB = textwrap.dedent('''
    """TEST-ONLY stub workers for the supervisor's rules."""
    import json, os, pathlib, sys, time
    req = json.loads(sys.stdin.readline())
    mode = os.environ['D2_STUB']
    target = pathlib.Path(req['target_dir'])
    sys.path.insert(0, os.environ['D2_PKG'])
    from physical_ai_server.data_processing import dataset_sync as S
    if mode == 'heartbeat':
        for i in range(12):
            print('DL_PROGRESS::' + json.dumps({'stage': 'verify', 'done': (i + 1) * 1000}), flush=True)
            time.sleep(0.05)
        print('DL_RESULT::' + json.dumps({'ok': True, 'revision': 'r'}), flush=True)
    elif mode == 'silent':
        time.sleep(0.6)
        print('DL_RESULT::' + json.dumps({'ok': True, 'revision': 'r'}), flush=True)
    elif mode == 'between_renames':
        tmp = pathlib.Path(f'{target}.tmp_sync'); (tmp / 'meta').mkdir(parents=True)
        (tmp / 'meta' / 'info.json').write_text('new')
        S.write_json_atomic(S.record_next_path(target), {'record': {'v': 1, 'repo_id': 'x/y', 'hub_sha': 'new'},
                                                         'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        target.rename(f'{target}.bak_sync')
        time.sleep(60)
    elif mode == 'before_first_rename':
        tmp = pathlib.Path(f'{target}.tmp_sync'); (tmp / 'meta').mkdir(parents=True)
        (tmp / 'meta' / 'info.json').write_text('new')
        S.write_json_atomic(S.record_next_path(target), {'record': {'v': 1, 'repo_id': 'x/y', 'hub_sha': 'new'},
                                                         'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        time.sleep(60)
''')


def _stub_process(hub, monkeypatch, mode, target, stall_s=60.0):
    (hub.env.wrap / 'd2_stub_worker.py').write_text(STUB)
    monkeypatch.setattr(NS, 'DOWNLOAD_WORKER_MODULE', 'd2_stub_worker')
    monkeypatch.setenv('PYTHONPATH', str(hub.env.wrap))
    monkeypatch.setenv('D2_STUB', mode)
    monkeypatch.setenv('D2_PKG', str(PKG_PARENT))
    svc = _service(hub)
    svc.download_stall_s = stall_s
    req = request(hub, 'x/y', target, 'sync', 'r')
    return svc, NS.DownloadProcess(svc, req)


def test_a_check_slower_than_the_stall_survives_on_its_heartbeats(hub, tmp_path, monkeypatch):
    target = hub.root / 'lena-schmidt/omx_f_a'
    svc, proc = _stub_process(hub, monkeypatch, 'heartbeat', target, stall_s=0.25)
    assert proc.run(threading.Event()) == {'ok': True, 'revision': 'r'}
    svc, proc = _stub_process(hub, monkeypatch, 'silent', target, stall_s=0.25)
    assert proc.run(threading.Event()) == {'ok': False, 'code': 'stalled'}


def _old_copy(target):
    (target / 'meta').mkdir(parents=True)
    (target / 'meta' / 'info.json').write_text('old')
    S.write_record(target, {'v': 1, 'repo_id': 'lena-schmidt/omx_f_a', 'hub_sha': 'old'})
    S.session_marker_path(target).write_text('{}')


def _kill_when(proc, pred):
    cancel = threading.Event()

    def watch():
        deadline = time.monotonic() + 30
        while not pred() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()
    threading.Thread(target=watch, daemon=True).start()
    return proc.run(cancel)


def test_a_kill_between_the_renames_is_recovered_at_once(hub, tmp_path, monkeypatch):
    target = hub.root / 'lena-schmidt/omx_f_a'
    _old_copy(target)
    svc, proc = _stub_process(hub, monkeypatch, 'between_renames', target)
    r = _kill_when(proc, lambda: Path(f'{target}.bak_sync').exists() and not target.exists())
    assert r == {'ok': False, 'code': 'cancelled'}
    assert (target / 'meta/info.json').read_text() == 'old'
    assert S.read_record(target)['hub_sha'] == 'old'
    assert not Path(f'{target}.bak_sync').exists() and not S.record_next_path(target).exists()
    assert not Path(f'{target}.tmp_sync').exists()


def test_a_kill_before_the_first_rename_keeps_the_old_copy_its_record_and_its_marker(hub, tmp_path, monkeypatch):
    target = hub.root / 'lena-schmidt/omx_f_a'
    _old_copy(target)
    svc, proc = _stub_process(hub, monkeypatch, 'before_first_rename', target)
    r = _kill_when(proc, lambda: S.record_next_path(target).exists())
    assert r == {'ok': False, 'code': 'cancelled'}
    assert (target / 'meta/info.json').read_text() == 'old'
    assert S.read_record(target)['hub_sha'] == 'old'
    assert S.session_marker_path(target).exists()
    assert not S.record_next_path(target).exists()
    assert sorted(p.name for p in target.parent.iterdir()) == sorted(
        ['omx_f_a', 'omx_f_a.sync.json', 'omx_f_a.session.json', '.omx_f_a.lock'])
