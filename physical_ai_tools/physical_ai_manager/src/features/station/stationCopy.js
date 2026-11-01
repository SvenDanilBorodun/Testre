// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Every German string of the camera station on the page, and every code the
// three station services answer with (station spec v6.13, §8, §9.2: „German
// strings live in one copy module; no English in the UI").
//
// THE CODES ARE SPELLED EXACTLY AS THE `.srv` COMMENTS SPELL THEM (§8.1–§8.3);
// the cross-package lockstep test (robotis_ai_setup/tests/test_station_lockstep
// .py) compares these lists with the interface files and the server. The
// server's own German `message` wins wherever it sends one; the sentences
// below are the page's fallback for a code the answer carries without text,
// and the page's own sentences (§9.1).

/** StationSession `state` (§8.1). */
export const SESSION_STATES = Object.freeze(['OFF', 'IDLE', 'ENTERING', 'ACTIVE']);

/** StationSession `reason_code` (§8.1). */
export const SESSION_REASON_CODES = Object.freeze([
  'RESUMED',
  'LEADER_ACTIVE',
  'OTHER_MODE_ACTIVE',
  'RETORQUE_PENDING',
  'BUSY_OTHER_TAB',
  'NATIVE_TIMEOUT',
  'NOT_STATION_RIG',
  'OWNER_MISMATCH',
  'SESSION_ENDED',
  'WORKFLOW_ACTIVE',
]);

/** StationSession `end_reason` (§8.1); '' is an unknown end. */
export const SESSION_END_REASONS = Object.freeze([
  'NATIVE_TIMEOUT',
  'LEADER_ACTIVE',
  'KEEPALIVE_LOST',
  'LEFT',
]);

/** StationCheck `result` (§8.2). */
export const CHECK_RESULTS = Object.freeze([
  'OK',
  'QUEUED',
  'MOVED',
  'WRONG_MAT',
  'REFS_MISSING',
  'NO_FRAME',
  'WRONG_SIZE',
  'NOT_CONFIGURED',
  'NOT_OWNER',
  'BUSY',
  'NOT_VERTICAL',
  'NO_DOT',
  'ROBOT_MOVED',
  'RESOLVE_REFUSED',
  'FAILED',
  'INVALID',
]);

/** StationCommission `result` (§8.3). */
export const COMMISSION_RESULTS = Object.freeze([
  'OK',
  'QUEUED',
  'NO_FRAME',
  'NOT_OWNER',
  'REFUSED',
  'FAILED',
  'INVALID',
]);

/** `job_state` of StationCheck and StationCommission (§8.2, §8.3). */
export const JOB_STATES = Object.freeze(['none', 'queued', 'running', 'retry_wait', 'done', 'failed', 'stale']);

/** The commands of each service (§8.1–§8.3). */
export const SESSION_COMMANDS = Object.freeze(['enter', 'leave', 'keepalive', 'status']);
export const CHECK_COMMANDS = Object.freeze(['camera', 'robot', 'resolve_camera', 'status']);
export const COMMISSION_COMMANDS = Object.freeze([
  'begin',
  'lens_capture',
  'lens_solve',
  'board_solve',
  'touch_begin',
  'touch_capture',
  'touch_solve',
  'mark_capture',
  'mark_solve',
  'commit',
  'cancel',
  'restore_previous',
  'status',
]);

/** Fallback sentences per StationSession `reason_code` (§5.2, §5.3). */
export const SESSION_REASON_DE = Object.freeze({
  RESUMED: 'Die Kamerastation läuft weiter.',
  LEADER_ACTIVE: 'Bitte zuerst den Leader-Arm ausschalten (Schalter in Roboter Studio).',
  OTHER_MODE_ACTIVE: 'Der Roboter ist gerade beschäftigt – bitte zuerst die laufende Aufgabe beenden.',
  RETORQUE_PENDING: 'Die Einrichtung wird gerade beendet – bitte kurz warten.',
  BUSY_OTHER_TAB: 'Roboter Studio ist in einem anderen Fenster geöffnet.',
  NATIVE_TIMEOUT: 'Die Stationskamera liefert kein 1080p-Bild.',
  NOT_STATION_RIG: 'Dieser Roboter ist keine Kamerastation.',
  OWNER_MISMATCH: 'Die Kamerastation wird jetzt in einem anderen Fenster benutzt.',
  SESSION_ENDED: 'Die Kamerastation wurde beendet.',
  WORKFLOW_ACTIVE: 'Stoppe zuerst dein Programm.',
});

/** Why the page's own session ended (§5.2 end reasons). */
export const SESSION_END_DE = Object.freeze({
  NATIVE_TIMEOUT: 'Die Stationskamera liefert kein 1080p-Bild.',
  LEADER_ACTIVE: 'Die Kamerastation wurde beendet, weil der Leader-Arm eingeschaltet ist.',
  KEEPALIVE_LOST: 'Die Kamerastation wurde beendet, weil die Verbindung zu lange unterbrochen war.',
  LEFT: 'Die Kamerastation wurde verlassen.',
  '': 'Die Kamerastation wurde beendet.',
});

/** Fallback sentences per StationCheck `result` (§7.1–§7.3). */
export const CHECK_RESULT_DE = Object.freeze({
  OK: 'In Ordnung.',
  QUEUED: 'Wird ausgeführt …',
  MOVED: 'Die Kamera wurde seit der Einrichtung bewegt.',
  WRONG_MAT: 'Diese Matte gehört nicht zu dieser Kalibrierung.',
  REFS_MISSING: 'Marker nicht gefunden – bitte die Matte freiräumen. Hilft das nicht, ist die Kamera stark '
    + 'verstellt (Lehrkraft).',
  NO_FRAME: 'Gerade kommt kein aktuelles Bild an.',
  WRONG_SIZE: 'Das Kamerabild hat die falsche Größe.',
  NOT_CONFIGURED: 'Die Kamerastation ist noch nicht eingerichtet oder ihre Kalibrierung ist ungültig – bitte '
    + 'Lehrkraft: Station einrichten.',
  NOT_OWNER: 'Dieses Fenster hält die Kamerastation nicht.',
  BUSY: 'Gerade läuft schon eine Prüfung.',
  NOT_VERTICAL: 'Der Greifer steht nicht senkrecht – bitte gerade auf den Punkt setzen.',
  NO_DOT: 'Kein Punkt in der Nähe – bitte die Spitze genau auf einen der vier Punkte setzen.',
  ROBOT_MOVED: 'Roboter oder Matte verschoben – bitte Lehrkraft: Station neu einrichten.',
  RESOLVE_REFUSED: 'Kamera zu stark verstellt – vollständige Einrichtung nötig.',
  FAILED: 'Der Arm konnte nicht verriegelt werden.',
  INVALID: 'Ungültige Anfrage.',
});

/** Fallback sentences per StationCommission `result` (§8.3). */
export const COMMISSION_RESULT_DE = Object.freeze({
  OK: 'Erledigt.',
  QUEUED: 'Wird ausgeführt …',
  NO_FRAME: 'Gerade kommt kein aktuelles Bild an.',
  NOT_OWNER: 'Dieses Fenster hält die Kamerastation nicht.',
  REFUSED: 'Das geht gerade nicht.',
  FAILED: 'Der Schritt ist fehlgeschlagen.',
  INVALID: 'Ungültige Anfrage.',
});

/** What a job is doing (§8.2, §8.3 `job_state`). */
export const JOB_STATE_DE = Object.freeze({
  none: '',
  queued: 'Wartet …',
  running: 'Läuft …',
  retry_wait: 'Wird gleich noch einmal versucht …',
  done: 'Fertig.',
  failed: 'Fehlgeschlagen.',
  stale: 'Nicht ausgeführt – der Zustand hat sich inzwischen geändert.',
});

/** The page's own sentences (§9.1, §9.2). */
export const STATION_DE = Object.freeze({
  title: 'Kamerastation',
  // §9.1 step 1 (D7).
  leaderFirst: 'Für die Kamerastation zuerst den Leader-Arm ausschalten.',
  // §9.1 step 3.
  linkLost: 'Verbindung zum Roboter unterbrochen – die Kamerastation wird beendet.',
  guiUnreachable: 'EduBotics-Programm nicht erreichbar – die Stationskamera ist aus.',
  guiRefused: 'Das EduBotics-Programm hat die Stationskamera nicht eingeschaltet.',
  enterNoAnswer: 'Der Roboter antwortet gerade nicht – die Kamerastation wird gleich noch einmal gestartet.',
  oldImage: 'Die Roboter-Software kennt die Kamerastation noch nicht – bitte die Umgebung aktualisieren.',
  // §9.1 step 4.
  stopProgramFirst: 'Stoppe zuerst dein Programm.',
  leave: 'Station verlassen',
  start: 'Station starten',
  leaveAndLeaderOn: 'Station verlassen und Leader einschalten',
  leaving: 'Station wird verlassen …',
  entering: 'Kamerastation wird gestartet …',
  // §9.2 page gates on other pages (D13).
  busyElsewhere: 'Roboter Studio nutzt gerade die Kamerastation.',
  // §5.4 sentences, shown before the server says them.
  notConfigured: 'Die Kamerastation ist noch nicht eingerichtet oder ihre Kalibrierung ist ungültig – bitte '
    + 'Lehrkraft: Station einrichten.',
  notVerified: 'Bitte zuerst den Stations-Check abschließen (Kamera und Roboter).',
  checkingConfig: 'Die Kamerastation wird abgefragt …',
  // State lines.
  stateIdle: 'Nicht gestartet',
  stateEntering: 'Kamera startet …',
  stateActive: 'Aktiv',
  stateVerified: 'Geprüft – der Roboter darf sich bewegen',
  sourceWaiting: 'Warte auf das Stationsbild …',
  // The three steps (§9.2).
  stepCamera: 'Kamera prüfen',
  stepRobot: 'Roboter prüfen',
  stepResolve: 'Kamera neu ausrichten (Lehrkraft)',
  commission: 'Station einrichten (Lehrkraft)',
  cameraOk: 'Kamera in Ordnung',
  cameraOpen: 'Kamera noch nicht geprüft',
  cameraRunning: 'Kamera wird geprüft …',
  robotOk: 'Roboter in Ordnung',
  robotRunning: 'Roboter wird geprüft …',
  robotOpen: 'Roboter noch nicht geprüft',
  cameraClearlyMoved: 'Kamera deutlich verstellt – Turm oder Matte verschoben?',
  // §7.3: the re-solve's confirmation.
  resolveConfirmTitle: 'Kamera neu ausrichten',
  resolveConfirm: 'Nur nach Rücksprache mit der Lehrkraft',
  resolveConfirmBody: 'Räume die Matte frei. Die Kamera wird dann neu auf die Matte ausgerichtet; danach musst '
    + 'du den Roboter noch einmal prüfen.',
  resolveGo: 'Neu ausrichten',
  cancel: 'Abbrechen',
  resolveRunning: 'Kamera wird neu ausgerichtet …',
  // §7.2: the robot check.
  robotIntro: 'Der Arm wird freigeschaltet. Setze die Greiferspitze genau auf einen der vier Punkte auf der '
    + 'Matte, halte den Greifer senkrecht und klicke „Prüfen“.',
  robotCheckGo: 'Prüfen',
  robotFreeing: 'Arm wird freigeschaltet …',
  robotFreeFailed: 'Der Arm konnte nicht freigeschaltet werden.',
  robotRelockFailed: 'Der Arm konnte nicht festgesetzt werden – bitte „Arm festsetzen“ im Tab Steuern drücken.',
  // §9.2 Startseite (utils/homeHealth).
  homeConfigured: 'Kamerastation eingerichtet',
  homeNotConfigured: 'Kamerastation nicht eingerichtet',
  // The metric line of a camera check.
  metrics: (rms, max) => `Abweichung im Mittel ${numberDe(rms)} mm, höchstens ${numberDe(max)} mm`,
  robotMetrics: (dot, dxy, dz) => `Punkt ${dot}: seitlich ${numberDe(dxy)} mm, Höhe ${numberDe(dz)} mm`,
});

/** The commissioning wizard (§6, §9.2). */
export const COMMISSION_DE = Object.freeze({
  title: 'Station einrichten',
  intro: 'Die Einrichtung macht die Lehrkraft einmal pro Station: Linse, Matte, Prüfmarken, dann der Roboter. '
    + 'Sie dauert etwa 15 Minuten.',
  begin: 'Einrichtung starten',
  restore: 'Vorherige Kalibrierung wiederherstellen',
  close: 'Schließen',
  cancel: 'Einrichtung abbrechen',
  cancelConfirm: 'Einrichtung abbrechen? Die bisherigen Schritte gehen verloren.',
  waitJob: 'Bitte warten, der Roboter ist noch beschäftigt …',
  steps: Object.freeze({
    lens: 'Linse',
    board: 'Matte ausrichten',
    marks: 'Prüfmarken',
    touch: 'Roboter',
    commit: 'Speichern',
  }),
  lensIntro: 'Halte die Linsen-Tafel (Marker 100–116) vor die Kamera: in jede Ecke, nah und fern, schräg nach '
    + 'oben, unten, links und rechts. Klicke bei jeder Stellung „Bild aufnehmen“.',
  lensCapture: 'Bild aufnehmen',
  lensSolve: 'Linse berechnen',
  lensCounts: (fit, held, cells) => `${fit} Bilder für die Berechnung, ${held} zur Kontrolle, ${cells} von 16 `
    + 'Bildfeldern abgedeckt',
  lensResult: (fit, held, max) => `Fehler ${numberDe(fit, 2)} px (Kontrolle ${numberDe(held, 2)} px, höchstens `
    + `${numberDe(max, 2)} px)`,
  boardIntro: 'Räume die Matte ganz frei. Dann wird die Kamera auf die gedruckte Tafel ausgerichtet.',
  boardSolve: 'Matte ausrichten',
  boardResult: (px) => `Ausgerichtet (Abweichung ${numberDe(px, 2)} px)`,
  marksIntro: 'Stelle einen Würfel mit Marker genau auf die angezeigte Prüfmarke und drehe ihn, wie angezeigt. '
    + 'Dann „Würfel erfassen“.',
  markNext: (mark, turn) => `Als Nächstes: Marke ${mark}, Würfel ${turn ? 'um 90° gedreht' : 'gerade'}`,
  markCapture: 'Würfel erfassen',
  markSolve: 'Prüfung auswerten',
  marksDone: 'Alle Prüfmarken erfasst.',
  marksResult: (n, rms, max) => `${n} Messungen, Abweichung im Mittel ${numberDe(rms)} mm, höchstens `
    + `${numberDe(max)} mm`,
  touchIntro: 'Räume die Würfel weg. Der Arm wird freigeschaltet; setze die Greiferspitze nacheinander auf die '
    + 'Punkte 1 bis 4 und klicke jeweils „Punkt erfassen“.',
  touchBegin: 'Arm freischalten',
  touchNext: (dot) => `Als Nächstes: Punkt ${dot}`,
  touchCapture: 'Punkt erfassen',
  touchSolve: 'Roboter vermessen',
  touchResult: (rms, shift, rot) => `Abweichung ${numberDe(rms)} mm, Verschiebung ${numberDe(shift)} mm, Drehung `
    + `${numberDe(rot, 2)}°`,
  commit: 'Einrichtung speichern',
  committed: 'Die Kamerastation ist eingerichtet.',
  noCommission: 'Gerade läuft keine Einrichtung.',
});

/** A decimal with a German comma; unknown is „–", never 0. */
export function numberDe(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '–';
  const n = Number(value);
  if (!Number.isFinite(n)) return '–';
  return n.toFixed(digits).replace('.', ',');
}

/** The sentence for a StationSession answer: the server's text, else ours. */
export function sessionMessageDe(res) {
  if (res && typeof res.message === 'string' && res.message.trim()) return res.message;
  const code = res && typeof res.reason_code === 'string' ? res.reason_code : '';
  return SESSION_REASON_DE[code] || '';
}

/** The sentence for an end: the server's text with its end reason, else ours. */
export function endMessageDe(res) {
  const reason = res && typeof res.end_reason === 'string' ? res.end_reason : '';
  if (res && typeof res.message === 'string' && res.message.trim() && reason) return res.message;
  return SESSION_END_DE[reason] || SESSION_END_DE[''];
}

/** The sentence for a check answer (§8.2): the server's, else ours. */
export function checkMessageDe(res) {
  if (res && typeof res.message === 'string' && res.message.trim()) return res.message;
  const code = res && typeof res.result === 'string' ? res.result : '';
  return CHECK_RESULT_DE[code] || '';
}

/** The sentence for a commission answer (§8.3): the server's, else ours. */
export function commissionMessageDe(res) {
  if (res && typeof res.message === 'string' && res.message.trim()) return res.message;
  const code = res && typeof res.result === 'string' ? res.result : '';
  return COMMISSION_RESULT_DE[code] || '';
}
