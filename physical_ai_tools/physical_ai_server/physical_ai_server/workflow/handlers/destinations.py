#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Destination-block handlers.

A destination is a named base-frame point. The teacher pre-pins them in
the editor (camera click → MarkDestination service → world XYZ written
into the block's hidden X/Y/Z fields). At run-time the handler simply
copies those fields into ``ctx.destinations`` keyed by NAME so motion
handlers can resolve "ablegen bei A" to a coordinate.
"""

from __future__ import annotations

import math
import re
from typing import Any

from physical_ai_server.workflow.handlers.motion import WorkflowError


UNPINNED_SENTINEL = '—'

# Audit fix #17: destination names appear in log lines and the React
# editor; restrict to ASCII alphanumerics + German umlauts + space /
# underscore / hyphen, capped at 40 chars. Rejecting weird control
# characters here keeps later log-strip rendering predictable and
# prevents log-message-spoofing tricks (a `\n[FEHLER] …` injection).
_DESTINATION_NAME_RE = re.compile(r'^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$')


# The alphabet, spelled out for the student. Kept next to the regex so the two
# cannot drift.
_NAME_ALPHABET_DE = (
    'Erlaubt sind Buchstaben (auch ä ö ü ß), Ziffern, Leerzeichen, '
    'Unterstrich und Bindestrich, höchstens 40 Zeichen.'
)


def _validate_destination_name(name: str) -> None:
    """Refuse a name outside the shared alphabet, NAMING the name and the
    alphabet.

    The message used to be the bare „Ungültiger Ziel-Name." — which aborts the
    whole run and tells the student neither which of their names is wrong nor
    what would be right. That matters more than usual here because the React
    validator (``blocks/destinations.js``) has no character class of its own, so
    every one of these first surfaces at RUN time, long after the name was
    typed. Measured 2026-09-07 against the server: 'A B-c_Ä' accepted; 'A!',
    'A/B', '日本', '😀', 'A\nB', 'A]B' and a 41-character name all refused with
    that one anonymous sentence.

    The offending name is echoed with its own brackets stripped so a crafted
    name cannot smuggle a sentinel into the log strip through the error message
    — the same injection this validator exists to prevent."""
    if not name or not _DESTINATION_NAME_RE.match(name):
        shown = str(name).replace('[', '(').replace(']', ')')
        shown = ' '.join(shown.split())[:40]
        raise WorkflowError(
            f'Ungültiger Ziel-Name: „{shown}". {_NAME_ALPHABET_DE}'
        )


def _validate_coordinates(name: str, x: float, y: float, z: float) -> None:
    """Refuse a non-finite pinned coordinate.

    ``float('NaN')`` / ``float('Infinity')`` / ``float('1e400')`` all parse, and
    the handler then stored them and reported SUCCESS. Measured 2026-09-07:
    ``('NaN', 0, 0)`` → 'Ziel "A" gespeichert (nan, 0.000, 0.000).' and both
    infinity spellings → 'inf'. Reachable without a crafted payload:
    ``applyPinnedCoordinates`` writes ``Number(v).toFixed(3)`` and
    ``Number(NaN).toFixed(3) === "NaN"``, so a degenerate projection lands the
    literal string in the Blockly field. ``WorkflowManager.start()`` already
    guards its own joint seed with ``math.isfinite``; this path did not, and
    RS-24's start-time pre-check that was supposed to catch it is dead code."""
    if not all(math.isfinite(v) for v in (x, y, z)):
        raise WorkflowError(
            f'Ziel „{name}" hat keine gültigen Koordinaten — bitte den Block '
            'auswählen und noch einmal in die Szenen-Kamera klicken.'
        )


def destination_pin(ctx, args: dict[str, Any]) -> None:
    name = (args.get('name') or '').strip()
    if not name:
        raise WorkflowError('Ziel-Name fehlt.')
    # Audit fix #17: reject names containing control chars / overly
    # long strings BEFORE we touch ctx.destinations — keeps the
    # subsequent log line and the React editor's destination list
    # clean.
    _validate_destination_name(name)
    raw_x = args.get('x')
    raw_y = args.get('y')
    raw_z = args.get('z')
    # An un-pinned block carries the sentinel '—' from the Blockly
    # field. Fail loudly so the student gets pointed at the missing
    # click-to-pin step instead of the runtime silently mapping the
    # block to (0, 0, z_table) — the audit-§1.4 bug.
    if (
        raw_x is None or raw_y is None or raw_z is None
        or raw_x == UNPINNED_SENTINEL
        or raw_y == UNPINNED_SENTINEL
        or raw_z == UNPINNED_SENTINEL
    ):
        raise WorkflowError(
            f'Ziel "{name}" wurde nicht gepinnt — bitte den Block '
            'auswählen und in die Szenen-Kamera klicken.'
        )
    try:
        x = float(raw_x)
        y = float(raw_y)
        z = float(raw_z)
    except (TypeError, ValueError):
        raise WorkflowError(f'Ziel "{name}" hat ungültige Koordinaten.')
    _validate_coordinates(name, x, y, z)
    ctx.destinations[name] = {'x': x, 'y': y, 'z': z, 'label': name}
    ctx.log(f'Ziel "{name}" gespeichert ({x:.3f}, {y:.3f}, {z:.3f}).')


def destination_ref(ctx, args: dict[str, Any]) -> str:
    """VALUE block: emit a pinned destination's NAME so it can be dropped into a
    ``move_to`` / ``drop_at`` value socket (the WS3 fix — ``destination_pin`` is
    a statement and can't fill a value socket). The motion handlers'
    ``_resolve_target`` resolves the returned name string against
    ``ctx.destinations`` (teacher-pinned points loaded at workflow start)."""
    name = (args.get('name') or '').strip()
    if not name or name == UNPINNED_SENTINEL:
        raise WorkflowError('Kein Ziel ausgewählt — bitte im Block ein Ziel wählen.')
    _validate_destination_name(name)
    if name not in ctx.destinations:
        raise WorkflowError(
            f'Unbekanntes Ziel: „{name}". Bitte das Ziel zuerst in der '
            'Szenen-Kamera anklicken (pinnen).'
        )
    return name


def destination_current(ctx, args: dict[str, Any]) -> None:
    """Save the gripper's current base-frame position under NAME. This is
    useful for "lege hier ab" workflows where the teacher physically
    moves the arm to the spot and pins it. Requires a forward-kinematics
    provider on the context — when missing, raises a German error."""
    name = (args.get('name') or '').strip()
    if not name:
        raise WorkflowError('Ziel-Name fehlt.')
    # Audit fix #17: same name validation as destination_pin so both
    # paths share the canonical alphabet.
    _validate_destination_name(name)
    if not callable(getattr(ctx, 'get_current_pose_xyz', None)):
        raise WorkflowError(
            'Aktuelle Position kann nicht ermittelt werden — Vorwärts-Kinematik fehlt.'
        )
    pos = ctx.get_current_pose_xyz()
    if pos is None:
        raise WorkflowError('Aktuelle Position ist unbekannt.')
    try:
        x, y, z = (float(v) for v in pos)
    except (TypeError, ValueError):
        raise WorkflowError('Aktuelle Position ist unbekannt.')
    _validate_coordinates(name, x, y, z)
    ctx.destinations[name] = {'x': x, 'y': y, 'z': z, 'label': name}
    ctx.log(f'Ziel "{name}" auf aktuelle Position gesetzt.')
