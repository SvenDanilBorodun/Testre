/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// MUST stay in sync with server-side coco_classes.py — enforced by a
// Jest test (objectClasses.sync.test.js).
export const OBJECT_CLASSES = [
  'Flasche',
  'Tasse',
  'Gabel',
  'Löffel',
  'Schüssel',
  'Banane',
  'Apfel',
  'Orange',
  'Karotte',
  'Brokkoli',
  'Maus',
  'Fernbedienung',
  'Handy',
  'Buch',
  'Schere',
  'Teddybär',
];

export const COLORS = [
  ['Rot', 'rot'],
  ['Grün', 'gruen'],
  ['Blau', 'blau'],
  ['Gelb', 'gelb'],
];

// Lower-case set used by validators so a typo in a typed dropdown
// option (rare but possible via Blockly's keyboard input) is rejected.
export const ALLOWED_COLOR_VALUES = new Set(['rot', 'gruen', 'blau', 'gelb']);

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
  CATEGORY_AUSGABE: 'Ausgabe',
  CATEGORY_VORSCHLAEGE: 'Vorschläge',

  // Motion blocks
  HOME: 'Heimposition',
  OPEN_GRIPPER: 'Greifer öffnen',
  CLOSE_GRIPPER: 'Greifer schließen',
  MOVE_TO: 'bewege zu %1',
  PICKUP: 'aufnehmen %1',
  DROP_AT: 'ablegen bei %1',
  WAIT_SECONDS: 'warte %1 Sekunden',

  // Perception blocks
  DETECT_COLOR: 'erkenne Farbe %1',
  WAIT_UNTIL_COLOR: 'warte bis Farbe %1 erkannt (max %2 s)',
  COUNT_COLOR: 'Anzahl Farbe %1',
  DETECT_MARKER: 'erkenne Marker %1',
  WAIT_UNTIL_MARKER: 'warte bis Marker %1 erkannt (max %2 s)',
  DETECT_OBJECT: 'erkenne Objekt %1',
  WAIT_UNTIL_OBJECT: 'warte bis Objekt %1 erkannt (max %2 s)',
  COUNT_OBJECT: 'Anzahl Objekt %1',
  DETECT_OPEN_VOCAB: 'finde Objekt mit Beschreibung %1',

  // Destinations
  DESTINATION_PIN: 'setze %1 = Pin (Klick auf Szenenkamera)',
  DESTINATION_CURRENT: 'setze %1 = aktuelle Position',

  // Output
  LOG: 'melde %1',
  PLAY_SOUND: 'Ton spielen',
  SPEAK_DE: 'sage %1',
  PLAY_TONE: 'spiele Ton %1 Hz für %2 s',

  // Events
  BROADCAST: 'sende Ereignis %1',
  WHEN_BROADCAST: 'wenn Ereignis %1 empfangen',
  WHEN_MARKER_SEEN: 'wenn Marker %1 erkannt',
  WHEN_COLOR_SEEN: 'wenn Farbe %1 erkannt (mind. %2 Pixel)',

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
    'Lokaler Speicher voll — bitte einen Workflow exportieren und löschen.',
  AUTOSAVE_RESTORED: 'Letzte Sitzung wiederhergestellt.',

  // Themes
  THEME_STANDARD: 'Standard',
  THEME_TRITANOPIA: 'Tritanopie-freundlich',
  THEME_DEUTERANOPIA: 'Deuteranopie-freundlich',
  THEME_HIGHCONTRAST: 'Hoher Kontrast',

  // Validators
  VALIDATOR_OUT_OF_RANGE: 'Wert außerhalb des erlaubten Bereichs.',
  VALIDATOR_NEGATIVE_NOT_ALLOWED: 'Negative Werte sind nicht erlaubt.',
  VALIDATOR_BAD_COLOR: 'Ungültige Farbe.',
  VALIDATOR_BAD_OBJECT_CLASS: 'Unbekannte Objektklasse.',

  // Run controls (debugger)
  RUN_START: 'Start',
  RUN_PAUSE: 'Pause',
  RUN_STEP: 'Schritt',
  RUN_CONTINUE: 'Weiter',
  RUN_STOP: 'Stopp',
  RUN_READY: 'Bereit',
  RUN_PAUSED: 'Pausiert',

  // Debug panel
  DEBUG_TAB_SENSORS: 'Sensoren',
  DEBUG_TAB_VARIABLES: 'Variablen',
  DEBUG_TAB_BREAKPOINTS: 'Haltepunkte',
  DEBUG_NO_VARIABLES: 'Noch keine Variablen.',
  DEBUG_NO_BREAKPOINTS: 'Noch keine Haltepunkte gesetzt.',
  DEBUG_BP_TOGGLE_HINT:
    'Rechtsklick auf einen Block, um einen Haltepunkt zu setzen.',
  DEBUG_FOLLOWER_JOINTS: 'Folge-Gelenke (rad)',
  DEBUG_GRIPPER_OPENING: 'Greifer geöffnet (rad)',
  DEBUG_VISIBLE_MARKERS: 'Sichtbare Marker',
  DEBUG_COLOR_COUNTS: 'Farb-Pixelzahl',
  DEBUG_VISIBLE_OBJECTS: 'Sichtbare Objekte',

  // Calibration wizard rebuild
  CALIB_STEP_1: 'Schritt 1 von 5',
  CALIB_STEP_2: 'Schritt 2 von 5',
  CALIB_STEP_3: 'Schritt 3 von 5',
  CALIB_STEP_4: 'Schritt 4 von 5',
  CALIB_STEP_5: 'Schritt 5 von 5',
  CALIB_DIVERSITY_HINT:
    '20 Bilder aus verschiedenen Winkeln sind besser als 50 ähnliche Bilder. '
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
  CALIB_COLOR_ONLY: 'Nur Farbprofil neu erfassen',

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

  // Cloud-vision
  CLOUD_VISION_TOGGLE: 'Cloud-Erkennung verwenden',
  CLOUD_VISION_LOADING: 'Cloud-Erkennung wird gestartet …',
};
