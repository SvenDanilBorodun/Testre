#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""German error sentences for a text program that ended with a fault (§3.6).

The student process's launcher reports ``__exit({kind, file, line, exc_type,
name, detail_line})`` over the data socket, and the supervisor reports
``exited`` / ``compile_error`` over the control socket. :func:`sentence` maps a
``kind`` to a German, student-facing line with placeholders for ``file``,
``line``, ``name`` and the ``suggestion`` from ``robot_api.suggest`` — and
**never interpolates the raw CPython or JVM message** (no ``{exc_message}``).
The class NAME of an otherwise-unclassified exception (``exc_type``) is a code
identifier, not a message, and is allowed on the ``other`` line.

The raw tool output (the ``javac`` line, the last traceback line) rides a
SEPARATE ``[TECHNIK]`` log line — the disclosed, owner-approved Rule §1
exception (decision A14) — and is never part of these sentences.

``german-strings-lint`` cannot see this module; ``test_code_errors_de.py`` is
the enforcement (every value German by the shipped predicate, no
``{exc_message}``, no ``Traceback`` / ``Error:`` fragment).
"""

from __future__ import annotations

# Kinds whose ``exited`` means the program FAULTED (as opposed to a clean exit
# with kind ``'ok'`` or none). ``compile`` is Java's build failure; ``robot`` is
# a WorkflowError raised inside the student process; ``other`` is the catch-all.
ERROR_KINDS = frozenset({
    'syntax', 'indentation', 'name', 'robot_method', 'type', 'zero_division',
    'index', 'recursion', 'import', 'memory', 'timeout', 'compile', 'robot',
    'other',
})

# Static German templates. ``robot`` and ``robot_method`` are built by
# :func:`sentence` (they carry dynamic clauses); the with-suggestion form of
# ``robot_method`` is kept here so the German test covers it too.
SENTENCES: dict[str, str] = {
    'syntax': ('Zeile {line} in {file}: Hier stimmt etwas mit der Schreibweise '
               'nicht — prüfe Klammern, Doppelpunkte und Anführungszeichen.'),
    'indentation': ('Zeile {line} in {file}: Die Einrückung passt nicht — Python '
                    'braucht überall gleich viele Leerzeichen.'),
    'name': ('Zeile {line} in {file}: „{name}“ ist unbekannt — wurde es vorher '
             'definiert oder anders geschrieben?'),
    # Chosen by :func:`sentence` for kind `name` with a DOTTED name, never a
    # wire kind: the launcher reports `from math import sqrtt` and
    # `math.sqrtt` as `name` = "math.sqrtt" (M3; a NameError's name never
    # holds a dot). An old server reads the plain `name` sentence for it.
    'name_in_module': ('Zeile {line} in {file}: „{name}“ gibt es im Modul „{module}“ '
                       'nicht — ist es anders geschrieben?'),
    'robot_method': ('Zeile {line} in {file}: robot.{name} gibt es nicht. '
                     'Meintest du robot.{suggestion}?'),
    # The name IS a robot call — the runner's stub just does not have it: the
    # runner image is older than the server's (mixed image tags). Never
    # „Meintest du robot.zeige?" for robot.zeige.
    'robot_method_version': ('Zeile {line} in {file}: robot.{name} kennt die '
                             'Programmier-Umgebung noch nicht — sie ist älter als '
                             'die Roboter-Software. Bitte die Umgebung neu starten, '
                             'damit beide auf demselben Stand sind; hilft das '
                             'nicht, den Lehrer rufen.'),
    'type': ('Zeile {line} in {file}: Ein Aufruf hat falsche oder fehlende '
             'Angaben — bitte die Werte prüfen.'),
    'zero_division': 'Zeile {line} in {file}: Division durch 0 ist nicht erlaubt.',
    'index': ('Zeile {line} in {file}: Dieses Element gibt es nicht — der Index '
              'oder Schlüssel liegt außerhalb.'),
    'recursion': ('Zeile {line} in {file}: Eine Funktion ruft sich ohne Ende '
                  'selbst auf — das darf nicht sein.'),
    'import': ('Zeile {line} in {file}: Das Modul „{name}“ gibt es hier nicht — '
               'nur die Standardbibliothek und „robot“ sind verfügbar.'),
    'memory': 'Das Programm braucht zu viel Speicher und wurde beendet.',
    'timeout': 'Das Programm läuft seit 10 Minuten und wurde beendet.',
    'killed': 'Programm wurde gestoppt.',
    'compile': ('Zeile {line} in {file}: Der Java-Compiler meldet einen Fehler — '
                'bitte die Meldung unten lesen.'),
    'busy': ('Die Programmier-Umgebung ist gerade belegt — bitte kurz warten und '
             'erneut starten.'),
    'runner_down': ('Die Programmier-Umgebung läuft nicht — bitte die Umgebung '
                    'neu starten.'),
    # The server's own verdict when the control connection ends with no
    # ``exited`` (the runner container died mid-program — A15 keeps it dead
    # until „Umgebung starten"); never a kind the student process reports.
    'runner_crashed': ('Die Programmier-Umgebung ist während des Programms '
                       'abgestürzt — bitte die Umgebung neu starten.'),
    'other': ('Zeile {line} in {file}: Das Programm ist mit einem Fehler '
              'abgebrochen ({exc_type}).'),
    # Chosen by :func:`sentence` from the exception, never a wire kind (no
    # runner ever reports it, so an old runner and a new server agree): Java's
    # NullPointerException and Python's AttributeError on None
    # (student_main.classify_exception names that one 'None').
    'null': ('Zeile {line} in {file}: Hier ist kein Wert da (None bzw. null) — '
             'wurde die Variable vorher belegt, und hat find etwas gefunden?'),
    # The Java launcher's NoSuchMethodException with no position: Main has no
    # `static void main(String[])` (m14). Chosen like 'null'.
    'no_main': ('In Main.java fehlt die Methode „public static void main(String[] '
                'args)“ — ohne sie kann das Programm nicht starten.'),
    # The Java launcher's ClassNotFoundException with no position: Main.java
    # carries a `package` line or no class `Main` (C6). Chosen like 'no_main',
    # so an old runner gets it too.
    'no_main_class': ('In Main.java fehlt die Klasse „Main“ — sie muss genau so heißen, '
                      'und über ihr darf keine „package“-Zeile stehen.'),
}

# The prefix the position-carrying kinds share; ``robot`` prepends it to the
# relayed (already-German) WorkflowError text.
_POSITION_PREFIX = 'Zeile {line} in {file}: '


def _special_kind(kind: str, file: str, line: int, name: str, exc_type: str) -> str:
    """The sentence a fault deserves beyond its wire kind, read from the
    exception the runner named (area C): the null sentence for Java's
    NullPointerException and Python's AttributeError on None, the missing
    entry point for the Java launcher's position-less NoSuchMethodException
    and its missing class for a position-less ClassNotFoundException (C6), and
    the module's sentence for a `name` that carries a dot (M3).
    Everything else keeps its kind."""
    if kind == 'name' and '.' in name:
        return 'name_in_module'
    if kind != 'other':
        return kind
    if exc_type == 'NullPointerException' or (exc_type == 'AttributeError'
                                              and name == 'None'):
        return 'null'
    if exc_type == 'NoSuchMethodException' and not (file and line):
        return 'no_main'
    if exc_type == 'ClassNotFoundException' and not (file and line):
        return 'no_main_class'
    return kind


def sentence(kind: str, *, file: str = '', line: int = 0, name: str = '',
             suggestion: str | None = None, relayed: str = '',
             exc_type: str = '') -> str:
    """The German sentence for ``kind``.

    ``robot_method`` drops the „Meintest du …" clause when there is no
    suggestion, and says the two halves' versions differ when the suggestion
    IS the name (the server knows the call, the runner's stub does not);
    ``robot`` prefixes the relayed (German-by-contract) text with the
    position; ``other`` names the exception CLASS only, unless the exception
    is a missing value, a missing ``main`` or a missing class ``Main``
    (:func:`_special_kind`); ``name`` with a dot names the module
    (``name_in_module``). A report with no file or no line drops the
    position prefix (F2: never
    „Zeile 0 in :"). An unknown kind falls back to ``other``.
    """
    kind = _special_kind(kind, file, line, name, exc_type)
    module = ''
    if kind == 'name_in_module':
        # The module part may itself be dotted (collections.abc); an undotted
        # name has no module to name and reads the plain `name` sentence.
        if '.' in name:
            module, name = name.rsplit('.', 1)
        else:
            kind = 'name'
    if kind == 'robot_method' and suggestion and suggestion == name:
        template = SENTENCES['robot_method_version']
    elif kind == 'robot_method':
        template = _POSITION_PREFIX + 'robot.{name} gibt es nicht.'
        if suggestion:
            template += ' Meintest du robot.{suggestion}?'
    elif kind == 'robot':
        template = _POSITION_PREFIX + '{relayed}'
    else:
        template = SENTENCES.get(kind) or SENTENCES['other']
    if not (file and line) and template.startswith(_POSITION_PREFIX):
        template = template[len(_POSITION_PREFIX):]
    return template.format(line=line, file=file, name=name, relayed=relayed,
                           suggestion=suggestion or '', exc_type=exc_type, module=module)
