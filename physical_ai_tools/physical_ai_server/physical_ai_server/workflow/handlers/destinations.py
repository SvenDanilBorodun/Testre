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

from typing import Any

from physical_ai_server.workflow.handlers.motion import WorkflowError


def destination_pin(ctx, args: dict[str, Any]) -> None:
    name = (args.get('name') or '').strip()
    if not name:
        raise WorkflowError('Ziel-Name fehlt.')
    try:
        x = float(args.get('x', 0.0))
        y = float(args.get('y', 0.0))
        z = float(args.get('z', ctx.z_table or 0.0))
    except (TypeError, ValueError):
        raise WorkflowError(f'Ziel "{name}" hat ungültige Koordinaten.')
    ctx.destinations[name] = {'x': x, 'y': y, 'z': z, 'label': name}
    ctx.log(f'Ziel "{name}" gespeichert ({x:.3f}, {y:.3f}, {z:.3f}).')


def destination_current(ctx, args: dict[str, Any]) -> None:
    """Save the gripper's current base-frame position under NAME. This is
    useful for "lege hier ab" workflows where the teacher physically
    moves the arm to the spot and pins it. Requires a forward-kinematics
    provider on the context — when missing, raises a German error."""
    name = (args.get('name') or '').strip()
    if not name:
        raise WorkflowError('Ziel-Name fehlt.')
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
