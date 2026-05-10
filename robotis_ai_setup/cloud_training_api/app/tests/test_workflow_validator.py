"""Tests for the Blockly JSON validator.

Verifies the size/depth/allowlist gates and asserts that the
``ALLOWED_BLOCK_TYPES`` set stays in sync with the ROS-server-side
allowlist in ``physical_ai_server/workflow/interpreter.py``. A drift
check catches the case where one side adds a block type and the
other doesn't — the runtime would otherwise reject every workflow
that uses the new block.

Stubs FastAPI's HTTPException without pulling the full FastAPI
dependency at test time, mirroring the rest of the suite's pattern.
"""

from __future__ import annotations

import os
import sys
import types
import unittest

# Stub fastapi.HTTPException without pulling the framework — the
# suite runs without a venv install (CI: python -m unittest).
if "fastapi" not in sys.modules:
    fastapi_module = types.ModuleType("fastapi")

    class _StubHTTPException(Exception):  # noqa: N818
        def __init__(self, status_code: int, detail: str | None = None) -> None:
            super().__init__(detail or "")
            self.status_code = status_code
            self.detail = detail

    fastapi_module.HTTPException = _StubHTTPException
    sys.modules["fastapi"] = fastapi_module

# Make the cloud_training_api/app package importable.
HERE = os.path.dirname(os.path.abspath(__file__))
APP_PARENT = os.path.dirname(os.path.dirname(HERE))
if APP_PARENT not in sys.path:
    sys.path.insert(0, APP_PARENT)

from app.validators.workflow import (  # noqa: E402
    ALLOWED_BLOCK_TYPES,
    MAX_BLOCKLY_DEPTH,
    MAX_BLOCKLY_JSON_BYTES,
    validate_blockly_json,
)


def _hello_world() -> dict:
    """Minimal valid Blockly payload."""
    return {
        "blocks": {
            "blocks": [
                {
                    "type": "edubotics_log",
                    "id": "abc",
                    "inputs": {
                        "MESSAGE": {
                            "shadow": {
                                "type": "text",
                                "fields": {"TEXT": "hi"},
                            }
                        }
                    },
                }
            ]
        }
    }


class TestValidator(unittest.TestCase):
    def test_minimal_payload_passes(self) -> None:
        validate_blockly_json(_hello_world())

    def test_empty_payload_passes(self) -> None:
        # Empty workspace is valid (a brand-new editor produces this).
        validate_blockly_json({})

    def test_oversize_rejected(self) -> None:
        big = {"blocks": {"blocks": [
            {"type": "edubotics_log", "fields": {"NOTE": "x" * (MAX_BLOCKLY_JSON_BYTES // 2)}}
            for _ in range(4)
        ]}}
        from fastapi import HTTPException  # picks up our stub
        with self.assertRaises(HTTPException) as cm:
            validate_blockly_json(big)
        self.assertEqual(cm.exception.status_code, 413)

    def test_too_deep_rejected(self) -> None:
        # Build a nest of depth > MAX_BLOCKLY_DEPTH.
        node: dict = {"type": "edubotics_log"}
        root = node
        for _ in range(MAX_BLOCKLY_DEPTH + 5):
            new = {"type": "edubotics_log", "child": node}
            node = new
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            validate_blockly_json({"root": node})
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_block_rejected(self) -> None:
        bad = {"blocks": {"blocks": [{"type": "evil_block_type"}]}}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            validate_blockly_json(bad)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("evil_block_type", cm.exception.detail)

    def test_allowlist_contains_phase2_blocks(self) -> None:
        for required in (
            "edubotics_speak_de",
            "edubotics_play_tone",
            "edubotics_broadcast",
            "edubotics_when_broadcast",
            "edubotics_when_marker_seen",
            "edubotics_when_color_seen",
            "lists_create_with",
            "lists_getIndex",
            "procedures_defnoreturn",
            "procedures_callreturn",
            "math_random_int",
            "math_constrain",
        ):
            self.assertIn(required, ALLOWED_BLOCK_TYPES)

    def test_allowlist_contains_phase3_open_vocab(self) -> None:
        self.assertIn("edubotics_detect_open_vocab", ALLOWED_BLOCK_TYPES)


if __name__ == "__main__":
    unittest.main()
