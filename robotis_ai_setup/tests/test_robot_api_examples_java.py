"""Every Java example the „Methoden" column shows COMPILES — under exactly the
imports the Java starter program has.

The column (`code/MethodenPanel.jsx`) shows one example line per robot method
and a short program per type, and „Einfügen" writes a method's line into the
student's program. The lines come from `robot_api_docs.json`, which
`physical_ai_server/workflow/robot_api.py::export_docs_json` renders; a line
that does not compile would be the first thing a Java student meets.

So this file builds ONE `Main.java` whose import block is EXACTLY the Java
starter's (`codeProject.js::STARTER_FILES.java`, read from the source and
pinned, so a starter change re-pins this test), puts every method example in
a method of its own (a `Robot.Greifziel` parameter for every variable the
example names) and every type example likewise, and compiles it against the
REAL runner sources (`Robot.java`, `RpcClient.java`, `Greifobjekt.java`,
`EduJson.java`). A negative control proves the import block is what is being
tested: the short `new Greifobjekt(…)` — without the starter importing it —
must be REFUSED.

Java needs a JDK ≥ 17: `$EDUBOTICS_JAVAC`, then `$JAVA_HOME_21_X64` (GitHub's
runners), then `javac` on PATH (the lookup of test_code_insert_cases.py);
without one this is SKIPPED, and `EDUBOTICS_REQUIRE_JAVAC=1` makes that a
failure. Deliberately stdlib-only.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CODE_DIR = (_REPO_ROOT / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'components'
             / 'Workshop' / 'code')
_DOCS_JSON = _CODE_DIR / 'robot_api_docs.json'
_CODE_PROJECT = _CODE_DIR / 'codeProject.js'
_RUNNER_JAVA = (_REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'code_runner' / 'runner' / 'java'
                / 'edubotics')
_RUNNER_SOURCES = ('Robot.java', 'RpcClient.java', 'Greifobjekt.java', 'EduJson.java')


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


def _starter_imports():
    """The `'import …;'` string literals of the Java starter, in order."""
    source = _CODE_PROJECT.read_text(encoding='utf-8')
    start = source.index('export const STARTER_FILES')
    java_at = source.index('java: Object.freeze(', start)
    block = source[java_at:source.index('}),', java_at)]
    return re.findall(r"'(import [^']*;)'", block)


def _indent(body, prefix):
    return '\n'.join(prefix + line if line else line for line in body.split('\n'))


def _main_java(imports, methods, types, extra_body=None):
    lines = list(imports) + ['', 'public class Main {']
    for i, m in enumerate(methods):
        refs = []
        for arg in m['example']['args']:
            if isinstance(arg, dict) and 'ref' in arg and arg['ref'] not in refs:
                refs.append(arg['ref'])
        params = ', '.join(f'Robot.Greifziel {r}' for r in refs)
        lines += [f'    // {m["name"]}', f'    static void ex{i}({params}) {{',
                  _indent(m['example_java'], '        '), '    }', '']
    for i, t in enumerate(types):
        lines += [f'    // {t["name"]}', f'    static void t{i}() {{',
                  _indent(t['example_java'], '        '), '    }', '']
    if extra_body is not None:
        lines += ['    static void control() {', _indent(extra_body, '        '), '    }', '']
    lines += ['    public static void main(String[] args) {', '    }', '}', '']
    return '\n'.join(lines)


def _compile(javac, main_source, tmp):
    src = os.path.join(tmp, 'src')
    lib = os.path.join(src, 'edubotics')
    out = os.path.join(tmp, 'out')
    os.makedirs(lib)
    os.makedirs(out)
    files = []
    for name in _RUNNER_SOURCES:
        shutil.copyfile(_RUNNER_JAVA / name, os.path.join(lib, name))
        files.append(os.path.join(lib, name))
    main = os.path.join(src, 'Main.java')
    with open(main, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(main_source)
    files.append(main)
    return subprocess.run([javac, '-Xlint:none', '-encoding', 'UTF-8', '-d', out, *files],
                          capture_output=True, text=True, timeout=300)


class StarterImports(unittest.TestCase):
    def test_the_java_starter_imports_exactly_robot(self):
        # Pinned: a change of the starter's imports must re-pin this file (and
        # decide whether the Greifobjekt example can lose its qualification).
        self.assertEqual(_starter_imports(), ['import edubotics.Robot;'])

    def test_the_docs_json_has_every_example(self):
        doc = json.loads(_DOCS_JSON.read_text(encoding='utf-8'))
        self.assertEqual(len(doc['methods']), 33)
        self.assertEqual([t['name'] for t in doc['types']], ['Greifziel', 'Greifobjekt'])
        for m in doc['methods']:
            self.assertTrue(m['example_java'].endswith(';'), m['name'])


@unittest.skipUnless(_JAVAC or os.environ.get('EDUBOTICS_REQUIRE_JAVAC') == '1',
                     'no JDK >= 17 (set EDUBOTICS_JAVAC, or EDUBOTICS_REQUIRE_JAVAC=1 to fail)')
class JavaExamplesCompile(unittest.TestCase):
    def test_every_example_compiles_under_the_starters_imports(self):
        self.assertIsNotNone(_JAVAC, 'EDUBOTICS_REQUIRE_JAVAC=1 but no JDK >= 17 was found')
        javac, _java = _JAVAC
        doc = json.loads(_DOCS_JSON.read_text(encoding='utf-8'))
        source = _main_java(_starter_imports(), doc['methods'], doc['types'])
        with tempfile.TemporaryDirectory(prefix='edu-java-examples-') as tmp:
            built = _compile(javac, source, tmp)
        self.assertEqual(built.returncode, 0,
                         f'the Methoden examples do not compile:\n{built.stderr}\n\n{source}')

    def test_the_negative_control_is_refused(self):
        # The short form needs an import the starter does not have: if this
        # compiled, the test above would not be testing the starter's imports.
        self.assertIsNotNone(_JAVAC, 'EDUBOTICS_REQUIRE_JAVAC=1 but no JDK >= 17 was found')
        javac, _java = _JAVAC
        control = ('Greifobjekt b = new Greifobjekt("Banane", new int[] {30, 31}, 0.040, 0.015);\n'
                   'Robot.grasp(b);')
        source = _main_java(_starter_imports(), [], [], extra_body=control)
        with tempfile.TemporaryDirectory(prefix='edu-java-control-') as tmp:
            built = _compile(javac, source, tmp)
        self.assertNotEqual(built.returncode, 0, 'the short Greifobjekt form compiled')
        self.assertIn('cannot find symbol', built.stderr)


if __name__ == '__main__':
    unittest.main()
