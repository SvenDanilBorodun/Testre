"""The token sentences for a busy robot name EVERY cause of busy (review e,
2026-10-04).

The node refuses a token change while ``physical_ai_server.py::_hf_token_busy``
is true: a recording, or the HF worker uploading, downloading or fetching a
dataset/model list (``hf_api_worker.HfApiWorker.is_busy``). Five sentences
explain that refusal in four places: the node's two answers
(``record_texts_de.HF_TOKEN_BUSY_DE`` / ``HF_TOKEN_CLEAR_QUEUED_DE``), the
Windows GUI's Protokoll line after a closed window (``docker_manager.
HF_CLEAR_REFUSED_DE``), the Startseite card (``hfTokenCopy.js``
``card.waitingNote``) and the Aufnahme page's start block (``recordCopy.js``
``problem.hfToken.busy``). They used to say „lädt gerade etwas hoch oder nimmt
auf", which is false for a download or a list fetch. This test holds all five
to the four causes, and the two sentences about a refused CLEAR to what the
node now does with it (it removes the token by itself once idle).

Stdlib only: the Python constants are read with ``ast`` (``docker_manager``
pulls in GUI modules), the JavaScript as text.
"""

import ast
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEXTS_PY = (REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
            / "data_processing" / "record_texts_de.py")
DOCKER_MANAGER_PY = REPO_ROOT / "robotis_ai_setup" / "gui" / "app" / "docker_manager.py"
SRC = REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src"
HF_COPY_JS = SRC / "features" / "hfToken" / "hfTokenCopy.js"
RECORD_COPY_JS = SRC / "components" / "Record" / "model" / "recordCopy.js"

# The four causes, each as a pattern a German sentence can carry in either
# word order (main clause „nimmt … auf", subordinate clause „aufnimmt").
CAUSES = {
    "recording": r"\bnimmt auf\b|\baufnimmt\b",
    "upload": r"\bhoch\b|\bhoch-",
    "download": r"\bherunter",
    "list fetch": r"\bListe\b",
}


def _py_constant(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name}: {name} not found")


def _js_entry(path, key):
    """A string value written as one literal or as literals joined with `+`."""
    text = path.read_text(encoding="utf-8")
    matches = re.findall(rf"(?:'{re.escape(key)}'|\b{re.escape(key)}):\s*((?:'[^'\\]*'\s*\+?\s*)+),", text)
    assert len(matches) == 1, (path.name, key, matches)
    return "".join(re.findall(r"'([^'\\]*)'", matches[0]))


def _sentences():
    return {
        "HF_TOKEN_BUSY_DE": _py_constant(TEXTS_PY, "HF_TOKEN_BUSY_DE"),
        "HF_TOKEN_CLEAR_QUEUED_DE": _py_constant(TEXTS_PY, "HF_TOKEN_CLEAR_QUEUED_DE"),
        "HF_CLEAR_REFUSED_DE": _py_constant(DOCKER_MANAGER_PY, "HF_CLEAR_REFUSED_DE"),
        "card.waitingNote": _js_entry(HF_COPY_JS, "card.waitingNote"),
        "problem.hfToken.busy": _js_entry(RECORD_COPY_JS, "busy"),
    }


class EveryBusySentenceNamesEveryCause(unittest.TestCase):

    def test_all_four_causes_in_all_five_sentences(self):
        for name, text in _sentences().items():
            for cause, pattern in CAUSES.items():
                with self.subTest(sentence=name, cause=cause):
                    self.assertRegex(text, pattern)

    def test_the_old_upload_only_wording_is_gone(self):
        for name, text in _sentences().items():
            with self.subTest(sentence=name):
                self.assertNotIn("lädt gerade etwas hoch oder nimmt auf", text)
                self.assertNotIn("Übertragung zu oder von Hugging Face", text)


class ARefusedClearIsDescribedAsTheNodeHandlesIt(unittest.TestCase):
    """Since 2026-10-04 the node remembers a clear it refused while busy and
    applies it on the first idle tick (_apply_pending_hf_token_clear)."""

    def test_both_say_the_robot_removes_it_by_itself(self):
        sentences = _sentences()
        for name in ("HF_TOKEN_CLEAR_QUEUED_DE", "HF_CLEAR_REFUSED_DE"):
            with self.subTest(sentence=name):
                self.assertIn("von selbst", sentences[name])

    def test_the_gui_no_longer_waits_for_the_next_login(self):
        text = _sentences()["HF_CLEAR_REFUSED_DE"]
        self.assertNotIn("nächsten Anmelden", text)
        # the fallback stays true: the slot is a tmpfs of the server container
        self.assertIn("Stoppen der Umgebung", text)


if __name__ == "__main__":
    unittest.main()
