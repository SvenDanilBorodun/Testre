"""Scene-camera provenance (D58): the pure module, the eighth merge check and the
library field.

``scene_provenance`` (stdlib only): the reader confines and bounds
(``dataset_profile``: ``legacy`` without a stamp, ``tower_v1`` with a valid one,
None for anything it cannot judge), the writer keeps the key order and writes
its temp file in the dataset ROOT (never under ``meta/``), the resume rule
refuses only two known, different profiles, and a provider's answer is checked.

With pyarrow/PyAV (CI's python-tests and the image): ``v3_surgery.check_compatible``
judges ``scene_camera`` as its eighth check, only with two or more sources; two
stampless sources pass it, so ``.github/scripts/daten_smoke.py``'s
``stats_check_false`` predicate holds unchanged; and every local library entry
names its dataset's profile.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from physical_ai_server import scene_provenance as P

STAMP = 'meta/edubotics_scene_camera.json'
TOWER = {'v': 1, 'scene_camera_profile': 'tower_v1', 'profile_id': '3efca10f3a05', 'platform': 'windows'}


def _dataset(root):
    (root / 'meta').mkdir(parents=True)
    (root / 'meta' / 'info.json').write_text('{"codebase_version": "v3.0"}', encoding='utf-8')
    return root


def _stamp(root, value):
    data = value if isinstance(value, bytes) else json.dumps(value).encode('utf-8')
    (root / STAMP).write_bytes(data)
    return data


def _tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


# ── the contract values ───────────────────────────────────────────────────────

def test_the_contract_values():
    assert P.STAMP_RELPATH == STAMP
    assert P.PROFILES == ('legacy', 'tower_v1')
    assert P.PROFILE_TOPIC == '/edubotics/scene_camera_profile'
    assert P.LEGACY_PROVIDER() == {'profile': 'legacy', 'profile_id': None, 'platform': None}


def test_the_module_is_stdlib_only_and_imports_no_package():
    tree = ast.parse(Path(P.__file__).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
            for name in names:
                assert name.split('.')[0] in ('__future__', 'json', 'os', 'pathlib', 're', 'stat'), name


# ── the reader ────────────────────────────────────────────────────────────────

def test_no_stamp_is_legacy(tmp_path):
    assert P.dataset_profile(_dataset(tmp_path / 'ds')) == 'legacy'
    assert P.read_stamp(tmp_path / 'ds') is None


def test_a_valid_stamp_is_tower(tmp_path):
    root = _dataset(tmp_path / 'ds')
    _stamp(root, TOWER)
    assert P.dataset_profile(root) == 'tower_v1'
    assert P.read_stamp(root) == TOWER
    _stamp(root, {**TOWER, 'profile_id': None, 'platform': None})
    assert P.dataset_profile(root) == 'tower_v1'


@pytest.mark.parametrize('what,value', [
    ('malformed json', b'{"v": 1, "scene_camera_profile": '),
    ('not utf-8', b'\xff\xfe{}'),
    ('not an object', b'["tower_v1"]'),
    ('unknown v', {**TOWER, 'v': 2}),
    ('v as text', {**TOWER, 'v': '1'}),
    ('v as bool', {**TOWER, 'v': True}),
    ('unknown value', {**TOWER, 'scene_camera_profile': 'tower_v2'}),
    ('profile as a list', {**TOWER, 'scene_camera_profile': ['tower_v1']}),
    ('a bad profile id', {**TOWER, 'profile_id': '3EFCA10F3A05'}),
    ('an unknown platform', {**TOWER, 'platform': 'mac'}),
    ('a missing key', {k: v for k, v in TOWER.items() if k != 'platform'}),
    ('an extra key', {**TOWER, 'extra': 1}),
    ('nested too deep', b'[' * 2000 + b']' * 2000),
])
def test_an_unreadable_stamp_is_unknown(tmp_path, what, value):
    root = _dataset(tmp_path / 'ds')
    _stamp(root, value)
    assert P.dataset_profile(root) is None, what


def test_a_stamp_above_the_bound_is_unknown(tmp_path):
    root = _dataset(tmp_path / 'ds')
    _stamp(root, json.dumps({**TOWER}).encode('utf-8') + b' ' * 4096)
    assert P.dataset_profile(root) is None
    _stamp(root, json.dumps(TOWER).encode('utf-8').ljust(4096, b' '))   # exactly at the bound
    assert P.dataset_profile(root) == 'tower_v1'


def test_a_symlinked_stamp_is_unknown(tmp_path):
    root = _dataset(tmp_path / 'ds')
    target = tmp_path / 'elsewhere.json'
    target.write_text(json.dumps(TOWER), encoding='utf-8')
    (root / STAMP).symlink_to(target)
    assert P.dataset_profile(root) is None


def test_a_stamp_that_is_a_directory_is_unknown(tmp_path):
    root = _dataset(tmp_path / 'ds')
    (root / STAMP).mkdir()
    assert P.dataset_profile(root) is None


def test_a_meta_folder_outside_the_dataset_is_unknown(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'edubotics_scene_camera.json').write_text(json.dumps(TOWER), encoding='utf-8')
    root = tmp_path / 'ds'
    root.mkdir()
    (root / 'meta').symlink_to(outside, target_is_directory=True)
    assert P.dataset_profile(root) is None
    with pytest.raises(ValueError):
        P.write_stamp(root, None, None)
    assert sorted(p.name for p in outside.iterdir()) == ['edubotics_scene_camera.json']


# ── the writer ────────────────────────────────────────────────────────────────

def test_the_writer_keeps_the_key_order_compact(tmp_path):
    root = _dataset(tmp_path / 'ds')
    P.write_stamp(root, '3efca10f3a05', 'windows')
    data = (root / STAMP).read_bytes()
    assert data == b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":"3efca10f3a05","platform":"windows"}'
    assert list(json.loads(data)) == ['v', 'scene_camera_profile', 'profile_id', 'platform']
    assert len(data) <= 512
    assert P.dataset_profile(root) == 'tower_v1'
    P.write_stamp(root, None, None)
    assert (root / STAMP).read_bytes() == \
        b'{"v":1,"scene_camera_profile":"tower_v1","profile_id":null,"platform":null}'
    assert not (root / '.edubotics_scene_camera.json.tmp').exists()


@pytest.mark.parametrize('profile_id,platform', [('3EFCA10F3A05', 'pi'), ('x', None), (None, 'mac'), (12, None)])
def test_the_writer_refuses_values_outside_the_schema(tmp_path, profile_id, platform):
    root = _dataset(tmp_path / 'ds')
    before = _tree(root)
    with pytest.raises(ValueError):
        P.write_stamp(root, profile_id, platform)
    assert _tree(root) == before


def test_the_temp_file_lives_in_the_root_and_is_fsynced(tmp_path, monkeypatch):
    root = _dataset(tmp_path / 'ds')
    seen = {}
    real_replace, real_fsync = os.replace, os.fsync
    synced = []

    def replace(src, dst):
        seen['src'], seen['dst'] = Path(src), Path(dst)
        seen['meta_files'] = sorted(p.name for p in (root / 'meta').iterdir())
        seen['tmp_bytes'] = Path(src).read_bytes()
        seen['synced_before'] = len(synced)
        return real_replace(src, dst)

    def fsync(fd):
        synced.append(fd)
        return real_fsync(fd)
    monkeypatch.setattr(P.os, 'replace', replace)
    monkeypatch.setattr(P.os, 'fsync', fsync)
    P.write_stamp(root, '8b7f72cb250c', 'pi')
    assert seen['src'] == root / '.edubotics_scene_camera.json.tmp'
    assert seen['dst'] == root / STAMP
    assert seen['meta_files'] == ['info.json'], 'nothing under meta/ before the rename'
    assert json.loads(seen['tmp_bytes'])['profile_id'] == '8b7f72cb250c'
    assert seen['synced_before'] == 1, 'the file is fsynced before the rename'
    assert len(synced) == 2, 'and the directory after it'


def test_a_crash_before_the_rename_leaves_nothing_the_upload_or_the_digest_sees(tmp_path, monkeypatch):
    root = _dataset(tmp_path / 'ds')
    before = _tree(root)

    def crash(src, dst):
        # what a power cut at this instant leaves: the temp file in the ROOT only
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()) == \
            ['.edubotics_scene_camera.json.tmp', 'meta/info.json']
        raise OSError('power cut')
    monkeypatch.setattr(P.os, 'replace', crash)
    with pytest.raises(OSError):
        P.write_stamp(root, None, 'windows')
    assert _tree(root) == before, 'no partial stamp, the temp file removed'


# ── the resume rule and the provider ──────────────────────────────────────────

@pytest.mark.parametrize('dataset,live,refused', [
    ('legacy', 'tower_v1', True), ('tower_v1', 'legacy', True),
    ('legacy', 'legacy', False), ('tower_v1', 'tower_v1', False),
    (None, 'tower_v1', False), (None, 'legacy', False), ('legacy', None, False), ('tower_v1', None, False),
    (None, None, False), ('tower_v2', 'legacy', False),
])
def test_resume_is_refused_only_on_proof(dataset, live, refused):
    assert P.resume_refused(dataset, live) is refused


def _raises():
    raise RuntimeError('no statement yet')


@pytest.mark.parametrize('provider,expected', [
    (None, None),
    (_raises, None),
    (lambda: None, None),
    (lambda: 'tower_v1', None),
    (lambda: {'profile': 'tower_v2', 'profile_id': None, 'platform': None}, None),
    (lambda: {'profile': 'tower_v1', 'profile_id': 'NOT-HEX', 'platform': None}, None),
    (lambda: {'profile': 'tower_v1', 'profile_id': None, 'platform': 'mac'}, None),
    (lambda: {'profile': None, 'profile_id': None, 'platform': None}, None),
    (lambda: {'profile': 'legacy', 'profile_id': None, 'platform': None}, 'legacy'),
    (lambda: {'profile': 'tower_v1', 'profile_id': '3efca10f3a05', 'platform': 'windows'}, 'tower_v1'),
])
def test_a_provider_answer_is_checked(provider, expected):
    answer = P.live_profile(provider)
    assert answer['profile'] == expected
    assert set(answer) == {'profile', 'profile_id', 'platform'}
    if expected is None:
        assert answer == {'profile': None, 'profile_id': None, 'platform': None}


def test_the_legacy_provider_reads_legacy():
    assert P.live_profile(P.LEGACY_PROVIDER) == {'profile': 'legacy', 'profile_id': None, 'platform': None}


# ── the copies (Daten's outputs) ─────────────────────────────────────────────

def test_one_source_is_copied_byte_for_byte_whatever_it_says(tmp_path):
    src, dst = _dataset(tmp_path / 'src'), _dataset(tmp_path / 'dst')
    later = b'{"v": 2, "scene_camera_profile": "tower_v2", "lens": "wide"}\n'
    _stamp(src, later)
    assert P.copy_stamp_bytes(src, dst) is True
    assert (dst / STAMP).read_bytes() == later
    assert P.copy_stamp([src], _dataset(tmp_path / 'dst2')) is True
    assert (tmp_path / 'dst2' / STAMP).read_bytes() == later


def test_a_stampless_source_copies_nothing(tmp_path):
    src, dst = _dataset(tmp_path / 'src'), _dataset(tmp_path / 'dst')
    before = _tree(dst)
    assert P.copy_stamp_bytes(src, dst) is False
    assert P.copy_stamp([src], dst) is False
    assert _tree(dst) == before


def test_a_symlinked_source_stamp_is_never_followed(tmp_path):
    src, dst = _dataset(tmp_path / 'src'), _dataset(tmp_path / 'dst')
    secret = tmp_path / 'secret.txt'
    secret.write_text('not yours', encoding='utf-8')
    (src / STAMP).symlink_to(secret)
    with pytest.raises(ValueError):
        P.copy_stamp_bytes(src, dst)
    assert not (dst / STAMP).exists()


def test_several_sources_carry_their_common_stamp(tmp_path):
    a, b, c = (_dataset(tmp_path / n) for n in ('a', 'b', 'c'))
    _stamp(a, TOWER)
    _stamp(b, {**TOWER, 'profile_id': '8b7f72cb250c', 'platform': 'pi'})
    _stamp(c, TOWER)
    out = _dataset(tmp_path / 'out')
    assert P.copy_stamp([a, c], out) is True
    assert P.read_stamp(out) == TOWER
    out2 = _dataset(tmp_path / 'out2')
    assert P.copy_stamp([a, b], out2) is True
    assert P.read_stamp(out2) == {**TOWER, 'profile_id': None, 'platform': None}


def test_several_legacy_sources_carry_no_stamp(tmp_path):
    a, b, out = (_dataset(tmp_path / n) for n in ('a', 'b', 'out'))
    assert P.copy_stamp([a, b], out) is False
    assert not (out / STAMP).exists()


@pytest.mark.parametrize('second', [None, b'{broken'])
def test_several_sources_that_differ_are_refused(tmp_path, second):
    a, b, out = (_dataset(tmp_path / n) for n in ('a', 'b', 'out'))
    _stamp(a, TOWER)
    if second is not None:
        _stamp(b, second)
    with pytest.raises(ValueError):
        P.copy_stamp([a, b], out)
    assert not (out / STAMP).exists()


# ── the eighth merge check (needs pyarrow/PyAV) ──────────────────────────────

@pytest.fixture()
def engine():
    pytest.importorskip('av')
    pytest.importorskip('pyarrow')
    pytest.importorskip('numpy')
    from physical_ai_server.data_processing import v3_surgery as V
    from test_v3_surgery_layout import build_dataset
    return V, build_dataset


def _checks(V, sources):
    return dict((cid, ok) for ok, cid in V.check_compatible([V.Source(s) for s in sources]))


def test_check_compatible_has_eight_checks_ending_in_scene_camera(engine, tmp_path):
    V, build = engine
    a = build(tmp_path / 'lena' / 'omx_f_a', lengths=(20,))
    b = build(tmp_path / 'lena' / 'omx_f_b', lengths=(20,))
    pairs = V.check_compatible([V.Source(a), V.Source(b)])
    assert [cid for _, cid in pairs] == list(V.MERGE_CHECKS)
    assert V.MERGE_CHECKS == ('version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats', 'scene_camera')
    assert all(ok for ok, _ in pairs), 'two stampless sources pass every check'


def test_one_source_is_never_refused_for_its_stamp(engine, tmp_path):
    V, build = engine
    a = build(tmp_path / 'lena' / 'omx_f_a', lengths=(20,))
    for value in ({**TOWER, 'v': 2}, b'{broken', TOWER):
        _stamp(a, value)
        assert _checks(V, [a])['scene_camera'] is True, value


@pytest.mark.parametrize('stamp_a,stamp_b,ok', [
    (None, None, True),
    (TOWER, TOWER, True),
    (TOWER, {**TOWER, 'profile_id': '8b7f72cb250c', 'platform': 'pi'}, True),   # one view, Windows and Pi
    (TOWER, None, False),
    (None, TOWER, False),
    (TOWER, b'{broken', False),
    (None, b'{broken', False),
    (None, {**TOWER, 'v': 2}, False),
])
def test_two_sources_must_share_one_readable_profile(engine, tmp_path, stamp_a, stamp_b, ok):
    V, build = engine
    a = build(tmp_path / 'lena' / 'omx_f_a', lengths=(20,))
    b = build(tmp_path / 'lena' / 'omx_f_b', lengths=(20,))
    for root, value in ((a, stamp_a), (b, stamp_b)):
        if value is not None:
            _stamp(root, value)
    checks = _checks(V, [a, b])
    assert checks['scene_camera'] is ok
    assert all(v for c, v in checks.items() if c != 'scene_camera'), 'only the eighth check judges the stamp'


def test_assemble_refuses_two_different_scene_cameras_before_lerobot(engine, tmp_path, monkeypatch):
    import sys
    V, build = engine
    a = build(tmp_path / 'lena' / 'omx_f_a', lengths=(20,))
    b = build(tmp_path / 'lena' / 'omx_f_b', lengths=(20,))
    _stamp(a, TOWER)
    monkeypatch.setitem(sys.modules, 'lerobot', None)
    with pytest.raises(V.SurgeryError) as e:
        V.assemble(tmp_path / 'out', [(V.Source(a), 0), (V.Source(b), 0)], 'lena/out')
    assert (e.value.code, e.value.detail) == ('incompatible', 'scene_camera')
    assert not (tmp_path / 'out').exists()


def test_the_daten_smoke_stats_predicate_is_unchanged_by_the_eighth_check(engine, tmp_path):
    """`.github/scripts/daten_smoke.py::stats_refusal` reads check_compatible into a
    dict (line 374) and needs every check but `stats` to be true: two stampless
    sources read legacy == legacy, so no CI change is needed (§2 correction 44)."""
    V, build = engine
    base = build(tmp_path / 'lena' / 'omx_f_base', lengths=(20,))
    noq = build(tmp_path / 'lena' / 'omx_f_noq', lengths=(20,), stat_names=('min', 'max', 'mean', 'std', 'count'))
    checks = dict((c, ok) for ok, c in V.check_compatible([V.Source(base), V.Source(noq)]))
    assert 'scene_camera' in checks
    assert checks['stats'] is False and all(v for c, v in checks.items() if c != 'stats')


# ── the library field ─────────────────────────────────────────────────────────

def test_every_local_entry_names_its_scene_camera_profile(engine, tmp_path):
    V, build = engine
    from physical_ai_server.daten import library as L
    ns = tmp_path / 'lena-schmidt'
    build(ns / 'omx_f_legacy', lengths=(20,))
    _stamp(build(ns / 'omx_f_tower', lengths=(20,)), TOWER)
    _stamp(build(ns / 'omx_f_unread', lengths=(20,)), b'{broken')
    (ns / 'omx_f_noinfo' / 'meta').mkdir(parents=True)
    lib = L.Library(root=tmp_path, robot_type='omx_f', clip_tmp_dir=tmp_path / 'clips')
    profiles = {e['name']: e['scene_camera_profile'] for e in lib.scan(['lena-schmidt'])}
    assert profiles == {'omx_f_legacy': 'legacy', 'omx_f_tower': 'tower_v1', 'omx_f_unread': None,
                        'omx_f_noinfo': 'legacy'}
