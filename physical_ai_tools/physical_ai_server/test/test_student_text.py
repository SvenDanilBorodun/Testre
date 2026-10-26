#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""C9-a — nothing inside a list or as a dict value reads „nichts".

``student_text`` rendered None as '' everywhere, so ``melde [1, nichts, a]``
read „[1, , a]". A top-level None keeps its rendering (empty in „verbinde",
„nichts" in ``log``); inside a container it is now the word ``log`` uses. This
pins ``student_text``'s own ASCII output, independent of how „melde" frames
brackets (B9).
"""

from __future__ import annotations

from physical_ai_server.workflow import student_text as st
from physical_ai_server.workflow.handlers import output
from physical_ai_server.workflow.student_text import student_text


def test_none_inside_a_list_reads_nichts():
    assert student_text([1, None, 'a']) == '[1, nichts, a]'


def test_none_as_a_dict_value_reads_nichts():
    assert student_text({'a': None}) == '{a: nichts}'


def test_none_in_a_nested_list_reads_nichts():
    assert student_text([[None], 2.0]) == '[[nichts], 2]'


def test_a_top_level_none_is_still_empty():
    """„verbinde" with an empty value adds nothing (the top level is unchanged)."""
    assert student_text(None) == ''


def test_the_none_word_equals_the_log_none_word():
    """``student_text`` cannot import ``output`` (``output`` imports it at module
    top), so the word lives twice and is pinned equal here."""
    assert st.NONE_IN_CONTAINER_DE == output.LOG_NONE_DE == 'nichts'
