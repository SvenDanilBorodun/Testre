"""The /edubotics/hf_token_state contract between the robot and the web client.

The student's Hugging Face token is stored with the cloud account and put on the
robot by the web page. The robot answers with ONE document on a latched 1 Hz
topic (``{v, seq, accepts, present, fp, busy}``, no token in it), and the page's
reconcile decision (``features/hfToken/syncDecision.js``) is built entirely on
reading that document. The two halves live in different languages and different
containers:

  * robot:  ``physical_ai_server/data_processing/hf_token_store.py``
            (``STATE_TOPIC``, ``SCHEMA_VERSION``, ``PUBLISH_PERIOD_S``,
            ``state_payload``) and the node that publishes it
            (``physical_ai_server.py``, plus the ``/register_hf_user`` service);
  * page:   ``features/hfToken/robotChannel.js`` (``STATE_TOPIC``,
            ``SET_SERVICE``, ``STATE_KEYS``, ``STATE_SCHEMA_VERSION``,
            ``STATE_STALE_MS``).

If one side moved alone the page would not fail loudly: a renamed topic is a
subscription that never fires (the page waits out its legacy grace and then
concludes the robot is an old image), a reordered or renamed key makes
``parseState`` return null for every message (the same silence), and a stale
window shorter than the publish period flips a healthy robot to "unknown" on
every other tick. So the names, the key order, the version and the timing are
held to one table here.

The JavaScript is read as text (the precedent:
``test_record_r5_lockstep.py``, ``test_breakpoint_log_prefix_lockstep.py``), so
``robotChannel.js`` keeps each constant as a plain ``export const NAME = <literal>;``.
The robot module is stdlib-only and is loaded by path. Every file is REQUIRED:
a missing one fails loudly, it never skips (a skipped lockstep is no lockstep).
"""

import importlib.util
import pathlib
import re
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PAS = REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
STORE_PY = PAS / "data_processing" / "hf_token_store.py"
NODE_PY = PAS / "physical_ai_server.py"
CHANNEL_JS = (REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src"
              / "features" / "hfToken" / "robotChannel.js")

# Low-entropy placeholder, shaped like a token (hf_ + 34 letters). Never a real one.
_PLACEHOLDER_TOKEN = "hf_" + "a" * 34


def _require(path):
    """The zero-file floor: a missing input is a failure with the path in it."""
    if not path.is_file():
        raise AssertionError(
            f"{path.relative_to(REPO_ROOT)} is missing: the hf_token_state "
            "lockstep has nothing to compare, and a skipped lockstep is no "
            "lockstep")
    return path


def _load_store():
    path = _require(STORE_PY)
    spec = importlib.util.spec_from_file_location(
        "_hf_token_state_lockstep_store", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _js_text():
    return _require(CHANNEL_JS).read_text(encoding="utf-8")


def _js_string(text, name):
    matches = re.findall(rf"export const {name} = '([^'\\]*)';", text)
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _js_number(text, name):
    matches = re.findall(rf"export const {name} = ([0-9][0-9_]*(?:\.[0-9]+)?);", text)
    assert len(matches) == 1, (name, matches)
    return float(matches[0].replace("_", ""))


def _js_string_list(text, name):
    """``export const NAME = Object.freeze(['a', 'b', ...]);`` -> ['a', 'b', ...]"""
    matches = re.findall(
        rf"export const {name} = Object\.freeze\(\[([^\]]*)\]\);", text)
    assert len(matches) == 1, (name, matches)
    return re.findall(r"'([^'\\]*)'", matches[0])


class TheJavaScriptReaderHasTeeth(unittest.TestCase):
    """The text readers must see a change, or the lockstep below proves nothing."""

    SAMPLE = (
        "export const STATE_TOPIC = '/edubotics/hf_token_state';\n"
        "export const STATE_KEYS = Object.freeze(['v', 'seq', 'busy']);\n"
        "export const STATE_STALE_MS = 6000;\n"
    )

    def test_the_readers_return_what_is_written(self):
        self.assertEqual(_js_string(self.SAMPLE, "STATE_TOPIC"),
                         "/edubotics/hf_token_state")
        self.assertEqual(_js_string_list(self.SAMPLE, "STATE_KEYS"),
                         ["v", "seq", "busy"])
        self.assertEqual(_js_number(self.SAMPLE, "STATE_STALE_MS"), 6000.0)

    def test_a_reordered_list_or_a_renamed_topic_reads_differently(self):
        reordered = self.SAMPLE.replace("['v', 'seq', 'busy']",
                                        "['seq', 'v', 'busy']")
        self.assertNotEqual(_js_string_list(reordered, "STATE_KEYS"),
                            _js_string_list(self.SAMPLE, "STATE_KEYS"))
        renamed = self.SAMPLE.replace("hf_token_state", "hf_token_status")
        self.assertNotEqual(_js_string(renamed, "STATE_TOPIC"),
                            _js_string(self.SAMPLE, "STATE_TOPIC"))

    def test_a_constant_that_stops_being_a_plain_literal_is_loud(self):
        computed = "export const STATE_STALE_MS = 3 * 2000;\n"
        with self.assertRaises(AssertionError):
            _js_number(computed, "STATE_STALE_MS")


class TheRobotAndThePageAgreeOnTheState(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.store = _load_store()
        cls.js = _js_text()

    # -- the names -----------------------------------------------------------
    def test_the_topic_is_one_string(self):
        self.assertEqual(_js_string(self.js, "STATE_TOPIC"),
                         self.store.STATE_TOPIC)
        self.assertEqual(self.store.STATE_TOPIC, "/edubotics/hf_token_state")

    def test_the_set_service_is_the_one_the_node_registers(self):
        service = _js_string(self.js, "SET_SERVICE")
        self.assertEqual(service, "/register_hf_user")
        node = _require(NODE_PY).read_text(encoding="utf-8")
        self.assertIn(f"'{service}'", node,
                      "physical_ai_server.py no longer registers the service "
                      "the page calls")
        self.assertEqual(_js_string(self.js, "SERVICE_TYPE"),
                         "physical_ai_interfaces/srv/SetHFUser")
        self.assertIn("SetHFUser", node)

    # -- the document --------------------------------------------------------
    def _payloads(self):
        """The document in each state the robot can be in."""
        with tempfile.TemporaryDirectory() as tmp:
            slot = pathlib.Path(tmp) / "slot" / "token"
            env = {self.store.PATH_ENV: str(slot)}
            absent = self.store.state_payload(1, False, environ=env)
            slot.parent.mkdir(parents=True)
            slot.write_text(_PLACEHOLDER_TOKEN, encoding="utf-8")
            present = self.store.state_payload(2, True, environ=env)
        no_slot = self.store.state_payload(3, False, environ={})
        return {"no personal slot (Jetson image)": no_slot,
                "slot empty": absent, "slot filled": present}

    def test_the_key_list_and_order_match_in_every_state(self):
        keys = _js_string_list(self.js, "STATE_KEYS")
        self.assertEqual(keys, ["v", "seq", "accepts", "present", "fp", "busy"])
        for label, payload in self._payloads().items():
            with self.subTest(label):
                self.assertEqual(list(payload), keys)

    def test_the_encoded_document_keeps_that_order_and_stays_compact(self):
        keys = _js_string_list(self.js, "STATE_KEYS")
        for label, payload in self._payloads().items():
            with self.subTest(label):
                text = self.store.encode_payload(payload)
                self.assertNotIn(" ", text)
                positions = [text.index(f'"{k}"') for k in keys]
                self.assertEqual(positions, sorted(positions))

    def test_the_value_types_are_what_the_page_parses(self):
        fp_shape = re.search(r"FP_SHAPE = /\^\[0-9a-f\]\{16\}\$/", self.js)
        self.assertIsNotNone(fp_shape, "robotChannel.js no longer pins a 16-hex fp")
        for label, p in self._payloads().items():
            with self.subTest(label):
                self.assertIs(type(p["v"]), int)
                self.assertIs(type(p["seq"]), int)
                for flag in ("accepts", "present", "busy"):
                    self.assertIs(type(p[flag]), bool, flag)
                if p["fp"] is not None:
                    self.assertRegex(p["fp"], r"^[0-9a-f]{16}$")
        states = self._payloads()
        self.assertIsNone(states["slot empty"]["fp"])
        self.assertTrue(states["slot filled"]["present"])
        self.assertIsNotNone(states["slot filled"]["fp"])
        # A robot that takes no personal token says so and claims nothing else.
        no_slot = states["no personal slot (Jetson image)"]
        self.assertFalse(no_slot["accepts"])
        self.assertFalse(no_slot["present"])
        self.assertIsNone(no_slot["fp"])

    def test_the_schema_version_is_one_on_both_sides(self):
        self.assertEqual(_js_number(self.js, "STATE_SCHEMA_VERSION"), 1.0)
        self.assertEqual(self.store.SCHEMA_VERSION, 1)
        for payload in self._payloads().values():
            self.assertEqual(payload["v"], 1)

    # -- the timing ----------------------------------------------------------
    def test_the_stale_window_outlasts_twice_the_publish_period(self):
        """A state older than ``STATE_STALE_MS`` is treated as no state at all, so
        the window must clear two publish periods or a healthy robot flickers to
        "unknown" whenever one tick is late."""
        stale_ms = _js_number(self.js, "STATE_STALE_MS")
        period_ms = float(self.store.PUBLISH_PERIOD_S) * 1000.0
        self.assertGreater(period_ms, 0.0)
        self.assertGreater(stale_ms, 2.0 * period_ms)

    # -- the node ------------------------------------------------------------
    def test_the_node_publishes_on_the_stores_constant_not_on_a_literal(self):
        node = _require(NODE_PY).read_text(encoding="utf-8")
        self.assertIn("hf_token_store.STATE_TOPIC", node,
                      "physical_ai_server.py does not publish on "
                      "hf_token_store.STATE_TOPIC")
        for quote in ("'", '"'):
            self.assertNotIn(f"{quote}{self.store.STATE_TOPIC}{quote}", node,
                             "the topic name is hard-coded in the node: a "
                             "rename in hf_token_store.py would split the two")


if __name__ == "__main__":
    unittest.main()
