"""The per-student Hugging Face token reaches the robot through a FILE (042).

The Startseite pushes the signed-in student's own token over /register_hf_user;
the node writes it atomically to ``$HF_TOKEN_PATH`` on a tmpfs, and
``huggingface_hub`` re-reads that file on every call, so no consumer passes a
token. That only works while compose keeps three promises, fenced here on the
student compose, the Orange-Pi compose and the byte-equal twin that actually
reaches a fielded Pi:

  * ``physical_ai_server`` sets ``HF_TOKEN_PATH=/run/edubotics-hf/token`` and
    mounts ONE tmpfs there (``size=1m,mode=0700``);
  * no compose sets ``HF_TOKEN``, ``HUGGING_FACE_HUB_TOKEN`` or
    ``HF_HUB_DISABLE_IMPLICIT_TOKEN`` anywhere (an environment token OUTRANKS
    the file and would silence every personal token) — the Jetson compose keeps
    its classroom ``HF_TOKEN=${EDUBOTICS_HF_TOKEN}`` and gets no path;
  * the ``code_runner`` sandbox (and every other service) never sees the slot.

The thin Dockerfile carries a build-time gate that pins the ``HF_TOKEN_PATH``
contract of whatever ``huggingface_hub`` the base ships. Its Python source is
passed to ``python3 -c "..."`` through backslash-continued ``RUN`` lines, so a
continuation line that keeps an indent makes it an ``IndentationError`` at BUILD
time (audit M1). This file compiles the script out of the Dockerfile text and
proves, with negative controls, that the check would catch that.

Deliberately stdlib-only (no PyYAML): the parsing is the line-shape parser of
``test_code_runner_sandbox.py``.
"""

import filecmp
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STUDENT = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.yml"
_GPU = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.gpu.yml"
_OPI = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.opi.yml"
_TWIN = _REPO_ROOT / "robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml"
_JETSON = _REPO_ROOT / "robotis_ai_setup/jetson_agent/docker-compose.jetson.yml"
_DOCKERFILE = _REPO_ROOT / "robotis_ai_setup/docker/physical_ai_server/Dockerfile"
_STORE_PY = (_REPO_ROOT / "physical_ai_tools/physical_ai_server/physical_ai_server"
             / "data_processing/hf_token_store.py")

_SLOT_PATH = "/run/edubotics-hf/token"
_SLOT_DIR = "/run/edubotics-hf"
_TMPFS = "/run/edubotics-hf:size=1m,mode=0700"

# (label, compose) — the three that carry the student stack.
_STACKS = (("student", _STUDENT), ("opi", _OPI), ("twin", _TWIN))
_NO_JETSON = (_STUDENT, _GPU, _OPI, _TWIN)

_GATE_RUN = "RUN env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN HF_TOKEN_PATH="


def _service_block(compose_path, service):
    """The lines of one compose service block (the origin gate's parser)."""
    lines = compose_path.read_text(encoding="utf-8").splitlines()
    out, inside = [], False
    for line in lines:
        if re.match(r"^  %s:\s*$" % re.escape(service), line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith("   ") and not line.startswith("  #"):
                break
            out.append(line)
    return out


def _service_names(compose_path):
    text = compose_path.read_text(encoding="utf-8")
    m = re.search(r"^services:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    assert m, f"{compose_path}: no services: section"
    return re.findall(r"^  ([A-Za-z0-9_]+):\s*$", m.group(1), re.M)


def _code_lines(block):
    return [line for line in block if line.strip() and not line.strip().startswith("#")]


def _strip_inline_comment(value):
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _items(block, key):
    """The `- item` entries under a `    key:` list, until the next key."""
    items, inside = [], False
    for line in _code_lines(block):
        if re.match(r"^    %s:\s*$" % re.escape(key), line):
            inside = True
            continue
        if inside:
            if re.match(r"^    \S", line):
                break
            m = re.match(r"^\s+-\s+(.*?)\s*$", line)
            if m:
                items.append(_strip_inline_comment(m.group(1)).strip('"').strip("'"))
    return items


def _code_text(compose_path):
    """The compose file with comment-only lines and trailing comments removed."""
    out = []
    for line in compose_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(_strip_inline_comment(line) if "#" in line else line.strip())
    return "\n".join(out)


# ── the compose contract ─────────────────────────────────────────────────────

class TestServerSlot(unittest.TestCase):
    def test_every_stack_sets_the_path_and_mounts_exactly_one_tmpfs(self):
        for label, path in _STACKS:
            with self.subTest(compose=label):
                block = _service_block(path, "physical_ai_server")
                self.assertTrue(block, f"{label}: no physical_ai_server block")
                env = _items(block, "environment")
                self.assertIn(f"HF_TOKEN_PATH={_SLOT_PATH}", env)
                self.assertEqual(sum(1 for e in env if e.startswith("HF_TOKEN_PATH")), 1)
                self.assertEqual(_items(block, "tmpfs"), [_TMPFS])

    def test_the_compose_path_is_what_the_node_accepts_and_the_tmpfs_is_its_directory(self):
        spec = importlib.util.spec_from_file_location("_hf_compose_store", _STORE_PY)
        store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store)
        self.assertTrue(store.accepts({store.PATH_ENV: _SLOT_PATH}))
        self.assertEqual(store.PATH_ENV, "HF_TOKEN_PATH")
        self.assertEqual(os.path.dirname(_SLOT_PATH), _SLOT_DIR)
        self.assertEqual(_TMPFS.split(":")[0], _SLOT_DIR)

    def test_the_tmpfs_is_private_and_small(self):
        self.assertIn("mode=0700", _TMPFS)
        self.assertIn("size=1m", _TMPFS)

    def test_the_two_opi_composes_are_byte_equal(self):
        # pi-compose-twin-guard in CI: a drifted twin makes the agent renew the
        # WRONG file while its own `config -q` gate proves it VALID, never CURRENT.
        self.assertTrue(filecmp.cmp(_OPI, _TWIN, shallow=False))


class TestNoEnvironmentTokenAnywhere(unittest.TestCase):
    """An environment token outranks the file, so none may exist."""

    _FORBIDDEN = (
        re.compile(r"(?<![A-Z0-9_])HF_TOKEN(?![A-Z0-9_])"),
        re.compile(r"HUGGING_FACE_HUB_TOKEN"),
        re.compile(r"HF_HUB_DISABLE_IMPLICIT_TOKEN"),
    )

    def test_no_student_compose_sets_one_in_code(self):
        for path in _NO_JETSON:
            text = _code_text(path)
            self.assertTrue(text.strip(), path)
            for pattern in self._FORBIDDEN:
                with self.subTest(compose=path.name, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_the_jetson_keeps_exactly_its_classroom_token_and_no_slot(self):
        text = _code_text(_JETSON)
        hits = re.findall(r"(?<![A-Z0-9_])HF_TOKEN(?![A-Z0-9_])[^\n]*", text)
        self.assertEqual(hits, ["HF_TOKEN=${EDUBOTICS_HF_TOKEN}"] * len(hits))
        self.assertGreaterEqual(len(hits), 1)
        self.assertNotIn("HF_TOKEN_PATH", text)
        self.assertNotIn("/run/edubotics-hf", text)
        self.assertNotIn("HUGGING_FACE_HUB_TOKEN", text)
        self.assertNotIn("HF_HUB_DISABLE_IMPLICIT_TOKEN", text)


class TestNothingElseSeesTheSlot(unittest.TestCase):
    def test_only_physical_ai_server_mentions_the_slot_in_code(self):
        for label, path in _STACKS:
            names = _service_names(path)
            self.assertIn("physical_ai_server", names)
            self.assertIn("code_runner", names)
            for service in names:
                block = _code_lines(_service_block(path, service))
                text = "\n".join(block)
                with self.subTest(compose=label, service=service):
                    if service == "physical_ai_server":
                        self.assertIn("/run/edubotics-hf", text)
                    else:
                        self.assertNotIn("/run/edubotics-hf", text)
                        self.assertNotIn("HF_TOKEN", text)

    def test_the_code_runner_block_never_names_the_slot_even_in_a_comment(self):
        for label, path in _STACKS:
            block = "\n".join(_service_block(path, "code_runner"))
            with self.subTest(compose=label):
                self.assertTrue(block.strip())
                self.assertNotIn("edubotics-hf", block)
                self.assertNotIn("HF_TOKEN", block)

    def test_the_code_runner_keeps_its_own_two_tmpfs_and_nothing_else(self):
        # test_code_runner_sandbox.py fences the exact set; this fences the slot's absence.
        for label, path in _STACKS:
            tmpfs = _items(_service_block(path, "code_runner"), "tmpfs")
            with self.subTest(compose=label):
                self.assertEqual(len(tmpfs), 2)
                self.assertTrue(all(not t.startswith("/run/") for t in tmpfs), tmpfs)


# ── the Dockerfile gate ──────────────────────────────────────────────────────

def _gate_script(text):
    """The Python source the gate's `python3 -c "..."` receives, built the way
    BuildKit joins a backslash-continued RUN (the backslash and the newline go,
    the next line's leading whitespace stays). Compiled, so a syntax or
    indentation error in the shipped text fails here instead of at build time."""
    lines = text.split("\n")
    i = next(k for k, line in enumerate(lines) if line.startswith(_GATE_RUN))
    joined = lines[i]
    while re.search(r"\\[ \t]*$", joined):
        i += 1
        joined = re.sub(r"\\[ \t]*$", "", joined) + lines[i]
    m = re.search(r'python3 -c "(.*)"$', joined)
    assert m, "the gate RUN no longer ends with python3 -c \"...\""
    return compile(m.group(1), "hf-gate", "exec"), m.group(1)


class TestDockerfileGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _DOCKERFILE.read_text(encoding="utf-8")

    def test_the_gate_is_present_and_compiles(self):
        code, source = _gate_script(self.text)
        self.assertIsNotNone(code)
        self.assertIn("HF_TOKEN_PATH gate OK", source)
        self.assertIn("HF_HUB_DISABLE_IMPLICIT_TOKEN", source)
        self.assertIn("os.remove(p)", source)

    def test_the_run_and_every_continuation_line_start_in_column_zero(self):
        lines = self.text.split("\n")
        i = next(k for k, line in enumerate(lines) if line.startswith(_GATE_RUN))
        while re.search(r"\\[ \t]*$", lines[i]):
            i += 1
            self.assertFalse(lines[i][:1].isspace(), f"indented continuation: {lines[i][:40]!r}")
        # the first script line directly follows `python3 -c "\`
        self.assertTrue(lines[next(k for k, line in enumerate(lines)
                                   if line.startswith(_GATE_RUN)) + 1].startswith("import os, huggingface_hub; "))

    def test_an_indented_variant_would_be_caught(self):
        """Negative controls: the compile check must FAIL on the two ways the
        text could be pasted wrong."""
        lines = self.text.split("\n")
        start = next(k for k, line in enumerate(lines) if line.startswith(_GATE_RUN))
        end = start
        while re.search(r"\\[ \t]*$", lines[end]):
            end += 1
        all_indented = lines[:start + 1] + ["  " + l for l in lines[start + 1:end + 1]] + lines[end + 1:]
        first_only = (lines[:start + 1] + ["  " + lines[start + 1]] + lines[start + 2:])
        for label, variant in (("all continuation lines", all_indented), ("first line", first_only)):
            with self.subTest(indented=label):
                with self.assertRaises(SyntaxError):  # IndentationError is a SyntaxError
                    _gate_script("\n".join(variant))

    def test_the_gate_sits_after_the_cv_bridge_gate_and_before_the_env_scrub(self):
        decode = self.text.index("cv_bridge functional decode gate OK")
        gate = self.text.index("HF_TOKEN_PATH gate OK")
        scrub = self.text.index("# Scrub upstream-base ENV leaks")
        self.assertLess(decode, gate)
        self.assertLess(gate, scrub)

    def test_it_unsets_the_environment_tokens_for_the_probe(self):
        self.assertIn("env -u HF_TOKEN -u HUGGING_FACE_HUB_TOKEN", self.text)

    def test_the_dockerfile_sets_no_environment_token_either(self):
        code = "\n".join(l for l in self.text.split("\n") if not l.strip().startswith("#"))
        # whole ENV instructions, backslash continuations included
        env_instructions = re.findall(r"^ENV\b(?:[^\n]*\\\n)*[^\n]*", code, re.M)
        self.assertTrue(env_instructions, "the thin Dockerfile has ENV instructions")
        for instruction in env_instructions:
            for pattern in TestNoEnvironmentTokenAnywhere._FORBIDDEN:
                self.assertIsNone(pattern.search(instruction), instruction[:60])

    @unittest.skipUnless(importlib.util.find_spec("huggingface_hub"),
                         "huggingface_hub is not installed in this environment")
    def test_the_gate_really_passes_against_the_installed_huggingface_hub(self):
        _code, source = _gate_script(self.text)
        with tempfile.TemporaryDirectory() as tmp:
            slot = os.path.join(tmp, "gate-token")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
                                "HF_HUB_DISABLE_IMPLICIT_TOKEN", "HF_HOME", "HF_OIDC_RESOURCE")}
            env["HF_TOKEN_PATH"] = slot
            env["HOME"] = tmp
            script = source.replace("/tmp/edubotics-hf-gate-token", slot)
            result = subprocess.run([sys.executable, "-c", script], env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-800:])
            self.assertIn("[HF-TOKEN] HF_TOKEN_PATH gate OK", result.stdout)
            self.assertFalse(os.path.exists(slot))  # the gate cleans up after itself


if __name__ == "__main__":
    unittest.main()
