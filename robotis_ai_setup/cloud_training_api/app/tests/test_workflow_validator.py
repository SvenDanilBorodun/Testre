"""Tests for the Blockly JSON validator.

Covers the size + depth gates that remain after the safety stripdown.
The block-type allowlist was removed so any block the editor produces
is accepted at the cloud gate; the ROS server interprets it.

Stubs FastAPI's HTTPException without pulling the full FastAPI
dependency at test time, mirroring the rest of the suite's pattern.
"""

from __future__ import annotations

import json
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
    CODE_LANGUAGES,
    MAX_BLOCKLY_DEPTH,
    MAX_BLOCKLY_JSON_BYTES,
    MAX_CODE_FILE_BYTES,
    MAX_CODE_FILES,
    MAX_CODE_PROJECT_BYTES,
    validate_blockly_json,
    validate_code_files,
    validate_code_language,
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
        for _ in range(MAX_BLOCKLY_DEPTH + 5):
            new = {"type": "edubotics_log", "child": node}
            node = new
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            validate_blockly_json({"root": node})
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_block_accepted(self) -> None:
        """After the stripdown, unknown block types are accepted at the
        cloud gate — the ROS server is the only judge of block types."""
        novel = {"blocks": {"blocks": [{"type": "novel_block_type"}]}}
        validate_blockly_json(novel)


def _refusal(fn, *args):
    """Run ``fn(*args)`` expecting the German HTTPException; return it."""
    from fastapi import HTTPException  # picks up our stub

    try:
        fn(*args)
    except HTTPException as exc:
        return exc
    raise AssertionError("expected a refusal")


class TestCodeLanguageValidator(unittest.TestCase):
    """``validate_code_language``: exactly the two languages the server's
    ``code_program.CODE_LANGUAGES`` routes on, nothing else."""

    def test_the_two_languages_pass_and_are_returned(self) -> None:
        self.assertEqual(CODE_LANGUAGES, ("python", "java"))
        for language in CODE_LANGUAGES:
            self.assertEqual(validate_code_language(language), language)

    def test_a_third_language_is_refused(self) -> None:
        self.assertEqual(_refusal(validate_code_language, "javascript").status_code, 400)

    def test_the_blockly_marker_is_not_a_code_language(self) -> None:
        # '' is the column default meaning „a Blockly program“; it is never a
        # language a code document may declare.
        self.assertEqual(_refusal(validate_code_language, "").status_code, 400)

    def test_case_and_type_are_strict(self) -> None:
        self.assertEqual(_refusal(validate_code_language, "Python").status_code, 400)
        self.assertEqual(_refusal(validate_code_language, None).status_code, 400)
        self.assertEqual(_refusal(validate_code_language, ["python"]).status_code, 400)


class TestCodeFilesValidator(unittest.TestCase):
    """``validate_code_files``: the cloud twin of the server's
    ``CodeProgram.from_payload`` refusals (§3.7 caps) — a count cap, a per-file
    cap and a TOTAL-PROJECT cap, all measured in UTF-8 BYTES."""

    def _python(self, **extra) -> dict:
        files = {"main.py": "import robot\nrobot.home()\n"}
        files.update(extra)
        return files

    def test_minimal_python_project_passes_and_is_returned(self) -> None:
        files = self._python()
        self.assertEqual(validate_code_files(files, "python"), files)

    def test_minimal_java_project_passes(self) -> None:
        files = {"Main.java": "public class Main { public static void main(String[] a) {} }"}
        self.assertEqual(validate_code_files(files, "java"), files)

    def test_nested_paths_pass(self) -> None:
        files = self._python(**{"lib/util/helpers.py": "def f():\n    return 1\n"})
        validate_code_files(files, "python")

    def test_not_an_object_rejected(self) -> None:
        exc = _refusal(validate_code_files, [("main.py", "x")], "python")
        self.assertEqual(exc.status_code, 400)

    def test_empty_project_rejected(self) -> None:
        self.assertEqual(_refusal(validate_code_files, {}, "python").status_code, 400)
        self.assertEqual(_refusal(validate_code_files, None, "python").status_code, 400)

    def test_file_count_cap(self) -> None:
        at_cap = self._python(**{f"m{i}.py": "x = 1\n" for i in range(MAX_CODE_FILES - 1)})
        self.assertEqual(len(at_cap), MAX_CODE_FILES)
        validate_code_files(at_cap, "python")
        over = dict(at_cap, **{"one_more.py": "x = 2\n"})
        self.assertEqual(len(over), MAX_CODE_FILES + 1)
        self.assertEqual(_refusal(validate_code_files, over, "python").status_code, 413)

    def test_per_file_cap_is_utf8_bytes_not_characters(self) -> None:
        # 'ä' is one character and TWO UTF-8 bytes: half the cap in characters
        # is exactly the cap in bytes and passes; one more byte is refused.
        umlauts = "ä" * (MAX_CODE_FILE_BYTES // 2)
        self.assertEqual(len(umlauts.encode("utf-8")), MAX_CODE_FILE_BYTES)
        validate_code_files(self._python(**{"big.py": umlauts}), "python")
        exc = _refusal(validate_code_files, self._python(**{"big.py": umlauts + "a"}), "python")
        self.assertEqual(exc.status_code, 413)
        ascii_over = "a" * (MAX_CODE_FILE_BYTES + 1)
        exc = _refusal(validate_code_files, self._python(**{"big.py": ascii_over}), "python")
        self.assertEqual(exc.status_code, 413)

    def test_project_cap_binds_when_every_file_is_under_its_own_cap(self) -> None:
        # Three files each comfortably under the per-file cap, together over
        # the project cap: only a TOTAL-PROJECT cap can refuse this (B11).
        chunk = "x" * (MAX_CODE_FILE_BYTES - 1024)
        files = self._python(**{"a.py": chunk, "b.py": chunk, "c.py": chunk})
        rendered = json.dumps(files, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(rendered), MAX_CODE_PROJECT_BYTES)
        self.assertEqual(_refusal(validate_code_files, files, "python").status_code, 413)

    def test_project_cap_measures_ensure_ascii_false(self) -> None:
        # Under ensure_ascii=True every 'ä' renders as the six ASCII characters
        # `ä` and this project would be ~3x over the cap; measured the way
        # the server and the control frame measure (ensure_ascii=False) it is
        # under the cap and must pass.
        content = "ä" * 30000
        files = self._python(**{"a.py": content, "b.py": content})
        escaped = json.dumps(files, ensure_ascii=True).encode("utf-8")
        raw = json.dumps(files, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(escaped), MAX_CODE_PROJECT_BYTES)
        self.assertLess(len(raw), MAX_CODE_PROJECT_BYTES)
        validate_code_files(files, "python")

    def test_dotdot_and_absolute_paths_rejected(self) -> None:
        for bad in ("../main.py", "lib/../main.py", "/main.py", "lib//x.py", "a b.py", "x.PY"):
            exc = _refusal(validate_code_files, self._python(**{bad: "x = 1\n"}), "python")
            self.assertEqual(exc.status_code, 400, bad)

    def test_reserved_stems_rejected(self) -> None:
        for bad in ("robot.py", "lib/robot.py", "edubotics_rpc.py", "Edubotics.py"):
            exc = _refusal(validate_code_files, self._python(**{bad: "x = 1\n"}), "python")
            self.assertEqual(exc.status_code, 400, bad)
        exc = _refusal(validate_code_files,
                       {"Main.java": "class Main {}", "EduboticsX.java": "class EduboticsX {}"},
                       "java")
        self.assertEqual(exc.status_code, 400)
        # The stem rule is case-sensitive, exactly like the server's: a root
        # `Robot.java` sits in the default package and cannot shadow the shipped
        # `edubotics.Robot`, so the server accepts it and the cloud must too.
        validate_code_files({"Main.java": "class Main {}", "Robot.java": "class Robot {}"}, "java")

    def test_missing_entry_file_rejected(self) -> None:
        exc = _refusal(validate_code_files, {"lib.py": "x = 1\n"}, "python")
        self.assertEqual(exc.status_code, 400)
        exc = _refusal(validate_code_files, {"Helper.java": "class Helper {}"}, "java")
        self.assertEqual(exc.status_code, 400)
        # The entry file must sit at the ROOT, not in a directory.
        exc = _refusal(validate_code_files, {"src/main.py": "x = 1\n"}, "python")
        self.assertEqual(exc.status_code, 400)

    def test_extension_must_match_the_language(self) -> None:
        exc = _refusal(validate_code_files, self._python(**{"Helper.java": "class Helper {}"}), "python")
        self.assertEqual(exc.status_code, 400)
        exc = _refusal(validate_code_files, {"Main.java": "class Main {}", "util.py": "x"}, "java")
        self.assertEqual(exc.status_code, 400)

    def test_non_string_path_or_content_rejected(self) -> None:
        self.assertEqual(_refusal(validate_code_files, {"main.py": 5}, "python").status_code, 400)
        self.assertEqual(_refusal(validate_code_files, {"main.py": None}, "python").status_code, 400)
        self.assertEqual(_refusal(validate_code_files, {5: "x"}, "python").status_code, 400)

    def test_an_unknown_language_is_refused_before_the_files_are_read(self) -> None:
        exc = _refusal(validate_code_files, self._python(), "javascript")
        self.assertEqual(exc.status_code, 400)

    def test_every_refusal_carries_a_detail(self) -> None:
        cases = (
            ({}, "python"),
            (self._python(**{"../x.py": "x"}), "python"),
            (self._python(**{"robot.py": "x"}), "python"),
            ({"lib.py": "x"}, "python"),
            (self._python(**{"a" * (MAX_CODE_FILE_BYTES + 1): "x"}), "python"),
        )
        for files, language in cases:
            exc = _refusal(validate_code_files, files, language)
            self.assertIsInstance(exc.detail, str)
            self.assertTrue(exc.detail.strip())


if __name__ == "__main__":
    unittest.main()
