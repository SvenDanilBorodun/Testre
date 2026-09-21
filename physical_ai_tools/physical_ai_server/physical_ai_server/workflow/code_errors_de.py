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
    'robot_method': ('Zeile {line} in {file}: robot.{name} gibt es nicht. '
                     'Meintest du robot.{suggestion}?'),
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
    'other': ('Zeile {line} in {file}: Das Programm ist mit einem Fehler '
              'abgebrochen ({exc_type}).'),
}

# The prefix the position-carrying kinds share; ``robot`` prepends it to the
# relayed (already-German) WorkflowError text.
_POSITION_PREFIX = 'Zeile {line} in {file}: '


def sentence(kind: str, *, file: str = '', line: int = 0, name: str = '',
             suggestion: str | None = None, relayed: str = '',
             exc_type: str = '') -> str:
    """The German sentence for ``kind``.

    ``robot_method`` drops the „Meintest du …" clause when there is no
    suggestion; ``robot`` prefixes the relayed (German-by-contract) text with
    the position; ``other`` names the exception CLASS only. An unknown kind
    falls back to ``other``.
    """
    if kind == 'robot_method':
        base = (_POSITION_PREFIX + 'robot.{name} gibt es nicht.').format(
            line=line, file=file, name=name)
        if suggestion:
            base += f' Meintest du robot.{suggestion}?'
        return base
    if kind == 'robot':
        return (_POSITION_PREFIX + '{relayed}').format(
            line=line, file=file, relayed=relayed)
    template = SENTENCES.get(kind) or SENTENCES['other']
    return template.format(line=line, file=file, name=name,
                           suggestion=suggestion or '', exc_type=exc_type)
