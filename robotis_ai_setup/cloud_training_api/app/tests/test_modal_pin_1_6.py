"""Cloud-Inferenz needs `modal.Server` and `Server.sessions` (Modal ≥ 1.6, D36).

  * requirements.txt pins `modal>=1.6,<2` (the training dispatch calls keep
    their signatures under 1.6.1; proved in spec §4.3);
  * the cloud API's `modal` importers are exactly `services/modal_client.py`
    (training) and `services/inference_edge.py` (inference), and the latter
    imports it inside functions only, so the route tests stub nothing.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
REQUIREMENTS = os.path.join(os.path.dirname(APP), "requirements.txt")


def _modal_imports(path: str):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    top, inner = [], []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        if any(n == "modal" or n.startswith("modal.") for n in names):
            (top if node in tree.body else inner).append(node)
    return top, inner


class TestModalPin(unittest.TestCase):
    def test_modal_pin_1_6(self) -> None:
        with open(REQUIREMENTS, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
        modal_lines = [ln for ln in lines if re.match(r"modal\b", ln)]
        self.assertEqual(modal_lines, ["modal>=1.6,<2"])

    def test_the_two_modal_importers(self) -> None:
        importers = {}
        for root, _dirs, files in os.walk(APP):
            if os.path.basename(root) == "tests":
                continue
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    top, inner = _modal_imports(path)
                    if top or inner:
                        importers[os.path.relpath(path, APP)] = (len(top), len(inner))
        self.assertEqual(set(importers), {os.path.join("services", "modal_client.py"),
                                          os.path.join("services", "inference_edge.py")})
        top, inner = importers[os.path.join("services", "inference_edge.py")]
        self.assertEqual(top, 0)
        self.assertGreaterEqual(inner, 1)


if __name__ == "__main__":
    unittest.main()
