"""Aufnahme 2.0 round 5: the strings and the threshold the robot and the page share.

The robot ends a session for a silent source with a sentence that starts
``Aufnahme beendet: ``, re-records a take after a short gap with one that
starts ``Signalaussetzer: ``, and answers a command it cannot take right now
with ``BUSY_DE`` (``physical_ai_server/data_processing/record_texts_de.py``).
The page reads those three to draw the „Abgebrochen, verworfen" and
„Signalaussetzer, wiederholt" rows and to keep „Verwerfen und beenden" from
sending FINISH after a busy RERECORD (``features/tasks/recordSession.js``,
``components/Record/model/recordCommands.js``). If either side's wording moved
alone, those decisions would silently fall back to „Bildverlust" or to keeping
a discarded take.

The robot also ends a session when a source has been silent for
``signal_status.SOURCE_STOPPED_S``; the page's banner says „steht" after
``utils/signalStatus.js::STALLED_AFTER_S``. One number on both sides, so the
page usually shows the robot's end sentence instead of its own banner.

The JavaScript is read as text (the precedent:
``test_breakpoint_log_prefix_lockstep.py``); the two Python modules are
stdlib-only and loaded by path.
"""

import importlib.util
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PAS = REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
TEXTS_PY = PAS / "data_processing" / "record_texts_de.py"
SIGNAL_PY = PAS / "signal_status.py"
SRC = REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src"
SESSION_JS = SRC / "features" / "tasks" / "recordSession.js"
COMMANDS_JS = SRC / "components" / "Record" / "model" / "recordCommands.js"
SIGNAL_JS = SRC / "utils" / "signalStatus.js"

SHARED = ("SOURCE_STOP_PREFIX_DE", "SOURCE_GAP_PREFIX_DE", "BUSY_DE",
          # round 6: the error stop's two dataset sentences (D5/F3), the
          # upload-off notice's prefix (F4), the finalize failure's prefix
          "ERROR_STOP_SAVED_DE", "ERROR_STOP_INCOMPLETE_DE", "UPLOAD_OFF_PREFIX_DE",
          "FINALIZE_FAILED_PREFIX_DE")


def _py_constants(path):
    spec = importlib.util.spec_from_file_location(f"_r5_lockstep_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {k: v for k, v in vars(module).items() if k.isupper()}


def _js_const(path, name):
    """A string constant, also when written as concatenated literals."""
    text = path.read_text(encoding="utf-8")
    matches = re.findall(rf"export const {name} = ((?:'[^'\\]*'\s*\+?\s*)+);", text)
    assert len(matches) == 1, (path.name, name, matches)
    return "".join(re.findall(r"'([^'\\]*)'", matches[0]))


def _js_number(path, name):
    text = path.read_text(encoding="utf-8")
    matches = re.findall(rf"export const {name} = ([0-9.]+);", text)
    assert len(matches) == 1, (path.name, name, matches)
    return float(matches[0])


class TheSharedSentencesAreByteEqual(unittest.TestCase):

    def test_prefixes_and_busy(self):
        py = _py_constants(TEXTS_PY)
        for name in SHARED:
            self.assertIn(name, py, name)
            self.assertEqual(_js_const(SESSION_JS, name), py[name], name)
        self.assertEqual(py["SOURCE_STOP_PREFIX_DE"], "Aufnahme beendet: ")
        self.assertEqual(py["SOURCE_GAP_PREFIX_DE"], "Signalaussetzer: ")
        self.assertEqual(
            py["BUSY_DE"],
            "Die Aufnahme ist gerade beschäftigt. Bitte versuch es gleich noch einmal.")

    def test_the_page_has_one_copy_of_busy(self):
        # recordCommands imports the session's constant instead of restating it
        commands = COMMANDS_JS.read_text(encoding="utf-8")
        self.assertIn("import { BUSY_DE } from '../../../features/tasks/recordSession';", commands)
        self.assertNotIn("beschäftigt", commands)

    def test_the_c7_end_reads_as_a_source_stop(self):
        # „Episode n konnte auch nach zwei Wiederholungen nicht …" (C7) ends the session like a
        # silent source: the page must draw the same „Abgebrochen, verworfen" row
        py = _py_constants(TEXTS_PY)
        self.assertTrue(py["FRAME_LOSS_END_DE"].startswith(py["SOURCE_STOP_PREFIX_DE"]))
        self.assertTrue(py["GAP_KEPT_DE"].startswith(py["SOURCE_GAP_PREFIX_DE"]))


class OneStalledThreshold(unittest.TestCase):

    def test_source_stopped_equals_the_pages_stalled(self):
        py = _py_constants(SIGNAL_PY)
        self.assertEqual(py["SOURCE_STOPPED_S"], 2.0)
        self.assertEqual(_js_number(SIGNAL_JS, "STALLED_AFTER_S"), py["SOURCE_STOPPED_S"])


if __name__ == "__main__":
    unittest.main()
