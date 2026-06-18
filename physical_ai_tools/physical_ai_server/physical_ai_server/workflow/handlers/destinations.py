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


def _validate_destination_name(name: str) -> None:
    if not name or not _DESTINATION_NAME_RE.match(name):
        raise WorkflowError('Ungültiger Ziel-Name.')


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
    x, y, z = pos
    ctx.destinations[name] = {'x': float(x), 'y': float(y), 'z': float(z), 'label': name}
    ctx.log(f'Ziel "{name}" auf aktuelle Position gesetzt.')
