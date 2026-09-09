#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ONE definition of how a block-runtime value reads to a German-speaking student.

There used to be THREE, and they disagreed on the values a student actually
produces:

    value        melde (_student_text)   verbinde (_to_text)   [VAR:] (_jsonable)
    Greifziel    „Greifziel (Marker 20)  Detection(centroid_   „Greifziel (…)"
                  bei x=0,150 m …"        px=(320.0, 240.0)…
    Position-Dict „x=0,150 m, y=…"        {'x': 0.15, 'y': …}   {"x": 0.15, …}
    [wahr]       „[True]"                „[True]"              [true]

So `sage <verbinde("Ich sehe ", finde Würfel)>` READ A PYTHON REPR ALOUD, and
`melde <meine Liste>` printed `True` where the same value alone printed „wahr".
Fixing `melde` alone left the defect one block away, because „verbinde" is how a
twelve-year-old builds a sentence.

This module is the shared answer. It imports NOTHING from the workflow package,
so both `handlers/output.py` and `interpreter.py` import it NORMALLY — the
mutual lazy-import pair those two used to need is gone with it.

The rule for adding a shape here: it belongs if a STUDENT can put the value into
an output or a „verbinde" socket. `_jsonable` stays separate and still returns
JSON *structures* for plain containers — the Variablen-Tafel renders them itself
— and calls in here only for an object it cannot serialize.
"""

from __future__ import annotations

from typing import Any

# How many container elements are rendered before the tail is summarised. The
# caller truncates the finished string anyway (MAX_LOG_CHARS), but building the
# text costs time and memory proportional to the STUDENT's list, not to the cap:
# the same lesson `_jsonable`'s budget was added for (a 10-million-element list
# cost 0.74 s and ~268 MB RSS to produce 2000 characters, on the ROS node's own
# thread). A student's list is capped at MAX_LIST_ITEMS = 1000, so 200 shows the
# first fifth of the largest list they can build and names the rest.
STUDENT_TEXT_MAX_ITEMS = 200

# Nesting depth before we stop descending. Blockly can build a list of lists,
# and `lists_repeat` can nest them; a cycle is not reachable through the blocks
# but a depth cap is cheaper than proving that stays true.
STUDENT_TEXT_MAX_DEPTH = 5


def _fmt_m(value: Any) -> str:
    """A metre value with 3 decimals and a German decimal comma."""
    try:
        return f'{float(value):.3f}'.replace('.', ',')
    except (TypeError, ValueError):
        return str(value)


def _scalar(value: Any) -> str:
    """None → '' (a missing item adds nothing to a „verbinde"). bool → German
    „wahr"/„falsch" (Rule §1). An integer-valued float drops the trailing „.0",
    so „Anzahl Würfel" (which evaluates to 3.0) reads „3", not „3.0".

    ``bool`` is tested BEFORE the numeric branch because it is an ``int``
    subclass — order is the only thing keeping True out of the number formatter.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'wahr' if value else 'falsch'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def student_text(value: Any, _depth: int = 0) -> str:
    """Stringify a block-runtime value the way a German-speaking student reads it.

    NEVER raises. A formatting bug must not break an output block, so every
    branch is inside one try and the fallback is the plain scalar rule.
    """
    try:
        # „Position von <Ziel>" — the block hands back a plain x/y/z dict.
        if isinstance(value, dict) and all(k in value for k in ('x', 'y', 'z')):
            return (f'x={_fmt_m(value["x"])} m, y={_fmt_m(value["y"])} m, '
                    f'z={_fmt_m(value["z"])} m')
        # A Greifziel („finde <Typ>"). Duck-typed on the two fields the student
        # surface names, so the sim's SimpleNamespace renders like the real
        # perception Detection.
        xyz = getattr(value, 'world_xyz_m', None)
        if xyz is not None or hasattr(value, 'aruco_id'):
            tag = getattr(value, 'aruco_id', None)
            head = 'Greifziel' if tag is None else f'Greifziel (Marker {tag})'
            if xyz is None:
                return f'{head}, Position noch unbekannt'
            return (f'{head} bei x={_fmt_m(xyz[0])} m, y={_fmt_m(xyz[1])} m, '
                    f'z={_fmt_m(xyz[2])} m')
        if _depth < STUDENT_TEXT_MAX_DEPTH:
            if isinstance(value, (list, tuple)):
                return _container(value, '[', ']', _depth)
            if isinstance(value, dict):
                return _mapping(value, _depth)
        return _scalar(value)
    except Exception:  # noqa: BLE001 — formatting never breaks an output block
        try:
            return _scalar(value)
        except Exception:  # noqa: BLE001
            return ''


def _container(value, open_c: str, close_c: str, depth: int) -> str:
    shown = [student_text(v, depth + 1) for v in list(value)[:STUDENT_TEXT_MAX_ITEMS]]
    if len(value) > STUDENT_TEXT_MAX_ITEMS:
        # Same wording as `_jsonable`'s tail, so the Protokoll and the
        # Variablen-Tafel say the same thing about the same list.
        shown.append(f'… ({len(value)} Elemente)')
    return f'{open_c}{", ".join(shown)}{close_c}'


def _mapping(value: dict, depth: int) -> str:
    items = list(value.items())[:STUDENT_TEXT_MAX_ITEMS]
    shown = [f'{k}: {student_text(v, depth + 1)}' for k, v in items]
    if len(value) > STUDENT_TEXT_MAX_ITEMS:
        shown.append(f'… ({len(value)} Elemente)')
    return '{' + ', '.join(shown) + '}'
