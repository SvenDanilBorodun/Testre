/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every student-visible string of the Roboter-Studio code editor, in one
// place (the `blocks/messages_de.js` idiom, kept separate because that file
// belongs to the Blockly surface). `{0}`/`{1}` are filled by `formatCode`.
//
// The cap refusals mirror the server's `code_program.py` / the cloud's
// `validators/workflow.py` sentences word for word, so a student who trips the
// same cap after a round-trip reads the same sentence — with ONE deliberate
// exception. `ERR_BAD_PATH` ADDS the rule („… erlaubt sind Buchstaben, Ziffern
// und _, höchstens drei Ordner, und die Endung .py oder .java"), because here
// the student is being asked for a name and can act on it; the server refuses
// a name already on the wire and says only that it is not allowed. Do not
// „harmonise" that one back — a refusal at the prompt that names no rule sends
// the student guessing.

export const CODE_DE = Object.freeze({
  NEW: 'Neu',
  NEW_TITLE: 'Neues Programm anlegen',
  NEW_LANGUAGE_FIXED: 'Die Sprache wird beim Anlegen gewählt und bleibt danach fest.',
  LANG_BLOCKS: 'Blöcke',
  LANG_BLOCKS_HINT: 'Bausteine ziehen — ohne Tippen.',
  LANG_PYTHON: 'Python',
  LANG_PYTHON_HINT: 'Echter Python-Code, mit Haltepunkten.',
  LANG_JAVA: 'Java',
  LANG_JAVA_HINT: 'Echter Java-Code — Starten und Stoppen.',
  HEADER_HINT_CODE: 'Programm schreiben, speichern und vom Roboter ausführen lassen.',

  FILES_TITLE: 'Dateien',
  FILE_NEW: 'Neue Datei',
  FILE_NEW_PROMPT: 'Name der neuen Datei (zum Beispiel hilfe.py oder ordner/hilfe.py):',
  FILE_RENAME: 'Umbenennen',
  FILE_RENAME_PROMPT: 'Neuer Name für „{0}“:',
  FILE_DELETE: 'Löschen',
  FILE_DELETE_CONFIRM: 'Datei „{0}“ wirklich löschen?',
  FILE_ENTRY_TITLE: 'Startdatei — hier beginnt das Programm.',
  ERR_ENTRY_DELETE: 'Die Startdatei „{0}“ kann nicht gelöscht werden.',
  ERR_ENTRY_RENAME: 'Die Startdatei „{0}“ kann nicht umbenannt werden.',
  ERR_FILE_EXISTS: 'Eine Datei „{0}“ gibt es schon.',
  ERR_NO_FILES: 'Das Programm enthält keine Dateien.',
  ERR_TOO_MANY_FILES: 'Das Programm hat zu viele Dateien (höchstens {0}).',
  ERR_FILE_TOO_BIG: 'Die Datei „{0}“ ist zu groß (höchstens {1} KiB).',
  ERR_PROJECT_TOO_BIG: 'Das Programm ist insgesamt zu groß (höchstens {0} KiB).',
  ERR_BAD_PATH: 'Der Dateiname „{0}“ ist nicht erlaubt — erlaubt sind Buchstaben, Ziffern und _, '
    + 'höchstens drei Ordner, und die Endung .py oder .java.',
  ERR_RESERVED: 'Der Dateiname „{0}“ ist reserviert.',
  ERR_WRONG_EXT: 'Die Datei „{0}“ passt nicht zur Sprache {1}.',
  ERR_MISSING_ENTRY: 'Die Startdatei „{0}“ fehlt.',

  EDITOR_LOADING: 'Editor wird geladen …',
  WORKFLOW_LOADING: 'Workflow wird geladen …',
  EDITOR_FAILED: 'Der Editor konnte nicht geladen werden — bitte die Seite neu laden.',

  RUN_UNSUPPORTED: 'Bitte zuerst die Umgebung aktualisieren — dieser Roboter kann noch keine '
    + 'Python-/Java-Programme ausführen.',
  RUN_TOO_BIG: 'Das Programm ist zu groß, um es zu starten — bitte Dateien kürzen.',
  NEW_PROGRAM_NAME: 'Neues Programm',

  // The debugger (A8). Python gets breakpoints; Java gets run and stop. The
  // asymmetry is SAID, in both places a student looks for it — beside the
  // editor and in the Debug-Panel — because „parity" is not claimed for
  // something only one language has.
  DEBUG_BP_HINT_PY: 'Haltepunkt setzen: links neben die Zeilennummer klicken.',
  DEBUG_JAVA_NO_BREAKPOINTS: 'Für Java gibt es noch keine Haltepunkte — Java-Programme '
    + 'lassen sich starten und stoppen, Schritt für Schritt geht nur in Python.',
  // „Noch keine Haltepunkte gesetzt." is NOT repeated here: the Blockly panel
  // already says it as `DE.DEBUG_NO_BREAKPOINTS` and the code panel reuses that
  // key. These two are the verbs of the list itself, which BreakpointList
  // carries as inline literals.
  DEBUG_BP_REMOVE: 'Haltepunkt entfernen: {0}',
  DEBUG_BP_CLEAR: 'Alle entfernen',
  // The pause asymmetry of §3.3: a code run is interrupted where it talks to
  // the robot, not between two lines. Saying so beats a student watching a
  // „pausiert" chip over a loop that keeps counting.
  RUN_PAUSE_CODE_HINT: 'Die Pause wirkt beim nächsten Roboter-Befehl oder Haltepunkt.',
  // Decision A14, the disclosed Rule §1 exception: the raw tool line (a CPython
  // traceback line, a `javac` message) is English and is shown anyway, because
  // for Java it is the only text that says what was actually wrong. The German
  // sentence LEADS; this label introduces the raw line beneath it.
  ERROR_TECHNIK_LABEL: 'Technische Meldung:',

  // The Sammlung inside a code program (2026-09-27, owner decisions O4–O9):
  // the editor's warnings and hover texts for asset names, the sidebar section,
  // the „Neu" menu, insertion, and the Variablen notes. The warnings follow
  // sammlung/referenceValidators.js: only a recording or a Ziel/Position the
  // Sammlung verifiably lacks is marked, never an object type.
  ASSET_MISSING_RECORDING: 'Eine Aufnahme „{0}“ gibt es in diesem Programm nicht — nimm sie mit '
    + '„Vormachen“ auf oder wähle eine vorhandene.',
  ASSET_MISSING_PLACE: '„{0}“ ist kein Ziel und keine Position deiner Sammlung und wird im Programm '
    + 'auch nicht gesetzt — lege es an oder wähle ein vorhandenes.',
  ASSET_KIND_PIN: 'Ziel',
  ASSET_KIND_POSE: 'Position',
  ASSET_KIND_CODE_PIN: 'Ziel, im Programm gesetzt',
  ASSET_KIND_RECORDING: 'Aufnahme',
  ASSET_KIND_COUNTER: 'Zähler',
  ASSET_KIND_OBJECT: 'Objekt',
  HOVER_PIN: 'Ziel „{0}“ · x {1} cm · y {2} cm',
  HOVER_POSE: 'Position „{0}“ · x {1} cm · y {2} cm · z {3} cm',
  HOVER_CODE_PIN: 'Ziel „{0}“ · wird im Programm mit pin gesetzt',
  HOVER_RECORDING: 'Aufnahme „{0}“ · {1} s',
  HOVER_RECORDING_VERSIONS: 'Aufnahme „{0}“ · {1} s · {2} Versionen',
  SAMMLUNG_NEW: 'Neu',
  SAMMLUNG_INSERT: 'Einfügen',
  SAMMLUNG_INSERT_TITLE: 'Unter der Zeile einfügen, in der zuletzt der Cursor stand — '
    + 'oder die Zeile in den Code ziehen.',
  INSERTED_AT: 'In {0} ab Zeile {1} eingefügt.',
  SAMMLUNG_OPEN_TAB: '„{0}“ in der Sammlung öffnen',
  SAMMLUNG_NEW_MENU: 'Neu in der Sammlung',
  TEACH_INSERT_LINES: 'Als Programm einfügen ({0} Zeilen)',
  TEACH_INSERT_LINE_ONE: 'Als Programm einfügen (1 Zeile)',
  TEACH_INSERT_FAILED: 'Die Zeilen konnten nicht eingefügt werden.',

  SUBMIT: 'Abgeben',
  SUBMIT_TITLE: 'Diesen Stand des Programms bei der Lehrkraft abgeben',
  SUBMIT_CONFIRM: 'Programm jetzt abgeben? Die Lehrkraft sieht danach genau diesen Stand.',
  SUBMIT_OK: 'Abgegeben.',
  SUBMIT_FAILED: 'Abgeben fehlgeschlagen: {0}',
  SUBMIT_SAVE_FAILED: 'Abgeben abgebrochen — das Programm konnte nicht gespeichert werden.',
  SUBMIT_BUSY: 'Bitte warten, bis das Programm beendet ist.',
});

export function formatCode(template, ...args) {
  return String(template).replace(/\{(\d+)\}/g, (m, i) => (args[i] === undefined ? m : String(args[i])));
}
