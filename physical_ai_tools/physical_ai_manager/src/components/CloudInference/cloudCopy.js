// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Every German sentence of the Inferenz page's cloud target (Cloud-Inferenz,
// spec §6.7), in one table. German UI, English code (CLAUDE.md Rule §1): real
// umlauts, typographic quotes. Placeholders `{name}` are filled by `fill`.
//
// The robot's own sentences (a refused start, the end of a run) come from the
// robot verbatim; the cloud API's refusals from its German `detail`. This table
// holds only what the page itself says.

export const cloudCopy = {
  target: { label: 'Wo soll dein Modell laufen?', cloud: 'In der Cloud', jetson: 'Klassen-Jetson' },
  disabled: {
    not_configured: 'Die Cloud-Inferenz ist in dieser Version nicht eingerichtet.',
    no_robot: 'Roboter nicht verbunden. Starte zuerst die Umgebung.',
    not_inferable: 'Dieser Roboter kann keine trainierten Modelle ausführen.',
    old_image: 'Roboter-Software aktualisieren, um die Cloud-Inferenz zu nutzen.',
  },
  panel: {
    title: 'Modell in der Cloud ausführen',
    intro: 'Dein Modell läuft auf einer Cloud-GPU, dein Roboter bleibt hier. Es wird nichts heruntergeladen.',
    privacy: 'Während des Laufs gehen Kamerabilder und Gelenkwerte verschlüsselt an eine GPU in einem EU-Rechenzentrum. Sie werden dort nur im Arbeitsspeicher verarbeitet und nicht gespeichert.',
    start: 'In der Cloud starten',
    starting: 'Wird gestartet …',
    stop: 'Stoppen',
    stopping: 'Wird gestoppt …',
    runningElsewhere: 'Auf dem Roboter läuft gerade eine Inferenz.',
    chooseModel: 'Wähle zuerst ein Modell.',
    preparing: 'Cloud-GPU wird gestartet …',
    preparingSub: 'Beim ersten Start kann das einige Minuten dauern.',
    released: 'Die Cloud-GPU wurde wieder freigegeben – beim Start wird sie neu vorbereitet.',
  },
  // The waiting card (CloudWait.jsx). Its titles are panel.preparing /
  // panel.preparingSub (the GPU start) and status.loading / status.loadingSub
  // (the model load); these are the lines under them. No percentage anywhere:
  // nobody knows how far a GPU start has come.
  waiting: {
    gpuWhat: 'Ein Rechner in einem EU-Rechenzentrum wird für dein Modell eingeschaltet.',
    gpuNormal: 'Das ist normal. Bleib auf dieser Seite – wenn du sie verlässt, wird der Start abgebrochen.',
    gpuLong: 'Gerade dauert es länger als sonst. Das liegt an der Cloud, nicht an dir.',
    retry: 'Noch nicht geklappt – die Seite versucht es von selbst noch einmal (Versuch {n} von {total}).',
    modelWhat: 'Das dauert meist nur einige Sekunden.',
    modelNormal: 'Sobald das Modell bereit ist, bewegt sich der Arm von selbst.',
    modelLong: 'Gerade dauert es länger als sonst. Klappt es nicht, beendet der Roboter den Lauf von selbst und sagt dir, warum.',
    elapsed: 'Wartezeit',
  },
  leader: {
    hint: 'Der Leader-Arm ist aktiv. Schalte mit dem Leader-Schalter in den Follower-Modus, bevor du in der Cloud startest.',
  },
  models: {
    title: 'Deine Modelle',
    loading: 'Modelle werden geladen …',
    empty: 'Noch kein fertig trainiertes Modell. Trainiere zuerst im Tab „Training“.',
    notAct: 'Noch nicht in der Cloud verfügbar (nur ACT-Modelle).',
    gpuStarting: 'Cloud-GPU wird gestartet … bitte kurz warten.',
    group: 'Gruppe',
  },
  summary: {
    cameras: 'Kameras im Modell: {list}',
    camerasMissing: 'Dieses Modell erwartet die Kamera(s) {missing} – am Roboter fehlen sie.',
    fps: 'Trainiert mit {fps} Bildern pro Sekunde',
  },
  usage: {
    line: 'Heute noch {min} Min. Cloud-Zeit (höchstens {session} Min. pro Lauf)',
    unknown: 'Cloud-Zeit heute: –',
  },
  status: {
    loading: 'Verbindung zur Cloud-GPU wird aufgebaut …',
    loadingSub: 'Das Modell wird geladen.',
    running: 'Läuft in der Cloud',
    holding: 'Arm hält an – warte auf die Cloud ({s} s)',
    rtt: 'Antwortzeit {ms} ms',
    planAge: 'Plan-Alter {ms} ms',
    gpu: 'GPU {ms} ms',
    endsIn: 'Endet in {mmss}',
    staleCamera: 'Kamera „{name}“ liefert seit über 5 Sekunden dasselbe Bild.',
  },
  errors: {
    robotOld: 'Roboter-Software aktualisieren, um die Cloud-Inferenz zu nutzen.',
    robotFailed: 'Der Roboter hat den Start abgelehnt.',
    robotUnreachable: 'Der Roboter antwortet nicht. Ist die Umgebung gestartet?',
    apiUnavailable: 'Die Cloud-Inferenz ist gerade nicht erreichbar. Bitte versuche es gleich noch einmal.',
  },
};

/**
 * Fill `{name}` placeholders. A value that is missing stays visible as its
 * placeholder rather than turning into „undefined".
 *
 * @param {string} template
 * @param {object} values
 * @returns {string}
 */
export function fill(template, values = {}) {
  return String(template).replace(/\{(\w+)\}/g, (whole, key) => (
    Object.prototype.hasOwnProperty.call(values, key) && values[key] !== undefined && values[key] !== null
      ? String(values[key])
      : whole
  ));
}

export default cloudCopy;
