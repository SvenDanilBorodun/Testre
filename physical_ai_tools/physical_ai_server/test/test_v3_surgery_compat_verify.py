"""Daten 2.0 engine — compatibility, verify's teeth, identity, the upload gate
(spec §D1 steps 2/4, ``verify``, ``episode_identity``, ``integrity``; P18, P21,
P28).

Two tiers. Without LeRobot (CI's python-tests: pyarrow/av/numpy only): the
``stats`` compatibility line refuses before LeRobot is imported, a footer-less
data file is a ``layout`` error, two still takes with equal rows but different
video have different identities, and ``integrity``'s own checks (a fake
``LeRobotDataset`` stands in for the final load). With LeRobot (the server
image; ``.github/scripts/daten_smoke.py`` repeats these in CI): every verify
mutation is caught, a source with another column order assembles, the data-file
roll-over at three settings, and the same take is the same id after the engine.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types

import pytest

av = pytest.importorskip('av')
pa = pytest.importorskip('pyarrow')
pq = pytest.importorskip('pyarrow.parquet')
np = pytest.importorskip('numpy')

from physical_ai_server.data_processing import v3_surgery as V  # noqa: E402
from test_v3_surgery_layout import build_dataset, build_real_dataset, default_motion  # noqa: E402

HAS_LEROBOT = importlib.util.find_spec('lerobot') is not None
needs_lerobot = pytest.mark.skipif(not HAS_LEROBOT, reason='LeRobot only in the server image')


@pytest.fixture()
def lerobot_or_stub(monkeypatch):
    """The real LeRobot where installed; else a stand-in whose LeRobotDataset
    accepts any root (integrity's own checks are what these tests judge)."""
    if HAS_LEROBOT:
        return
    for name in ('lerobot', 'lerobot.datasets'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    mod = types.ModuleType('lerobot.datasets.lerobot_dataset')
    mod.LeRobotDataset = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, 'lerobot.datasets.lerobot_dataset', mod)


# ── without LeRobot ─────────────────────────────────────────────────────────

def test_no_quantile_stats_fail_the_stats_check_and_refuse_before_lerobot(tmp_path, monkeypatch):
    a = build_dataset(tmp_path / 'lena' / 'omx_f_a')
    b = build_dataset(tmp_path / 'lena' / 'omx_f_b',
                      stat_names=('min', 'max', 'mean', 'std', 'count'))
    sa, sb = V.Source(a), V.Source(b)
    checks = dict((cid, ok) for ok, cid in V.check_compatible([sa, sb]))
    assert list(checks) == list(V.MERGE_CHECKS)
    assert checks == {**{c: True for c in V.MERGE_CHECKS}, 'stats': False}
    # the refusal comes before LeRobot is even imported (CI has none)
    monkeypatch.setitem(sys.modules, 'lerobot', None)
    with pytest.raises(V.SurgeryError) as e:
        V.assemble(tmp_path / 'out', [(sa, 0), (sb, 0)], 'lena/out')
    assert (e.value.code, e.value.detail) == ('incompatible', 'stats')
    assert not (tmp_path / 'out').exists()


# (the fps lives in each camera's video info too, and a camera set comes with
# its own video info and statistics — those checks fail WITH the primary one)
@pytest.mark.parametrize('what,kw,failing', [
    ('robot', {'robot_type': 'so100'}, ['robot']),
    ('fps', {'fps': 25}, ['fps', 'video']),
    ('cameras', {'cameras': ('gripper',)}, ['cameras', 'video', 'stats']),
    ('joints', {'joints': 7}, ['joints']),
    ('video', {'h': 32}, ['video']),
    ('version', {'codebase_version': 'v2.1'}, ['version']),
])
def test_each_compatibility_check_fails_for_its_cause(tmp_path, what, kw, failing):
    a = V.Source(build_dataset(tmp_path / 'lena' / 'omx_f_a'))
    b = V.Source(build_dataset(tmp_path / 'lena' / f'omx_f_{what}', **kw))
    bad = [cid for ok, cid in V.check_compatible([a, b]) if not ok]
    assert bad == failing, bad


def test_a_footer_less_data_file_is_a_layout_error_never_a_raw_one(tmp_path):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_cut')
    f = root / 'data' / 'chunk-000' / 'file-000.parquet'
    f.write_bytes(f.read_bytes()[:-200])
    src = V.Source(root)                     # meta is fine; the data file is read lazily
    with pytest.raises(V.SurgeryError) as e:
        src.data_rows(0)
    assert e.value.code == 'layout'


def test_two_still_takes_with_equal_rows_but_different_video_are_two_episodes(tmp_path):
    still = lambda ep, n, j: (np.zeros((n, j), np.float32), np.zeros((n, j), np.float32))  # noqa: E731
    a = V.Source(build_dataset(tmp_path / 'lena' / 'omx_f_a', lengths=(20,), motion=still, seed=1))
    b = V.Source(build_dataset(tmp_path / 'lena' / 'omx_f_b', lengths=(20,), motion=still, seed=2))
    assert V.payload_columns(a.data_rows(0)) == V.payload_columns(b.data_rows(0))
    assert a.data_rows(0).select(V.payload_columns(a.data_rows(0))).equals(
        b.data_rows(0).select(V.payload_columns(b.data_rows(0))))
    assert V.episode_identity(a, 0) != V.episode_identity(b, 0)
    c = V.Source(build_dataset(tmp_path / 'lena' / 'omx_f_c', lengths=(20,), motion=still, seed=1))
    assert V.episode_identity(a, 0) == V.episode_identity(c, 0), 'the same take is the same id'


def _crash(root, *, footer=True, episodes_file=True, totals=True):
    """A second session the environment stop killed (audit M2): a data file
    without its parquet footer, its meta/episodes never written, info.json
    already counting the episodes."""
    if footer:
        extra = root / 'data' / 'chunk-000' / 'file-001.parquet'
        t = pq.read_table(root / 'data' / 'chunk-000' / 'file-000.parquet')
        pq.write_table(t, extra)
        extra.write_bytes(extra.read_bytes()[:-200])
    if episodes_file:
        # a meta/episodes file of the dead session that LeRobot never finished
        (root / 'meta' / 'episodes' / 'chunk-000' / 'file-001.parquet').write_bytes(b'PAR1')
    if totals:
        p = root / 'meta' / 'info.json'
        info = json.loads(p.read_text())
        info['total_episodes'] += 2
        info['total_frames'] += 40
        p.write_text(json.dumps(info))


@pytest.mark.parametrize('leftover', ['footer', 'episodes_file', 'totals', 'all'])
def test_integrity_refuses_a_crashed_sessions_leftovers(tmp_path, lerobot_or_stub, leftover):
    root = build_dataset(tmp_path / 'lena' / f'omx_f_{leftover}')
    flags = {k: (leftover in (k, 'all')) for k in ('footer', 'episodes_file', 'totals')}
    _crash(root, **flags)
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert e.value.code == 'broken'


def test_integrity_passes_an_intact_dataset_and_skips_known_good_videos(tmp_path, lerobot_or_stub, monkeypatch):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_ok')
    opened = []
    real_open = V.av.open

    def spy(path, *a, **k):
        opened.append(Path(str(path)).name if False else Path(str(path)).relative_to(root.resolve()).as_posix())
        return real_open(path, *a, **k)
    monkeypatch.setattr(V.av, 'open', spy)
    good = 'videos/observation.images.gripper/chunk-000/file-000.mp4'
    src = V.integrity(root, known_good=frozenset({good}))
    assert len(src.episodes) == 3
    assert good not in opened
    assert 'videos/observation.images.scene/chunk-000/file-000.mp4' in opened


def test_integrity_refuses_a_truncated_video(tmp_path, lerobot_or_stub):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_trunc')
    v = root / 'videos' / 'observation.images.scene' / 'chunk-000' / 'file-000.mp4'
    v.write_bytes(v.read_bytes()[: len(v.read_bytes()) // 2])
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert e.value.code == 'broken'


# ── the sub-checks no other test pins (V1-2), on a recording stand-in for LeRobot ──

@pytest.fixture()
def lerobot_calls(monkeypatch):
    """A RECORDING stand-in for the two LeRobot entry points verify/integrity
    call, installed whether LeRobot exists or not: these tests judge OUR calls
    into it (that the loader runs, that every episode is decoded twice), which
    a stand-in that accepts everything cannot show."""
    calls = {'load': [], 'decode': [], 'load_error': None, 'decode_error_on': None}

    def loader(repo_id, root=None, **kw):
        calls['load'].append((repo_id, Path(root).resolve()))
        if calls['load_error'] is not None:
            raise calls['load_error']

    def decode(path, timestamps, tolerance_s=None, backend=None, **kw):
        calls['decode'].append((Path(path).name, Path(path).parent.parent.name, list(timestamps),
                                tolerance_s, backend))
        if calls['decode_error_on'] is not None and len(calls['decode']) == calls['decode_error_on']:
            raise RuntimeError('cannot decode')
    for name in ('lerobot', 'lerobot.datasets'):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    ld = types.ModuleType('lerobot.datasets.lerobot_dataset')
    ld.LeRobotDataset = loader
    vu = types.ModuleType('lerobot.datasets.video_utils')
    vu.decode_video_frames = decode
    monkeypatch.setitem(sys.modules, 'lerobot.datasets.lerobot_dataset', ld)
    monkeypatch.setitem(sys.modules, 'lerobot.datasets.video_utils', vu)
    return calls


def _self_parts(root):
    src = V.Source(root)
    return [(src, e) for e in range(len(src.episodes))]


def test_verify_loads_with_lerobot_and_decodes_the_first_and_last_frame_of_every_episode(tmp_path, lerobot_calls):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_v', lengths=(20, 21, 22))
    assert V.verify(root, _self_parts(root)) == []
    assert lerobot_calls['load'] == [('verify/check', root.resolve())]
    fps = V.Source(root).info['fps']
    want = []
    for ep, length in enumerate((20, 21, 22)):
        r = V.Source(root).episodes[ep]
        for k in ('observation.images.gripper', 'observation.images.scene'):
            fo = r[f'videos/{k}/from_timestamp']
            want += [('file-000.mp4', k, [fo], V.DECODE_TOLERANCE_S, 'pyav'),
                     ('file-000.mp4', k, [fo + (length - 1) / fps], V.DECODE_TOLERANCE_S, 'pyav')]
    assert lerobot_calls['decode'] == want


def test_a_loader_or_a_decoder_failure_is_a_problem_never_an_exception(tmp_path, lerobot_calls):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_v', lengths=(20, 21))
    lerobot_calls['decode_error_on'] = 3                     # episode 0, the scene camera's first frame
    assert V.verify(root, _self_parts(root)) == [(0, 'error:RuntimeError')]
    lerobot_calls['decode_error_on'] = None
    lerobot_calls['load_error'] = RuntimeError('does not load')
    assert V.verify(root, _self_parts(root)) == [('load', 'RuntimeError')]


def test_verify_counts_an_episode_no_part_accounts_for(tmp_path, lerobot_calls):
    """The totals: an output holding one episode more than its parts (every
    other check passes — the data, the row order and each listed episode are
    right) is `total_episodes` AND `total_frames`."""
    root = build_dataset(tmp_path / 'lena' / 'omx_f_v', lengths=(20, 21, 22))
    assert V.verify(root, _self_parts(root)[:-1]) == ['total_episodes', 'total_frames']


def test_integrity_runs_lerobots_loader_last(tmp_path, lerobot_calls):
    root = build_dataset(tmp_path / 'lena' / 'omx_f_i')
    assert len(V.integrity(root).episodes) == 3
    assert lerobot_calls['load'] == [('integrity/check', root.resolve())]
    lerobot_calls['load_error'] = RuntimeError('does not load')
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert (e.value.code, e.value.detail) == ('broken', 'RuntimeError')


def test_integrity_refuses_a_hole_in_the_global_index(tmp_path, lerobot_calls):
    """Every row is there and every episode has its length, but the data files'
    `index` columns are not 0..total_frames-1 (LeRobot addresses frames by it)."""
    root = build_dataset(tmp_path / 'lena' / 'omx_f_i')
    f = next((root / 'data').rglob('*.parquet'))
    t = pq.read_table(f)
    idx = t['index'].to_pylist()
    idx[-1] += 5
    pq.write_table(t.set_column(t.column_names.index('index'), 'index', pa.array(idx, type=t['index'].type)), f)
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert (e.value.code, e.value.detail) == ('broken', 'data index')
    assert lerobot_calls['load'] == [], 'refused before the loader'


@pytest.mark.parametrize('field', ['total_episodes', 'total_frames'])
def test_integrity_refuses_totals_that_disagree_with_the_episodes(tmp_path, lerobot_calls, field):
    root = build_dataset(tmp_path / 'lena' / f'omx_f_{field}')
    p = root / 'meta' / 'info.json'
    info = json.loads(p.read_text())
    info[field] += 1
    p.write_text(json.dumps(info))
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert (e.value.code, e.value.detail) == ('broken', field)


# ── with LeRobot (the server image) ─────────────────────────────────────────

@pytest.fixture(scope='module')
def real(tmp_path_factory):
    if not HAS_LEROBOT:
        pytest.skip('LeRobot only in the server image')
    base = tmp_path_factory.mktemp('real')
    return build_real_dataset(base / 'lena' / 'omx_f_real', sessions=((12, 13, 12), (11, 14)))


def _assembled(tmp_path, real, parts_idx, name='out'):
    src = V.Source(real)
    parts = [(src, e) for e in parts_idx]
    out = tmp_path / 'lena' / name
    V.assemble(out, parts, f'lena/{name}')
    return out, parts


@needs_lerobot
def test_a_clean_assembly_verifies(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [4, 0, 2])
    assert V.verify(out, parts) == []


@needs_lerobot
def test_two_swapped_episodes_inside_a_data_file_are_row_order(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [0, 1, 2])
    f = out / 'data' / 'chunk-000' / 'file-000.parquet'
    t = pq.read_table(f)
    e0 = t.filter(pa.compute.equal(t['episode_index'], 0))
    e1 = t.filter(pa.compute.equal(t['episode_index'], 1))
    rest = t.filter(pa.compute.greater(t['episode_index'], 1))
    pq.write_table(pa.concat_tables([e1, e0, rest]), f)
    assert ('data', 'row_order') in V.verify(out, parts)


@needs_lerobot
def test_one_changed_state_value_is_data(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [0, 1])
    f = out / 'data' / 'chunk-000' / 'file-000.parquet'
    t = pq.read_table(f)
    col = t['observation.state'].to_pylist()
    col[5] = [v + 0.001 for v in col[5]]
    t = t.set_column(t.schema.get_field_index('observation.state'), t.schema.field('observation.state'),
                     pa.array(col, type=t.schema.field('observation.state').type))
    pq.write_table(t, f)
    assert (0, 'data') in V.verify(out, parts)


@needs_lerobot
def test_one_changed_per_episode_statistic_is_stats(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [0, 1])
    f = next((out / 'meta' / 'episodes').rglob('*.parquet'))
    t = pq.read_table(f)
    col = t['stats/action/mean'].to_pylist()
    col[1] = [v + 0.5 for v in col[1]]
    t = t.set_column(t.schema.get_field_index('stats/action/mean'), t.schema.field('stats/action/mean'),
                     pa.array(col, type=t.schema.field('stats/action/mean').type))
    pq.write_table(t, f)
    assert (1, 'stats') in V.verify(out, parts)


@needs_lerobot
def test_a_truncated_output_video_is_a_problem_never_an_exception(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [0, 1, 2])
    v = next((out / 'videos' / 'observation.images.scene').rglob('*.mp4'))
    data = v.read_bytes()
    v.write_bytes(data[: len(data) * 2 // 3])
    problems = V.verify(out, parts)
    assert problems
    with pytest.raises(V.SurgeryError) as e:
        V.verify_or_raise(out, parts)
    assert e.value.code == 'verify_failed'


@needs_lerobot
def test_a_source_with_another_column_order_assembles_and_verifies(tmp_path, real):
    other = tmp_path / 'lena' / 'omx_f_rev'
    shutil.copytree(real, other)
    for f in (other / 'data').rglob('*.parquet'):
        t = pq.read_table(f)
        pq.write_table(t.select(list(reversed(t.column_names))), f)
    a, b = V.Source(real), V.Source(other)
    parts = [(a, 0), (b, 1), (a, 3)]
    out = tmp_path / 'lena' / 'omx_f_mix'
    V.assemble(out, parts, 'lena/omx_f_mix')
    assert V.verify(out, parts) == []


@needs_lerobot
@pytest.mark.parametrize('mb,chunks', [(0.001, None), (0.001, 2), (0.0001, 1)])
def test_the_data_file_roll_over(tmp_path, real, mb, chunks):
    src_root = tmp_path / 'lena' / 'omx_f_roll'
    shutil.copytree(real, src_root)
    p = src_root / 'meta' / 'info.json'
    info = json.loads(p.read_text())
    info['data_files_size_in_mb'] = mb
    if chunks:
        info['chunks_size'] = chunks
    p.write_text(json.dumps(info))
    src = V.Source(src_root)
    parts = [(src, e) for e in range(len(src.episodes))]
    out = tmp_path / 'lena' / 'omx_f_rolled'
    V.assemble(out, parts, 'lena/omx_f_rolled')
    files = sorted((out / 'data').rglob('*.parquet'))
    assert len(files) == len(parts) and len(files) > 1
    assert V.verify(out, parts) == []


@needs_lerobot
def test_the_same_take_keeps_its_identity_through_the_engine(tmp_path, real):
    out, parts = _assembled(tmp_path, real, [3, 1])
    src, o = V.Source(real), V.Source(out)
    assert V.episode_identity(o, 0) == V.episode_identity(src, 3)
    assert V.episode_identity(o, 1) == V.episode_identity(src, 1)
    assert V.episode_identity(o, 0) != V.episode_identity(o, 1)


@needs_lerobot
def test_integrity_with_the_real_loader(tmp_path, real):
    root = tmp_path / 'lena' / 'omx_f_int'
    shutil.copytree(real, root)
    assert len(V.integrity(root).episodes) == 5
    _crash(root)
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert e.value.code == 'broken'


def _v21(root):
    p = root / 'meta' / 'info.json'
    info = json.loads(p.read_text())
    info['codebase_version'] = 'v2.1'                        # only LeRobot's own loader refuses this
    p.write_text(json.dumps(info, indent=4))


@needs_lerobot
def test_integrity_and_verify_refuse_what_only_lerobots_loader_refuses(tmp_path, real):
    """V1-2: the REAL loader runs inside both: a copy every check of ours
    passes and LeRobot 0.5.1 will not load (another codebase version)."""
    root = tmp_path / 'lena' / 'omx_f_v21'
    shutil.copytree(real, root)
    _v21(root)
    with pytest.raises(V.SurgeryError) as e:
        V.integrity(root)
    assert (e.value.code, e.value.detail) == ('broken', 'BackwardCompatibilityError')
    out, parts = _assembled(tmp_path, real, [0, 1])
    assert V.verify(out, parts) == []
    _v21(out)
    assert V.verify(out, parts) == [('load', 'BackwardCompatibilityError')]


@needs_lerobot
def test_verify_decodes_with_lerobots_decoder(tmp_path, real, monkeypatch):
    import lerobot.datasets.video_utils as vu
    calls = []
    real_decode = vu.decode_video_frames

    def spy(path, timestamps, *a, **k):
        calls.append((Path(path).name, list(timestamps), k.get('backend')))
        return real_decode(path, timestamps, *a, **k)
    monkeypatch.setattr(vu, 'decode_video_frames', spy)
    out, parts = _assembled(tmp_path, real, [0, 3, 1])
    assert V.verify(out, parts) == []
    assert len(calls) == 2 * len(parts) * len(V.Source(out).video_keys)
    assert {c[2] for c in calls} == {'pyav'}


def test_motion_helper_is_deterministic():
    s1, a1 = default_motion(2, 30, 6)
    s2, a2 = default_motion(2, 30, 6)
    assert np.array_equal(s1, s2) and np.array_equal(a1, a2)
