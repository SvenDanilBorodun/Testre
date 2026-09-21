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
// The cap refusals mirror the server's `code_program.py` sentences word for
// word, so a student who trips the same cap after a round-trip reads the same
// sentence.

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
  RUN_EMPTY: 'Das Programm ist leer.',
  RUN_TOO_BIG: 'Das Programm ist zu groß, um es zu starten — bitte Dateien kürzen.',
  SAVE_EMPTY: 'Das Programm ist leer.',
  NEW_PROGRAM_NAME: 'Neues Programm',

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
