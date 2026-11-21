#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The legacy calibration is gone (spec 40 S7, decision D15; WP-16).

Roboter Studio is calibrated by the tower's mat check and arm check
(``workflow/autocal/``). The board-on-table extrinsic, the 20-photo intrinsic
step, the touch-off and the ground-truth verify correction are deleted, with
their ten env knobs and their five ROS services:

* ``workflow/calibration_manager.py`` does not exist and nothing imports it;
* no code of the server package uses one of S7's symbols (an identifier, an
  attribute, a parameter, an import or a string naming it);
* none of the ten env names and none of the five service names occurs in the
  server package or in the three composes;
* ``_init_ros_service`` registers ``/calibration/start``, ``/cancel`` and
  ``/status`` and none of the five;
* no string literal and no comment of the server package sends a student to
  „Tisch vermessen"; such a sentence became S7's one sentence.

``physical_ai_server.py`` imports rclpy, so the node is read as source, the way
the other node tests read it.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path
import tokenize

import pytest

from physical_ai_server.workflow.handlers import motion

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_REPO = Path(__file__).resolve().parents[3]
_NODE = _PKG / 'physical_ai_server.py'
_COMPOSES = tuple(_REPO / rel for rel in (
    'robotis_ai_setup/docker/docker-compose.yml',
    'robotis_ai_setup/docker/docker-compose.opi.yml',
    'robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml',
))

# S7's symbols, the touch-off's node state and the callbacks of the five services.
_REMOVED_SYMBOLS = frozenset((
    'calibration_manager', 'CalibrationManager', '_get_or_create_calibration_manager',
    '_apply_xy_correction', '_apply_yaw_bias', 'xy_correction', 'yaw_bias_rad',
    'read_verify_correction', '_force_recalib', '_recalibrated_this_boot',
    '_active_calib_step', '_calibration_stop_event', '_verify_points',
    'calibration_capture_callback', 'calibration_solve_callback',
    'calibration_preview_callback', 'calibration_verify_callback',
    'calibration_history_callback',
))
_REMOVED_ENV_NAMES = (
    'EDUBOTICS_BOARD_ORIGIN_X_M', 'EDUBOTICS_BOARD_ORIGIN_Y_M', 'EDUBOTICS_BOARD_TABLE_Z_M',
    'EDUBOTICS_BOARD_YAW_DEG', 'EDUBOTICS_EXTRINSIC_REGION_X_MIN',
    'EDUBOTICS_EXTRINSIC_REGION_X_MAX', 'EDUBOTICS_EXTRINSIC_REGION_Y_ABS',
    'EDUBOTICS_EXTRINSIC_BURST_FRAMES', 'EDUBOTICS_EXTRINSIC_BURST_INTERVAL_S',
    'EDUBOTICS_FORCE_RECALIBRATION',
)
_REMOVED_SERVICES = (
    '/calibration/capture_frame', '/calibration/solve', '/calibration/preview',
    '/calibration/verify', '/calibration/history',
)
_KEPT_SERVICES = ('/calibration/start', '/calibration/cancel', '/calibration/status')
_PHRASE = 'Tisch vermessen'


def _package_files():
    files = sorted(p for p in _PKG.rglob('*.py') if '__pycache__' not in p.parts)
    assert len(files) > 50, f'the server package was not found under {_PKG}'
    return files


def _texts():
    for path in _COMPOSES:
        assert path.is_file(), f'{path} is missing'
    return [(p, p.read_text(encoding='utf-8')) for p in (*_package_files(), *_COMPOSES)]


def _names_used(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, ast.keyword) and node.arg is not None:
            yield node.arg
        elif isinstance(node, ast.alias):
            yield from node.name.split('.')
            if node.asname:
                yield node.asname
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield from node.module.split('.')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_the_legacy_calibration_module_is_deleted():
    assert not (_PKG / 'workflow' / 'calibration_manager.py').exists()


def test_no_code_of_the_server_package_uses_a_removed_symbol():
    found = []
    for path in _package_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        found += [f'{path.relative_to(_PKG)}: {name}'
                  for name in sorted(set(_names_used(tree)) & _REMOVED_SYMBOLS)]
    assert found == [], found


@pytest.mark.parametrize('name', _REMOVED_ENV_NAMES + _REMOVED_SERVICES)
def test_no_removed_env_name_or_service_name_occurs(name):
    found = [str(path.relative_to(_REPO)) for path, text in _texts() if name in text]
    assert found == [], f'{name} still occurs in {found}'


def _registered_services():
    tree = ast.parse(_NODE.read_text(encoding='utf-8'))
    init = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == '_init_ros_service')
    table = next(n.value for n in ast.walk(init)
                 if isinstance(n, ast.Assign) and isinstance(n.value, ast.List)
                 and any(isinstance(t, ast.Name) and t.id == 'service_definitions'
                         for t in n.targets))
    return {entry.elts[0].value for entry in table.elts
            if isinstance(entry, ast.Tuple) and isinstance(entry.elts[0], ast.Constant)}


def test_the_five_calibration_services_are_not_registered():
    names = _registered_services()
    assert set(_KEPT_SERVICES) <= names, names
    assert names.isdisjoint(_REMOVED_SERVICES), sorted(names & set(_REMOVED_SERVICES))


def test_no_string_literal_of_the_server_package_says_tisch_vermessen():
    found = []
    for path in _package_files():
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        found += [f'{path.relative_to(_PKG)}:{node.lineno}' for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and _PHRASE in node.value]
    assert found == [], found


def test_no_comment_of_the_server_package_says_tisch_vermessen():
    found = []
    for path in _package_files():
        tokens = tokenize.generate_tokens(io.StringIO(path.read_text(encoding='utf-8')).readline)
        found += [f'{path.relative_to(_PKG)}:{tok.start[0]}' for tok in tokens
                  if tok.type == tokenize.COMMENT and _PHRASE in tok.string]
    assert found == [], found


def test_the_one_sentence_that_replaced_tisch_vermessen():
    """A LITERAL pin (S7): where a block or the jog needs the grasp height and
    has none, the student is sent to the calibration."""
    assert motion.CALIBRATION_FIRST_DE == (
        'Bitte zuerst die Kalibrierung abschließen (Roboter Studio → Kalibrierung).')
