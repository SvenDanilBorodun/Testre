"""The code_runner compose service is a sandbox only as a WHOLE (2026-09-21).

A student's Python or Java program runs for real inside the `code_runner`
container beside physical_ai_server. Its supervisor is root with three
capabilities so it can spawn the student as another uid and kill it; six
compose keys with no precedent in this repo are what keep that root from
being root of anything else, and their security value exists only in
combination — `cap_drop: ALL` is theatre without `no-new-privileges`, a
read-only rootfs without `network_mode: none` still exfiltrates, an ipc
volume is fine only while nothing else is mounted. That is why they are
fenced in ONE file (decision B6, the `test_rosbridge_origin_gate.py` shape),
on the student compose, the opi compose AND the byte-equal twin that
actually reaches a fielded Pi.

Deliberately stdlib-only (no PyYAML) so this keeps riding the deps-free
`robotis_ai_setup` suite; the parsing is the line-shape parser the origin
gate uses, which reads exactly what a human reads.
"""

import ast
import filecmp
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_STUDENT = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.yml"
_OPI = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.opi.yml"
_TWIN = _REPO_ROOT / "robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml"
_GPU = _REPO_ROOT / "robotis_ai_setup/docker/docker-compose.gpu.yml"
_JETSON = _REPO_ROOT / "robotis_ai_setup/jetson_agent/docker-compose.jetson.yml"
_RUNNER_LIMITS = _REPO_ROOT / "robotis_ai_setup/docker/code_runner/runner/runner_limits.py"

# (label, compose, the image short name the service must reference)
_COMPOSES = (
    ("student", _STUDENT, "code-runner"),
    ("opi", _OPI, "code-runner-opi"),
    ("twin", _TWIN, "code-runner-opi"),
)
_EVERY_COMPOSE = (_STUDENT, _GPU, _OPI, _TWIN, _JETSON)

_SERVICE = "code_runner"
_IPC_VOLUME = "code_runner_ipc"
_IPC_MOUNT = "/run/edubotics/code"


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


def _code_lines(block):
    return [line for line in block if line.strip() and not line.strip().startswith("#")]


def _strip_inline_comment(value):
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _scalar(block, key):
    """The value of a `    key: value` line at the service's own indent."""
    for line in _code_lines(block):
        m = re.match(r"^    %s:\s*(.*?)\s*$" % re.escape(key), line)
        if m:
            return _strip_inline_comment(m.group(1))
    return None


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


def _top_level_volumes(compose_path):
    text = compose_path.read_text(encoding="utf-8")
    m = re.search(r"^volumes:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    if not m:
        return []
    return re.findall(r"^  ([A-Za-z0-9_]+):", m.group(1), re.M)


class _EveryCompose(unittest.TestCase):
    """Runs each assertion over the student, opi and twin blocks."""

    def blocks(self):
        for label, path, image in _COMPOSES:
            self.assertTrue(path.is_file(), path)
            block = _service_block(path, _SERVICE)
            # Zero-block floor: an absent service would make every
            # assertion below vacuous after a rename.
            self.assertTrue(block, f"{label}: no `{_SERVICE}` service block")
            yield label, path, image, block


class TheServiceExists(_EveryCompose):
    def test_service_exists_on_student_opi_and_twin(self):
        for label, _path, image, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_scalar(block, "container_name"), _SERVICE)
                self.assertEqual(
                    _scalar(block, "image"),
                    "${REGISTRY:-ghcr.io/svendanilborodun}/%s:${IMAGE_TAG:-latest}" % image,
                    f"{label}: the runner image ref must resolve the way the "
                    f"other three do (registry + tag from the .env)")

    def test_the_twin_is_byte_equal_to_the_opi_compose(self):
        # ci.yml::pi-compose-twin-guard's `cmp -s`, asserted here too: the
        # twin is the copy that reaches a Pi (agent.py::_renew_compose).
        self.assertTrue(filecmp.cmp(_OPI, _TWIN, shallow=False),
                        "pi_agent/docker/docker-compose.opi.yml drifted from the opi compose")


class TheSandboxKeys(_EveryCompose):
    def test_network_mode_none(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_scalar(block, "network_mode"), '"none"',
                                 "the RPC rides the ipc volume, never a port; a network "
                                 "would let student code reach the LAN")
                self.assertIsNone(_scalar(block, "networks"))
                self.assertEqual(_items(block, "networks"), [])

    def test_read_only_rootfs(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_scalar(block, "read_only"), "true")

    def test_cap_drop_all_and_exactly_setuid_setgid_kill(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_items(block, "cap_drop"), ["ALL"])
                self.assertEqual(sorted(_items(block, "cap_add")), ["KILL", "SETGID", "SETUID"],
                                 "the supervisor needs exactly Popen(user=) + killpg/uid "
                                 "sweep; DAC_OVERRIDE or SYS_ADMIN here is a root shell")

    def test_no_new_privileges(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertIn("no-new-privileges:true", _items(block, "security_opt"))

    def test_tmpfs_carries_size(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                entries = _items(block, "tmpfs")
                self.assertTrue(entries, "no tmpfs: a read-only rootfs has nowhere for a run")
                targets = {e.split(":", 1)[0] for e in entries}
                self.assertEqual(targets, {"/work", "/tmp"})
                for entry in entries:
                    self.assertRegex(entry, r":.*\bsize=\d+[kmg]?\b",
                                     f"{entry!r}: an unbounded tmpfs lets a student fill "
                                     f"the host's RAM")
                    self.assertIn("mode=1777", entry,
                                  f"{entry!r}: uid 10001 must be able to create its run dir")

    def test_supervisor_is_uid_0_and_not_pid_1(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_scalar(block, "user"), '"0:0"',
                                 "a non-root supervisor gets no effective capabilities and "
                                 "cannot spawn as another uid (P23)")
                self.assertEqual(_scalar(block, "init"), "true",
                                 "tini must be PID 1 so the supervisor is not (A7.4) and "
                                 "so a student's orphans are reaped")

    def test_limits_present(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertRegex(_scalar(block, "mem_limit") or "", r"^\d+[mg]$")
                self.assertRegex(_scalar(block, "pids_limit") or "", r"^\d+$")
                self.assertRegex(_scalar(block, "cpus") or "", r"^\d+(\.\d+)?$")

    def test_no_environment_block_no_ports_no_dev_no_hf_token(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                code = "\n".join(_code_lines(block))
                self.assertIsNone(_scalar(block, "environment"),
                                  "no environment: compose-env-parity then sees {} on both "
                                  "sides, and nothing in the runner tree reads one")
                self.assertNotIn("environment", code)
                self.assertIsNone(_scalar(block, "ports"))
                self.assertNotIn("/dev", code)
                self.assertNotIn("HF_TOKEN", code)
                self.assertNotIn("privileged", code)
                self.assertNotIn("depends_on", code)

    def test_restart_is_no(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertEqual(_scalar(block, "restart"), '"no"',
                                 "decision A15: the runner stays restart: \"no\" like every "
                                 "other student-stack service")

    def test_the_healthcheck_probes_the_control_socket(self):
        for label, _p, _i, block in self.blocks():
            with self.subTest(compose=label):
                self.assertIn('["CMD", "test", "-S", "%s/runner.sock"]' % _IPC_MOUNT,
                              "\n".join(_code_lines(block)))


class TheIpcVolume(_EveryCompose):
    def test_ipc_volume_is_mounted_on_both_services_and_declared(self):
        for label, path, _i, block in self.blocks():
            with self.subTest(compose=label):
                mount = f"{_IPC_VOLUME}:{_IPC_MOUNT}"
                self.assertEqual(_items(block, "volumes"), [mount],
                                 "the ipc volume is the runner's ONLY mount")
                server = _service_block(path, "physical_ai_server")
                self.assertTrue(server, f"{label}: physical_ai_server block not found")
                self.assertIn(mount, _items(server, "volumes"),
                              "the server binds rpc.sock there and connects to runner.sock")
                self.assertIn(_IPC_VOLUME, _top_level_volumes(path))
                self.assertNotIn(_SERVICE, _items(server, "depends_on"))

    def test_the_mount_path_is_the_runners_ipc_dir(self):
        tree = ast.parse(_RUNNER_LIMITS.read_text(encoding="utf-8"))
        consts = {n.targets[0].id: n.value.value for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                  and isinstance(n.targets[0], ast.Name)}
        self.assertEqual(consts["IPC_DIR"], _IPC_MOUNT)
        self.assertEqual(consts["CONTROL_SOCKET_NAME"], "runner.sock")

    def test_no_service_anywhere_mounts_the_docker_socket(self):
        for path in _EVERY_COMPOSE:
            with self.subTest(compose=path.name):
                self.assertTrue(path.is_file(), path)
                self.assertNotIn("docker.sock", path.read_text(encoding="utf-8"),
                                 "a docker socket mount is root on the host; the runner "
                                 "must never see one, and no other service needs one")


class TheWholeSandboxHoldsTogether(_EveryCompose):
    """The one assertion the six keys are worth: all of them, at once."""

    def test_every_sandbox_property_holds_on_every_compose(self):
        for label, path, _i, block in self.blocks():
            with self.subTest(compose=label):
                code = "\n".join(_code_lines(block))
                holds = {
                    "network_mode none": _scalar(block, "network_mode") == '"none"',
                    "read_only": _scalar(block, "read_only") == "true",
                    "cap_drop ALL": _items(block, "cap_drop") == ["ALL"],
                    "cap_add exact": sorted(_items(block, "cap_add")) == ["KILL", "SETGID", "SETUID"],
                    "no-new-privileges": "no-new-privileges:true" in _items(block, "security_opt"),
                    "tmpfs sized": bool(_items(block, "tmpfs")) and all(
                        "size=" in e for e in _items(block, "tmpfs")),
                    "user 0:0": _scalar(block, "user") == '"0:0"',
                    "init": _scalar(block, "init") == "true",
                    "mem_limit": _scalar(block, "mem_limit") is not None,
                    "pids_limit": _scalar(block, "pids_limit") is not None,
                    "cpus": _scalar(block, "cpus") is not None,
                    "restart no": _scalar(block, "restart") == '"no"',
                    "no environment": "environment" not in code,
                    "no ports": _scalar(block, "ports") is None,
                    "no /dev": "/dev" not in code,
                    "no HF_TOKEN": "HF_TOKEN" not in code,
                    "ipc volume only": _items(block, "volumes") == [f"{_IPC_VOLUME}:{_IPC_MOUNT}"],
                    "no docker.sock": "docker.sock" not in path.read_text(encoding="utf-8"),
                }
                self.assertEqual([k for k, v in holds.items() if not v], [],
                                 f"{label}: the sandbox is only a sandbox with ALL of these")


if __name__ == "__main__":
    unittest.main()
