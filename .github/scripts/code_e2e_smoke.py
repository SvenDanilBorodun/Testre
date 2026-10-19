#!/usr/bin/env python3
"""Roboter Studio text programs — the two-container smoke (owner decision Q7).

Runs INSIDE the built physical-ai-server image while the code-runner image of
the same run is up beside it, the two sharing ONE volume at
/run/edubotics/code exactly as the student compose wires them
(docker-publish.yml::smoke-test). Everything below is the shipped code: the
server's CodeRpcServer / CodeProgram / WorkflowManager and handlers, the
runner's supervisor, both launchers (student_main.py, edubotics.Launcher) and
both generated stubs. Only what needs BOTH containers runs here (spec m13);
the Blockly rows and the unit fences run in ci.yml::python-tests.

1. The fault matrix (area C): Java and Python programs that fail in every way
   a student's program can, each with its phase and its exact German sentence
   (file and line included), and the [TECHNIK] line where the tool's own
   words carry information (javac -g names the variable, javac's symbol).
2. Every Methoden example (robot_api's docs, the column's „Einfügen" line),
   run ALONE in both languages in the simulator with one Würfel in reach,
   the payload Ziele „Ablage" and „Start" and the Bewegung „Winken": each ends
   `finished` (C7). The Greifziel type example inside
   `while robot.sees("wuerfel")` makes exactly one pass (F20); the Greifobjekt
   type example ends with the simulator's „nicht sichtbar" sentence (an A13
   object can never be a sim object).
3. The Python put-back case on a calibrated-rig emulation (Q8): `while True:
   grasp; drop_at` with ONE cube a student puts back twice after 6 s — three
   grasps, and Stopp ends the wait inside the RPC.

The rig-emulation fixture is the checkout's
physical_ai_tools/physical_ai_server/test/rig_emulation.py, mounted NEXT TO
this script (the image excludes test/). Prints one JSON line; exit 0 only when
every case holds.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from physical_ai_server.workflow import robot_api  # noqa: E402
from physical_ai_server.workflow.code_rpc import CodeRpcServer  # noqa: E402

import rig_emulation as rig_mod  # noqa: E402 — mounted next to this script

IPC_DIR = '/run/edubotics/code'
RUNNER_SOCKET = os.path.join(IPC_DIR, 'runner.sock')
RUNNER_WAIT_S = 60.0
POISON = {'languageVersion': 0,
          'blocks': [{'type': 'edubotics_code_program_v1', 'id': 'poison'}]}
PID = 'omx_full'


class _Patches:
    """The one thing the fixture takes from pytest: ``monkeypatch.setattr``."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, value = self._undo.pop()
            setattr(target, name, value)


def _wait_for_runner():
    deadline = time.monotonic() + RUNNER_WAIT_S
    while time.monotonic() < deadline:
        if os.path.exists(RUNNER_SOCKET):
            return True
        time.sleep(0.2)
    return False


def _payload(language, files, destinations=(), trajectories=None):
    return {'language': language, 'files': files, 'destinations': list(destinations),
            'trajectories': trajectories or {}, 'blocks': POISON}


def _run(server, language, files, *, objects=(), destinations=(), trajectories=None,
         simulated=True, timeout=90.0, stop_after=None, student=None):
    """One program on a fresh rig; returns (phase, errors, logs, rig)."""
    patches = _Patches()
    try:
        rig = rig_mod.Rig(patches, PID, objects=list(objects), simulated=simulated,
                          code_rpc=server)
        stop = None
        if student is not None:
            stop, _events = rig.student(*student)
        try:
            phase = rig.run(_payload(language, files, destinations, trajectories),
                            timeout=timeout, stop_after=stop_after)
        finally:
            if stop is not None:
                stop.set()
        return phase, rig.errors(), rig.logs(), rig
    finally:
        patches.undo()


# ══════════════════════════════════════════════════════════════════════════
# 1. the fault matrix
# ══════════════════════════════════════════════════════════════════════════

_I = 'import edubotics.Robot;\n'


def _java_main(body, imports=_I):
    return {'Main.java': imports + 'public class Main {\n'
                                   '    public static void main(String[] args) {\n'
            + body + '\n    }\n}\n'}


_INDEX_DE = 'Dieses Element gibt es nicht — der Index oder Schlüssel liegt außerhalb.'
_ZERO_DE = 'Division durch 0 ist nicht erlaubt.'
_NULL_DE = ('Hier ist kein Wert da (None bzw. null) — wurde die Variable vorher belegt, '
            'und hat find etwas gefunden?')
_INPUT_DE = 'Eingaben über die Tastatur gibt es hier nicht — das Programm läuft auf dem Roboter.'
_TYPE_DE = 'Ein Aufruf hat falsche oder fehlende Angaben — bitte die Werte prüfen.'
_GREIFZIEL_DE = robot_api.NOT_A_GREIFZIEL_DE
_OBJ_LABEL_DE = ('„obj“ muss ein Objekt-Name aus Buchstaben ohne Umlaute (a–z), Ziffern und _ '
                 'sein (höchstens 24 Zeichen). Meintest du "wuerfel"?')

# name → (language, files, phase, error or None, a [TECHNIK] fragment or None,
#         log lines that must appear)
MATRIX = {
    'java_pkgprivate_ok': ('java', {'Main.java': _I + 'class Main {\n    public static void main('
                                    'String[] args) {\n        Robot.log("ok");\n    }\n}\n'},
                           'finished', None, None, ['ok']),
    'java_pkgprivate_index': ('java', {'Main.java': _I + 'class Main {\n    public static void main('
                                       'String[] args) {\n        int[] a = new int[2];\n'
                                       '        a[3] = 1;\n    }\n}\n'},
                              'error', f'Zeile 5 in Main.java: {_INDEX_DE}', None, []),
    'java_static_init_div': ('java', {'Main.java': _I + 'public class Main {\n    static int z = 0;\n'
                                      '    static int x = 5 / z;\n    public static void main('
                                      'String[] args) {\n        Robot.home();\n    }\n}\n'},
                             'error', f'Zeile 4 in Main.java: {_ZERO_DE}', None, []),
    'java_helper_static_init': ('java', {'Main.java': _I + 'public class Main {\n    public static '
                                         'void main(String[] args) {\n        System.out.println('
                                         'Helfer.X);\n    }\n}\n',
                                         'Helfer.java': 'public class Helfer {\n    static int[] a = '
                                         'new int[1];\n    public static int X = a[3];\n}\n'},
                                'error', f'Zeile 3 in Helfer.java: {_INDEX_DE}', None, []),
    'java_helper_package': ('java', {'Main.java': _I + 'import tiere.Hund;\npublic class Main {\n'
                                     '    public static void main(String[] args) {\n'
                                     '        Hund.bell(0);\n    }\n}\n',
                                     'tiere/Hund.java': 'package tiere;\n\npublic class Hund {\n'
                                     '    public static void bell(int n) {\n'
                                     '        System.out.println(10 / n);\n    }\n}\n'},
                            'error', f'Zeile 5 in tiere/Hund.java: {_ZERO_DE}', None, []),
    'java_helper_misplaced_null': ('java', {'Main.java': _I + 'import tiere.Katze;\npublic class '
                                            'Main {\n    public static void main(String[] args) {\n'
                                            '        Katze.f(null);\n    }\n}\n',
                                            'Katze.java': 'package tiere;\n\npublic class Katze {\n'
                                            '    public static void f(String s) {\n'
                                            '        System.out.println(s.length());\n    }\n}\n'},
                                   'error', f'Zeile 5 in Katze.java: {_NULL_DE}', '"s" is null', []),
    'java_scanner': ('java', {'Main.java': _I + 'import java.util.Scanner;\npublic class Main {\n'
                              '    public static void main(String[] args) {\n'
                              '        Scanner sc = new Scanner(System.in);\n'
                              '        int n = sc.nextInt();\n        System.out.println(n);\n'
                              '    }\n}\n'},
                     'error', f'Zeile 6 in Main.java: {_INPUT_DE}', None, []),
    'java_buffered_reader': ('java', {'Main.java': _I + 'import java.io.*;\npublic class Main {\n'
                                      '    public static void main(String[] args) throws '
                                      'IOException {\n        BufferedReader r = new '
                                      'BufferedReader(new InputStreamReader(System.in));\n'
                                      '        System.out.println(r.readLine());\n    }\n}\n'},
                             'error', f'Zeile 6 in Main.java: {_INPUT_DE}', None, []),
    'java_stdin_read': ('java', {'Main.java': _I + 'public class Main {\n    public static '
                                 'void main(String[] args) throws Exception {\n'
                                 '        System.out.println(System.in.read());\n    }\n}\n'},
                        'error', f'Zeile 4 in Main.java: {_INPUT_DE}', None, []),
    'java_null_local': ('java', _java_main('        String s = null;\n'
                                           '        System.out.println(s.length());'),
                        'error', f'Zeile 5 in Main.java: {_NULL_DE}', '"s" is null', []),
    'java_null_field': ('java', {'Main.java': _I + 'public class Main {\n    static String s;\n'
                                 '    public static void main(String[] args) {\n'
                                 '        System.out.println(s.length());\n    }\n}\n'},
                        'error', f'Zeile 5 in Main.java: {_NULL_DE}', None, []),
    'java_null_ziel': ('java', _java_main('        Robot.moveAbove(null);'),
                       'error', f'Zeile 4 in Main.java: {_GREIFZIEL_DE}', None, []),
    'java_robot_refusal': ('java', _java_main('        Robot.waitSeconds(-5);'),
                           'error', 'Zeile 4 in Main.java: Robot.waitSeconds: „seconds“ muss '
                           'zwischen 0.0 und 300.0 liegen.', None, []),
    'java_recursion': ('java', {'Main.java': _I + 'public class Main {\n    static int f(int n) { '
                                'return f(n + 1); }\n    public static void main(String[] args) '
                                '{\n        f(0);\n    }\n}\n'},
                       'error', 'Zeile 3 in Main.java: Eine Funktion ruft sich ohne Ende selbst '
                       'auf — das darf nicht sein.', None, []),
    'java_unicode_identifier': ('java', _java_main('        int größe = 0;\n'
                                                   '        System.out.println(5 % größe);'),
                                'error', f'Zeile 5 in Main.java: {_ZERO_DE}', None, []),
    'java_runtime_exception': ('java', _java_main('        throw new RuntimeException("kaputt");'),
                               'error', 'Zeile 4 in Main.java: Das Programm ist mit einem Fehler '
                               'abgebrochen (RuntimeException).', 'kaputt', []),
    'java_cast': ('java', _java_main('        Object o = "x";\n        Integer i = (Integer) o;'),
                  'error', f'Zeile 5 in Main.java: {_TYPE_DE}', None, []),
    'java_parse': ('java', _java_main('        int n = Integer.parseInt("zwölf");'),
                   'error', f'Zeile 4 in Main.java: {_TYPE_DE}', None, []),
    'java_exit_3': ('java', _java_main('        System.exit(3);'),
                    'error', 'Das Programm ist mit einem Fehler abgebrochen (Code 3).', None, []),
    'java_nonpublic_main': ('java', {'Main.java': _I + 'public class Main {\n    static void main('
                                     'String[] args) {\n        Robot.log("lief");\n    }\n}\n'},
                            'finished', None, None, ['lief']),
    'java_nonstatic_main': ('java', {'Main.java': _I + 'public class Main {\n    public void main('
                                     'String[] args) {\n        Robot.log("nie");\n    }\n}\n'},
                            'error', 'In Main.java fehlt die Methode „public static void '
                            'main(String[] args)“ — ohne sie kann das Programm nicht starten.',
                            None, []),
    'java_worker_thread_exception': ('java', {'Main.java': _I + 'public class Main {\n    public '
                                              'static void main(String[] args) throws Exception {\n'
                                              '        Thread t = new Thread(() -> { int[] a = '
                                              'new int[1]; a[2] = 0; });\n        t.start(); '
                                              't.join();\n        Robot.log("weiter");\n    }\n}\n'},
                                     'finished', None, None, ['weiter']),
    'java_compile_symbol': ('java', _java_main('        Robot.moveAboev(null);'),
                            'error', 'Zeile 4 in Main.java: Der Java-Compiler meldet einen Fehler '
                            '— bitte die Meldung unten lesen.', 'symbol: method moveAboev', []),
    'java_log_values': ('java', _java_main('        Robot.log(java.util.List.of(1, "b"));\n'
                                           '        Robot.log(new int[] {1, 2});\n'
                                           '        Robot.log((Object) null);'),
                        'finished', None, None, ['(1, b)', '(1, 2)', 'nichts']),
    'java_greifobjekt_name': ('java', _java_main('        edubotics.Greifobjekt g = new '
                                                 'edubotics.Greifobjekt("Rote Banane", new int[] '
                                                 '{30, 31}, 0.040, 0.015);\n'
                                                 '        Robot.log(g.name());'),
                              'finished', None, None, ['rote_banane']),
    'java_sees_label': ('java', _java_main('        Robot.sees("Würfel");'),
                        'error', f'Zeile 4 in Main.java: Robot.sees: {_OBJ_LABEL_DE}', None, []),
    'py_none_attribute': ('python', {'main.py': 'import robot\nz = robot.find("wuerfel")\n'
                                     'print(z.x)\n'},
                          'error', f'Zeile 3 in main.py: {_NULL_DE}', None, []),
    'py_input': ('python', {'main.py': 'import robot\nn = input("Zahl? ")\n'},
                 'error', f'Zeile 2 in main.py: {_INPUT_DE}', None, []),
    'py_zero_division': ('python', {'main.py': 'import robot\nx = 1 / 0\n'},
                         'error', f'Zeile 2 in main.py: {_ZERO_DE}', None, []),
    'py_robot_refusal': ('python', {'main.py': 'import robot\nrobot.wait(-5)\n'},
                         'error', 'Zeile 2 in main.py: robot.wait: „seconds“ muss zwischen 0.0 '
                         'und 300.0 liegen.', None, []),
    'py_none_ziel': ('python', {'main.py': 'import robot\nrobot.move_above(None)\n'},
                     'error', f'Zeile 2 in main.py: {_GREIFZIEL_DE}', None, []),
    'py_log_values': ('python', {'main.py': 'import robot\nrobot.log([1, "b"])\nrobot.log(None)\n'},
                      'finished', None, None, ['(1, b)', 'nichts']),
    'py_sees_label': ('python', {'main.py': 'import robot\nrobot.sees("Würfel")\n'},
                      'error', f'Zeile 2 in main.py: robot.sees: {_OBJ_LABEL_DE}', None, []),
    'py_greifobjekt_attributes': ('python', {'main.py': 'import robot\nclass Würfelchen('
                                             'robot.Greifobjekt):\n    tag_ids = [30]\n'
                                             '    hoehe_m = 0.03\n    greiftiefe_m = 0.05\n'},
                                  'error', 'Zeile 2 in main.py: Objekt „wuerfelchen": Greiftiefe '
                                  '(greiftiefe_m=0.05) darf nicht größer als die Objekthöhe '
                                  '(hoehe_m=0.03) sein.', None, []),
    'py_exit_3': ('python', {'main.py': 'import sys\nsys.exit(3)\n'},
                  'error', 'Das Programm ist mit einem Fehler abgebrochen (Code 3).', None, []),
}


def run_matrix(server):
    failures = {}
    for name, (language, files, want_phase, want_error, technik, want_logs) in MATRIX.items():
        phase, errors, logs, _rig = _run(server, language, files, timeout=90.0)
        problems = []
        if phase != want_phase:
            problems.append(f'phase {phase}')
        if want_error is not None and (not errors or errors[-1] != want_error):
            problems.append(f'error {errors[-1:]!r}')
        if want_error is None and errors:
            problems.append(f'unexpected error {errors[-1:]!r}')
        if technik is not None and not any(
                line.startswith('[TECHNIK] ') and technik in line for line in logs):
            problems.append(f'no [TECHNIK] line with {technik!r}')
        for line in want_logs:
            if line not in logs:
                problems.append(f'no log line {line!r}')
        if problems:
            failures[name] = problems
    return {'cases': len(MATRIX), 'failures': failures}


# ══════════════════════════════════════════════════════════════════════════
# 2. every Methoden example, both languages, in the simulator (C7)
# ══════════════════════════════════════════════════════════════════════════

ABLAGE = {'name': 'Ablage', 'kind': 'pin', 'x': 0.10, 'y': 0.15, 'z': 0.0}
START = {'name': 'Start', 'kind': 'pin', 'x': 0.16, 'y': -0.08, 'z': 0.0}
CUBE = rig_mod.cube(0.18, 0.0)


def _winken():
    from physical_ai_server import robot_profiles as rp
    prof = rp.ROBOT_PROFILES[PID]
    home = [float(v) for v in prof.home_joints_rad]
    points = []
    for k in range(26):
        t = k / 25.0
        q = list(home)
        q[0] += 0.4 * t
        points.append([round(v, 4) for v in q] + [float(prof.gripper_open_rad), round(t, 3)])
    return {'Winken': {'fps': 25, 'points': points}}


def _java_program(body):
    lines = '\n'.join('        ' + line for line in body.split('\n'))
    return {'Main.java': 'import edubotics.Robot;\n\npublic class Main {\n'
                         '    public static void main(String[] args) {\n' + lines + '\n    }\n}\n'}


def _python_program(body):
    return {'main.py': 'import robot\n\n' + body + '\n'}


def run_examples(server):
    docs = json.loads(robot_api.export_docs_json())
    failures = {}
    runs = 0
    world = dict(objects=[CUBE], destinations=[ABLAGE, START], trajectories=_winken())
    for m in docs['methods']:
        for language, files in (('python', _python_program(m['example_py'])),
                                ('java', _java_program(m['example_java']))):
            phase, errors, logs, _rig = _run(server, language, files, **world)
            runs += 1
            if phase != 'finished' or errors:
                failures[f"{m['name']}/{language}"] = {'phase': phase, 'error': errors[-1:],
                                                       'logs': logs[-4:]}
    types = {t['name']: t for t in docs['types']}
    # F20: one pass of the Greifziel example inside „solange sichtbar".
    greifziel = types['Greifziel']
    py_loop = ('while robot.sees("wuerfel"):\n    robot.log("Durchgang")\n'
               + '\n'.join('    ' + line for line in greifziel['example_py'].split('\n')))
    java_loop = ('while (Robot.sees("wuerfel")) {\n    Robot.log("Durchgang");\n'
                 + '\n'.join('    ' + line for line in greifziel['example_java'].split('\n'))
                 + '\n}')
    for language, files in (('python', _python_program(py_loop)),
                            ('java', _java_program(java_loop))):
        phase, errors, logs, _rig = _run(server, language, files, **world)
        runs += 1
        passes = logs.count('Durchgang')
        if phase != 'finished' or errors or passes != 1:
            failures[f'Greifziel/{language}'] = {'phase': phase, 'error': errors[-1:],
                                                 'passes': passes}
    # An A13 object can never be a sim object: the simulator's „nicht sichtbar".
    greifobjekt = types['Greifobjekt']
    for language, files in (('python', _python_program(greifobjekt['example_py'])),
                            ('java', _java_program(greifobjekt['example_java']))):
        phase, errors, logs, _rig = _run(server, language, files, **world)
        runs += 1
        last = errors[-1] if errors else ''
        if phase != 'error' or 'Banane' not in last or 'sichtbar' not in last:
            failures[f'Greifobjekt/{language}'] = {'phase': phase, 'error': errors[-1:]}
    return {'runs': runs, 'failures': failures}


# ══════════════════════════════════════════════════════════════════════════
# 3. the Python put-back case (Q8)
# ══════════════════════════════════════════════════════════════════════════

PUTBACK_DELAY_S = 6.0
PUTBACK_STOP_AFTER_S = 20.0


def run_putback(server):
    files = _python_program('while True:\n    robot.grasp("wuerfel")\n'
                            '    robot.drop_at("Ablage")')
    pick = rig_mod.PICK
    phase, errors, logs, rig = _run(
        server, 'python', files, objects=[rig_mod.cube(*pick[0])], destinations=[rig_mod.ABLAGE],
        simulated=False, timeout=PUTBACK_STOP_AFTER_S + 60.0, stop_after=PUTBACK_STOP_AFTER_S,
        student=((rig_mod.ABLAGE['x'], rig_mod.ABLAGE['y']), pick, 2, PUTBACK_DELAY_S))
    grasps = rig.grasps()
    ok = phase == 'stopped' and grasps == 3 and errors == []
    return {'phase': phase, 'grasps': grasps, 'errors': errors[-1:],
            'failures': {} if ok else {'putback': {'phase': phase, 'grasps': grasps,
                                                   'logs': logs[-6:]}}}


def main():
    if not _wait_for_runner():
        print(json.dumps({'ok': False, 'error': f'no {RUNNER_SOCKET} after {RUNNER_WAIT_S} s'}))
        return 1
    server = CodeRpcServer(os.path.join(IPC_DIR, 'rpc.sock'))
    t0 = time.monotonic()
    try:
        result = {'matrix': run_matrix(server), 'examples': run_examples(server),
                  'putback': run_putback(server)}
    finally:
        server.close()
    ok = all(not part['failures'] for part in result.values())
    result['ok'] = ok
    result['seconds'] = round(time.monotonic() - t0, 1)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
