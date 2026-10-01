"""`/edubotics/signal_status` — the pure half (Aufnahme 2.0, spec §2.2/§2.4).

The node publishes FACTS, never verdicts: per configured source the measured
callback rate over ~2 s and the age of its last message, plus the free bytes on
the dataset filesystem with both floors. The React page decides what is slow or
stalled (it knows the fps being recorded). This module is stdlib-only so the
schema, the rate maths and the German disk sentences are testable without ROS.
"""

from __future__ import annotations

import json
import os
import types

import pytest

from physical_ai_server import signal_status as ss


# ── constants ────────────────────────────────────────────────────────────────

def test_contract_constants():
    assert ss.TOPIC == '/edubotics/signal_status'
    assert ss.SCHEMA_VERSION == 1
    assert ss.PUBLISH_PERIOD_S == 1.0
    assert ss.RATE_WINDOW_S == 2.0
    assert ss.DISK_START_FLOOR_BYTES == 3_000_000_000
    assert ss.DISK_CRITICAL_FLOOR_BYTES == 1_000_000_000
    assert ss.DISK_CHECK_INTERVAL_S == 1.0
    assert ss.SOURCE_STOPPED_S == 2.0
    assert ss.SOURCE_GAP_S == 0.25
    assert ss.INGEST_ALIVE_S == 1.0


def test_module_is_stdlib_only():
    import ast
    tree = ast.parse(open(ss.__file__, encoding='utf-8').read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    assert imported <= {'__future__', 'collections', 'json', 'os', 'shutil', 'typing',
                        'pathlib', 'math'}, imported


# ── disk ─────────────────────────────────────────────────────────────────────

def test_disk_free_bytes_walks_to_the_nearest_existing_ancestor(tmp_path, monkeypatch):
    seen = []

    def _usage(path):
        seen.append(str(path))
        return types.SimpleNamespace(total=100, used=40, free=60)

    monkeypatch.setattr(ss.shutil, 'disk_usage', _usage)
    missing = tmp_path / 'not' / 'yet' / 'there'
    assert ss.disk_free_bytes(missing) == 60
    assert seen == [str(tmp_path)]


def test_disk_free_bytes_real_filesystem(tmp_path):
    free = ss.disk_free_bytes(tmp_path)
    assert isinstance(free, int) and free > 0


def test_disk_free_bytes_oserror_is_none(tmp_path, monkeypatch):
    def _boom(path):
        raise OSError('statvfs failed')

    monkeypatch.setattr(ss.shutil, 'disk_usage', _boom)
    assert ss.disk_free_bytes(tmp_path) is None


def test_format_gb_de():
    assert ss.format_gb_de(3_000_000_000) == '3,0 GB'
    assert ss.format_gb_de(52_345_678_901) == '52,3 GB'
    assert ss.format_gb_de(900_000_000) == '0,9 GB'


def test_disk_start_refusal_de():
    assert ss.disk_start_refusal_de(2_400_000_000) == (
        'Nur noch 2,4 GB frei. Zum Aufnehmen sind mindestens 3,0 GB nötig. '
        'Lösche alte Datensätze im Tab Daten.')


def test_disk_critical_stop_de_is_phase_neutral():
    text = ss.disk_critical_stop_de(900_000_000)
    assert text == (
        'Der Speicher ist fast voll (nur noch 0,9 GB frei), deshalb wurde die '
        'Aufnahme beendet. Bereits gespeicherte Episoden bleiben erhalten. '
        'Lösche alte Datensätze im Tab Daten.')
    # Nothing may have been saved (warm-up, reset, a run < 1 s): the sentence
    # must not claim that this episode was saved.
    assert 'Episode wurde gespeichert' not in text


# ── rate tracker ─────────────────────────────────────────────────────────────

def test_first_update_is_not_measurable():
    rt = ss.RateTracker()
    assert rt.update(100.0, {'camera:scene': 10}) == {'camera:scene': None}


def test_rate_over_one_second():
    rt = ss.RateTracker()
    rt.update(100.0, {'camera:scene': 0, 'follower:follower': 0})
    out = rt.update(101.0, {'camera:scene': 30, 'follower:follower': 99})
    assert out == {'camera:scene': 30.0, 'follower:follower': 99.0}


def test_rate_needs_at_least_0_9_s():
    rt = ss.RateTracker()
    rt.update(100.0, {'a': 0})
    assert rt.update(100.5, {'a': 15}) == {'a': None}


def test_rate_uses_the_oldest_snapshot_inside_the_window():
    rt = ss.RateTracker()
    rt.update(100.0, {'a': 0})
    rt.update(101.0, {'a': 10})
    # Oldest snapshot within window_s + 0.5 = 2.5 s is t=100: 60 counts / 2 s.
    assert rt.update(102.0, {'a': 60}) == {'a': 30.0}
    # At t=103 the t=100 snapshot is 3 s old: reference is t=101 (Δ 2 s).
    assert rt.update(103.0, {'a': 70}) == {'a': 30.0}


def test_rate_is_rounded_to_one_decimal():
    rt = ss.RateTracker()
    rt.update(100.0, {'a': 0})
    assert rt.update(103.0 - 1.0, {'a': 23}) == {'a': 11.5}
    rt = ss.RateTracker()
    rt.update(0.0, {'a': 0})
    assert rt.update(3.0 - 1.0, {'a': 59}) == {'a': 29.5}


def test_silent_source_is_zero_not_none():
    rt = ss.RateTracker()
    rt.update(100.0, {'leader:leader': 0})
    assert rt.update(101.0, {'leader:leader': 0}) == {'leader:leader': 0.0}


def test_new_id_is_none_until_it_has_history():
    rt = ss.RateTracker()
    rt.update(100.0, {'a': 0})
    out = rt.update(101.0, {'a': 30, 'b': 5})
    assert out == {'a': 30.0, 'b': None}
    assert rt.update(102.0, {'a': 60, 'b': 35}) == {'a': 30.0, 'b': 30.0}


def test_count_going_down_drops_that_ids_history():
    rt = ss.RateTracker()
    rt.update(100.0, {'a': 100, 'b': 0})
    out = rt.update(101.0, {'a': 5, 'b': 30})   # a's counter restarted
    assert out == {'a': None, 'b': 30.0}
    assert rt.update(102.0, {'a': 35, 'b': 60}) == {'a': 30.0, 'b': 30.0}


def test_history_is_bounded():
    rt = ss.RateTracker()
    for i in range(100):
        rt.update(100.0 + i * 0.1, {'a': i})
    assert len(rt._snapshots) <= 16


# ── sources + payload ────────────────────────────────────────────────────────

_COUNTERS = [
    {'id': 'camera:gripper', 'kind': 'camera', 'name': 'gripper',
     'topic': '/gripper/image_raw', 'count': 300, 'last_mono': 99.97},
    {'id': 'camera:scene', 'kind': 'camera', 'name': 'scene',
     'topic': '/scene/image_raw', 'count': 110, 'last_mono': 99.92},
    {'id': 'follower:follower', 'kind': 'follower', 'name': 'follower',
     'topic': '/joint_states', 'count': 990, 'last_mono': 99.99},
    {'id': 'leader:leader', 'kind': 'leader', 'name': 'leader',
     'topic': '/leader/joint_trajectory', 'count': 0, 'last_mono': None},
]


def test_source_entries_from_counters():
    rates = {'camera:gripper': 29.8, 'camera:scene': 11.2,
             'follower:follower': 98.9, 'leader:leader': None}
    entries = ss.source_entries(_COUNTERS, rates, now=100.0, include_leader=True)
    assert entries == [
        {'kind': 'camera', 'name': 'gripper', 'topic': '/gripper/image_raw',
         'hz': 29.8, 'age_s': 0.03},
        {'kind': 'camera', 'name': 'scene', 'topic': '/scene/image_raw',
         'hz': 11.2, 'age_s': 0.08},
        {'kind': 'follower', 'name': 'follower', 'topic': '/joint_states',
         'hz': 98.9, 'age_s': 0.01},
        {'kind': 'leader', 'name': 'leader', 'topic': '/leader/joint_trajectory',
         'hz': None, 'age_s': None},
    ]


def test_source_entries_drop_the_leader_when_the_profile_has_none():
    entries = ss.source_entries(_COUNTERS, {}, now=100.0, include_leader=False)
    assert [e['kind'] for e in entries] == ['camera', 'camera', 'follower']
    assert all(e['hz'] is None for e in entries)


def test_build_payload_is_exactly_schema_v1():
    sources = ss.source_entries(
        _COUNTERS, {'camera:gripper': 29.8}, now=100.0, include_leader=True)
    payload = ss.build_payload(seq=42, uptime_s=12.04, recording=False,
                               sources=sources, free_bytes=52_345_678_901)
    assert list(payload) == ['v', 'seq', 'uptime_s', 'recording', 'sources', 'disk']
    assert payload['v'] == 1
    assert payload['seq'] == 42
    assert payload['uptime_s'] == 12.0
    assert payload['recording'] is False
    assert payload['disk'] == {'free_bytes': 52_345_678_901,
                               'start_floor_bytes': 3_000_000_000,
                               'critical_floor_bytes': 1_000_000_000}
    for src in payload['sources']:
        assert list(src) == ['kind', 'name', 'topic', 'hz', 'age_s']


def test_build_payload_is_v1_order_plus_an_optional_trailing_ingest():
    sources = ss.source_entries(_COUNTERS, {}, now=100.0, include_leader=True)
    payload = ss.build_payload(seq=1, uptime_s=1.0, recording=True, sources=sources,
                               free_bytes=None, ingest={'alive': True, 'age_s': 0.01})
    assert list(payload) == ['v', 'seq', 'uptime_s', 'recording', 'sources', 'disk', 'ingest']
    assert payload['v'] == 1
    assert payload['ingest'] == {'alive': True, 'age_s': 0.01}
    # without the caller's liveness the payload is exactly v1
    plain = ss.build_payload(seq=1, uptime_s=1.0, recording=True, sources=sources,
                             free_bytes=None)
    assert list(plain) == ['v', 'seq', 'uptime_s', 'recording', 'sources', 'disk']
    assert json.loads(ss.encode_payload(payload))['ingest']['alive'] is True


@pytest.mark.parametrize('last,now,expected', [
    (None, 5.0, {'alive': False, 'age_s': None}),
    (99.99, 100.0, {'alive': True, 'age_s': 0.01}),
    (99.0, 100.0, {'alive': False, 'age_s': 1.0}),
    (98.5, 100.0, {'alive': False, 'age_s': 1.5}),
    (101.0, 100.0, {'alive': True, 'age_s': 0.0}),
])
def test_ingest_status(last, now, expected):
    assert ss.ingest_status(last, now) == expected


def test_build_payload_disk_null_when_statvfs_failed():
    payload = ss.build_payload(seq=1, uptime_s=0.0, recording=True, sources=[],
                               free_bytes=None)
    assert payload['disk'] is None
    assert payload['recording'] is True


def test_encode_payload_is_compact_utf8_json():
    payload = ss.build_payload(seq=1, uptime_s=1.0, recording=False, sources=[
        {'kind': 'camera', 'name': 'Würfel', 'topic': '/w', 'hz': 1.0, 'age_s': 0.5}],
        free_bytes=None)
    text = ss.encode_payload(payload)
    assert 'Würfel' in text           # ensure_ascii=False
    assert ', ' not in text and ': ' not in text
    assert json.loads(text) == payload


@pytest.mark.parametrize('age,expected', [(0.004, 0.0), (1.234, 1.23), (2.999, 3.0)])
def test_age_is_rounded_to_two_decimals(age, expected):
    counters = [{'id': 'camera:scene', 'kind': 'camera', 'name': 'scene',
                 'topic': '/scene/image_raw', 'count': 1, 'last_mono': 100.0 - age}]
    entries = ss.source_entries(counters, {}, now=100.0, include_leader=False)
    assert entries[0]['age_s'] == expected


def test_str_path_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.shutil, 'disk_usage',
                        lambda path: types.SimpleNamespace(free=7))
    assert ss.disk_free_bytes(os.fspath(tmp_path)) == 7
