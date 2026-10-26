#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""B9 (owner decision OQ-5 B): a student's line keeps its ASCII brackets.

„melde [1, b]" read „(1, b)" — `emit_student_text` turned every `[`/`]` into
`(`/`)` so a student line could not spoof a sentinel. Now only a line whose
first character other than a space or U+FEFF is `[` gets an invisible U+2060
WORD JOINER in front (`output.STUDENT_LINE_GUARD`): every client parser of a
student line is anchored at the line's first character
(`robotis_ai_setup/tests/test_student_line_sentinels_lockstep.py`), so the
line can never START a sentinel. The guard counts toward MAX_LOG_CHARS.
"""

from __future__ import annotations

import re
import types

from physical_ai_server.workflow.handlers import output as out

_GUARD = '⁠'

# The client's anchored parsers of a student line
# (hooks/useRosTopicSubscription.js::interceptToken, RunControls.jsx), as
# Python regexes — none may match a guarded line.
_CLIENT_PARSERS = (
    re.compile(r'^\[SOUND\]$'),
    re.compile(r'^\[TONE:([\d.]+):([\d.]+)\]$'),
    re.compile(r'^\[SPEAK:(.*)\]$', re.S),
    re.compile(r'^\[TOAST:(info|success|warning|error):([\d.]+):(.*)\]$', re.S),
    re.compile(r'^\[VAR:([^=]+)=(.*)\]$', re.S),
    re.compile(r'^\[CNT:(.+)=(-?\d+)\]$', re.S),
    re.compile(r'^\[TECHNIK\] '),
)


def _ctx():
    ns = types.SimpleNamespace(msgs=[])
    ns.log = ns.msgs.append
    return ns


def _melde(value):
    c = _ctx()
    out.log(c, {'message': value})
    assert len(c.msgs) == 1, c.msgs
    return c.msgs[0]


def _parsed_by_the_client(line: str) -> bool:
    # JS trim() strips what Python's \s strips plus U+FEFF; U+2060 is neither.
    trimmed = line.strip().strip('﻿')
    return any(p.search(line) or p.search(trimmed) for p in _CLIENT_PARSERS)


def test_the_guard_is_the_word_joiner():
    assert out.STUDENT_LINE_GUARD == _GUARD
    assert not _GUARD.isspace() and _GUARD.strip() == _GUARD


def test_a_list_keeps_its_ascii_brackets():
    assert _melde([1, 'b']) == _GUARD + '[1, b]'


def test_a_forged_sentinel_is_shown_as_text():
    for text in ('[VAR:x=1]', '   [VAR:x=1]', '[SOUND]', '[TECHNIK] x', '[CNT:a=1]',
                 '[TOAST:info:3:hallo]', '[SPEAK:hallo]', '[TONE:440:1]'):
        line = _melde(text)
        assert _GUARD in line, line
        assert not _parsed_by_the_client(line), line
    assert _melde('   [VAR:x=1]') == '   ' + _GUARD + '[VAR:x=1]'


def test_a_leading_byte_order_mark_is_guarded_too():
    """review-3 mi-B: U+FEFF is the one character JS trim() strips that
    Python's \\s does not."""
    line = _melde('﻿[VAR:x=1]')
    assert line == '﻿' + _GUARD + '[VAR:x=1]'
    assert not _parsed_by_the_client(line)


def test_a_guarded_line_at_the_cap_is_exactly_the_cap():
    """review-3 mi-D: the guard runs before the truncation, so a guarded line
    is cut one character earlier and is never MAX_LOG_CHARS + 1 long."""
    line = _melde('[' + 'x' * 5000)
    assert len(line) == out.MAX_LOG_CHARS
    assert line.startswith(_GUARD + '[')
    assert line.endswith(' …')


def test_a_bracket_inside_the_line_needs_no_guard():
    assert _melde('a [b]') == 'a [b]'
    assert _melde('a\n[FEHLER] alles kaputt') == 'a [FEHLER] alles kaputt'


def test_print_output_is_guarded_the_same_way():
    c = _ctx()
    out.emit_student_text(c, '﻿[VAR:y=2]', 'ausgabe')
    out.emit_student_text(c, '[1, 2]', 'ausgabe')
    assert c.msgs == ['﻿' + _GUARD + '[VAR:y=2]', _GUARD + '[1, 2]']
