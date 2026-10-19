#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""The German diagnosis table (§3.6): every sentence German by the shipped
predicate, none carrying a raw tool message, none with a {exc_message} slot."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from physical_ai_server.workflow import code_errors_de as ce

_REPO = Path(__file__).resolve().parents[3]


def _gdl():
    spec = importlib.util.spec_from_file_location(
        'gdl', _REPO / '.github' / 'scripts' / 'german_detail_lint.py')
    gdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gdl)
    return gdl


def _is_german(gdl, s: str) -> bool:
    # The precedent predicate (tests/test_feetech_bus.py, test_code_rpc.py):
    # a German marker present AND no transliteration digraph. There is no
    # is_german function in the script (P30); this is the composition.
    return bool(gdl.GERMAN_CHARS.search(s) or gdl.GERMAN_WORDS.search(s)) \
        and not gdl.TRANSLITERATIONS.search(s)


def test_every_static_sentence_is_german_and_carries_no_raw_message():
    gdl = _gdl()
    assert ce.SENTENCES, 'the table is empty'
    for kind, template in ce.SENTENCES.items():
        assert _is_german(gdl, template), (kind, template)
        assert '{exc_message}' not in template, kind
        # Never a leaked English tool fragment — the raw line rides [TECHNIK].
        assert 'Traceback' not in template and 'Error:' not in template, kind


def test_the_built_kinds_are_german_too():
    gdl = _gdl()
    # robot_method with and without a suggestion.
    with_sug = ce.sentence('robot_method', file='main.py', line=5, name='hom',
                           suggestion='home')
    no_sug = ce.sentence('robot_method', file='main.py', line=5, name='xyzzy')
    assert 'Meintest du robot.home?' in with_sug
    assert 'Meintest du' not in no_sug
    for s in (with_sug, no_sug):
        assert _is_german(gdl, s)
        assert '{exc_message}' not in s
    # robot relays the (German-by-contract) WorkflowError text verbatim.
    relayed = ce.sentence('robot', file='main.py', line=9,
                          relayed='Objekt nicht gefunden.')
    assert 'Objekt nicht gefunden.' in relayed and relayed.startswith('Zeile 9')
    assert _is_german(gdl, relayed)
    # other names the exception CLASS only (a code identifier), never a message.
    other = ce.sentence('other', file='main.py', line=2, exc_type='ValueError')
    assert 'ValueError' in other and _is_german(gdl, other)


def test_robot_method_suggestion_comes_from_the_api_table():
    # robot.hom → robot.home is the difflib hit the server passes as suggestion.
    from physical_ai_server.workflow import robot_api
    assert robot_api.suggest('hom') == 'home'
    s = ce.sentence('robot_method', file='main.py', line=1, name='hom',
                    suggestion=robot_api.suggest('hom'))
    assert 'robot.home' in s


def test_error_kinds_cover_the_faulting_exits_and_exclude_the_clean_ones():
    assert 'name' in ce.ERROR_KINDS and 'compile' in ce.ERROR_KINDS
    assert 'robot' in ce.ERROR_KINDS and 'other' in ce.ERROR_KINDS
    # killed / runner_down / busy are NOT faults (a clean stop / an unavailable
    # runner is reported without a phase-error diagnosis of the program).
    assert 'killed' not in ce.ERROR_KINDS
    assert 'runner_down' not in ce.ERROR_KINDS
    assert 'busy' not in ce.ERROR_KINDS
    # runner_crashed is the SERVER's verdict on a control connection that ended
    # with no `exited` (fix round 1): a sentence in the table, never a kind the
    # student process may report about itself through __exit.
    assert 'runner_crashed' in ce.SENTENCES
    assert 'runner_crashed' not in ce.ERROR_KINDS


def test_a_call_the_robot_knows_but_the_runner_lacks_names_the_version_gap():
    """Review round 2 (mi8): an OLD runner (no `zeige` in its stub) against a
    NEW server raised AttributeError → kind robot_method, and the difflib
    suggestion WAS the name: „robot.zeige gibt es nicht. Meintest du
    robot.zeige?". When the suggestion is the name itself, the two halves'
    versions differ, and the sentence says so with a next step."""
    from physical_ai_server.workflow import robot_api
    gdl = _gdl()
    for name in robot_api.PYTHON_NAMES:
        assert robot_api.suggest(name) == name, name
    s = ce.sentence('robot_method', file='main.py', line=3, name='zeige',
                    suggestion=robot_api.suggest('zeige'))
    assert 'Meintest du' not in s
    assert s.startswith('Zeile 3 in main.py: robot.zeige ')
    assert 'Programmier-Umgebung' in s and 'neu starten' in s
    assert _is_german(gdl, s) and '{' not in s
    # A real misspelling still gets its suggestion.
    assert 'Meintest du robot.zeige?' in ce.sentence(
        'robot_method', file='main.py', line=3, name='zeigen',
        suggestion=robot_api.suggest('zeigen'))


# ── area C: the Java launcher's reports, read the same way as Python's ─────

def test_a_report_without_a_position_drops_the_position_prefix():
    """F2: `System.exit(3)` (and any fault with no project frame) used to read
    „Zeile 0 in : … (Code 3)". With no file or no line, the sentence starts
    with what the program did."""
    s = ce.sentence('other', exc_type='Code 3')
    assert s == 'Das Programm ist mit einem Fehler abgebrochen (Code 3).'
    assert ce.sentence('other', file='Main.java', line=0, exc_type='Code 1') \
        == 'Das Programm ist mit einem Fehler abgebrochen (Code 1).'
    assert ce.sentence('robot', relayed='Arm kaputt.') == 'Arm kaputt.'
    assert ce.sentence('zero_division', file='', line=4) \
        == 'Division durch 0 ist nicht erlaubt.'
    # With a position nothing changes.
    assert ce.sentence('zero_division', file='Main.java', line=4) \
        == 'Zeile 4 in Main.java: Division durch 0 ist nicht erlaubt.'
    gdl = _gdl()
    for kind in ce.SENTENCES:
        s = ce.sentence(kind, exc_type='X', name='n')
        assert 'Zeile 0' not in s and ' in :' not in s, (kind, s)
        assert _is_german(gdl, s), (kind, s)


_NULL_DE = ('Hier ist kein Wert da (None bzw. null) — wurde die Variable vorher '
            'belegt, und hat find etwas gefunden?')


def test_the_null_sentence_is_chosen_from_the_exception_in_both_languages():
    java = ce.sentence('other', file='Main.java', line=7, exc_type='NullPointerException')
    py = ce.sentence('other', file='main.py', line=2, exc_type='AttributeError', name='None')
    assert java == f'Zeile 7 in Main.java: {_NULL_DE}'
    assert py == f'Zeile 2 in main.py: {_NULL_DE}'
    # Any other AttributeError (a name that is not None) stays the class name.
    other = ce.sentence('other', file='main.py', line=2, exc_type='AttributeError', name='x')
    assert other == 'Zeile 2 in main.py: Das Programm ist mit einem Fehler abgebrochen (AttributeError).'
    assert _is_german(_gdl(), java)


def test_missing_static_main_is_german():
    """m14: the launcher reports a missing or non-static `main` as kind
    `other` with exc_type NoSuchMethodException and no position."""
    s = ce.sentence('other', exc_type='NoSuchMethodException')
    assert s == ('In Main.java fehlt die Methode „public static void main(String[] args)“ — '
                 'ohne sie kann das Programm nicht starten.')
    assert _is_german(_gdl(), s)
    # The student's own reflective NoSuchMethodException has a position: it
    # is an ordinary fault, not the missing entry point.
    assert ce.sentence('other', file='Main.java', line=9, exc_type='NoSuchMethodException') \
        == 'Zeile 9 in Main.java: Das Programm ist mit einem Fehler abgebrochen (NoSuchMethodException).'
