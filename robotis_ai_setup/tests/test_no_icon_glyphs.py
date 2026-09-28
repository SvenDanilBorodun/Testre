"""No emoji or symbol glyph in any Python string a person can be shown.

Owner decision D7 (2026-09-28): the product draws icons from ONE icon style,
never glyphs in text. The React side is fenced by
``physical_ai_manager/src/components/icons/__tests__/noIconGlyphs.test.js``;
this is the Python half: every string constant (f-string parts included) of
the shipped Python — the Windows GUI, the Pi agent, the cloud API, the Jetson
agent, the Modal worker, the code runner and the ROS server package — outside
docstrings and tests. A glyph there reaches a tkinter label, a toast, a
Protokoll line or a developer console; the last is harmless but the rule is
simpler kept whole. The ranges are the React fence's; ``→`` (U+2192) and the
other typography stay allowed.

Deps-free: ``ast`` only.
"""

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "robotis_ai_setup"

# The same ranges as noIconGlyphs.test.js::BANNED_RANGES.
BANNED_RANGES = (
    (0x1F000, 0x1FAFF), (0x2300, 0x23FF), (0x25A0, 0x25FF), (0x2600, 0x27BF),
    (0x2195, 0x21FF), (0x2900, 0x297F), (0x2B00, 0x2BFF), (0x2139, 0x2139),
    (0x22EF, 0x22EF), (0xFE0F, 0xFE0F), (0x20E3, 0x20E3),
)

# root → files to scan (tests excluded). Each root must yield files.
ROOTS = {
    "gui/app": SETUP / "gui" / "app",
    "gui/main.py": SETUP / "gui" / "main.py",
    "pi_agent": SETUP / "pi_agent",
    "cloud_training_api/app": SETUP / "cloud_training_api" / "app",
    "jetson_agent": SETUP / "jetson_agent",
    "modal_training": SETUP / "modal_training",
    "docker/code_runner/runner": SETUP / "docker" / "code_runner" / "runner",
    "physical_ai_server": REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server",
}

_TEST_DIRS = {"tests", "test", "__pycache__"}


def banned_code_points(text):
    return [f"U+{ord(c):04X}" for c in text
            if any(lo <= ord(c) <= hi for lo, hi in BANNED_RANGES)]


def _is_test(path):
    return bool(_TEST_DIRS & set(path.parts)) or path.name.startswith("test_")


def python_files(root):
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.py") if not _is_test(p.relative_to(root)))


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def glyph_hits(source):
    """[(line, code points, text)] for every non-docstring string constant."""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            points = banned_code_points(node.value)
            if node.value.strip() == "\u00d7":
                points.append("a lone \u00d7")
            if points:
                hits.append((node.lineno, points, node.value[:80]))
    return hits


class TheDetectorSeesGlyphs(unittest.TestCase):

    def test_strings_fstring_parts_and_escapes_are_caught_docstrings_are_not(self):
        source = (
            '"""A docstring may say \\u25b6."""\n'
            'a = "\\u25b6 Start"\n'
            'b = f"\\u2713 {x} fertig"\n'
            'c = "\\U0001F600"\n'
            'd = "\\u00d7"\n'
            'e = "x \\u2192 y 3 \\u00d7 4 \\u2026 \\u2014"\n'
            'def f():\n'
            '    """Neither may a function docstring \\u26a0."""\n'
            '    return "\\u26a0\\ufe0f"\n'
        )
        self.assertEqual(sorted(h[0] for h in glyph_hits(source)), [2, 3, 4, 5, 9])


class NoGlyphInShippedPython(unittest.TestCase):

    def test_every_root_is_scanned_and_holds_no_glyph(self):
        violations = []
        for name, root in ROOTS.items():
            files = python_files(root)
            # A root that yields nothing (a moved directory) must fail, never pass.
            self.assertTrue(files, f"no Python files found under {name}")
            for path in files:
                for line, points, text in glyph_hits(path.read_text(encoding="utf-8")):
                    rel = path.relative_to(REPO_ROOT)
                    violations.append(f"{rel}:{line} {','.join(points)} {text!r}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
