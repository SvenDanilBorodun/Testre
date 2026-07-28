"""Fixture tests for `.github/scripts/pi_shipped_compose_relpath.py`.

That script is the shared derivation behind two gates — `ci.yml::pi-compose-twin-guard`
(source tree) and `release.yml::pi-agent-tarball` (the built artifact) — which
fence the opi compose twin the Pi agent repairs a fielded rig ONTO. Both used to
restate the path as a shell literal; deriving it from
`pi_agent/constants.py::SHIPPED_COMPOSE_RELPATH` is what makes them assert the
path `agent.py::_shipped_compose_path` actually reads.

The failure mode the tests below exist for is the one the ps_* guards taught:
a guard that cannot fail is worse than no guard. If this script ever answered
with a default, a guessed path, or silently exited 0 on a constant it could not
parse, both gates would go green while fencing a file nothing reads — and the
twin, whose whole reason to exist is that nothing downstream can catch a stale
copy, would ship unchecked.

Deps-free and cross-platform: plain Python run via sys.executable, fed on stdin.
"""

import pathlib
import subprocess
import sys
import unittest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".github" / "scripts" / "pi_shipped_compose_relpath.py"
)
_REAL_CONSTANTS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "pi_agent" / "constants.py"
)


def _run(source=None, argv_path=None):
    """Run the script over `source` on stdin, or over `argv_path`.

    Returns (returncode, stdout, stderr).
    """
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), argv_path or "-"],
        input="" if argv_path else source,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestPiComposeRelpathDerivation(unittest.TestCase):
    def test_it_reads_the_REAL_constants_file_the_gates_point_at(self):
        """The end-to-end claim, against the file that actually ships.

        A fixture-only suite would keep passing after a refactor of the real
        constant into a form the parser refuses — and the first sign would be a
        failed release."""
        rc, out, err = _run(argv_path=str(_REAL_CONSTANTS))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), "docker/docker-compose.opi.yml")

    def test_it_follows_the_constant_rather_than_remembering_a_path(self):
        """THE property. If the answer were baked in anywhere, both gates would
        keep fencing the old path after a rename — green, and useless."""
        rc, out, _ = _run('SHIPPED_COMPOSE_RELPATH = os.path.join("compose", "opi.yml")\n')
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "compose/opi.yml")
        rc, out, _ = _run('SHIPPED_COMPOSE_RELPATH = "elsewhere/x.yml"\n')
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "elsewhere/x.yml")

    def test_a_posix_answer_even_from_a_multi_part_join(self):
        """tar members and `cmp` paths are always `/`-joined, so the derivation
        has to be too — regardless of the host running the gate."""
        rc, out, _ = _run(
            'SHIPPED_COMPOSE_RELPATH = os.path.join("a", "b", "c.yml")\n')
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "a/b/c.yml")

    def test_it_refuses_rather_than_guesses(self):
        """Every refusal must be a LOUD non-zero with an ::error:: — the shell
        wrappers turn a non-zero into their own annotated failure, and a silent
        exit 0 with empty stdout would make both gates check the path
        `pi_agent/` (i.e. a directory), which `test -f` and `grep -qxF` would
        then report as a missing twin: the right verdict for the wrong reason,
        on a release that is actually fine."""
        for source, why in (
            ("X = 1\n", "constant absent"),
            ('SHIPPED_COMPOSE_RELPATH = os.path.join(_SUB, "x.yml")\n',
             "a non-literal join argument"),
            ('SHIPPED_COMPOSE_RELPATH = f"{_SUB}/x.yml"\n', "an f-string"),
            ('SHIPPED_COMPOSE_RELPATH = os.path.join("..", "x.yml")\n',
             "path traversal"),
            ('SHIPPED_COMPOSE_RELPATH = "/etc/x.yml"\n', "an absolute path"),
            ('SHIPPED_COMPOSE_RELPATH = ""\n', "an empty path"),
            ("def broken(:\n", "a file that does not parse"),
        ):
            with self.subTest(why=why):
                rc, out, err = _run(source)
                self.assertEqual(rc, 1, f"{why}: expected a refusal, got {out!r}")
                self.assertIn("::error::", err, why)
                self.assertEqual(out.strip(), "", why)

    def test_usage_error_is_also_a_refusal(self):
        proc = subprocess.run([sys.executable, str(_SCRIPT)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::", proc.stderr)


if __name__ == "__main__":
    unittest.main()
