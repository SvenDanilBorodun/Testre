"""Record-path strings a student reads are German (Aufnahme 2.0, spec §2.6 item 6).

Static fences (the behaviour is pinned by test_record_command_dispatch.py and
test_record_timer_finishing.py):

* ``_data_collection_timer_callback`` runs only for recordings, so no English
  HEAD sentence may reach its published status there (log lines stay English —
  they are for the maintainer, CLAUDE.md rule 1);
* in ``user_interaction_callback`` every HEAD English answer survives ONLY as the
  non-record alternative (``… if rec else '<HEAD>'`` or the ``else`` of an
  ``if rec``): the Inferenz page keeps HEAD's text byte for byte (F3). The one
  exception is multi-task MOVE_TO_NEXT, unchanged and unreachable from the page;
* no transliteration (`enthaelt`, `Aufloesung`, `pruefen`) in the record path;
* the arms are „Leader-Arm“/„Follower-Arm“ and the cameras go through
  ``camera_name_de`` (F6a vocabulary).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _PKG / 'physical_ai_server.py'
_DM_PY = _PKG / 'data_processing' / 'data_manager.py'
_SERVER_SRC = _SERVER_PY.read_text(encoding='utf-8')
_DM_SRC = _DM_PY.read_text(encoding='utf-8')

_LOG_METHODS = {'info', 'warning', 'warn', 'error', 'debug'}

# HEAD's English record-path sentences (spec §2.6 table).
_HEAD_ENGLISH = (
    'Restarting the recording.',
    'Recording started',
    'Not currently recording',
    'Recording stopped',
    'All operations terminated',
    'Task skipped successfully',
    'Error in user interaction',
    'Re-recording current episode',
    'Moved to next episode',
    'data not received within timeout period',
    'Failed to convert messages',
    'please check the robot type again',
    'Invalid repository name',
)


def _function(src, name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


def _parents(root):
    parents = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_log_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS)


def _string_nodes(fn):
    """(node, text) for every string literal / f-string outside a logger call."""
    parents = _parents(fn)
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            text = ''.join(v.value for v in node.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and not isinstance(parents.get(node), ast.JoinedStr)):
            text = node.value
        else:
            continue
        up, in_log = node, False
        while up in parents:
            up = parents[up]
            if _is_log_call(up):
                in_log = True
                break
        if not in_log:
            out.append((node, text))
    return out, parents


def _is_rec(test):
    return isinstance(test, ast.Name) and test.id == 'rec'


def _is_not_rec(test):
    return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
            and _is_rec(test.operand))


def _on_non_record_side(node, parents):
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, (ast.IfExp, ast.If)):
            orelse = parent.orelse if isinstance(parent.orelse, list) else [parent.orelse]
            body = parent.body if isinstance(parent.body, list) else [parent.body]
            if _is_rec(parent.test) and child in orelse:
                return True
            if _is_not_rec(parent.test) and child in body:
                return True
        child = parent
    return False


def test_record_timer_publishes_no_head_english():
    strings, _ = _string_nodes(_function(_SERVER_SRC, '_data_collection_timer_callback'))
    for _, text in strings:
        for english in _HEAD_ENGLISH:
            assert english not in text, (english, text)
    joined = ' '.join(t for _, t in strings)
    assert 'Die Kameras senden keine Bilder' in joined
    assert 'Der Follower-Arm sendet keine Daten' in joined
    assert 'Der Leader-Arm sendet keine Daten' in joined


def test_record_exception_sentence_carries_no_exception_text():
    fn = _function(_SERVER_SRC, '_data_collection_timer_callback')
    strings, _ = _string_nodes(fn)
    for node, text in strings:
        if isinstance(node, ast.JoinedStr) and 'Aufnahme gestoppt' in text:
            raise AssertionError('the record() stop sentence must not be an f-string '
                                 'interpolating the exception')
    assert any('Aufnahme gestoppt: Frame konnte nicht gespeichert werden.' in t
               for _, t in strings)


def test_command_answers_are_head_english_only_off_the_record_page():
    fn = _function(_SERVER_SRC, 'user_interaction_callback')
    strings, parents = _string_nodes(fn)
    offenders = []
    for node, text in strings:
        hits = [e for e in _HEAD_ENGLISH if e in text]
        if not hits:
            continue
        if _on_non_record_side(node, parents):
            continue
        # Multi-task MOVE_TO_NEXT is unchanged (dead from the page, spec §2.9).
        if text == 'Moved to next episode' and _in_multi_task_branch(node, parents):
            continue
        offenders.append(text)
    assert offenders == []


def _in_multi_task_branch(node, parents):
    child = node
    while child in parents:
        parent = parents[child]
        if isinstance(parent, ast.If) and child in parent.body:
            test = ast.unparse(parent.test)
            if 'task_instruction' in test and '> 1' in test:
                return True
        child = parent
    return False


def test_rec_is_derived_from_the_task_type():
    fn = _function(_SERVER_SRC, 'user_interaction_callback')
    src = ast.unparse(fn)
    assert re.search(r"rec = .*task_type.*== 'record'", src), src[:400]


def test_no_transliterations_on_the_record_path():
    for src in (_SERVER_SRC, _DM_SRC):
        assert not re.search(r'enthaelt|Aufloesung|pruefen', src)


def test_arm_vocabulary():
    for src in (_SERVER_SRC, _DM_SRC):
        assert 'Leitarm' not in src and 'Folgearm' not in src


def test_camera_names_go_through_camera_name_de():
    # (The record tick's dead camera-fps sentence was deleted in round 5, R5-4d.)
    convert = ast.unparse(_function(_DM_SRC, 'convert_msgs_to_raw_datas'))
    # Round 7: the recording sentence is built by record_texts_de (which names
    # the camera through its camera_name_de); the data manager's camera_name_de
    # is that same function.
    assert 'record_texts_de.stale_camera_recording_de(stale' in convert
    assert 'camera_name_de = record_texts_de.camera_name_de' in _DM_SRC
    # Round 5: the node's record-path sentences (a source that stops, the busy
    # answer, the error-stop note) come from the one module of German record
    # texts, whose camera_name_de is the same vocabulary.
    tree = ast.parse(_SERVER_SRC)
    imported = [a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                and n.module == 'physical_ai_server.data_processing'
                for a in n.names]
    assert 'record_texts_de' in imported
    timer = ast.unparse(_function(_SERVER_SRC, '_data_collection_timer_callback'))
    assert 'record_texts_de.source_stop_de(' in timer
