"""Shared Blockly-JSON validator used by both the student
``/workflows`` router and the teacher template router.

Extracted from ``routes/workflows.py`` after the audit found that the
teacher endpoint at ``routes/teacher.py`` was inserting the workflow
without size or depth checks (audit §2.1) — every Blockly write path
must call ``validate_blockly_json`` before touching Postgres.

The block-type allowlist is mirrored from
``physical_ai_server/workflow/interpreter.py:ALLOWED_BLOCK_TYPES`` so a
malformed payload can be rejected at the cloud API gate rather than
making it all the way to the ROS server.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


MAX_BLOCKLY_JSON_BYTES = 256 * 1024
MAX_BLOCKLY_DEPTH = 64
MAX_NAME_LENGTH = 100


# MUST stay in sync with
# robotis_ai_setup/docker/physical_ai_server/overlays/workflow/interpreter.py
# :ALLOWED_BLOCK_TYPES. Drift is detectable via the unit test in
# cloud_training_api/app/tests/test_workflow_validator.py.
ALLOWED_BLOCK_TYPES: frozenset[str] = frozenset({
    # Motion / output / destinations
    "edubotics_home",
    "edubotics_open_gripper",
    "edubotics_close_gripper",
    "edubotics_move_to",
    "edubotics_pickup",
    "edubotics_drop_at",
    "edubotics_wait_seconds",
    "edubotics_destination_pin",
    "edubotics_destination_current",
    "edubotics_log",
    "edubotics_play_sound",
    "edubotics_speak_de",
    "edubotics_play_tone",
    # Events
    "edubotics_broadcast",
    "edubotics_when_broadcast",
    "edubotics_when_marker_seen",
    "edubotics_when_color_seen",
    # Perception
    "edubotics_detect_color",
    "edubotics_wait_until_color",
    "edubotics_count_color",
    "edubotics_detect_marker",
    "edubotics_wait_until_marker",
    "edubotics_detect_object",
    "edubotics_wait_until_object",
    "edubotics_count_objects_class",
    "edubotics_detect_open_vocab",
    # Logic + variables (Blockly built-ins)
    "controls_if",
    "controls_repeat_ext",
    "controls_whileUntil",
    "controls_for",
    "controls_forEach",
    "logic_compare",
    "logic_operation",
    "logic_negate",
    "logic_boolean",
    "math_number",
    "math_arithmetic",
    "math_random_int",
    "math_constrain",
    "math_modulo",
    "math_round",
    "variables_get",
    "variables_set",
    "text",
    # Lists
    "lists_create_with",
    "lists_repeat",
    "lists_length",
    "lists_isEmpty",
    "lists_indexOf",
    "lists_getIndex",
    "lists_setIndex",
    "lists_getSublist",
    # Procedures
    "procedures_defnoreturn",
    "procedures_defreturn",
    "procedures_callnoreturn",
    "procedures_callreturn",
    "procedures_ifreturn",
})


def _walk_block_types(payload: Any, found: set[str]) -> None:
    """Best-effort recursive walk to collect every ``type`` key in the
    payload. Blockly serialisation puts blocks under
    ``payload["blocks"]["blocks"][...]`` plus nested ``inputs``
    /``next``/``shadow`` entries — we walk arbitrary dict/list
    structures defensively because authoring tools sometimes emit
    slightly different shapes.
    """
    if isinstance(payload, dict):
        btype = payload.get("type")
        if isinstance(btype, str):
            found.add(btype)
        for value in payload.values():
            _walk_block_types(value, found)
    elif isinstance(payload, list):
        for item in payload:
            _walk_block_types(item, found)


def validate_blockly_json(payload: dict) -> None:
    """Defang malicious or runaway payloads before they hit Postgres.

    Three cheap checks: total serialised size, nested depth, and a
    block-type allowlist mirrored from the ROS server. Real semantic
    validation (color enums, class enums, math ranges) runs on the ROS
    server when ``StartWorkflow`` is called.
    """
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Workflow-JSON ist ungültig: {e}")
    if len(encoded.encode("utf-8")) > MAX_BLOCKLY_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workflow ist zu groß (>{MAX_BLOCKLY_JSON_BYTES // 1024} KB).",
        )

    def _depth(node: Any, current: int) -> int:
        if current > MAX_BLOCKLY_DEPTH:
            return current
        if isinstance(node, dict):
            return max((_depth(v, current + 1) for v in node.values()), default=current)
        if isinstance(node, list):
            return max((_depth(v, current + 1) for v in node), default=current)
        return current

    if _depth(payload, 0) > MAX_BLOCKLY_DEPTH:
        raise HTTPException(status_code=400, detail="Workflow ist zu tief verschachtelt.")

    found: set[str] = set()
    _walk_block_types(payload, found)
    bad = found - ALLOWED_BLOCK_TYPES
    if bad:
        # Sort for deterministic error strings (matters for tests).
        sample = ", ".join(sorted(bad)[:5])
        raise HTTPException(
            status_code=400,
            detail=f"Unbekannte Block-Typen: {sample}",
        )
