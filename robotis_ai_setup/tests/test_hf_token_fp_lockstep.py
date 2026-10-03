"""The cloud API and the robot agree on what a Hugging Face token IS (042).

The student's token is validated and encrypted by the cloud API
(``cloud_training_api/app/services/hf_credentials.py``) and written to the
robot's tmpfs slot by ``physical_ai_server/data_processing/hf_token_store.py``.
The Startseite decides "push / clear / noop" by comparing the fingerprint the
cloud stores with the fingerprint the robot derives from its file; if the two
``fingerprint()`` functions or the two shape rules ever drifted, every token
would look "different" forever and the reconcile loop would re-push it on every
tick (or, worse, accept a token one side calls malformed).

Both modules are stdlib-only at import time (``cryptography`` and
``huggingface_hub`` are imported lazily inside the cloud functions that need
them) and are loaded BY PATH, so this deps-free test needs neither. One vector
table, run against both. The fingerprint algorithm is
``sha256("edubotics-hf-token-fp:" + token)`` truncated to 16 hex characters.
"""

import hashlib
import importlib.util
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CLOUD_PY = (REPO_ROOT / "robotis_ai_setup" / "cloud_training_api" / "app" / "services"
            / "hf_credentials.py")
STORE_PY = (REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
            / "data_processing" / "hf_token_store.py")

# (token, fingerprint). Low-entropy on purpose: the secret scan reads this file.
FINGERPRINTS = (
    ("hf_" + "a" * 34, "c1770a7966b0771e"),
    ("hf_" + "abcdefghijklmnopqrstuvwxyzABCDEFGH", "006da5aabd66bbc9"),
    ("hf_0123456789_-0123456789", "ce20c2c58e1061d5"),
    ("hf_" + "Z" * 16, "c287328277ed770f"),
    ("hf_" + "x" * 256, "40d885025c3959d6"),
)

SHAPES = (
    ("hf_" + "a" * 15, False),
    ("hf_" + "a" * 16, True),
    ("hf_" + "a" * 34, True),
    ("hf_" + "a" * 256, True),
    ("hf_" + "a" * 257, False),
    ("hf_" + "a" * 34 + "\n", False),   # `$` would accept this; fullmatch must not
    ("hf_" + "a" * 34 + "\r\n", False),
    (" " + "hf_" + "a" * 34, False),
    ("hf_" + "a" * 34 + " ", False),
    ("HF_" + "a" * 34, False),
    ("hf-" + "a" * 34, False),
    ("hf_" + "a" * 20 + " " + "a" * 20, False),
    ("hf_" + "é" * 20, False),
    ("", False),
    ("hf_", False),
    (5, False),
    (None, False),
    (b"hf_" + b"a" * 34, False),
    (["hf_" + "a" * 34], False),
)


def _load(path, name):
    assert path.is_file(), f"{path} is missing: the lockstep would pass vacuously"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestFingerprintAndShapeLockstep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = _load(CLOUD_PY, "_hf_lockstep_cloud")
        cls.robot = _load(STORE_PY, "_hf_lockstep_robot")

    def test_both_sides_exist(self):
        self.assertTrue(CLOUD_PY.is_file())
        self.assertTrue(STORE_PY.is_file())

    def test_the_fingerprint_vectors_agree_on_both_sides(self):
        for token, expected in FINGERPRINTS:
            self.assertEqual(self.cloud.fingerprint(token), expected, token[:12])
            self.assertEqual(self.robot.fingerprint(token), expected, token[:12])

    def test_the_vectors_are_the_documented_algorithm(self):
        for token, expected in FINGERPRINTS:
            digest = hashlib.sha256(b"edubotics-hf-token-fp:" + token.encode("utf-8"))
            self.assertEqual(digest.hexdigest()[:16], expected)

    def test_the_domain_prefix_is_the_same_constant_on_both_sides(self):
        self.assertEqual(self.cloud.FP_DOMAIN, b"edubotics-hf-token-fp:")
        self.assertEqual(self.robot.FP_DOMAIN, b"edubotics-hf-token-fp:")

    def test_the_shape_rule_agrees_on_both_sides(self):
        for value, expected in SHAPES:
            self.assertIs(self.cloud.valid_shape(value), expected, repr(value)[:40])
            self.assertIs(self.robot.valid_shape(value), expected, repr(value)[:40])

    def test_the_shape_pattern_is_the_same_text(self):
        self.assertEqual(self.cloud.TOKEN_SHAPE.pattern, self.robot.TOKEN_SHAPE.pattern)
        self.assertEqual(self.cloud.TOKEN_SHAPE.pattern, r"hf_[A-Za-z0-9_-]{16,256}")

    def test_neither_side_anchors_with_a_dollar_sign(self):
        # `re.match(r"^...$", s)` accepts a trailing newline; both sides use fullmatch.
        for path in (CLOUD_PY, STORE_PY):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"TOKEN_SHAPE\s*=\s*re\.compile\(r?['\"]\^", text), path.name)
            self.assertIn("TOKEN_SHAPE.fullmatch", text, path.name)

    def test_a_token_the_robot_reads_back_has_the_fingerprint_the_cloud_stored(self):
        # The reconcile loop's whole premise, end to end through the real functions.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            slot = pathlib.Path(tmp) / "slot" / "token"
            for token, expected in FINGERPRINTS:
                self.robot.write(token, str(slot))
                read_back = self.robot.read(str(slot))
                self.assertEqual(read_back, token)
                self.assertEqual(self.robot.fingerprint(read_back), self.cloud.fingerprint(token))
                self.assertEqual(self.cloud.fingerprint(token), expected)


if __name__ == "__main__":
    unittest.main()
