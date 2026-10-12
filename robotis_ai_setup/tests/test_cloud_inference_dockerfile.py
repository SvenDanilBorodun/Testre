"""The thin server Dockerfile ships the Cloud-Inferenz robot client (spec §5.9).

Three edits are pinned here, on the Dockerfile with its pure-comment lines removed (the
Rule §5 test's rule, so a comment that merely MENTIONS a pin can never satisfy it):

1. the interfaces block asserts the generated ``_start_cloud_inference.py`` right after
   ``_daten_command.py`` (a colcon regression that skips the new srv fails the build);
2. the runtime-deps install pins ``aiohttp==3.14.1`` and its assert line imports it (both
   server images carried aiohttp unpinned and unrequired before);
3. a CLOUD-INFERENZ gate after the node-import gate imports the client, its pinned transport
   and the generated srv. Its Python source is extracted the way BuildKit + sh + bash hand it
   to ``python3 -c`` and compiled here, so a quoting or syntax slip fails this test instead of
   the image build.

No compose, env or Pi-twin change belongs to this feature: the cloud URL is a constant, so an
unauthenticated rosbridge client cannot redirect camera frames.

Deps-free: stdlib only.
"""

import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO / "robotis_ai_setup" / "docker" / "physical_ai_server" / "Dockerfile"
_COMPOSES = (
    _REPO / "robotis_ai_setup" / "docker" / "docker-compose.yml",
    _REPO / "robotis_ai_setup" / "docker" / "docker-compose.opi.yml",
)
_PKG = _REPO / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server" / "inference"


def _code_text(text):
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


def _joined_instructions(code):
    """Each Dockerfile instruction as ONE line (backslash-newline removed, as Docker does)."""
    out, cur = [], ""
    for line in code.split("\n"):
        if re.search(r"\\[ \t]*$", line):
            cur += re.sub(r"\\[ \t]*$", "", line)
            continue
        cur += line
        if cur.strip():
            out.append(cur)
        cur = ""
    if cur.strip():
        out.append(cur)
    return out


def _cloud_gate(code):
    gates = [i for i in _joined_instructions(code) if "[CLOUD] client + srv import OK" in i]
    assert len(gates) == 1, f"expected one CLOUD gate, found {len(gates)}"
    return gates[0]


def _gate_python(instruction):
    """The script python3 receives: the bash -c "..." string after sh's double-quote
    processing (``\\"`` → ``"``), then the single-quoted python3 -c argument."""
    m = re.match(r'^RUN bash -c "(.*)"$', instruction.strip())
    assert m, instruction[:80]
    inner = m.group(1).replace('\\"', '"')
    assert "$" not in inner.split("python3 -c", 1)[1], "a $ would be expanded by sh inside the double quotes"
    m2 = re.search(r"python3 -c '(.*)'$", inner)
    assert m2, inner[-120:]
    return inner, m2.group(1)


class TheThinDockerfileShipsTheCloudClient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _DOCKERFILE.read_text(encoding="utf-8")
        cls.code = _code_text(cls.text)

    def test_the_generated_srv_is_asserted_right_after_daten_command(self):
        lines = [ln.strip() for ln in self.code.split("\n")]
        daten = lines.index('test -f "$install_root/srv/_daten_command.py" && \\')
        self.assertEqual(lines[daten + 1], 'test -f "$install_root/srv/_start_cloud_inference.py" && \\')

    def test_aiohttp_is_pinned_in_the_runtime_deps_and_imported_by_its_assert(self):
        runs = [i for i in _joined_instructions(self.code) if "opencv-contrib-python==4.10.0.84" in i]
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertRegex(run, r"\baiohttp==3\.14\.1\b")
        self.assertIn("requests==2.32.3", run)
        m = re.search(r'python3 -c "import ([^;"]+);', run)
        self.assertTrue(m, run[-200:])
        self.assertIn("aiohttp", [n.strip() for n in m.group(1).split(",")])
        # exactly one aiohttp pin in the whole file, and it is 3.14.1
        self.assertEqual(re.findall(r"aiohttp==([0-9.]+)", self.code), ["3.14.1"])

    def test_the_cloud_gate_sits_after_the_node_import_gate(self):
        node = self.code.index("physical_ai_server node module imports OK")
        gate = self.code.index("[CLOUD] client + srv import OK")
        daten = self.code.index("[DATEN] modules import")
        self.assertLess(node, gate)
        self.assertLess(gate, daten)

    def test_the_cloud_gate_sources_ros_and_the_workspace(self):
        inner, _ = _gate_python(_cloud_gate(self.code))
        self.assertTrue(inner.startswith("set -e && "))
        self.assertIn("source /opt/ros/jazzy/setup.bash && ", inner)
        self.assertIn("source /root/ros2_ws/install/setup.bash && ", inner)

    def test_the_cloud_gate_python_compiles_and_names_every_piece(self):
        _, script = _gate_python(_cloud_gate(self.code))
        compile(script, "cloud-gate", "exec")
        self.assertIn('assert aiohttp.__version__ == "3.14.1", aiohttp.__version__', script)
        self.assertIn("from physical_ai_server.inference import cloud_contract, cloud_protocol, "
                      "cloud_schedule, cloud_client, cloud_texts_de", script)
        self.assertIn("from physical_ai_interfaces.srv import StartCloudInference", script)
        self.assertIn("from std_srvs.srv import Trigger", script)
        self.assertIn('assert cloud_protocol.CLOUD_BASE_URL.startswith("https://")', script)
        self.assertNotIn("'", script)          # the argument is single-quoted for bash

    def test_a_broken_gate_would_be_caught(self):
        """Negative control: the extraction really compiles what bash would hand over."""
        broken = _cloud_gate(self.code).replace('\\"3.14.1\\"', '\\"3.14.1')
        _, script = _gate_python(broken)
        with self.assertRaises(SyntaxError):
            compile(script, "cloud-gate", "exec")

    def test_every_module_the_gate_imports_exists_in_the_package(self):
        for name in ("cloud_contract", "cloud_schedule", "cloud_client", "cloud_texts_de"):
            with self.subTest(name):
                self.assertTrue((_PKG / f"{name}.py").is_file(), name)

    def test_no_compose_forwards_anything_for_the_cloud(self):
        for path in _COMPOSES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotIn("cloud_inference", text.lower())
                self.assertNotIn("modal.direct", text)


if __name__ == "__main__":
    unittest.main()
