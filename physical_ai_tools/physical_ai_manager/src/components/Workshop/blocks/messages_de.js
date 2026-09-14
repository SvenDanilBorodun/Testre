/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Robot-arm safety envelope mirrored from omx_f_config.yaml so the
// editor's setValidator() can clamp move_to coordinates *before* a
// runtime safety_envelope rejects the trajectory. The values here MUST
// stay loose enough that legitimate destinations aren't rejected — the
// server is still the authoritative envelope, this is just a UX hint.
export const WORKSPACE_BOUNDS_M = {
  x: { min: -0.40, max: 0.40 },
  y: { min: -0.40, max: 0.40 },
  z: { min: -0.05, max: 0.50 },
};

export const DE = {
  // Toolbox categories
  CATEGORY_BEWEGUNG: 'Bewegung',
  CATEGORY_WAHRNEHMUNG: 'Wahrnehmung',
  CATEGORY_EREIGNISSE: 'Ereignisse',
  CATEGORY_ZIELE: 'Ziele',
  CATEGORY_LOGIK: 'Logik',
  CATEGORY_LISTE: 'Listen',
  CATEGORY_VARIABLEN: 'Variablen',
  CATEGORY_FUNKTIONEN: 'Funktionen',
  CATEGORY_MATHE: 'Mathe',
  CATEGORY_TEXT: 'Text',
  CATEGORY_AUSGABE: 'Ausgabe',
  CATEGORY_ZAEHLER: 'Zähler',
  CATEGORY_VORSCHLAEGE: 'Vorschläge',
  // Shown in „Vorschläge" before the student has used any block. Replaces the
  // English placeholder @blockly/suggested-blocks hardcodes (BlocklyWorkspace).
  SUGGESTED_EMPTY: 'Noch keine Blöcke benutzt — hier erscheinen die, die du oft verwendest.',

  // @blockly/workspace-backpack ships its five context-menu strings in ENGLISH
  // (`Blockly.Msg.COPY_TO_BACKPACK` …) and Blockly's German catalog defines
  // none of them, so the editor would show „Copy to Backpack" in an otherwise
  // German menu. `BlocklyWorkspace::germanizeBackpackMenu` assigns these onto
  // `Blockly.Msg` after the plugin module loads and before any Backpack is
  // constructed — two of the four menu entries capture their text at
  // REGISTRATION time, so a later assignment would come too late.
  BACKPACK_COPY: 'In den Rucksack kopieren',
  BACKPACK_COPY_ALL: 'Alle Blöcke in den Rucksack kopieren',
  BACKPACK_PASTE_ALL: 'Alle Blöcke aus dem Rucksack einfügen',
  BACKPACK_REMOVE: 'Aus dem Rucksack entfernen',
  BACKPACK_EMPTY: 'Rucksack leeren',

  // Motion blocks
  HOME: 'Heimposition',
  OPEN_GRIPPER: 'Greifer öffnen',
  CLOSE_GRIPPER: 'Greifer schließen',
  MOVE_TO: 'bewege zu %1',
  PICKUP: 'aufnehmen %1',
  DROP_AT: 'ablegen bei %1',
  WAIT_SECONDS: 'warte %1 Sekunden',
  // Grasp split (Phase 1) — explicit grasp-sequence motion steps, each
  // consuming a Greifziel (except LIFT, which has no argument).
  MOVE_ABOVE: 'fahre über %1',
  DESCEND_TO: 'senke auf %1',
  CLOSE_ON_OBJECT: 'schließe um %1',
  LIFT: 'hebe an',

  // Batch 2b — replay a recorded hand-guided motion. The trajectory NAME field
  // is a hard contract with the Python interpreter (`_build_args` lowercases →
  // args['name']; the server resolves it against ctx.trajectories). The run bar
  // (RunControls) injects the referenced trajectories as a top-level sibling.
  REPLAY_TRAJECTORY: 'spiele Bewegung %1 ab',

  // Phase-2 Tempo — OPTIONAL per-move „mit Tempo" dropdown on the transit blocks
  // (bewege zu / fahre über / hebe an / aufnehmen / ablegen). The first option
  // „Standard" → the server's _move_tempo returns None → the global run-bar
  // tempo applies; the others force that move's speed. The field name
  // GESCHWINDIGKEIT is a hard contract (interpreter lowercases → args['geschwindigkeit']).
  TEMPO_FIELD_LABEL: 'Tempo',
  TEMPO_GLOBAL: 'Standard',
  TEMPO_LANGSAM: 'langsam',
  TEMPO_NORMAL: 'normal',
  TEMPO_SCHNELL: 'schnell',

  // Named-object grasping (AprilTag). Custom-init blocks use a prefix label +
  // a runtime-populated dropdown (no %1), so these are prefixes, not templates.
  GRASP_OBJECT_PREFIX: 'Greife',
  SEE_OBJECT_PREFIX: 'sehe ich',
  COUNT_OBJECT_PREFIX: 'Anzahl',
  OBJECT_TYPE_LOADING: '(lädt …)',
  OBJECT_TYPE_EMPTY: '(kein Objekt)',
  // P2 loop + event blocks (custom-init, prefix/suffix fragments around the
  // OBJECT_TYPE dropdown).
  WHILE_VISIBLE_PREFIX: 'Solange',
  WHILE_VISIBLE_SUFFIX: 'sichtbar',
  // #6 student repetition cap on the loop block (0 = unbegrenzt).
  WHILE_VISIBLE_MAX_PREFIX: 'höchstens',
  WHILE_VISIBLE_MAX_SUFFIX: 'Mal',
  WAIT_UNTIL_OBJECT_SEEN_PREFIX: 'warte bis',
  WAIT_UNTIL_OBJECT_SEEN_MID: 'sichtbar, max',
  WAIT_UNTIL_OBJECT_SEEN_SUFFIX: 's',
  // „warte bis Greifer hält (max N s)" — Boolean value block, no dropdown.
  WAIT_UNTIL_HELD_PREFIX: 'warte bis Greifer hält, max',
  WAIT_UNTIL_HELD_SUFFIX: 's',
  WHEN_OBJECT_SEEN_PREFIX: 'wenn',
  WHEN_OBJECT_SEEN_SUFFIX: 'erkannt',
  // Grasp split (Phase 1) — perception blocks producing/consuming a Greifziel.
  // FIND_OBJECT_PREFIX is a prefix label (runtime-populated dropdown, no %1).
  FIND_OBJECT_PREFIX: 'finde',
  OBJECT_POSITION: 'Position von %1',
  GRASP_HELD: 'Greifer hält etwas?',
  MARK_DONE: 'merke %1 als erledigt',

  // Control (Phase-2 quick-win blocks, Logik category)
  FOREVER: 'wiederhole fortlaufend',
  WAIT_UNTIL: 'warte bis %1',
  // Editor-side warning on „wiederhole fortlaufend": the block carries a
  // nextStatement connector, so a student can snap blocks underneath it — where
  // they are silent dead code, because the only exit from the loop is Stopp.
  // The interpreter logs the same fact at RUN time (_exec_forever); this is the
  // half that says so while the program is still being written.
  FOREVER_DEAD_CODE_WARNING:
    // Impersonal, like the rest of this surface — not „Zieh sie".
    'Blöcke unter diesem Block laufen nie. Bitte nach oben oder in die Schleife ziehen.',
  // „sonst" toggle on the Blockly controls_if block (see control.js).
  IF_ELSE_ADD: 'sonst',

  // Destinations
  DESTINATION_PIN: 'setze %1 = Pin (Klick auf Szenenkamera)',
  DESTINATION_CURRENT: 'setze %1 = aktuelle Position',
  DESTINATION_REF: 'Ziel %1',

  // Output
  LOG: 'melde %1',
  PLAY_SOUND: 'Ton spielen',
  SPEAK_DE: 'sage %1',
  PLAY_TONE: 'spiele Ton %1 Hz für %2 s',
  // On-screen message (toast). %1 = text, %2 = severity dropdown, %3 = seconds.
  TOAST: 'zeige Meldung %1 (%2) für %3 s',
  TOAST_LEVEL_INFO: 'Info',
  TOAST_LEVEL_SUCCESS: 'Erfolg',
  TOAST_LEVEL_WARNING: 'Warnung',
  TOAST_LEVEL_ERROR: 'Fehler',

  // Events
  BROADCAST: 'sende Ereignis %1',
  WHEN_BROADCAST: 'wenn Ereignis %1 empfangen',

  // Counters (Zähler) — a named per-run integer counter.
  COUNTER_RESET: 'setze Zähler %1 auf 0',
  COUNTER_ADD: 'erhöhe Zähler %1 um 1',
  COUNTER_GET: 'Zähler %1',
  WHEN_COUNTER_GT: 'wenn Zähler %1 größer als %2',

  // Sensor history
  AVERAGE_LAST_N: 'Mittelwert der letzten %1 Werte von %2',

  // Toolbar / autosave
  TOOLBAR_UNDO: 'Rückgängig',
  TOOLBAR_REDO: 'Wiederholen',
  TOOLBAR_ZOOM_FIT: 'Ansicht anpassen',
  TOOLBAR_SAVE: 'Speichern',
  TOOLBAR_EXPORT: 'Exportieren',
  TOOLBAR_IMPORT: 'Importieren',
  TOOLBAR_PDF_EXPORT: 'Als PDF exportieren',
  TOOLBAR_THEME: 'Farbschema',
  TOOLBAR_SETTINGS: 'Einstellungen',
  AUTOSAVE_LABEL: 'Letzte Speicherung',
  AUTOSAVE_NEVER: 'noch nicht gespeichert',
  AUTOSAVE_JUST_NOW: 'gerade eben',
  AUTOSAVE_SECONDS_AGO: 'vor %1 s',
  AUTOSAVE_MINUTES_AGO: 'vor %1 min',
  AUTOSAVE_QUOTA_FULL:
    'Browser-Speicher voll. Bitte einige gespeicherte Workflows löschen oder Cache leeren.',
  AUTOSAVE_TOO_BIG:
    'Dieser Workflow ist zu groß zum Speichern (Limit 256 KB). Bitte einige Blöcke entfernen.',
  AUTOSAVE_RESTORED: 'Letzte Sitzung wiederhergestellt.',

  // Themes
  THEME_STANDARD: 'Standard',
  THEME_TRITANOPIA: 'Tritanopie-freundlich',
  THEME_DEUTERANOPIA: 'Deuteranopie-freundlich',
  THEME_HIGHCONTRAST: 'Hoher Kontrast',

  // Validators
  VALIDATOR_OUT_OF_RANGE: 'Wert außerhalb des erlaubten Bereichs.',
  VALIDATOR_NEGATIVE_NOT_ALLOWED: 'Negative Werte sind nicht erlaubt.',

  // Run controls (debugger)
  RUN_START: 'Start',
  RUN_PAUSE: 'Pause',
  RUN_STEP: 'Schritt',
  RUN_CONTINUE: 'Weiter',
  RUN_STOP: 'Stopp',
  RUN_READY: 'Bereit',
  RUN_PAUSED: 'Pausiert',
  RUN_RUNNING: 'Läuft',
  RUN_ERROR: 'Fehler',

  // Phase-2 Tempo — global run-bar speed control (langsam ×0.5 / normal ×1 /
  // schnell ×2). Applied to the WHOLE program at the next Start.
  RUN_TEMPO_LABEL: 'Tempo',
  RUN_TEMPO_SLOW: 'langsam',
  RUN_TEMPO_NORMAL: 'normal',
  RUN_TEMPO_FAST: 'schnell',

  // Roboter Studio right dock (Werkzeug-Dock) — the tabbed, collapsible panel
  // that replaced the tall stacked right column. Up to two panels open at once.
  DOCK_TAB_CAMERA: 'Kamera',
  DOCK_TAB_CONTROL: 'Steuern',
  DOCK_TAB_RECORD: 'Aufnehmen',
  DOCK_TAB_3D: '3D-Ansicht',
  DOCK_TAB_TUTORIAL: 'Lernpfad',
  DOCK_TAB_DEBUG: 'Debug',
  DOCK_COLLAPSE: 'Werkzeuge einklappen',
  DOCK_EXPAND: 'Werkzeuge ausklappen',
  DOCK_CLOSE_PANEL: 'Panel schließen',
  DOCK_CLOSE_BUSY: 'Erst beenden, dann schließen.',
  DOCK_OPEN_WORKFLOW: 'Öffnen',
  DOCK_LOG_LABEL: 'Protokoll',
  DOCK_CODE_LABEL: 'Code',

  // Debug panel
  DEBUG_TAB_SENSORS: 'Sensoren',
  DEBUG_TAB_VARIABLES: 'Variablen',
  DEBUG_TAB_BREAKPOINTS: 'Haltepunkte',
  DEBUG_NO_VARIABLES: 'Noch keine Variablen.',
  // The Debug-Panel's „Variablen" tab holds TWO labelled sections: the
  // Blockly variables and the „Zähler" blocks' own store. Separate
  // headings because they are separate stores — a student may have a
  // variable AND a counter both called „Punkte".
  DEBUG_SECTION_VARIABLES: 'Variablen',
  DEBUG_SECTION_COUNTERS: 'Zähler',
  DEBUG_NO_COUNTERS: 'Noch keine Zähler.',
  DEBUG_NO_BREAKPOINTS: 'Noch keine Haltepunkte gesetzt.',
  DEBUG_BP_TOGGLE_HINT:
    'Alt+Klick auf einen Block, um einen Haltepunkt zu setzen.',
  DEBUG_FOLLOWER_JOINTS: 'Folge-Gelenke (rad)',
  DEBUG_GRIPPER_OPENING: 'Greifer geöffnet (rad)',
  DEBUG_VISIBLE_MARKERS: 'Sichtbare Marker',

  // Calibration wizard rebuild
  CALIB_DIVERSITY_HINT:
    'Mindestens 12 Bilder für die Kamerakalibrierung und 14 für die '
    + 'Hand-Auge-Kalibrierung aus verschiedenen Winkeln aufnehmen — '
    + 'wenige diverse Bilder sind besser als viele ähnliche. '
    + 'Mindestens die Hälfte sollte den Würfel schräg zeigen.',
  CALIB_COVERAGE_GAP:
    'Noch Aufnahmen in den schraffierten Bereichen aufnehmen.',
  CALIB_QUALITY_GOOD: 'gut',
  CALIB_QUALITY_OK: 'okay',
  CALIB_QUALITY_POOR: 'schlecht',
  CALIB_QUALITY_BADGE: 'Qualität: %1',
  CALIB_AGREEMENT_EXCELLENT: 'hervorragend',
  CALIB_AGREEMENT_GOOD: 'gut',
  CALIB_AGREEMENT_FAIR: 'mäßig',
  CALIB_AGREEMENT_POOR: 'schlecht',
  CALIB_AGREEMENT_LABEL: 'Übereinstimmung PARK ↔ TSAI: %1',
  CALIB_VERIFY_TITLE: 'Jetzt prüfen',
  CALIB_VERIFY_INSTRUCTION:
    'Lege einen Marker auf die markierte Stelle und klicke auf '
    + '"Position prüfen". Der Roboter zeigt dir, wie gut die Kalibrierung ist.',
  CALIB_VERIFY_RESULT: 'Abweichung: %1 mm',
  CALIB_HISTORY_TITLE: 'Kalibrierungs-Verlauf',
  CALIB_HISTORY_EMPTY: 'Noch keine früheren Kalibrierungen vorhanden.',
  CALIB_HISTORY_LOAD: 'Diese Version laden',

  // Workflow versions
  VERSION_HISTORY: 'Verlauf',
  VERSION_LOAD: 'laden',
  VERSION_NONE: 'Noch keine ältere Version vorhanden.',

  // Skillmaps / tutorials
  SKILLMAP_TITLE: 'Lernpfad',
  TUTORIAL_NEXT: 'Nächster Schritt',
  TUTORIAL_PREV: 'Vorheriger Schritt',
  TUTORIAL_RESTRICT_TOOLBOX: 'Werkzeugkasten passend zum Schritt',
  TUTORIAL_DONE: 'Lernpfad abgeschlossen',

  // Gallery
  GALLERY_TITLE: 'Galerie',
  GALLERY_BY: 'von',
  GALLERY_CLONE: 'Klonen',
  GALLERY_EMPTY: 'Noch keine Workflows in der Galerie.',

  // Sammlung — Ziele and Positionen held in the workflow document
  // (sammlung/destinationStore.js). `%1` is filled by formatDe below.
  ERR_NAME_TAKEN: 'Der Name „%1" ist schon vergeben.',
  ERR_STORE_FULL: 'In diesem Workflow gibt es schon 64 Ziele und Positionen.',
  ERR_COORDINATES: 'Diese Stelle hat keine gültigen Koordinaten.',
  // rename/remove of an entry that is already gone (an undo in between).
  ERR_DESTINATION_MISSING: 'Diesen Eintrag gibt es nicht mehr.',

  // Camera click → Ziel (CameraFeedOverlay + WorkshopPage). No prompt: the
  // point gets an automatic name and an inline rename field at the click.
  CAMERA_ZIEL_CREATED: 'Ziel „%1" gesetzt.',
  CAMERA_PIN_WRITTEN: 'Koordinaten in Block „%1" geschrieben.',
  CAMERA_RENAME_ARIA: 'Ziel umbenennen',
  CAMERA_RENAME_TITLE: 'Enter speichert den Namen, Esc behält ihn.',
  TEACH_AUTO_NAME_ZIEL: 'Ziel %1',

  // Simulator previews (utils/simPreview.js::previewMessageDe). The server's
  // lead-in refusal advises lifting the arm — impossible in a simulator.
  PREVIEW_LEAD_IN_BELOW_TABLE:
    'Die Aufnahme beginnt unter dem Tisch des Simulators — im Simulator kann sie nicht abgespielt werden.',
  // Card ▶ aria-label (sammlung/AssetCardInflater.js); the buttons come later.
  PREVIEW_START: 'Im Simulator ansehen',

  // Sammlung toolbox groups (blocks/toolbox.js + sammlung/toolboxCategories.js).
  CATEGORY_AUFNAHMEN: 'Aufnahmen',
  CATEGORY_POSITIONEN: 'Positionen',
  FLY_TEACH_RECORDING: '✋ Bewegung vormachen',
  FLY_TEACH_POSE: '✋ Position vormachen',
  FLY_TEACH_ZIEL: '✋ Ziel vormachen',
  FLY_MANAGE: 'Alle verwalten …',
  FLY_PIN_CAMERA: 'Ziel in der Kamera setzen',
  FLY_PIN_SIM: 'Ziel auf den Sim-Tisch setzen',
  FLY_PIN_CAMERA_HINT: 'Klicke ins Kamerabild, um ein Ziel zu setzen.',
  FLY_SECTION_MISSING: 'Fehlt im Programm',
  FLY_SECTION_PROGRAM: 'Im Programm gesetzt',
  FLY_SECTION_YOURS: 'Deine Ziele',
  FLY_RECORDINGS_COUNT: '%1 von 16',
  FLY_RECORDINGS_LOADING: 'Aufnahmen werden geladen …',
  FLY_RECORDINGS_ERROR: 'Aufnahmen konnten nicht geladen werden.',
  FLY_RECORDINGS_EMPTY: 'Noch keine Aufnahmen.',
  FLY_RECORDINGS_TEACHER: 'Aufnahmen gehören zu einem Schüler-Workflow.',
  FLY_PLACES_EMPTY: 'Noch keine Ziele in deiner Sammlung.',
  FLY_POSES_EMPTY: 'Noch keine Positionen.',
  FLY_COUNT_VARIABLEN_ONE: '1 Variable',
  FLY_COUNT_VARIABLEN: '%1 Variablen',
  FLY_COUNT_ZIELE_ONE: '1 Ziel',
  FLY_COUNT_ZIELE: '%1 Ziele',
  FLY_COUNT_POSITIONEN_ONE: '1 Position',
  FLY_COUNT_POSITIONEN: '%1 Positionen',

  // Sammlung cards: second line, chips, robot labels (sammlung/assetIndex.js,
  // sammlung/format.js).
  CARD_MANAGE: 'Verwalten',
  CARD_RECORDING_META: '%1 s · %2 Punkte',
  CARD_PLACE_META: 'x %1 · y %2 mm · %3',
  CARD_POSE_META: 'z %1 mm · %2',
  ROBOT_SHORT_OMX: 'OMX',
  ROBOT_SHORT_EDU6: '6-Achs',
  ROBOT_SHORT_EDU1: 'Edu:1',
  ROBOT_LABEL_OMX: 'OpenMANIPULATOR-X',
  ROBOT_LABEL_EDU6: 'EduBotics 6-Achs',
  ROBOT_LABEL_EDU1: 'Edu:1',
  CARD_SOURCE_CAMERA: 'Kamera',
  CARD_SOURCE_SIM: 'Sim-Tisch',
  CARD_SOURCE_TOUCH: 'am Tisch',
  CARD_SOURCE_CAPTURE: 'gemessen',
  CARD_PROGRAM_PIN_META: 'im Programm · x %1 · y %2 mm',
  CARD_VARIABLE_NO_VALUE: 'noch kein Wert',
  CARD_VARIABLE_VALUE: '%1 · vor %2 s',
  CHIP_USED: '%1× benutzt',
  CHIP_UNUSED: 'nicht benutzt',
  CHIP_VERSIONS: '%1 Versionen',
  CHIP_MISSING: 'fehlt',
  CHIP_SEARCHED_ONE_RECORDING: '1 Block sucht diese Aufnahme',
  CHIP_SEARCHED_MANY_RECORDING: '%1 Blöcke suchen diese Aufnahme',
  CHIP_OTHER_ROBOT: 'anderer Roboter',
  CHIP_OVERRIDDEN: 'im Programm überschrieben',
  CHIP_SIM_REFUSED: 'im Simulator abgelehnt',
  CHIP_UNREACHABLE: 'nicht erreichbar',

  // Keyed canvas warnings for names the program uses but the workflow does not
  // have (sammlung/referenceValidators.js).
  WARN_MISSING_RECORDING:
    'Eine Aufnahme „%1" gibt es in diesem Workflow nicht. Nimm sie auf oder wähle eine vorhandene.',
  WARN_MISSING_DESTINATION:
    '„%1" ist nicht in deiner Sammlung — lege das Ziel neu an oder wähle ein vorhandenes.',

  // Sammlung drawer (sammlung/SammlungDrawer.jsx and its detail views).
  SAMMLUNG_TITLE: 'Sammlung',
  DRAWER_CLOSE: 'Sammlung schließen',
  DRAWER_NAME: 'Name',
  DRAWER_RENAME: 'Umbenennen',
  DRAWER_SAVE_NAME: 'Speichern',
  DRAWER_CANCEL: 'Abbrechen',
  DRAWER_DURATION: 'Dauer',
  DRAWER_DURATION_VALUE: '%1 s · %2 Punkte · %3 Hz',
  DRAWER_ROBOT: 'Roboter',
  DRAWER_ROBOT_LEGACY: 'OMX (alte Aufnahme)',
  DRAWER_RECORDED_AT: 'Aufgenommen',
  DRAWER_COORDS: 'Koordinaten',
  DRAWER_COORDS_VALUE: 'x %1 · y %2 · z %3 mm',
  DRAWER_SOURCE: 'Herkunft',
  DRAWER_USED_IN: 'Benutzt in',
  DRAWER_USED_NOWHERE_RECORDING: 'Nirgends — ziehe den Block aus der Gruppe „Aufnahmen" ins Programm.',
  DRAWER_USED_NOWHERE_PLACE: 'Nirgends — ziehe den Block aus der Gruppe „Ziele" ins Programm.',
  DRAWER_USED_NOWHERE_POSE: 'Nirgends — ziehe den Block aus der Gruppe „Positionen" ins Programm.',
  DRAWER_USED_NOWHERE_VARIABLE: 'Nirgends — ziehe den Block aus der Gruppe „Variablen" ins Programm.',
  DRAWER_DISABLED_SUFFIX: '(ausgeschaltet)',
  DRAWER_OLDER_VERSIONS: 'Ältere Versionen',
  DRAWER_VERSION_NOT_PLAYED: 'wird nicht abgespielt',
  DRAWER_DELETE_VERSION: 'Löschen',
  DRAWER_DELETE_RECORDING: 'Aufnahme löschen',
  DRAWER_DELETE_PLACE: 'Ziel löschen',
  DRAWER_DELETE_POSE: 'Position löschen',
  DRAWER_DELETE_VARIABLE: 'Variable löschen',
  DRAWER_EMPTY_TAB: 'Hier ist noch nichts.',
  CONFIRM_DELETE_USED:
    '„%1" wird in %2 Blöcken benutzt. Trotzdem löschen? Die Blöcke bleiben stehen und zeigen danach eine Warnung.',
  CONFIRM_DELETE_USED_ONE:
    '„%1" wird in 1 Block benutzt. Trotzdem löschen? Der Block bleibt stehen und zeigt danach eine Warnung.',
  CONFIRM_REPLACE_RECORDING: 'Eine Bewegung „%1" gibt es schon. Ersetzen?',
  CONFIRM_YES_DELETE: 'Löschen',
  CONFIRM_YES_REPLACE: 'Ersetzen',
  UNDO: 'Rückgängig',
  TOAST_DELETED: '„%1" gelöscht.',
  TOAST_RENAMED: '„%1" heißt jetzt „%2".',
  ERR_RENAME_FAILED: 'Umbenennen fehlgeschlagen: %1',
  ERR_RENAME_SPLIT:
    'Achtung: Die Aufnahme heißt jetzt „%1", der Workflow ist aber noch nicht gespeichert. Bitte erneut speichern.',
  TOAST_DISMISS: 'Schließen',
  ERR_DELETE_FAILED: 'Löschen fehlgeschlagen: %1',
  ERR_RECORDING_NAME:
    'Der Name darf nur Buchstaben, Ziffern, Leerzeichen, _ und - enthalten (höchstens 40 Zeichen).',
};

/**
 * Fill `%1`, `%2`, … in a DE template with the given arguments.
 *
 * A placeholder with no matching argument stays as written (`%2` with one
 * argument reads `%2`), so a missing argument is visible in the UI instead of
 * silently becoming „undefined".
 */
export function formatDe(template, ...args) {
  return String(template).replace(/%(\d)/g, (m, i) => (args[Number(i) - 1] ?? m));
}
