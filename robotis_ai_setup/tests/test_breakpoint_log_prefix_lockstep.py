"""The breakpoint Protokoll line and the client's pause icon share ONE prefix.

A breakpoint hit writes ``Haltepunkt erreicht: <block id>`` from two places —
``workflow/interpreter.py::Interpreter._pause_for_breakpoint`` (Blockly) and
``workflow/code_rpc.py::RunSession._paused`` (Python) — as plain text; the
React Protokoll draws the pause icon in front of every entry that starts with
``RunControls.jsx::BREAKPOINT_LOG_PREFIX`` (owner decision D7: icons, never a
glyph in the text). If either side's wording moved alone, the icon would
silently vanish, so the three spellings are compared here (the precedent for a
test reading React source: ``test_teach_preview_velocity_floor.py``).
"""

import ast
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = (REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
            / "workflow")
RUN_CONTROLS = (REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "components"
                / "Workshop" / "RunControls.jsx")

_JS_PREFIX_RE = re.compile(r"export const BREAKPOINT_LOG_PREFIX = '([^']*)';")


def _js_prefix():
    matches = _JS_PREFIX_RE.findall(RUN_CONTROLS.read_text(encoding="utf-8"))
    assert len(matches) == 1, matches
    return matches[0]


def _breakpoint_log_prefixes(path):
    """The constant head of every ``ctx.log(f'Haltepunkt … {…}')`` in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log" and node.args):
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.JoinedStr) or not arg.values:
            continue
        head = arg.values[0]
        if isinstance(head, ast.Constant) and "Haltepunkt" in str(head.value):
            out.append(head.value)
    return out


class TheBreakpointLinePrefixIsOneString(unittest.TestCase):

    def test_both_server_writers_and_the_client_agree(self):
        prefix = _js_prefix()
        self.assertEqual(prefix, "Haltepunkt erreicht: ")
        for name in ("interpreter.py", "code_rpc.py"):
            found = _breakpoint_log_prefixes(WORKFLOW / name)
            self.assertEqual(found, [prefix], name)


if __name__ == "__main__":
    unittest.main()
