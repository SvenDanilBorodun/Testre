#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower calibration's state, as the robot publishes it and the page reads it.

The calibration service (``workflow/autocal/service.py``, spec 40 S4) publishes
``/edubotics/calibration_state`` — ``{"v": 1, "state", "code", "message_de",
…}`` — and answers ``/calibration/start``'s five steps. Roboter Studio's
calibration panel (``components/Workshop/calibration/``) chooses its view from
the state, its buttons from the code, and shows a sentence for every code even
when a message is missing. If either side renamed a state, a code, a step or
the topic alone, the panel would silently show the wrong view (or none), and a
sentence edited on one side only would tell the student two different things.

So, byte for byte: the topic and the version; the five steps; the seven
states; every code the robot can put into the state (``texts_de.BY_CODE`` and
the service's own) with its German sentence, the format fields included; the
target-removed button; the way-home button's label as the robot's refusal
names it. The JavaScript is read as text (the precedent:
``test_record_r5_lockstep.py``); ``texts_de.py`` is stdlib-only and loaded by
path, ``service.py`` (it imports NumPy) is read as source.
"""

import ast
import importlib.util
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AUTOCAL = REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server" / "workflow" / "autocal"
TEXTS_PY = AUTOCAL / "texts_de.py"
SERVICE_PY = AUTOCAL / "service.py"
CALIB_JS = (REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "components" / "Workshop"
            / "calibration")
COPY_JS = CALIB_JS / "calibrationCopy.js"
MODEL_JS = CALIB_JS / "calibrationModel.js"
HOOK_JS = CALIB_JS / "useCalibrationState.js"

# The codes the service writes itself (beside texts_de.BY_CODE) and their sentences.
SERVICE_CODES = {
    "arm_check_running": "ARM_CHECK_RUNNING_DE",
    "confirm_target_removed": "CONFIRM_TARGET_REMOVED_DE",
    "check_failed": "CHECK_FAILED_DE",
    "handheld_running": "HANDHELD_RUNNING_DE",
    "handheld_done": "HANDHELD_DONE_DE",
    "handheld_failed": "HANDHELD_FAILED_DE",
}
STATES = ("ok", "blocked", "needs_arm_check", "arm_check_running", "confirm_target_removed",
          "handheld_running", "unknown")


def _texts():
    spec = importlib.util.spec_from_file_location("_calib_lockstep_texts_de", TEXTS_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _service():
    return ast.parse(SERVICE_PY.read_text(encoding="utf-8"))


def _service_const(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in service.py")


def _js_text(path):
    assert path.is_file(), f"{path} is missing"
    return path.read_text(encoding="utf-8")


def _js_string(literal):
    """One JS expression of single-quoted literals joined by ``+``."""
    return "".join(re.findall(r"'([^'\\]*)'", literal))


def _js_const_string(path, name):
    matches = re.findall(rf"export const {name} = ((?:'[^'\\]*'\s*\+?\s*)+);", _js_text(path))
    assert len(matches) == 1, (path.name, name, matches)
    return _js_string(matches[0])


def _js_frozen_object(path, name):
    """``export const NAME = Object.freeze({ key: 'text', 'a/b': 'x' + 'y', … });`` as a dict."""
    text = _js_text(path)
    match = re.search(rf"export const {name} = Object\.freeze\(\{{(.*?)\n\}}\);", text, re.S)
    assert match, (path.name, name)
    body = match.group(1)
    entries = re.findall(r"^\s*(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_]*)):\s*((?:'[^'\\]*'\s*\+?\s*)+),?\s*$",
                         body, re.M)
    out = {}
    for quoted, bare, literal in entries:
        key = quoted or bare
        assert key not in out, key
        out[key] = _js_string(literal)
    # Every non-blank, non-comment line of the body was read: nothing hides.
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("//")]
    joined = sum(1 for ln in lines if re.match(r"^\s*(?:'[^']+'|[A-Za-z_][A-Za-z0-9_]*):", ln))
    assert joined == len(out), (name, joined, len(out))
    return out


def _js_frozen_list(path, name):
    match = re.search(rf"export const {name} = Object\.freeze\(\[(.*?)\]\);", _js_text(path), re.S)
    assert match, (path.name, name)
    return re.findall(r"'([^'\\]*)'", match.group(1))


class TheTopicTheVersionAndTheSteps(unittest.TestCase):

    def test_the_topic(self):
        self.assertEqual(_js_const_string(HOOK_JS, "CALIBRATION_STATE_TOPIC"),
                         _service_const(_service(), "STATE_TOPIC"))
        self.assertEqual(_service_const(_service(), "STATE_TOPIC"), "/edubotics/calibration_state")

    def test_the_version(self):
        match = re.findall(r"export const CALIBRATION_STATE_VERSION = (\d+);", _js_text(MODEL_JS))
        self.assertEqual(match, [str(_service_const(_service(), "STATE_VERSION"))])

    def test_the_five_steps(self):
        # 'home' (X-TARGETSTART): the panel's way to the Grundstellung, where the arm check starts.
        steps = _service_const(_service(), "STEPS")
        self.assertEqual(steps, ("mat_check", "arm_check", "target_removed", "handheld", "home"))
        js = _js_frozen_object(MODEL_JS, "CALIBRATION_STEPS")
        self.assertEqual(sorted(js.values()), sorted(steps))


class TheStates(unittest.TestCase):

    def test_every_state_the_service_writes_is_one_of_the_seven(self):
        source = SERVICE_PY.read_text(encoding="utf-8")
        written = set(re.findall(r"_set_state\(\s*'([a-z_]+)'", source))
        written |= set(re.findall(r"_state_dict\(\s*'([a-z_]+)'", source))
        written |= set(re.findall(r"^\s+state = '([a-z_]+)'$", source, re.M))
        self.assertEqual(written, set(STATES))

    def test_the_page_knows_exactly_the_seven(self):
        self.assertEqual(_js_frozen_list(MODEL_JS, "CALIBRATION_STATES"), list(STATES))


class TheCodesAndTheirSentences(unittest.TestCase):

    def test_every_code_the_service_writes_has_a_sentence_on_both_sides(self):
        texts = _texts()
        source = SERVICE_PY.read_text(encoding="utf-8")
        codes = set(re.findall(r"_set_state\(\s*'[a-z_]+',\s*'([a-z_/]+)'", source))
        codes.discard("unknown")
        self.assertLessEqual(codes, set(texts.BY_CODE) | set(SERVICE_CODES))
        self.assertIn("check_failed", source)

    def test_every_sentence_is_byte_equal(self):
        texts = _texts()
        expected = dict(texts.BY_CODE)
        for code, name in SERVICE_CODES.items():
            expected[code] = getattr(texts, name)
        js = _js_frozen_object(COPY_JS, "CALIBRATION_SENTENCES_DE")
        self.assertEqual(sorted(js), sorted(expected))
        for code, sentence in expected.items():
            self.assertEqual(js[code], sentence, code)

    def test_the_format_fields_are_the_robots(self):
        js = _js_frozen_object(COPY_JS, "CALIBRATION_SENTENCES_DE")
        self.assertIn("{mm}", js["tower_displaced"])
        self.assertIn("{k}", js["too_few_poses"])
        self.assertIn("{n}", js["too_few_poses"])
        self.assertIn("{i}", js["arm_check_running"])
        self.assertIn("{k}", js["handheld_running"])

    def test_the_droop_warning_and_the_target_button(self):
        texts = _texts()
        self.assertEqual(_js_const_string(COPY_JS, "DROOP_WARNING_DE"), texts.DROOP_WARNING_DE)
        self.assertEqual(_js_const_string(COPY_JS, "TARGET_REMOVED_BUTTON_DE"), texts.TARGET_REMOVED_BUTTON_DE)

    def test_the_way_home_button_is_the_one_the_refusal_names(self):
        texts = _texts()
        labels = re.findall(r"^\s*ARM_CHECK_HOME: '([^'\\]*)',$", _js_text(COPY_JS), re.M)
        self.assertEqual(labels, ["Arm in die Grundstellung fahren"])
        self.assertIn(f"„{labels[0]}“", texts.ARM_CHECK_NEEDS_HOME_DE)

    def test_the_sentences_keep_their_literal_umlauts(self):
        js = _js_frozen_object(COPY_JS, "CALIBRATION_SENTENCES_DE")
        joined = " ".join(js.values())
        for letter in ("ä", "ö", "ü", "„", "“"):
            self.assertIn(letter, joined, letter)

if __name__ == "__main__":
    unittest.main()
