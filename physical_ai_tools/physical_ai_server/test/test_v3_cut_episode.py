"""Daten 2.0 — the ONE decode-order cutter (spec §D1 step 4b, R-23, P20).

Three fixtures, all built here with PyAV (no LeRobot): the recorder's GOP-2
stream (I-P-I-P, no B-frames), an ALIGNED B-frame stream (a forced IDR at every
episode start, packets reordered: dts != pts order) and an UNALIGNED continuous
re-encode (a keyframe every 30 frames from the file start, B-frames on, the
episode boundaries ignored). Every episode of every camera either becomes an
exact clip — exactly ``length`` packets, pts in ``[from, to)``, decoding to the
source's own frames — or is refused ``unaligned``; never a wrong count. A
pts-ordered cutter (stop at the first packet with pts >= end in decode order)
gets the unaligned fixture wrong, which proves the fixture has teeth. A
B-frame clip's first dts is negative after the shift; the mp4 muxer accepts it.
"""

from __future__ import annotations

import pytest

av = pytest.importorskip('av')
np = pytest.importorskip('numpy')

from physical_ai_server.data_processing import v3_surgery as V  # noqa: E402
from test_v3_surgery_layout import build_dataset  # noqa: E402

LENGTHS = (30, 30, 25, 35, 30, 33)


def _pts_ordered_clip(path, from_ts, to_ts, out_path):
    """The first prototype's cut (stop at the first packet with pts >= end in
    decode order, start at the first packet with pts >= start), muxed; returns
    the number of frames the clip DECODES to."""
    c = av.open(str(path))
    try:
        s = c.streams.video[0]
        tb = s.time_base
        a, b = round(from_ts / tb), round(to_ts / tb)
        c.seek(a, stream=s, backward=True, any_frame=False)
        out = av.open(str(out_path), 'w', format='mp4')
        o = out.add_stream_from_template(template=s, opaque=True)
        o.time_base = s.time_base
        for p in c.demux(s):
            if p.dts is None or p.pts < a:
                continue
            if p.pts >= b:
                break
            p.pts -= a
            p.dts -= a
            p.stream = o
            out.mux(p)
        out.close()
    finally:
        c.close()
    return len(_decoded(out_path))


def _decoded(path):
    c = av.open(str(path))
    try:
        s = c.streams.video[0]
        return [(f.pts, f.to_ndarray(format='rgb24')) for f in c.decode(s)]
    finally:
        c.close()


@pytest.fixture(scope='module')
def fixtures(tmp_path_factory):
    base = tmp_path_factory.mktemp('cut')
    return {
        'gop2': build_dataset(base / 'lena' / 'omx_f_gop2', lengths=LENGTHS),
        'bframe_aligned': build_dataset(base / 'lena' / 'omx_f_bf', lengths=LENGTHS, gop=30, bframes=2),
        'unaligned': _unaligned(build_dataset(base / 'lena' / 'omx_f_un', lengths=LENGTHS)),
    }


def _unaligned(root):
    """Re-encode every video file as ONE continuous x264 stream: a keyframe every
    30 frames from the FILE start, B-frames on, the same pts (make_unaligned.py)."""
    for f in sorted(root.glob('videos/*/*/*.mp4')):
        cin = av.open(str(f))
        s = cin.streams.video[0]
        tmp = f.with_suffix('.re.mp4')
        out = av.open(str(tmp), 'w', options={'movflags': 'faststart'})
        o = out.add_stream('libx264', rate=30, options={'g': '30', 'bf': '2', 'sc_threshold': '0'})
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
    return root


def _check(root, tmp_path):
    src = V.Source(root)
    exact = refused = 0
    for ep, row in enumerate(src.episodes):
        length = int(row['length'])
        for k in src.video_keys:
            path = src.video_file(ep, k)
            ft, tt = row[f'videos/{k}/from_timestamp'], row[f'videos/{k}/to_timestamp']
            clip = tmp_path / f'{root.name}_{ep}_{k}.mp4'
            try:
                n = V.write_clip(path, ft, tt, length, clip)
            except V.SurgeryError as e:
                assert e.code == 'unaligned'
                refused += 1
                continue
            assert n == length
            got = _decoded(clip)
            assert len(got) == length, (root.name, ep, k)
            frames = _decoded(path)
            tb = av.open(str(path)).streams.video[0].time_base
            a, b = round(ft / tb), round(tt / tb)
            want = [img for pts, img in sorted(frames, key=lambda x: x[0]) if a <= pts < b]
            assert len(want) == length
            for (_, g), w in zip(sorted(got, key=lambda x: x[0]), want):
                assert np.array_equal(g, w), (root.name, ep, k)
            data = clip.read_bytes()
            assert data.find(b'moov') < data.find(b'mdat'), 'faststart: moov before mdat'
            exact += 1
    return exact, refused


def test_the_recorder_gop2_stream_cuts_exactly_everywhere(fixtures, tmp_path):
    exact, refused = _check(fixtures['gop2'], tmp_path)
    assert (exact, refused) == (len(LENGTHS) * 2, 0)


def test_an_aligned_bframe_stream_cuts_exactly_everywhere(fixtures, tmp_path):
    root = fixtures['bframe_aligned']
    # the fixture really is reordered (else this case proves nothing)
    c = av.open(str(next(root.glob('videos/*/*/*.mp4'))))
    pk = [p for p in c.demux(c.streams.video[0]) if p.pts is not None]
    c.close()
    assert any(p.dts != p.pts for p in pk[1:]) and [p.pts for p in pk] != sorted(p.pts for p in pk)
    exact, refused = _check(root, tmp_path)
    assert (exact, refused) == (len(LENGTHS) * 2, 0)


def test_an_unaligned_stream_is_exact_or_refused_never_a_wrong_count(fixtures, tmp_path):
    root = fixtures['unaligned']
    exact, refused = _check(root, tmp_path)
    assert exact >= 2 and refused >= 2, (exact, refused)
    # teeth: the pts-ordered cutter makes at least one WRONG clip of these episodes
    src = V.Source(root)
    wrong = 0
    for ep, row in enumerate(src.episodes):
        for k in src.video_keys:
            n = _pts_ordered_clip(src.video_file(ep, k), row[f'videos/{k}/from_timestamp'],
                                  row[f'videos/{k}/to_timestamp'], tmp_path / f'naive_{ep}_{k}.mp4')
            wrong += n != int(row['length'])
    assert wrong >= 1


def test_the_engine_refuses_the_unaligned_episodes_as_unaligned(fixtures):
    src = V.Source(fixtures['unaligned'])
    with pytest.raises(V.SurgeryError) as e:
        for ep in range(len(src.episodes)):
            V.episode_identity(src, ep)
    assert e.value.code == 'unaligned'


def test_a_negative_first_dts_muxes(fixtures, tmp_path):
    src = V.Source(fixtures['bframe_aligned'])
    row = src.episodes[1]
    key = src.video_keys[0]
    c = av.open(str(src.video_file(1, key)))
    try:
        s = c.streams.video[0]
        pkts, a, _ = V.cut_episode(c, s, row[f'videos/{key}/from_timestamp'],
                                   row[f'videos/{key}/to_timestamp'], int(row['length']))
        assert pkts[0].dts - a < 0, 'the fixture must give a negative first dts after the shift'
    finally:
        c.close()
    clip = tmp_path / 'neg.mp4'
    assert V.write_clip(src.video_file(1, key), row[f'videos/{key}/from_timestamp'],
                        row[f'videos/{key}/to_timestamp'], int(row['length']), clip) == int(row['length'])
    assert len(_decoded(clip)) == int(row['length'])
