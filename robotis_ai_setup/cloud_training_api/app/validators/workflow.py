"""Shared Blockly-JSON validator used by both the student
``/workflows`` router and the teacher template router.

Extracted from ``routes/workflows.py`` after the audit found that the
teacher endpoint at ``routes/teacher.py`` was inserting the workflow
without size or depth checks (audit §2.1) — every Blockly write path
must call ``validate_blockly_json`` before touching Postgres.

The size + depth caps are kept as OOM / stack-overflow protections.
The block-type allowlist was removed in the safety-stripdown so a
workflow referencing a new block doesn't get rejected at the cloud API
gate; the ROS server interprets whatever it sees.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


MAX_BLOCKLY_JSON_BYTES = 256 * 1024
MAX_BLOCKLY_DEPTH = 64
MAX_NAME_LENGTH = 100

# Roboter Studio Phase-3 Sim-Szene. Much smaller than blockly_json — it's a
# flat list of placed catalog objects ({type, tag_id, x, y, yaw}). 64 KB is
# generous (hundreds of objects) while still bounding a malicious payload
# before it reaches Postgres.
MAX_SIM_SCENE_JSON_BYTES = 64 * 1024
# Defense-in-depth object-count cap (parity with the server-side _MAX_SIM_OBJECTS):
# a ≤64 KB scene could still carry hundreds of entries; bound the count so a junk
# scene never reaches Postgres / the ROS sim resolve loop.
MAX_SIM_SCENE_OBJECTS = 64


def validate_blockly_json(payload: dict) -> None:
    """Defang malicious or runaway payloads before they hit Postgres.

    Two cheap checks: total serialised size and nested depth. Real
    semantic validation (block types, color enums, class enums, math
    ranges) runs on the ROS server when ``StartWorkflow`` is called.
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

    # Off-by-one fix: a tree of EXACTLY MAX_BLOCKLY_DEPTH levels must be
    # accepted; one level deeper must be rejected. The depth count is the
    # number of nested container hops (dict/list); a scalar at the root
    # has depth 0, one nested dict has depth 1, etc. We early-bail as
    # soon as we observe depth > MAX_BLOCKLY_DEPTH so the recursion stays
    # bounded even on adversarial input that uses non-container values
    # to skip the predicate.
    def _depth(node: Any, current: int) -> int:
        if current > MAX_BLOCKLY_DEPTH:
            return current
        if isinstance(node, dict):
            return max((_depth(v, current + 1) for v in node.values()), default=current)
        if isinstance(node, list):
            return max((_depth(v, current + 1) for v in node), default=current)
        return current

    if _depth(payload, 0) >= MAX_BLOCKLY_DEPTH + 1:
        raise HTTPException(status_code=400, detail="Workflow ist zu tief verschachtelt.")


def validate_sim_scene(scene: dict) -> None:
    """Defang a malicious or runaway Sim-Szene before it hits Postgres.

    Mirrors ``validate_blockly_json``: a JSON-object type check plus a total
    serialised-size cap. The semantic shape (objects must sit inside the IK
    annulus, tag_ids unique, etc.) is enforced by the ROS sim runtime at
    placement / run time — the cloud gate only guards size + type.
    """
    if not isinstance(scene, dict):
        raise HTTPException(
            status_code=400, detail="Sim-Szene muss ein JSON-Objekt sein."
        )
    try:
        encoded = json.dumps(scene)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Sim-Szene-JSON ist ungültig: {e}")
    if len(encoded.encode("utf-8")) > MAX_SIM_SCENE_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Sim-Szene ist zu groß (>{MAX_SIM_SCENE_JSON_BYTES // 1024} KB).",
        )
    objects = scene.get("objects")
    if objects is not None and not isinstance(objects, list):
        raise HTTPException(
            status_code=400, detail="Sim-Szene „objects“ muss eine Liste sein."
        )
    if isinstance(objects, list) and len(objects) > MAX_SIM_SCENE_OBJECTS:
        raise HTTPException(
            status_code=413,
            detail=f"Zu viele Objekte in der Sim-Szene (höchstens {MAX_SIM_SCENE_OBJECTS}).",
        )
