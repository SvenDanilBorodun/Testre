"""The dataset README is LeRobot's own card (spec §7.3, owner decisions O5 + D6).

Two halves:

* deps-free: the call into ``create_lerobot_dataset_card`` (a stand-in module) —
  ``license`` only for a public dataset, the repo_id, the cleaned tags, the info;
* the real LeRobot (server image; skipped where lerobot is not installed): the
  card renders, its YAML front matter parses with
  ``huggingface_hub.repocard.metadata_load`` (an invalid README makes
  ``upload_large_folder`` retry its commit forever), carries ``license:
  apache-2.0`` only when public, ``task_categories``, ``tags`` and ``configs``,
  and names the repo_id.
"""

from __future__ import annotations

import ast
import sys
import types

import pytest

from physical_ai_server.data_processing import dataset_card as dc

_INFO = {
    'codebase_version': 'v3.0',
    'robot_type': 'omx_f',
    'fps': 30,
    'total_episodes': 2,
    'features': {'observation.state': {'dtype': 'float32', 'shape': [6]}},
}


@pytest.fixture
def fake_lerobot(monkeypatch):
    calls = []

    class _Card:
        def __init__(self, text):
            self.text = text

        def __str__(self):
            return self.text

    def _create(tags=None, dataset_info=None, **kwargs):
        calls.append({'tags': tags, 'dataset_info': dataset_info, **kwargs})
        return _Card('---\nCARD\n---')

    utils = types.ModuleType('lerobot.datasets.utils')
    utils.create_lerobot_dataset_card = _create
    monkeypatch.setitem(sys.modules, 'lerobot', types.ModuleType('lerobot'))
    monkeypatch.setitem(sys.modules, 'lerobot.datasets', types.ModuleType('lerobot.datasets'))
    monkeypatch.setitem(sys.modules, 'lerobot.datasets.utils', utils)
    return calls


def test_a_public_dataset_gets_apache_2_0(fake_lerobot):
    text = dc.build_dataset_card('alice/wuerfel', _INFO, ['robotis', 'omx_f'], public=True)
    assert text == '---\nCARD\n---'
    (call,) = fake_lerobot
    assert call == {'tags': ['robotis', 'omx_f'], 'dataset_info': _INFO,
                    'repo_id': 'alice/wuerfel', 'license': 'apache-2.0'}


def test_a_private_dataset_gets_no_license(fake_lerobot):
    dc.build_dataset_card('alice/wuerfel', _INFO, ['robotis'], public=False)
    (call,) = fake_lerobot
    assert 'license' not in call
    assert call['repo_id'] == 'alice/wuerfel'


def test_tags_are_cleaned(fake_lerobot):
    dc.build_dataset_card('a/b', _INFO, ['robotis', '', None, ' omx_f ', 'robotis'], public=False)
    assert fake_lerobot[0]['tags'] == ['robotis', 'omx_f']
    assert dc.card_tags(None) == []


def test_module_imports_lerobot_lazily():
    tree = ast.parse(open(dc.__file__, encoding='utf-8').read())
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or '').split('.')[0])
    assert top <= {'__future__', 'typing'}, top


# ── the real LeRobot (server image) ──────────────────────────────────────────

@pytest.mark.parametrize('public', [True, False])
def test_the_real_card_has_valid_front_matter(public, tmp_path):
    pytest.importorskip('lerobot.datasets.utils')
    repocard = pytest.importorskip('huggingface_hub.repocard')
    text = dc.build_dataset_card('alice/wuerfel', _INFO, ['robotis', 'omx_f'], public=public)
    path = tmp_path / 'README.md'
    path.write_text(text, encoding='utf-8')
    meta = repocard.metadata_load(path)
    assert meta is not None
    assert meta.get('task_categories') == ['robotics']
    assert set(['LeRobot', 'robotis', 'omx_f']) <= set(meta.get('tags') or [])
    assert meta.get('configs') == [{'config_name': 'default', 'data_files': 'data/*/*.parquet'}]
    if public:
        assert meta.get('license') == 'apache-2.0'
    else:
        assert 'license' not in meta
    assert 'alice/wuerfel' in text
    assert '"codebase_version": "v3.0"' in text
