"""The Java runner's hand-written JSON encoder (`runner/java/edubotics/EduJson.java`).

C5: a Java program that passed `Double.NaN` (or ±Infinity) to a robot call
failed CLIENT-side with the English `IllegalArgumentException("non-finite
number")`, read by the launcher as the generic type sentence — while Python
sends the JSON tokens `NaN`/`Infinity` and the robot refuses them in German
(„„seconds“ muss eine Zahl sein."). And a `char` (boxed to `Character`) or any
other `CharSequence` (a `StringBuilder`) handed to `Robot.speak(Object)` /
`toast(Object, …)` could not be encoded at all. Now EduJson writes CPython's
`json.dumps` tokens for a non-finite double and a `Character`/`CharSequence`
as its string; every other unencodable type still throws, which the
generated `RpcClient` turns into the German `NOT_SENDABLE_DE`.

Compiles `EduJson.java` with a tiny probe in the same package and runs it.
Java needs a JDK ≥ 17: `$EDUBOTICS_JAVAC`, then `$JAVA_HOME_21_X64`, then
`javac` on PATH; without one this is SKIPPED, and `EDUBOTICS_REQUIRE_JAVAC=1`
(CI) makes that a failure. Deliberately stdlib-only.
"""

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUNNER_JAVA = (_REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner' / 'java'
                / 'edubotics')


def _find_javac():
    # Duplicated from test_code_insert_cases.py::_find_javac (tests/ has no
    # __init__.py, so a module of this directory is not importable by name).
    candidates = [os.environ.get('EDUBOTICS_JAVAC')]
    home = os.environ.get('JAVA_HOME_21_X64')
    if home:
        candidates.append(os.path.join(home, 'bin', 'javac'))
    candidates.append(shutil.which('javac'))
    for javac in candidates:
        if not javac or not os.path.isfile(javac):
            continue
        java = os.path.join(os.path.dirname(javac), 'java')
        try:
            out = subprocess.run([javac, '-version'], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (out.stdout + out.stderr).strip()
        try:
            major = int(text.split()[1].split('.')[0])
        except (IndexError, ValueError):
            continue
        if out.returncode == 0 and major >= 17 and os.path.isfile(java):
            return javac, java
    return None


_JAVAC = _find_javac()

# One probe line per case: the case's name, a TAB, what encode() returned (or
# `THROWS <class>` when it threw).
_PROBE = r'''package edubotics;

import java.util.Arrays;

public final class EduJsonProbe {
    private EduJsonProbe() {
    }

    private static void show(String name, Object value) {
        String out;
        try {
            out = EduJson.encode(value);
        } catch (IllegalArgumentException e) {
            out = "THROWS " + e.getClass().getSimpleName();
        }
        System.out.println(name + "\t" + out);
    }

    public static void main(String[] args) {
        show("nan", Double.NaN);
        show("inf", Double.POSITIVE_INFINITY);
        show("ninf", Double.NEGATIVE_INFINITY);
        show("fnan", Float.NaN);
        show("finite", 2.5);
        show("char", 'a');
        show("quote_char", '"');
        show("builder", new StringBuilder("ab"));
        show("list", Arrays.asList(1, Double.NaN, 'z'));
        show("scanner", new java.util.Scanner("x"));
    }
}
'''


@unittest.skipUnless(_JAVAC or os.environ.get('EDUBOTICS_REQUIRE_JAVAC') == '1',
                     'no JDK >= 17 (set EDUBOTICS_JAVAC, or EDUBOTICS_REQUIRE_JAVAC=1 to fail)')
class EduJsonEncodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert _JAVAC is not None, 'EDUBOTICS_REQUIRE_JAVAC=1 but no JDK >= 17 was found'
        javac, java = _JAVAC
        work = pathlib.Path(tempfile.mkdtemp(prefix='edujson-'))
        src = work / 'src' / 'edubotics'
        src.mkdir(parents=True)
        shutil.copy(_RUNNER_JAVA / 'EduJson.java', src / 'EduJson.java')
        (src / 'EduJsonProbe.java').write_text(_PROBE, encoding='utf-8')
        classes = work / 'classes'
        build = subprocess.run(
            [javac, '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', str(classes),
             str(src / 'EduJson.java'), str(src / 'EduJsonProbe.java')],
            capture_output=True, text=True, timeout=120)
        assert build.returncode == 0, build.stderr
        ran = subprocess.run([java, '-cp', str(classes), 'edubotics.EduJsonProbe'],
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, ran.stderr
        cls.out = dict(line.split('\t', 1) for line in ran.stdout.splitlines())

    def test_a_non_finite_double_is_cpythons_token(self):
        self.assertEqual(self.out['nan'], 'NaN')
        self.assertEqual(self.out['inf'], 'Infinity')
        self.assertEqual(self.out['ninf'], '-Infinity')
        self.assertEqual(self.out['fnan'], 'NaN')
        self.assertEqual(self.out['finite'], '2.5')
        # The server's json.loads reads exactly these tokens.
        self.assertTrue(all(v != v or abs(v) == float('inf') for v in
                            json.loads(f"[{self.out['nan']},{self.out['inf']},{self.out['ninf']}]")))

    def test_a_char_and_a_char_sequence_are_their_string(self):
        self.assertEqual(self.out['char'], '"a"')
        self.assertEqual(self.out['quote_char'], '"\\""')
        self.assertEqual(self.out['builder'], '"ab"')
        self.assertEqual(json.loads(self.out['list'])[2], 'z')

    def test_every_other_type_still_throws(self):
        self.assertEqual(self.out['scanner'], 'THROWS IllegalArgumentException')


if __name__ == '__main__':
    unittest.main()
