#!/usr/bin/env python3
"""Daten 2.0 — the edit engine's smoke test against the REAL LeRobot 0.5.1 writer.

Runs INSIDE the built physical-ai-server image (docker-publish.yml::smoke-test,
amd64 + opi, with /opt/ros/jazzy and /root/ros2_ws sourced, HF_HUB_OFFLINE=1),
so everything below is the shipped code: the recorder's writer
(LeRobotDatasetWrapper, streaming h264), the stream-copy engine
(data_processing/v3_surgery.py), the edit operations (data_editor_v3), the sync
model (dataset_sync) and the hints (dataset_hints). The deps-free unit tests run
the engine without LeRobot; this is the one place CI proves the lossless claim
with LeRobot's own loader, decoder and statistics.

Checks (spec §H8): a 2-camera fixture written by the real writer in two
sessions; delete / split / merge through data_editor_v3 with the P1 full check
(LeRobot loads the output, every frame of every kept episode decodes at the
train-time timestamps pixel-equal to the source, data rows, spans, totals, every
statistic equal to LeRobot's aggregate of the kept episodes); verify and its
mutations (a flipped video byte, a changed data value, two swapped episodes
inside a data file, a changed per-episode statistic); one clip per camera
(frame count = length, moov before mdat); the R-2 refusals (absolute and custom
video_path); the unaligned refusal on a re-encoded copy; the stats refusal on a
copy without quantile statistics; the data-file roll-over at a tiny
data_files_size_in_mb; „Beide behalten"'s three-way plan on two sessions of one
base plus a deletion; two still takes kept apart by episode_identity; integrity
refusing a crashed copy; a hint on an injected fault; meta_digest stability.
Prints ONE JSON line of numbers; exit 0 only when every check holds.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault('HF_HUB_OFFLINE', '1')
ROOT = Path(tempfile.mkdtemp(prefix='daten_smoke_'))
os.environ['HF_LEROBOT_HOME'] = str(ROOT / 'lerobot_home')

import av  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.compute as pc  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402
import torch  # noqa: E402

from lerobot.datasets.compute_stats import aggregate_stats  # noqa: E402
from lerobot.datasets.lerobot_dataset import LeRobotDataset  # noqa: E402
from lerobot.datasets.utils import DEFAULT_FEATURES  # noqa: E402
from lerobot.datasets.video_utils import decode_video_frames  # noqa: E402
from physical_ai_server.data_processing import data_editor_v3 as E  # noqa: E402
from physical_ai_server.data_processing import dataset_hints as HINTS  # noqa: E402
from physical_ai_server.data_processing import dataset_sync as S  # noqa: E402
from physical_ai_server.data_processing import v3_surgery as V  # noqa: E402
from physical_ai_server.data_processing.lerobot_dataset_wrapper import LeRobotDatasetWrapper  # noqa: E402

FPS = 30
H, W = 96, 128
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
CAMS = ('gripper', 'scene')
FAILED = []
NUMBERS = {}
T0 = time.time()


def check(name, ok, detail=''):
    if not ok:
        FAILED.append(name if not detail else f'{name}: {detail}'[:300])
    return ok


def motion(ep, n, *, still=False, no_grasp=False):
    t = np.arange(n) / FPS
    if still:
        a = np.zeros((n, 6), np.float32)
        return a, a.copy()
    a = np.stack([0.4 * np.sin(0.8 * (k + 1) * t + ep + k) for k in range(6)], 1).astype(np.float32)
    a[:, 5] = (0.3 + 0.3 * np.sin(0.9 * t + ep)).astype(np.float32)
    if no_grasp:
        a[:, 5] = a[0, 5]
    s = np.concatenate([np.repeat(a[:1], 2, 0), a[:-2]], 0).astype(np.float32)
    return s, a


def image(cam, ep, f, seed=0):
    img = np.zeros((H, W, 3), np.uint8)
    img[:, :, 0] = (f * 5 + cam * 60 + seed * 31) % 256
    img[:, :, 1] = (ep * 29 + seed * 7) % 256
    img[: H // 2, : W // 2, 2] = (f * 11) % 256
    img[H // 2:, W // 2:, 2] = (ep * 3 + f) % 256
    return img


def write(root, sessions, *, offset=0, seed=0, faults=None, video_mb=None):
    """The recorder's writer: each session a resumed LeRobotDatasetWrapper."""
    root = Path(root)
    repo = f'{root.parent.name}/{root.name}'
    features = DEFAULT_FEATURES.copy()
    for cam in CAMS:
        features[f'observation.images.{cam}'] = {'dtype': 'video', 'names': ['height', 'width', 'channels'],
                                                 'shape': (H, W, 3)}
    features['observation.state'] = {'dtype': 'float32', 'names': JOINTS, 'shape': (6,)}
    features['action'] = {'dtype': 'float32', 'names': JOINTS, 'shape': (6,)}
    ep = offset
    for si, lengths in enumerate(sessions):
        if si == 0 and not root.exists():
            ds = LeRobotDatasetWrapper.create(repo_id=repo, fps=FPS, features=features, use_videos=True,
                                              root=str(root))
            if video_mb:
                ds.meta.update_chunk_settings(video_files_size_in_mb=video_mb)
        else:
            ds = LeRobotDatasetWrapper(repo, str(root))
        ds.set_robot_type('omx_f')
        for n in lengths:
            kind = (faults or {}).get(ep, '')
            st, ac = motion(ep, n, still=kind == 'still', no_grasp=kind == 'no_grasp')
            for f in range(n):
                frame = {f'observation.images.{c}': image(ci, ep, f, seed) for ci, c in enumerate(CAMS)}
                frame['observation.state'] = st[f]
                frame['action'] = ac[f]
                frame['task'] = 'Lege den gelben Würfel in die blaue Schale.' if ep % 3 else 'Lege den roten Würfel.'
                ds.add_frame(frame)
            ds.save_episode()
            ep += 1
        ds.finalize()
    return root


def copy(src, dst):
    dst = Path(dst)
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def full_check(out_root, parts):
    """The P1 full check (spec §0): every frame decodes pixel-equal, data rows,
    spans, totals, __getitem__, and every statistic = aggregate_stats(kept)."""
    probs = []
    out = V.Source(out_root)
    ds = LeRobotDataset('smoke/check', root=out_root)
    fps = out.info['fps']
    if out.info['total_episodes'] != len(parts):
        probs.append('total_episodes')
    if out.info['total_frames'] != sum(int(s.episodes[e]['length']) for s, e in parts):
        probs.append('total_frames')
    if len(ds) != out.info['total_frames']:
        probs.append('len(ds)')
    frames = 0
    for new_ep, (s, ep) in enumerate(parts):
        ro, rs = out.episodes[new_ep], s.episodes[ep]
        n = int(ro['length'])
        if list(ro['tasks']) != list(rs['tasks']):
            probs.append((new_ep, 'tasks'))
        to_, ts_ = out.data_rows(new_ep), s.data_rows(ep)
        for col in ('observation.state', 'action', 'timestamp', 'frame_index'):
            if to_[col].to_pylist() != ts_[col].to_pylist():
                probs.append((new_ep, 'data', col))
        if to_['index'].to_pylist() != list(range(ro['dataset_from_index'], ro['dataset_to_index'])):
            probs.append((new_ep, 'index'))
        for k in out.video_keys:
            fo, to = ro[f'videos/{k}/from_timestamp'], ro[f'videos/{k}/to_timestamp']
            fs = rs[f'videos/{k}/from_timestamp']
            if abs((to - fo) - n / fps) > 0.5 / fps:
                probs.append((new_ep, k, 'span'))
            a = decode_video_frames(out.video_file(new_ep, k), [fo + i / fps for i in range(n)],
                                    tolerance_s=1e-4, backend='pyav')
            b = decode_video_frames(s.video_file(ep, k), [fs + i / fps for i in range(n)],
                                    tolerance_s=1e-4, backend='pyav')
            if not torch.equal(a, b):
                probs.append((new_ep, k, 'pixels'))
            frames += n
        for gi in (ro['dataset_from_index'], ro['dataset_to_index'] - 1):
            if int(ds[gi]['episode_index']) != new_ep:
                probs.append((new_ep, 'getitem'))
    agg = aggregate_stats([s.stats(e) for s, e in parts])
    st = json.loads((Path(out_root) / 'meta' / 'stats.json').read_text())
    n_stats = 0
    for f, d in agg.items():
        for k, v in d.items():
            n_stats += 1
            if f not in st or k not in st[f] or not np.allclose(np.array(st[f][k], dtype=float),
                                                                 np.array(v, dtype=float), rtol=1e-6, atol=1e-6):
                probs.append(('stats', f, k))
    return probs, frames, n_stats


def run(name, fn):
    t = time.time()
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a crashed scenario is a failed check
        FAILED.append(f'{name}: {type(e).__name__}: {e}'[:300])
        traceback.print_exc(file=sys.stderr)
    NUMBERS[f'{name}_s'] = round(time.time() - t, 2)


# ── the fixture ──────────────────────────────────────────────────────────────

BASE = ROOT / 'ds' / 'lena-schmidt' / 'omx_f_smoke'


def fixture():
    write(BASE, [(30, 31, 32, 30, 33, 31), (30, 34, 30, 31)])
    src = V.Source(BASE)
    NUMBERS['fixture_episodes'] = len(src.episodes)
    check('fixture_two_sessions', len(src.episodes) == 10
          and len(list((BASE / 'videos' / 'observation.images.scene').rglob('*.mp4'))) == 2
          and len(list((BASE / 'meta' / 'episodes').rglob('*.parquet'))) == 2)


def digest():
    d1, d2 = S.meta_digest(BASE), S.meta_digest(BASE)
    other = copy(BASE, ROOT / 'ds' / 'x' / 'omx_f_digest')
    d3 = S.meta_digest(other)
    f = next((other / 'data').rglob('*.parquet'))
    f.write_bytes(f.read_bytes())          # rewrite data: not part of the version id
    check('meta_digest_stable', d1 == d2 == d3 == S.meta_digest(other))
    (other / 'meta' / 'stats.json').write_text('{}')
    check('meta_digest_changes_with_meta', S.meta_digest(other) != d1)


def delete():
    target = copy(BASE, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_del')
    src = V.Source(BASE)
    left = E.delete_episodes_v3(str(target), [0, 5, 9])
    keep = [e for e in range(10) if e not in (0, 5, 9)]
    probs, frames, n_stats = full_check(target, [(src, e) for e in keep])
    NUMBERS['delete_frames_checked'] = frames
    NUMBERS['delete_stats_checked'] = n_stats
    check('delete_full_check', left == 7 and probs == [], str(probs[:3]))
    check('delete_record_files', S.read_record(target)['files'] == S.files_manifest(target))


def split():
    target = copy(BASE, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_split')
    new = ROOT / 'ds' / 'lena-schmidt' / 'omx_f_split_neu'
    src = V.Source(BASE)
    kept, moved = E.split_episodes_v3(str(target), [1, 2, 7], str(new), display_name='Neu')
    p1, _, _ = full_check(new, [(src, e) for e in (1, 2, 7)])
    p2, _, _ = full_check(target, [(src, e) for e in range(10) if e not in (1, 2, 7)])
    check('split_full_check', (kept, moved) == (7, 3) and p1 == [] and p2 == [], str((p1 + p2)[:3]))
    check('split_no_journal', not S.journal_path(target).exists())


def merge():
    other = write(ROOT / 'ds' / 'max-weber' / 'omx_f_smoke', [(32, 30, 31)], offset=40, seed=3)
    out = ROOT / 'ds' / 'lena-schmidt' / 'omx_f_merged'
    n = E.merge_datasets_v3([str(BASE), str(other)], str(out), display_name='Zusammen')
    a, b = V.Source(BASE), V.Source(other)
    probs, _, _ = full_check(out, [(a, e) for e in range(10)] + [(b, e) for e in range(3)])
    check('merge_full_check', n == 13 and probs == [], str(probs[:3]))


def verify_mutations():
    src = V.Source(BASE)
    parts = [(src, e) for e in (2, 0, 4, 1)]
    out = ROOT / 'ds' / 'v' / 'omx_f_ok'
    V.assemble(out, parts, 'v/omx_f_ok')
    check('verify_clean', V.verify(out, parts) == [])

    def mutated(name, fn, want):
        o = copy(out, ROOT / 'ds' / 'v' / f'omx_f_{name}')
        fn(o)
        problems = V.verify(o, parts)
        NUMBERS[f'verify_{name}_problems'] = len(problems)
        return check(f'verify_{name}', bool(problems) and (want is None or any(want(p) for p in problems)),
                     str(problems[:3]))

    def flip_video(o):
        v = next((o / 'videos' / 'observation.images.scene').rglob('*.mp4'))
        b = bytearray(v.read_bytes())
        b[len(b) // 2] ^= 0xFF
        v.write_bytes(bytes(b))

    def change_value(o):
        f = next((o / 'data').rglob('*.parquet'))
        t = pq.read_table(f)
        col = t['observation.state'].to_pylist()
        col[3] = [x + 0.001 for x in col[3]]
        t = t.set_column(t.schema.get_field_index('observation.state'), t.schema.field('observation.state'),
                         pa.array(col, type=t.schema.field('observation.state').type))
        pq.write_table(t, f)

    def swap_rows(o):
        f = next((o / 'data').rglob('*.parquet'))
        t = pq.read_table(f)
        e0 = t.filter(pc.equal(t['episode_index'], 0))
        e1 = t.filter(pc.equal(t['episode_index'], 1))
        rest = t.filter(pc.greater(t['episode_index'], 1))
        pq.write_table(pa.concat_tables([e1, e0, rest]), f)

    def change_stat(o):
        f = next((o / 'meta' / 'episodes').rglob('*.parquet'))
        t = pq.read_table(f)
        col = t['stats/action/mean'].to_pylist()
        col[2] = [x + 0.5 for x in col[2]]
        t = t.set_column(t.schema.get_field_index('stats/action/mean'), t.schema.field('stats/action/mean'),
                         pa.array(col, type=t.schema.field('stats/action/mean').type))
        pq.write_table(t, f)

    mutated('flipped_video_byte', flip_video, None)
    mutated('changed_value', change_value, lambda p: isinstance(p, tuple) and 'data' in p)
    mutated('swapped_rows', swap_rows, lambda p: p == ('data', 'row_order'))
    mutated('changed_statistic', change_stat, lambda p: isinstance(p, tuple) and 'stats' in p)


def clips():
    src = V.Source(BASE)
    r = src.episodes[3]
    for k in src.video_keys:
        out = ROOT / f'clip_{k}.mp4'
        n = V.write_clip(src.video_file(3, k), r[f'videos/{k}/from_timestamp'], r[f'videos/{k}/to_timestamp'],
                         int(r['length']), out)
        c = av.open(str(out))
        decoded = sum(1 for _ in c.decode(c.streams.video[0]))
        c.close()
        data = out.read_bytes()
        check(f'clip_{k}', n == decoded == int(r['length']) and data.find(b'moov') < data.find(b'mdat'),
              f'{n} {decoded} {r["length"]}')


def r2_refusals():
    for name, path in (('absolute_video_path', '/tmp/x/{video_key}.mp4'),
                       ('custom_video_path', 'vids/{video_key}/{chunk_index}/{file_index}.mp4')):
        d = copy(BASE, ROOT / 'ds' / 'r2' / f'omx_f_{name}')
        info = json.loads((d / 'meta' / 'info.json').read_text())
        info['video_path'] = path
        (d / 'meta' / 'info.json').write_text(json.dumps(info))
        try:
            V.Source(d)
            check(f'r2_{name}', False, 'not refused')
        except V.SurgeryError as e:
            check(f'r2_{name}', e.code == 'unsupported', e.code)


def unaligned():
    d = copy(BASE, ROOT / 'ds' / 'u' / 'omx_f_unaligned')
    for f in sorted(d.glob('videos/*/*/*.mp4')):
        cin = av.open(str(f))
        s = cin.streams.video[0]
        tmp = f.with_suffix('.re.mp4')
        out = av.open(str(tmp), 'w', options={'movflags': 'faststart'})
        o = out.add_stream('libx264', rate=FPS, options={'g': '30', 'bf': '2', 'sc_threshold': '0'})
        o.width, o.height, o.pix_fmt = s.codec_context.width, s.codec_context.height, 'yuv420p'
        o.time_base = s.time_base
        for fr in cin.decode(s):
            fr.pict_type = av.video.frame.PictureType.NONE
            for p in o.encode(fr):
                out.mux(p)
        for p in o.encode():
            out.mux(p)
        out.close()
        cin.close()
        tmp.replace(f)
    try:
        E.delete_episodes_v3(str(d), [0])
        check('unaligned_refused', False, 'not refused')
    except E.DataEditError as e:
        check('unaligned_refused', e.code == 'unaligned' and str(e) == E.surgery_message_de(e), e.code)


def stats_refusal():
    d = copy(BASE, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_noq')
    for f in (d / 'meta' / 'episodes').rglob('*.parquet'):
        t = pq.read_table(f)
        pq.write_table(t.drop([c for c in t.column_names if c.startswith('stats/') and c.rsplit('/', 1)[1][0] == 'q']), f)
    st = json.loads((d / 'meta' / 'stats.json').read_text())
    (d / 'meta' / 'stats.json').write_text(json.dumps({f: {k: v for k, v in x.items() if not k.startswith('q')}
                                                        for f, x in st.items()}))
    checks = dict((c, ok) for ok, c in V.check_compatible([V.Source(BASE), V.Source(d)]))
    check('stats_check_false', checks['stats'] is False and all(v for c, v in checks.items() if c != 'stats'))
    try:
        E.merge_datasets_v3([str(BASE), str(d)], str(ROOT / 'ds' / 'lena-schmidt' / 'omx_f_noq_merged'))
        check('stats_refused', False, 'not refused')
    except E.DataEditError as e:
        check('stats_refused', e.code == 'incompatible' and 'Statistiken' in str(e), str(e))


def roll_over():
    d = copy(BASE, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_roll')
    info = json.loads((d / 'meta' / 'info.json').read_text())
    info['data_files_size_in_mb'] = 0.001
    (d / 'meta' / 'info.json').write_text(json.dumps(info))
    ref = copy(d, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_roll_ref')
    E.delete_episodes_v3(str(d), [4])
    files = len(list((d / 'data').rglob('*.parquet')))
    NUMBERS['rollover_data_files'] = files
    probs, _, _ = full_check(d, [(V.Source(ref), e) for e in range(10) if e != 4])
    check('rollover', files > 1 and probs == [], f'{files} {probs[:3]}')


def keep_both():
    base = write(ROOT / 'ds' / 'kb' / 'omx_f_base', [(30, 31, 32, 30)], offset=60)
    local = copy(base, ROOT / 'ds' / 'lena-schmidt' / 'omx_f_kb')
    E.delete_episodes_v3(str(local), [1])                          # deleted HERE since the sync
    hub = copy(base, ROOT / 'ds' / 'kb' / 'omx_f_hubcopy')
    write(hub, [(31, 30, 32)], offset=70)                          # a session THERE (resumed)
    write(local, [(30, 33)], offset=80)                            # and one HERE (resumed on the engine output)
    tmp_keep = copy(hub, Path(f'{local}.tmp_keep'))
    tmp_base = copy(base, Path(f'{local}.tmp_base'))
    ids = lambda root: [V.episode_identity(s, e) for s in [V.Source(root)] for e in range(len(s.episodes))]  # noqa: E731
    want = ids(base)
    want = [want[0], want[2], want[3]] + ids(hub)[4:] + ids(local)[3:]
    res = E.union_episodes_v3(str(local), str(tmp_keep), str(tmp_base), hub_sha='a' * 40, hub_trees={'data': 'x'})
    got = ids(local)
    NUMBERS['keep_both_episodes'] = len(got)
    check('keep_both_three_way', sorted(got) == sorted(want) and len(got) == len(set(got)) == 8
          and res['three_way'], f'{len(got)} vs {len(want)}')
    check('keep_both_deleted_stays_deleted', ids(base)[1] not in got)
    check('keep_both_tmps_gone', not tmp_keep.exists() and not tmp_base.exists())
    rec = S.read_record(local)
    check('keep_both_record_is_the_hub_copys', rec['hub_sha'] == 'a' * 40 and rec['files'] == S.files_manifest(local))


def still_takes():
    a = write(ROOT / 'ds' / 'st' / 'omx_f_a', [(30,)], offset=90, seed=1, faults={90: 'still'})
    b = write(ROOT / 'ds' / 'st' / 'omx_f_b', [(30,)], offset=90, seed=2, faults={90: 'still'})
    sa, sb = V.Source(a), V.Source(b)
    rows_equal = sa.data_rows(0).select(V.payload_columns(sa.data_rows(0))).equals(
        sb.data_rows(0).select(V.payload_columns(sb.data_rows(0))))
    check('still_takes_apart', rows_equal and V.episode_identity(sa, 0) != V.episode_identity(sb, 0))
    check('still_takes_union_keeps_both', len(S.plan_keep_both([V.episode_identity(sa, 0)],
                                                                 [V.episode_identity(sb, 0)])) == 2)


def integrity():
    ok = copy(BASE, ROOT / 'ds' / 'i' / 'omx_f_ok')
    check('integrity_intact', len(V.integrity(ok).episodes) == 10)
    crashed = copy(BASE, ROOT / 'ds' / 'i' / 'omx_f_crashed')
    f = sorted((crashed / 'data').rglob('*.parquet'))[-1]
    f.write_bytes(f.read_bytes()[:-200])                                # no parquet footer
    sorted((crashed / 'meta' / 'episodes').rglob('*.parquet'))[-1].unlink()   # the buffered metadata never written
    try:
        V.integrity(crashed)
        check('integrity_crashed_refused', False, 'not refused')
    except V.SurgeryError as e:
        check('integrity_crashed_refused', e.code == 'broken', e.code)


def hints():
    d = write(ROOT / 'ds' / 'h' / 'omx_f_hints', [(150, 150, 150, 150, 150)], offset=100,
              faults={102: 'no_grasp'})
    src = V.Source(d)
    eps = []
    for e in range(len(src.episodes)):
        t = src.data_rows(e)
        eps.append((np.array(t['observation.state'].to_pylist()), np.array(t['action'].to_pylist())))
    names = src.info['features']['action']['names']
    flagged = [i for i, h in enumerate(HINTS.dataset_hints(eps, FPS, names)) if h]
    NUMBERS['hint_flagged'] = flagged
    check('hints_injected_fault', flagged == [2], str(flagged))


for name, fn in (('fixture', fixture), ('digest', digest), ('delete', delete), ('split', split), ('merge', merge),
                 ('verify', verify_mutations), ('clips', clips), ('r2', r2_refusals), ('unaligned', unaligned),
                 ('stats', stats_refusal), ('rollover', roll_over), ('keep_both', keep_both),
                 ('still', still_takes), ('integrity', integrity), ('hints', hints)):
    if name != 'fixture' and FAILED and FAILED[0].startswith('fixture'):
        break
    run(name, fn)

NUMBERS['secs'] = round(time.time() - T0, 1)
print(json.dumps({'ok': not FAILED, 'failed': FAILED, **NUMBERS}, ensure_ascii=False))
shutil.rmtree(ROOT, ignore_errors=True)
sys.exit(0 if not FAILED else 1)
