#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower camera's isolation guards G1, G2 and G9 (spec 40 S1, decision D4).

The tower camera's full-resolution stream is Roboter Studio's alone: the
recorder, inference, the Daten engine and the pages that show the outside
stream never learn it exists, and the server learns the scene-camera profile
from the arm container's statement, never from the environment.

* G1 — no ``config/*_config.yaml`` camera entry and no profile's
  ``camera_roles`` names ``studio`` (so no recorder or policy ever asks for it).
* G2 — no must-not-touch file (spec 40 §5) names the studio stream, the camera
  state topic, the consumer module or the split module.
* G9 — the server package never names ``EDUBOTICS_SCENE_CAMERA_PROFILE``.

Each guard is a function over files, checked on the repository and on a copy
with the violation its mutation plants. D58's provenance code
(``scene_provenance``, ``/edubotics/scene_camera_profile``) is named by
neither guard and stays green under both.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

from physical_ai_server import robot_profiles

_PS = Path(__file__).resolve().parents[1]
_REPO = _PS.parents[1]
_PKG = _PS / 'physical_ai_server'
_PM_SRC = _REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src'

G1_WORD = 'studio'
G2_STRINGS = ('studio/tower', 'scene_camera_state', 'studio_camera', 'scene_split')
G9_NAME = 'EDUBOTICS_SCENE_CAMERA_PROFILE'
_CAMERA_LIST_KEYS = ('camera_topic_list', 'observation_list')
_SKIP_DIRS = {'__pycache__', 'node_modules'}

# Spec 40 §5's must-not-touch list (decision D4), as paths under the repository.
_MNT_PATHS = (
    'physical_ai_tools/physical_ai_server/physical_ai_server/communication',
    'physical_ai_tools/physical_ai_server/physical_ai_server/data_processing',
    'physical_ai_tools/physical_ai_server/physical_ai_server/signal_status.py',
    'physical_ai_tools/physical_ai_server/physical_ai_server/inference',
    'physical_ai_tools/physical_ai_server/physical_ai_server/workflow/path_guard.py',
    'robotis_ai_setup/modal_inference',
    'physical_ai_tools/physical_ai_server/config',
    'physical_ai_tools/physical_ai_interfaces',
    'physical_ai_tools/physical_ai_manager/src/components/Record',
    'physical_ai_tools/physical_ai_manager/src/components/Home',
    'physical_ai_tools/physical_ai_manager/src/components/CloudInference',
    'physical_ai_tools/physical_ai_manager/src/components/Daten',
    'physical_ai_tools/physical_ai_manager/src/pages/RecordPage.js',
    'physical_ai_tools/physical_ai_manager/src/pages/HomePage.js',
    'physical_ai_tools/physical_ai_manager/src/pages/InferencePage.js',
    'physical_ai_tools/physical_ai_manager/src/pages/EditDatasetPage.js',
    'physical_ai_tools/physical_ai_manager/src/features/tasks',
    'physical_ai_tools/physical_ai_manager/src/features/cloudInference',
    'physical_ai_tools/physical_ai_manager/src/features/editDataset',
    'physical_ai_tools/physical_ai_manager/src/utils/homeHealth.js',
    'physical_ai_tools/physical_ai_manager/src/utils/__tests__/homeHealth.test.js',
    'physical_ai_tools/physical_ai_manager/src/StudentApp.js',
)
# The four pages' own tests ("and their tests").
_MNT_PAGE_TEST_PREFIXES = ('RecordPage', 'HomePage', 'InferencePage', 'EditDatasetPage')

# The two thin profile descriptors beside the server's registry.
_DESCRIPTORS = (
    _REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'constants.py',
    _REPO / 'robotis_ai_setup' / 'pi_agent' / 'constants.py',
)


def _files(roots):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob('*')):
            if path.is_file() and not _SKIP_DIRS.intersection(path.parts):
                yield path


def _text(path):
    return path.read_bytes().decode('utf-8', errors='replace')


# ── G1 ───────────────────────────────────────────────────────────────────────

def _camera_entries(doc):
    """Every entry of every camera_topic_list / observation_list in a config."""
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key in _CAMERA_LIST_KEYS and isinstance(value, list):
                yield from (str(entry) for entry in value)
            else:
                yield from _camera_entries(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from _camera_entries(value)


def g1_violations(config_files, camera_roles_by_source):
    """``(where, entry)`` for each camera entry or camera role naming studio."""
    out = []
    for path in config_files:
        for entry in _camera_entries(yaml.safe_load(path.read_text(encoding='utf-8'))):
            if G1_WORD in entry.lower():
                out.append((str(path), entry))
    for where, roles in camera_roles_by_source:
        out.extend((where, role) for role in roles if G1_WORD in str(role).lower())
    return out


def _descriptor_camera_roles(path):
    """Every literal ``"camera_roles": (…)`` of a descriptor table, by AST."""
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'camera_roles':
                    out.append((f'{path}:{node.lineno}', tuple(ast.literal_eval(value))))
    return out


def _all_camera_roles():
    roles = [(f'robot_profiles:{pid}', tuple(p.camera_roles))
             for pid, p in robot_profiles.ROBOT_PROFILES.items()]
    for path in _DESCRIPTORS:
        found = _descriptor_camera_roles(path)
        assert len(found) == len(robot_profiles.ROBOT_PROFILES), (path, found)
        roles.extend(found)
    return roles


def _config_files(config_dir):
    files = sorted(config_dir.glob('*_config.yaml'))
    assert files, config_dir
    return files


def test_g1_no_camera_entry_and_no_camera_role_names_studio():
    configs = _config_files(_PS / 'config')
    roles = _all_camera_roles()
    entries = [e for p in configs
               for e in _camera_entries(yaml.safe_load(p.read_text(encoding='utf-8')))]
    assert any(':' in e for e in entries)           # the camera topics were really read
    assert g1_violations(configs, roles) == []


def test_g1_mutation_a_studio_camera_planted_in_a_config_copy_is_caught(tmp_path):
    source = _PS / 'config' / 'omx_f_config.yaml'
    text = source.read_text(encoding='utf-8')
    anchor = '        - scene:/scene/image_raw/compressed\n'
    assert anchor in text
    copy = tmp_path / 'omx_f_config.yaml'
    copy.write_text(text.replace(anchor, anchor + '        - studio:/studio/tower/compressed\n'),
                    encoding='utf-8')
    assert g1_violations([copy], []) == [(str(copy), 'studio:/studio/tower/compressed')]
    observed = tmp_path / 'observed.yaml'
    observed.write_text(text.replace('        - scene\n', '        - scene\n        - studio\n'),
                        encoding='utf-8')
    assert g1_violations([observed], []) == [(str(observed), 'studio')]


def test_g1_mutation_a_studio_camera_role_is_caught():
    assert g1_violations([], [('omx_full', ('gripper', 'scene', 'studio'))]) == [
        ('omx_full', 'studio')]


# ── G2 ───────────────────────────────────────────────────────────────────────

def _mnt_roots():
    roots = [_REPO / rel for rel in _MNT_PATHS]
    missing = [str(r) for r in roots if not r.exists()]
    assert not missing, f'a must-not-touch path moved; the guard would scan nothing: {missing}'
    roots += sorted(p for p in (_PM_SRC / 'pages' / '__tests__').iterdir()
                    if p.name.startswith(_MNT_PAGE_TEST_PREFIXES))
    return roots


def g2_violations(roots):
    """``(file, string)`` for each must-not-touch file naming a studio string."""
    out = []
    for path in _files(roots):
        text = _text(path)
        out.extend((str(path), s) for s in G2_STRINGS if s in text)
    return out


def test_g2_no_must_not_touch_file_names_the_studio_stream():
    roots = _mnt_roots()
    scanned = list(_files(roots))
    assert len(scanned) > 200
    assert _PKG / 'data_processing' / 'data_manager.py' in scanned
    assert g2_violations(roots) == []


def test_g2_mutation_each_string_planted_in_a_data_manager_copy_is_caught(tmp_path):
    text = (_PKG / 'data_processing' / 'data_manager.py').read_text(encoding='utf-8')
    for string in G2_STRINGS:
        copy = tmp_path / string.replace('/', '_') / 'data_manager.py'
        copy.parent.mkdir()
        copy.write_text(text + f'\n# {string}\n', encoding='utf-8')
        assert g2_violations([copy.parent]) == [(str(copy), string)]


# ── G9 ───────────────────────────────────────────────────────────────────────

def _server_package_files():
    return [p for p in _files([_PS]) if 'test' not in p.relative_to(_PS).parts[:1]]


def g9_violations(files):
    return [str(p) for p in files if G9_NAME in _text(p)]


def test_g9_the_server_package_never_names_the_profile_key():
    files = _server_package_files()
    assert _PKG / 'physical_ai_server.py' in files
    assert _PKG / 'scene_provenance.py' in files
    assert _PKG / 'workflow' / 'studio_camera.py' in files
    assert not any(p.is_relative_to(_PS / 'test') for p in files)
    assert g9_violations(files) == []


def test_g9_mutation_the_key_planted_in_a_node_copy_is_caught(tmp_path):
    text = (_PKG / 'physical_ai_server.py').read_text(encoding='utf-8')
    copy = tmp_path / 'physical_ai_server.py'
    copy.write_text(text + f"\nPROFILE = os.environ.get('{G9_NAME}')\n", encoding='utf-8')
    assert g9_violations([copy]) == [str(copy)]


# ── D58's provenance code is named by neither guard (R4) ─────────────────────

def test_d58s_provenance_code_is_present_and_named_by_neither_guard():
    provenance = (_PKG / 'scene_provenance.py').read_text(encoding='utf-8')
    assert "PROFILE_TOPIC = '/edubotics/scene_camera_profile'" in provenance
    for word in G2_STRINGS + (G9_NAME,):
        assert word not in provenance
        assert 'scene_provenance' not in word and 'scene_camera_profile' not in word
    # The recorder names the provenance module, and G2 still passes over it.
    assert 'scene_provenance' in (_PKG / 'data_processing' / 'data_manager.py').read_text(
        encoding='utf-8')
