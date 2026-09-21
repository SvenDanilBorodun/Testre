"""The code-runner tree and its Dockerfile, as shipped (deps-free).

``robotis_ai_setup/docker/code_runner/`` is outside all seven compileall
roots and every ``ci.yml`` scan (the workflow file may not be edited for
this), so this file is the syntax gate for the shipped Python and the fence
on the two things §3.1 forbids in the tree: an ``EDUBOTICS_`` token (the
``code_runner`` compose service carries no ``environment:`` block, so a read
here would be dead code with a live-looking name) and a Dockerfile that
fetches anything but noble's ``python3``.
"""

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TREE = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner'
_RUNNER = _TREE / 'runner'
_DOCKERFILE = _TREE / 'Dockerfile'

# The exact tree the image copies to /opt/edubotics/runner. A new file is a
# deliberate addition here (image_source_parity's third kind diffs the tree).
_EXPECTED_FILES = {
    'runner_limits.py', 'supervisor.py', 'student_main.py', 'sandbox_exec.py',
    'work_helper.py', 'selftest.py',
    'lib/robot.py', 'lib/edubotics_debug.py',
    'java/edubotics/EduJson.java', 'java/edubotics/RpcClient.java',
    'java/edubotics/Robot.java', 'java/edubotics/Greifobjekt.java',
    'java/edubotics/SmokeMain.java',
}


class RunnerTree(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_RUNNER.is_dir(), _RUNNER)
        self.files = sorted(p for p in _RUNNER.rglob('*') if p.is_file()
                            and '__pycache__' not in p.parts)
        self.assertTrue(self.files, 'zero-file floor')

    def test_every_runner_py_parses_and_names_no_EDUBOTICS_token(self):
        pys = [p for p in self.files if p.suffix == '.py']
        self.assertGreaterEqual(len(pys), 8)
        for path in pys:
            ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        token = re.compile(r'EDUBOTICS_[A-Z0-9_]+')
        for path in self.files + [_DOCKERFILE]:
            text = path.read_text(encoding='utf-8')
            self.assertIsNone(token.search(text),
                              f'{path.relative_to(_TREE)} names an EDUBOTICS_* variable; the '
                              f'runner service forwards none, so the read would be dead code')

    def test_the_tree_is_exactly_the_expected_set(self):
        rel = {str(p.relative_to(_RUNNER)) for p in self.files}
        self.assertEqual(rel, _EXPECTED_FILES)

    def test_the_students_import_surface_is_the_stub_and_the_hook_only(self):
        # lib/ is what student_main puts on sys.path; nothing else may land there.
        lib = {p.name for p in (_RUNNER / 'lib').iterdir() if p.is_file()}
        self.assertEqual(lib, {'robot.py', 'edubotics_debug.py'})

    def test_no_runner_module_reads_the_environment_except_the_launcher(self):
        """The two student names are read by student_main alone; the
        supervisor SETS them and reads nothing."""
        for path in self.files:
            if path.suffix != '.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            reads = [ast.unparse(n) for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and ast.unparse(n) == 'os.environ']
            if path.name in ('student_main.py', 'robot.py'):
                self.assertTrue(reads, path.name)
            else:
                self.assertEqual(reads, [], f'{path.name} reads the environment')


class DockerfileContract(unittest.TestCase):
    def setUp(self):
        self.assertTrue(_DOCKERFILE.is_file(), _DOCKERFILE)
        self.text = _DOCKERFILE.read_text(encoding='utf-8')
        self.code = '\n'.join(line for line in self.text.splitlines()
                              if not line.lstrip().startswith('#'))

    def test_public_base(self):
        self.assertRegex(self.code, r'(?m)^FROM eclipse-temurin:21-jdk-noble\s*$')
        self.assertEqual(self.code.count('FROM '), 1)

    def test_no_pip_install_and_no_network_fetch_besides_apt_python3(self):
        for forbidden in ('pip install', 'pip3 install', 'curl ', 'wget ', 'git clone',
                          'ADD http'):
            self.assertNotIn(forbidden, self.code, forbidden)
        installs = re.findall(r'apt-get install[^\\\n]*', self.code)
        self.assertEqual(len(installs), 1, installs)
        self.assertRegex(installs[0], r'--no-install-recommends python3\s*$')

    def test_no_USER_instruction_the_compose_user_governs(self):
        self.assertNotRegex(self.code, r'^\s*USER\b', 'user: "0:0" in the compose governs')

    def test_cmd_is_the_supervisor(self):
        self.assertIn('CMD ["python3", "/opt/edubotics/runner/supervisor.py"]', self.code)
        self.assertNotIn('ENTRYPOINT', self.code)

    def test_the_build_gate_uses_hasattr_never_import_sys_monitoring(self):
        self.assertIn("hasattr(sys, 'monitoring')", self.code)
        # `self.code`, not `self.text`: the header comment names the trap.
        self.assertNotIn('import sys.monitoring', self.code)
        self.assertIn('sys.version_info >= (3, 12)', self.code)

    def test_the_student_uid_is_created_with_gid_10001(self):
        self.assertIn('groupadd -g 10001 runner', self.code)
        self.assertRegex(self.code, r'useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin runner')

    def test_the_java_tree_compiles_warning_free_and_its_smoke_runs_at_build(self):
        self.assertIn('javac -Xlint:all -Werror', self.code)
        self.assertIn("edubotics.SmokeMain | grep -q 'JAVA SMOKE OK'", self.code)
        self.assertIn('-d /opt/edubotics/runner/java-classes', self.code)

    def test_the_self_test_build_rungs_gate_the_build(self):
        self.assertIn('python3 /opt/edubotics/runner/selftest.py --build', self.code)

    def test_the_tree_lands_where_the_constants_say(self):
        self.assertIn('COPY runner/ /opt/edubotics/runner/', self.code)
        limits = (_RUNNER / 'runner_limits.py').read_text(encoding='utf-8')
        self.assertIn("RUNNER_ROOT = '/opt/edubotics/runner'", limits)
        self.assertIn("JAVA_HOME = '/opt/java/openjdk'", limits)


if __name__ == '__main__':
    unittest.main()
