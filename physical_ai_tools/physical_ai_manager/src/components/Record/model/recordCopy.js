// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Every German string of the Aufnahme page (spec §3.14), in one place.
//
// German UI, English code (CLAUDE.md Rule §1): real umlauts, typographic
// quotes, no icon glyphs (the icons are components/icons), and the page's own
// vocabulary (owner decision F6a): „Leader-Arm" / „Follower-Arm", never
// Leitarm/Folgearm, and the cameras by their role („Greifer-Kamera",
// „Szenen-Kamera"). Key labels are words („Leertaste", „Strg+Umschalt+X"),
// never ⇧/⌘ glyphs (H9).
//
// Groups are shaped like the props of the components that print them, so
// the page can hand a group over as it is.

import { VALIDATION_DE, formatMinSec } from '../../../utils/recordTaskInfo';
import { COLLISION_END_NOTE_DE } from '../../../features/tasks/recordSession';
import { CAMERA_ROLE_LABELS_DE } from '../../../utils/robotIdentity';

export { VALIDATION_DE, COLLISION_END_NOTE_DE };

const episodes = (n) => (n === 1 ? 'Episode' : 'Episoden');

/** „Greifer-Kamera" / „Szenen-Kamera" / „Kamera „x"" for an unknown role. */
export function cameraNameDe(role) {
  return CAMERA_ROLE_LABELS_DE[role] || `Kamera „${role}“`;
}

/** „Follower-Arm" / „Leader-Arm". */
export const armNameDe = (kind) => (kind === 'leader' ? 'Leader-Arm' : 'Follower-Arm');

/** A decimal with a German comma: 11.2 → „11,2"; whole numbers stay whole. */
export function numberDe(value, digits = 1) {
  if (value === null || value === undefined || value === '') return '–';
  const n = Number(value);
  if (!Number.isFinite(n)) return '–';
  const fixed = Number.isInteger(n) ? String(n) : n.toFixed(digits);
  return fixed.replace('.', ',');
}

/** Bytes as „2,4 GB" (the server's format_gb_de). */
export const gbDe = (bytes) => `${(Number(bytes) / 1e9).toFixed(1).replace('.', ',')} GB`;

/** „HH:MM" of a wall time, local. */
export function clockDe(wallMs) {
  if (!Number.isFinite(wallMs)) return '';
  const d = new Date(wallMs);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** „mm:ss" of seconds, for the REC badge. */
export function mmss(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export const RECORD_COPY = Object.freeze({
  header: Object.freeze({
    eyebrow: 'Aufnahme',
    fallbackTitle: 'Neue Aufgabe',
  }),

  view: Object.freeze({
    group: 'Ansicht',
    cams: 'Kameras',
    '3d': '3D',
    both: 'Beides',
  }),

  preset: Object.freeze({
    group: 'Blickwinkel',
    persp: 'Perspektive',
    front: 'Vorne',
    side: 'Seite',
    top: 'Oben',
  }),

  tiles: Object.freeze({
    tile3d: 'Follower-Arm live',
    twinLoading: '3D-Modell wird geladen …',
    noStream: 'Kamerabild nicht verfügbar',
  }),

  track: Object.freeze({
    aria: 'Phasenleiste',
    warmup: 'Aufwärmen',
    record: (n) => `Aufnehmen · Ep. ${n}`,
    save: 'Speichern',
    reset: 'Zurücksetzen',
    left: (s) => `noch ${s} s`,
    saving: 'speichert',
    paused: 'pausiert',
    idle: 'Bereit · Aufwärmen → Aufnehmen → Speichern → Zurücksetzen',
    allDone: 'Alle Episoden aufgenommen',
  }),

  // PhaseOverlay's `copy` (F6d wording).
  overlay: Object.freeze({
    warmKicker: 'Vor Episode 1',
    warmTitle: 'Aufwärmen',
    warmLine: 'Nimm den Leader-Arm in die Hand. Gleich startet Episode 1.',
    resetKicker: (n) => `Nach Episode ${n}`,
    resetTitle: 'Zurücksetzen',
    resetLine: 'Stell alles wieder so hin wie am Anfang: Gegenstände an den Start, Leader-Arm in die '
      + 'Ausgangslage.',
    next: (n, N, E) => ({ pre: 'Als Nächstes: ', bold: `Episode ${n} von ${N}`, post: ` · ${E} s Aufnahme` }),
    pictoLeader: 'Leader-Arm',
    pictoBack: 'zurück zum Start',
    los: 'Los!',
  }),

  rec: Object.freeze({
    badge: (seconds) => `REC ${mmss(seconds)}`,
    until: 'noch bis zum Speichern',
  }),

  chips: Object.freeze({
    saving: (n) => `Episode ${n} wird gespeichert …`,
    saved: (n) => `Episode ${n} gespeichert`,
  }),

  pill: Object.freeze({
    ready: 'Bereit',
    readySub: (N, E) => `${N} ${episodes(N)} à ${E} s`,
    starting: 'Startet …',
    startingSub: 'Aufnahme wird vorbereitet',
    startingSlow: 'Dauert länger als gewohnt …',
    warmup: 'Aufwärmen',
    warmupSub: (s) => `noch ${s} s · dann Episode 1`,
    recording: 'Aufnahme',
    recordingSub: (n, N, s) => `Episode ${n} von ${N} · noch ${s} s`,
    saving: 'Speichern …',
    savingSub: (n, N) => `Episode ${n} von ${N}`,
    resetting: 'Zurücksetzen',
    resettingSub: (s, n) => `noch ${s} s · dann Episode ${n}`,
    collision: 'Unterbrochen',
    collisionSub: 'Kollision',
    finishing: 'Wird abgeschlossen',
    done: 'Fertig',
    doneSub: (k) => `${k} ${episodes(k)} gespeichert`,
    stopped: 'Aufnahme gestoppt',
    finalizeFailed: 'Mit Fehler beendet',
    offline: 'Nicht verbunden',
    connecting: 'Verbinde …',
    inference: 'Inferenz läuft (Aufnahme nicht möglich)',
  }),

  dots: Object.freeze({
    label: 'Episoden',
    more: (k) => `+${k}`,
  }),

  btn: Object.freeze({
    start: 'Aufnahme starten',
    skipWarmup: 'Jetzt starten',
    skipReset: 'Jetzt weiter',
    redo: 'Wiederholen',
    saveNow: 'Jetzt speichern',
    end: 'Beenden',
    endCollision: 'Erst das Kollisionsfenster abschließen.',
  }),

  kbd: Object.freeze({
    space: 'Leertaste',
    right: '→',
    left: '←',
    end: 'Strg+Umschalt+X',
    esc: 'Esc',
  }),

  mute: Object.freeze({
    labelOff: 'Ton ausschalten',
    labelOn: 'Ton einschalten',
  }),

  question: Object.freeze({
    endTitle: (n) => `Episode ${n} ist noch nicht fertig.`,
    endSub: 'Die Zeit läuft weiter, bis du dich entscheidest.',
    keep: 'Behalten und beenden',
    discard: 'Verwerfen und beenden',
    back: 'Weiter aufnehmen',
  }),

  card: Object.freeze({
    offlineTitle: 'Keine Verbindung zum Roboter',
    offlineWindows: 'Die Umgebung läuft nicht. Öffne EduBotics auf dem Desktop und klicke „Umgebung starten“. '
      + 'Deine Eingaben rechts bleiben erhalten.',
    offlinePi: 'Die Umgebung läuft nicht. Starte sie im Tab „System“. Deine Eingaben rechts bleiben erhalten.',
    connecting: 'Verbindung zum Roboter wird hergestellt …',
    inference: 'Gerade läuft eine Inferenz — Beende sie im Tab Inferenz, bevor du aufnimmst.',
    collisionTitle: 'Aufnahme unterbrochen: Kollision',
    collision: (n) => `Aufnahme unterbrochen: Kollision — Episode ${n} wurde verworfen. Das Kollisionsfenster `
      + 'führt dich in zwei Schritten zurück. Danach geht es mit Zurücksetzen und derselben Episode weiter.',
    collisionBody: (n) => `Episode ${n} wurde verworfen. Das Kollisionsfenster führt dich in zwei Schritten `
      + 'zurück. Danach geht es mit Zurücksetzen und derselben Episode weiter.',
  }),

  // Problem banner sentences (spec §3.9). The first sentence is the problem,
  // the rest what to do.
  problem: Object.freeze({
    linkLost: 'Die Verbindung zum Roboter ist unterbrochen. Die Aufnahme läuft auf dem Roboter weiter.',
    diskLow: (free, floor) => `Nur noch ${gbDe(free)} frei. Zum Aufnehmen sind mindestens ${gbDe(floor)} `
      + 'nötig. Lösche alte Datensätze im Tab Daten.',
    diskLowRecording: (free, critical) => `Der Speicher wird knapp (nur noch ${gbDe(free)} frei). Unter `
      + `${gbDe(critical)} beendet der Roboter die Aufnahme von selbst. Lösche danach alte Datensätze im Tab Daten.`,
    diskCritical: (free) => `Der Speicher ist fast voll (nur noch ${gbDe(free)} frei). Lösche alte Datensätze `
      + 'im Tab Daten.',
    cameraStalled: (role) => `Die ${cameraNameDe(role)} sendet keine Bilder. Prüfe das Kabel. Hilft das `
      + 'nicht, starte die Umgebung neu.',
    followerStalled: 'Der Follower-Arm sendet keine Daten. Prüfe Kabel und Stromversorgung des Follower-Arms.',
    leaderOff: 'Der Leader-Arm ist ausgeschaltet (nur Follower-Arm). Schalte ihn im Roboter Studio wieder ein, '
      + 'dann kannst du aufnehmen.',
    leaderNotActivated: 'Der Roboter ist nicht aktiviert, der Leader-Arm sendet keine Daten. Aktiviere ihn auf '
      + 'der Startseite.',
    leaderStalled: 'Der Leader-Arm sendet keine Daten. Prüfe Kabel und Stromversorgung des Leader-Arms.',
    cameraSlow: (role, hz, fps) => `Die ${cameraNameDe(role)} liefert nur ${numberDe(hz)} statt ${numberDe(fps)} `
      + 'Bilder pro Sekunde. Aufnehmen geht, die Videos ruckeln aber. Steck die Kamera direkt am PC ein, '
      + 'nicht am USB-Hub.',
    armSlow: (kind, hz, fps) => `Der ${armNameDe(kind)} meldet nur ${numberDe(hz)} statt mindestens `
      + `${numberDe(fps)} Messungen pro Sekunde. Die Aufnahme kann ruckeln.`,
    startUploading: 'Dieser Datensatz wird gerade noch hochgeladen. Warte, bis das Hochladen fertig ist, oder '
      + 'wähle einen anderen Aufgabennamen.',
    timeout: 'Der Roboter hat nicht rechtzeitig geantwortet. Bitte versuch es noch einmal.',
    noConnection: 'Keine Verbindung zum Roboter.',
    homeLabel: 'Startseite',
  }),

  note: Object.freeze({
    redo: (n) => `Episode ${n} wird wiederholt.`,
    nothingSaved: 'Beendet. Es wurde keine Episode gespeichert, also wird nichts hochgeladen.',
    alreadySaved: 'Die Episode war schon fertig und wurde gespeichert.',
    // Q8: the run started too soon after „Wiederholen" — the robot drops it.
    keepDroppedAfterRedo: 'Diese Episode war zu kurz nach dem Wiederholen und wird nicht gespeichert.',
  }),

  // TaskCard's `labels`.
  task: Object.freeze({
    title: 'Aufgabe',
    chipEditable: 'bearbeitbar',
    chipLocked: 'gesperrt',
    name: 'Aufgabenname',
    namePlaceholder: 'z. B. Würfel in die Schale',
    instruction: 'Aufgabenanweisung',
    instructionPlaceholder: 'z. B. Greife den roten Würfel und lege ihn in die Schale.',
    flow: 'Ablauf',
    flowParts: 'Aufwärmen · Episode · Zurücksetzen · Episoden',
    dec: (l) => `${l} verringern`,
    inc: (l) => `${l} erhöhen`,
    advanced: 'Erweitert',
    upload: 'Nach dem Beenden hochladen',
    visibility: 'Sichtbarkeit',
    private: 'Privat',
    public: 'Öffentlich',
    userId: 'Benutzer-ID',
    reload: 'Neu laden',
    noUserId: 'Keine Benutzer-ID gefunden',
    fps: 'Bilder pro Sekunde',
    tags: 'Stichwörter',
  }),

  stepper: Object.freeze({
    warmupTime: 'Aufwärmen',
    episodeTime: 'Episode',
    resetTime: 'Zurücksetzen',
    numEpisodes: 'Episoden',
    seconds: 's',
  }),

  estimate: (totalS, N) => `≈ ${formatMinSec(totalS)} min für ${N} ${episodes(N)}. Aufwärmen nur einmal am `
    + 'Anfang, nach der letzten Episode kein Zurücksetzen.',

  save: Object.freeze({
    private: 'Wird privat auf Hugging Face gespeichert als',
    public: 'Wird ÖFFENTLICH auf Hugging Face gespeichert als',
    local: 'Wird nur auf diesem Rechner gespeichert als',
  }),

  locked: Object.freeze({
    running: 'Während der Aufnahme gesperrt.',
    offline: 'Nicht verbunden. Du kannst die Aufgabe bearbeiten, sobald der Roboter verbunden ist.',
    inference: 'Gesperrt, solange eine Inferenz läuft.',
    finished: 'Die Aufnahme ist beendet. Klicke „Neue Aufnahme“, um die nächste Aufgabe einzugeben.',
  }),

  session: Object.freeze({
    title: 'Diese Sitzung',
    chip: (k) => `${k} gespeichert`,
    empty: 'Noch keine Episode in dieser Sitzung.',
    episode: (n) => `Episode ${n}`,
    saved: (hhmm) => `Gespeichert · ${hhmm}`,
    savedEarly: (hhmm) => `Gespeichert (vorzeitig) · ${hhmm}`,
    outcome: Object.freeze({
      redo: 'Wiederholt, verworfen',
      collision: 'Kollision, verworfen',
      drop: 'Bildverlust, verworfen',
      ended: 'Beim Beenden verworfen',
    }),
    discardedSub: (n, hhmm) => `Episode ${n} · ${hhmm}`,
    adopted: (k) => (k === 1
      ? '1 Episode wurde vor dem Neuladen gespeichert.'
      : `${k} Episoden wurden vor dem Neuladen gespeichert.`),
    sumLeft: (k, N) => `${k} von ${N} ${episodes(N)}`,
    sumRight: (mmssText) => `Gesamt ${mmssText} min`,
  }),

  finish: Object.freeze({
    eyebrowRunning: 'Wird abgeschlossen',
    eyebrowDone: 'Fertig',
    titleRunning: (k) => {
      if (!(k > 0)) return 'Aufnahme wird abgeschlossen …';
      return k === 1 ? '1 Episode wird gespeichert' : `${k} Episoden werden gespeichert`;
    },
    titleDone: 'Dein Datensatz ist bereit',
    titleFailed: 'Hochladen fehlgeschlagen',
    titleStopped: 'Die Aufnahme wurde gestoppt',
    eyebrowFinalizeFailed: 'Mit Fehler beendet',
    titleFinalizeFailed: 'Datensatz unvollständig',
    stepFinalize: 'Datensatz abschließen',
    stepUpload: 'Zu Hugging Face hochladen',
    stepUploadPrivate: 'Zu Hugging Face hochladen (privat)',
    stepUploadPublic: 'Zu Hugging Face hochladen (öffentlich)',
    stepRegister: 'In deiner Datensatzliste eintragen',
    pct: (p) => `${p} %`,
    notStarted: 'Das Hochladen hat nicht begonnen.',
    uploadOff: 'Nicht hochgeladen (Hochladen ist ausgeschaltet)',
    background: 'Das Hochladen läuft im Hintergrund weiter.',
    later: 'Du kannst den Datensatz später im Tab Daten hochladen.',
    unknown: 'Der Stand des Hochladens ist unbekannt, weil die Verbindung unterbrochen war. Prüfe den '
      + 'Datensatz im Tab Daten.',
    collisionEnd: COLLISION_END_NOTE_DE,
    saved: 'Gespeichert als',
    savedPrivate: 'Privat gespeichert als',
    savedPublic: 'Öffentlich gespeichert als',
    savedLocal: 'Auf diesem Rechner gespeichert als',
    registerHint: 'Hochgeladen, aber nicht in deiner Liste eingetragen. Klicke im Tab Training auf '
      + '„Datensätze synchronisieren“.',
    toTraining: 'Weiter zum Training',
    newRecording: 'Neue Aufnahme',
  }),

  actionbar: Object.freeze({
    aria: 'Aufnahmesteuerung',
  }),

  validation: VALIDATION_DE,
});

/** Q8 (owner decision): the notice when „Behalten und beenden" drops the run. */
export const KEEP_DROPPED_AFTER_REDO = RECORD_COPY.note.keepDroppedAfterRedo;

export default RECORD_COPY;
