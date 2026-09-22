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
