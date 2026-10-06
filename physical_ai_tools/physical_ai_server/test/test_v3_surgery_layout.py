"""Daten 2.0 engine — layout and confinement (spec §D1 step 1, R-2, P19).

A dataset is only opened when its ``info.json`` names LeRobot's default
templates and every feature key is safe; every file it makes us open is built
from the default templates and confined under the dataset root (symlinks
followed). P19's seven attacks are refused ``unsupported`` before any output
exists and the victim stays byte-identical.

This module also holds the fixture builders the other Daten engine tests import
(``from test_v3_surgery_layout import build_dataset``):

* :func:`build_dataset` writes a v3.0 dataset in the RECORDER's layout with
  pyarrow + PyAV only (no LeRobot): ``time_base 1/15360``, 512 ticks per frame,
  libx264 GOP 2 without B-frames (I-P-I-P), every episode starting on a
  keyframe, one concatenated video file per camera — so the cutter, the layout
  rules and the episode identity run in CI (pyarrow/av/numpy only);
* :func:`build_real_dataset` uses the recorder's own writer
  (``LeRobotDatasetWrapper``) and therefore runs only where LeRobot is
  installed (the server image; ``.github/scripts/daten_smoke.py`` covers the
  same in CI).
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

av = pytest.importorskip('av')
pa = pytest.importorskip('pyarrow')
pq = pytest.importorskip('pyarrow.parquet')
np = pytest.importorskip('numpy')

from physical_ai_server.data_processing import v3_surgery as V  # noqa: E402

FPS = 30
TB = Fraction(1, 15360)
TICKS = 512                                    # 15360 / 30
STAT_NAMES = ('min', 'max', 'mean', 'std', 'count', 'q01', 'q10', 'q50', 'q90', 'q99')
JOINT_NAMES = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']


def _stats(values, names=STAT_NAMES):
    a = np.asarray(values, dtype=np.float64)
    if a.ndim == 1:
        a = a[:, None]
    out = {}
    for n in names:
        if n == 'min':
            v = a.min(0)
        elif n == 'max':
            v = a.max(0)
        elif n == 'mean':
            v = a.mean(0)
        elif n == 'std':
            v = a.std(0)
        elif n == 'count':
            v = np.array([len(a)])
        else:
            v = np.quantile(a, int(n[1:]) / 100.0, axis=0)
        out[n] = np.asarray(v).tolist()
    return out


def default_motion(ep, n, joints):
    """Smooth, distinct joint motion per episode: state follows action by 2 frames."""
    t = np.arange(n) / FPS
    act = np.stack([0.4 * np.sin(0.8 * t * (k + 1) + ep) for k in range(joints)], axis=1)
    act[:, -1] = 0.3 + 0.3 * np.sign(np.sin(1.3 * t + ep))      # the gripper opens and closes
    state = np.concatenate([np.repeat(act[:1], 2, 0), act[:-2]], axis=0)
    return state.astype(np.float32), act.astype(np.float32)


def _frame(cam, ep, f, h, w, seed):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = (f * 7 + cam * 40 + seed * 13) % 256
    img[:, :, 1] = (ep * 37 + seed * 29) % 256
    img[: h // 2, : w // 2, 2] = (f * 3) % 256
    return img


def _encode(path, n_frames, starts, frame_fn, h, w, gop=2, bframes=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(path), 'w', options={'movflags': 'faststart'})
    opts = {'g': str(gop), 'bf': str(bframes), 'sc_threshold': '0'}
    if bframes:
        opts['x264-params'] = 'forced-idr=1:open-gop=0'
    s = out.add_stream('libx264', rate=FPS, options=opts)
    s.width, s.height, s.pix_fmt = w, h, 'yuv420p'
    s.time_base = TB
    for g in range(n_frames):
        fr = av.VideoFrame.from_ndarray(frame_fn(g), format='rgb24')
        fr.pts = g * TICKS
        fr.time_base = TB
        fr.pict_type = av.video.frame.PictureType.I if g in starts else av.video.frame.PictureType.NONE
        for p in s.encode(fr):
            out.mux(p)
    for p in s.encode():
        out.mux(p)
    out.close()


def build_dataset(root, *, lengths=(20, 21, 22), cameras=('gripper', 'scene'), joints=6,
                  tasks=None, robot_type='omx_f', fps=FPS, stat_names=STAT_NAMES, seed=0,
                  motion=None, h=48, w=64, gop=2, bframes=0, codebase_version='v3.0',
                  column_order=None):
    """A v3.0 dataset in the recorder's layout, written without LeRobot.

    ``lengths`` per episode; ``tasks`` per episode (default one task);
    ``motion(ep, n, joints) -> (state, action)`` float32 arrays; ``seed`` changes
    the video pixels only (two datasets with equal rows but different video).
    Returns the dataset root."""
    root = Path(root)
    shutil.rmtree(root, ignore_errors=True)
    (root / 'meta' / 'episodes' / 'chunk-000').mkdir(parents=True)
    motion = motion or default_motion
    names = (list(JOINT_NAMES) if joints == 6
             else [f'joint{k + 1}' for k in range(joints - 1)] + ['gripper_joint_1'])
    tasks = list(tasks or ['Lege den gelben Würfel in die blaue Schale.'] * len(lengths))
    task_list = []
    for t in tasks:
        if t not in task_list:
            task_list.append(t)
    total = int(sum(lengths))
    starts, s0 = [], 0
    for n in lengths:
        starts.append(s0)
        s0 += n
    # ---- data
    cols = {'timestamp': [], 'frame_index': [], 'episode_index': [], 'index': [], 'task_index': [],
            'observation.state': [], 'action': []}
    per_ep = []
    for ep, n in enumerate(lengths):
        st, ac = motion(ep, n, joints)
        per_ep.append((st, ac))
        cols['timestamp'] += [np.float32(i / fps) for i in range(n)]
        cols['frame_index'] += list(range(n))
        cols['episode_index'] += [ep] * n
        cols['index'] += list(range(starts[ep], starts[ep] + n))
        cols['task_index'] += [task_list.index(tasks[ep])] * n
        cols['observation.state'] += [list(map(float, r)) for r in st]
        cols['action'] += [list(map(float, r)) for r in ac]
    vec = pa.list_(pa.float32(), joints)
    arrays = {
        'timestamp': pa.array(cols['timestamp'], pa.float32()),
        'frame_index': pa.array(cols['frame_index'], pa.int64()),
        'episode_index': pa.array(cols['episode_index'], pa.int64()),
        'index': pa.array(cols['index'], pa.int64()),
        'task_index': pa.array(cols['task_index'], pa.int64()),
        'observation.state': pa.array(cols['observation.state'], vec),
        'action': pa.array(cols['action'], vec),
    }
    order = list(column_order or ['timestamp', 'frame_index', 'episode_index', 'index', 'task_index',
                                  'observation.state', 'action'])
    (root / 'data' / 'chunk-000').mkdir(parents=True)
    pq.write_table(pa.table({c: arrays[c] for c in order}), root / 'data' / 'chunk-000' / 'file-000.parquet')
    # ---- videos: one concatenated file per camera, a keyframe at every episode start
    keys = [f'observation.images.{c}' for c in cameras]
    for ci, key in enumerate(keys):
        def frame_fn(g, ci=ci):
            ep = max(i for i, s in enumerate(starts) if s <= g)
            return _frame(ci, ep, g - starts[ep], h, w, seed)
        _encode(root / 'videos' / key / 'chunk-000' / 'file-000.mp4', total, set(starts), frame_fn,
                h, w, gop=gop, bframes=bframes)
    # ---- meta
    feats = {
        'timestamp': {'dtype': 'float32', 'shape': [1], 'names': None},
        'frame_index': {'dtype': 'int64', 'shape': [1], 'names': None},
        'episode_index': {'dtype': 'int64', 'shape': [1], 'names': None},
        'index': {'dtype': 'int64', 'shape': [1], 'names': None},
        'task_index': {'dtype': 'int64', 'shape': [1], 'names': None},
        'observation.state': {'dtype': 'float32', 'shape': [joints], 'names': names},
        'action': {'dtype': 'float32', 'shape': [joints], 'names': names},
    }
    for key in keys:
        feats[key] = {'dtype': 'video', 'shape': [h, w, 3], 'names': ['height', 'width', 'channels'],
                      'info': {'video.height': h, 'video.width': w, 'video.codec': 'h264',
                               'video.pix_fmt': 'yuv420p', 'video.is_depth_map': False,
                               'video.fps': fps, 'video.channels': 3, 'has_audio': False}}
    info = {'codebase_version': codebase_version, 'robot_type': robot_type,
            'total_episodes': len(lengths), 'total_frames': total, 'total_tasks': len(task_list),
            'chunks_size': 1000, 'data_files_size_in_mb': 100, 'video_files_size_in_mb': 200,
            'fps': fps, 'splits': {'train': f'0:{len(lengths)}'},
            'data_path': V.DEFAULT_DATA_PATH, 'video_path': V.DEFAULT_VIDEO_PATH, 'features': feats}
    (root / 'meta' / 'info.json').write_text(json.dumps(info, indent=4), encoding='utf-8')
    pq.write_table(pa.table({'task_index': pa.array(range(len(task_list)), pa.int64()),
                             'task': pa.array(task_list, pa.large_string())}),
                   root / 'meta' / 'tasks.parquet')
    rows = []
    for ep, n in enumerate(lengths):
        st, ac = per_ep[ep]
        r = {'episode_index': ep, 'tasks': [tasks[ep]], 'length': n, 'data/chunk_index': 0,
             'data/file_index': 0, 'dataset_from_index': starts[ep], 'dataset_to_index': starts[ep] + n}
        for key in keys:
            r[f'videos/{key}/chunk_index'] = 0
            r[f'videos/{key}/file_index'] = 0
            r[f'videos/{key}/from_timestamp'] = starts[ep] / fps
            r[f'videos/{key}/to_timestamp'] = (starts[ep] + n) / fps
        for feat, vals in (('timestamp', [i / fps for i in range(n)]), ('frame_index', range(n)),
                           ('episode_index', [ep] * n), ('index', range(starts[ep], starts[ep] + n)),
                           ('task_index', [task_list.index(tasks[ep])] * n),
                           ('observation.state', st), ('action', ac)):
            for k, v in _stats(list(vals), stat_names).items():
                r[f'stats/{feat}/{k}'] = v
        for key in keys:
            for k, v in _stats([[0.5, 0.5, 0.5]] * n, stat_names).items():
                r[f'stats/{key}/{k}'] = v
        r['meta/episodes/chunk_index'] = 0
        r['meta/episodes/file_index'] = 0
        rows.append(r)
    pq.write_table(pa.Table.from_pylist(rows), root / 'meta' / 'episodes' / 'chunk-000' / 'file-000.parquet')
    stats = {}
    (root / 'meta' / 'stats.json').write_text(json.dumps(stats), encoding='utf-8')
    return root


def build_real_dataset(root, *, sessions=((20, 21, 22),), cameras=('gripper', 'scene'), seed=0,
                       motion=None, video_mb=None, h=48, w=64, tasks=None):
    """A dataset written by the RECORDER's writer (LeRobotDatasetWrapper, h264,
    streaming encoding). Each session is a resumed writer (a new video file and
    a new meta/episodes file, P4/P15). Requires LeRobot."""
    pytest.importorskip('lerobot')
    from lerobot.datasets.utils import DEFAULT_FEATURES
    from physical_ai_server.data_processing.lerobot_dataset_wrapper import LeRobotDatasetWrapper
    root = Path(root)
    shutil.rmtree(root, ignore_errors=True)
    repo = f'{root.parent.name}/{root.name}'
    motion = motion or default_motion
    features = DEFAULT_FEATURES.copy()
    for cam in cameras:
        features[f'observation.images.{cam}'] = {'dtype': 'video', 'names': ['height', 'width', 'channels'],
                                                 'shape': (h, w, 3)}
    features['observation.state'] = {'dtype': 'float32', 'names': list(JOINT_NAMES), 'shape': (6,)}
    features['action'] = {'dtype': 'float32', 'names': list(JOINT_NAMES), 'shape': (6,)}
    ep_global = 0
    for si, lengths in enumerate(sessions):
        if si == 0:
            ds = LeRobotDatasetWrapper.create(repo_id=repo, fps=FPS, features=features, use_videos=True,
                                              root=str(root))
            if video_mb:
                ds.meta.update_chunk_settings(video_files_size_in_mb=video_mb)
        else:
            ds = LeRobotDatasetWrapper(repo, str(root))
        ds.set_robot_type('omx_f')
        for n in lengths:
            st, ac = motion(ep_global, n, 6)
            for f in range(n):
                frame = {f'observation.images.{c}': _frame(ci, ep_global, f, h, w, seed)
                         for ci, c in enumerate(cameras)}
                frame['observation.state'] = st[f]
                frame['action'] = ac[f]
                frame['task'] = (tasks or {}).get(ep_global, 'Lege den gelben Würfel in die blaue Schale.')
                ds.add_frame(frame)
            ds.save_episode()
            ep_global += 1
        ds.finalize()
    return root


def tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob('*')):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


# ── the tests ────────────────────────────────────────────────────────────────

@pytest.fixture()
def honest(tmp_path):
    return build_dataset(tmp_path / 'ds' / 'lena-schmidt' / 'omx_f_base')


def _mutate_info(root, fn):
    p = root / 'meta' / 'info.json'
    info = json.loads(p.read_text())
    fn(info)
    p.write_text(json.dumps(info))


def _mutate_row(root, col, value):
    f = root / 'meta' / 'episodes' / 'chunk-000' / 'file-000.parquet'
    t = pq.read_table(f)
    vals = t[col].to_pylist()
    vals[0] = value
    t = t.set_column(t.schema.get_field_index(col), pa.field(col, pa.int64()), pa.array(vals, pa.int64()))
    pq.write_table(t, f)


def _attacks(victim_dir):
    victim = str(victim_dir)
    return {
        'absolute_video_path': ('info', lambda i: i.__setitem__('video_path', victim + '/{video_key}.mp4')),
        'dotdot_video_path': ('info', lambda i: i.__setitem__(
            'video_path', '../../../../victim/{video_key}.mp4')),
        'custom_video_path': ('info', lambda i: i.__setitem__(
            'video_path', 'vids/{video_key}/{chunk_index}/{file_index}.mp4')),
        'absolute_data_path': ('info', lambda i: i.__setitem__('data_path', victim + '/{chunk_index}.parquet')),
        'slash_feature_key': ('info', lambda i: i['features'].__setitem__(
            '../../x', i['features'].pop('observation.images.scene'))),
        'negative_chunk_index': ('row', ('videos/observation.images.gripper/chunk_index', -1)),
        'symlinked_video_file': ('link', None),
    }


@pytest.mark.parametrize('attack', sorted(_attacks('/x')))
def test_P19_every_attack_is_refused_unsupported_before_any_output(tmp_path, honest, attack):
    victim = tmp_path / 'victim'
    victim.mkdir()
    for key in ('observation.images.gripper', 'observation.images.scene'):
        shutil.copy(honest / 'videos' / key / 'chunk-000' / 'file-000.mp4', victim / f'{key}.mp4')
    before = tree_digest(victim)
    d = tmp_path / 'ds' / 'lena-schmidt' / f'omx_f_{attack}'
    shutil.copytree(honest, d)
    kind, arg = _attacks(victim)[attack]
    if kind == 'info':
        _mutate_info(d, arg)
    elif kind == 'row':
        _mutate_row(d, *arg)
    else:
        v = d / 'videos' / 'observation.images.gripper' / 'chunk-000' / 'file-000.mp4'
        v.unlink()
        os.symlink(victim / 'observation.images.gripper.mp4', v)
    out = tmp_path / 'out'
    with pytest.raises(V.SurgeryError) as e:
        src = V.Source(d)
        V.assemble(out, [(src, ep) for ep in range(len(src.episodes))], 'x/y')
    assert e.value.code == 'unsupported', (attack, e.value)
    assert not out.exists()
    assert tree_digest(victim) == before


def test_the_honest_dataset_is_accepted(honest):
    src = V.Source(honest)
    assert len(src.episodes) == 3
    assert src.video_keys == ['observation.images.gripper', 'observation.images.scene']
    assert src.task_by_index == {0: 'Lege den gelben Würfel in die blaue Schale.'}
    assert src.data_rows(1).num_rows == 21
    assert src.video_file(2, 'observation.images.scene') == (
        honest / 'videos' / 'observation.images.scene' / 'chunk-000' / 'file-000.mp4').resolve()


def test_a_symlinked_meta_dir_is_refused_too(tmp_path, honest):
    outside = tmp_path / 'outside_meta'
    shutil.copytree(honest / 'meta', outside)
    d = tmp_path / 'ds' / 'lena-schmidt' / 'omx_f_link'
    shutil.copytree(honest, d)
    shutil.rmtree(d / 'meta' / 'episodes')
    os.symlink(outside / 'episodes', d / 'meta' / 'episodes')
    with pytest.raises(V.SurgeryError) as e:
        V.Source(d)
    assert e.value.code == 'unsupported'


@pytest.mark.parametrize('breakage', ['no_info', 'bad_json', 'no_episodes', 'no_tasks', 'gap_in_indices',
                                      'missing_column'])
def test_every_unreadable_layout_is_a_layout_error_never_a_raw_exception(tmp_path, honest, breakage):
    d = tmp_path / 'ds' / 'lena-schmidt' / f'omx_f_{breakage}'
    shutil.copytree(honest, d)
    if breakage == 'no_info':
        (d / 'meta' / 'info.json').unlink()
    elif breakage == 'bad_json':
        (d / 'meta' / 'info.json').write_text('{"codebase_version": "v3.0", "fps"')
    elif breakage == 'no_episodes':
        shutil.rmtree(d / 'meta' / 'episodes')
    elif breakage == 'no_tasks':
        (d / 'meta' / 'tasks.parquet').unlink()
    elif breakage == 'gap_in_indices':
        _mutate_row(d, 'episode_index', 7)
    else:
        f = d / 'meta' / 'episodes' / 'chunk-000' / 'file-000.parquet'
        pq.write_table(pq.read_table(f).drop(['data/chunk_index']), f)
    with pytest.raises(V.SurgeryError) as e:
        V.Source(d)
    assert e.value.code in ('layout', 'unsupported'), breakage
    if breakage != 'missing_column':
        assert e.value.code == 'layout'


def test_the_templates_and_key_rule_are_the_contract_s():
    import importlib.util
    p = Path(V.__file__).resolve().parents[1] / 'daten' / 'contract.py'
    spec = importlib.util.spec_from_file_location('_contract_for_v3', p)
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    assert V.FEATURE_KEY_RE.pattern == c.FEATURE_KEY_RE
    assert V.MAX_INDEX == c.MAX_INDEX
    assert V.MERGE_CHECKS == c.MERGE_CHECKS


def test_importing_the_engine_imports_no_lerobot():
    import subprocess
    import sys
    code = ('import sys, importlib.util; '
            f'spec = importlib.util.spec_from_file_location("v3s", {str(Path(V.__file__))!r}); '
            'm = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); '
            'print(sorted(n for n in sys.modules if n.split(".")[0] in ("lerobot", "torch", "rclpy")))')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == '[]'


def test_outputs_use_the_default_templates(tmp_path):
    src_root = build_real_dataset(tmp_path / 'ds' / 'lena-schmidt' / 'omx_f_real', sessions=((12, 13, 12),))
    src = V.Source(src_root)
    out = tmp_path / 'ds' / 'lena-schmidt' / 'omx_f_out'
    V.assemble(out, [(src, 2), (src, 0)], 'lena-schmidt/omx_f_out')
    info = json.loads((out / 'meta' / 'info.json').read_text())
    assert info['data_path'] == V.DEFAULT_DATA_PATH
    assert info['video_path'] == V.DEFAULT_VIDEO_PATH
    assert V.verify(out, [(src, 2), (src, 0)]) == []
    for p in out.rglob('*'):
        if p.is_file() and p.suffix in ('.parquet', '.mp4') and 'meta' not in p.parts:
            rel = p.relative_to(out).as_posix()
            assert rel.startswith(('data/chunk-', 'videos/observation.images.')), rel
