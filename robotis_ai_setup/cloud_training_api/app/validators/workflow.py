"""Shared Blockly-JSON validator used by both the student
``/workflows`` router and the teacher template router.

Extracted from ``routes/workflows.py`` after the audit found that the
teacher endpoint at ``routes/teacher.py`` was inserting the workflow
without size or depth checks (audit §2.1) — every Blockly write path
must call ``validate_blockly_json`` before touching Postgres.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


MAX_BLOCKLY_JSON_BYTES = 256 * 1024
MAX_BLOCKLY_DEPTH = 64
MAX_NAME_LENGTH = 100


def validate_blockly_json(payload: dict) -> None:
    """Defang malicious or runaway payloads before they hit Postgres.

    Two cheap checks: total serialised size and nested depth. Real
    semantic validation runs on the ROS server when StartWorkflow is
    called (block-type allowlist, color/class enums, etc.) — this
    layer just keeps the database from eating a gigabyte string.
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
