// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// Every German sentence of the per-student Hugging-Face-Token feature (the
// Startseite card and the channel to the robot), in one flat table.
//
// German UI, English code (CLAUDE.md Rule §1): real umlauts, typographic quotes
// „…“, no icon glyphs (icons come from components/icons) and no English word a
// student could read. The keys are dotted paths (`card.pill.active`) so the card
// can ask for exactly the sentence it needs and a test can scan the whole table
// in one pass. Nothing here ever contains, formats or echoes a token.

export const HF_TOKEN_COPY = Object.freeze({
  'card.title': 'Hugging-Face-Token',

  // The explanation shown while no token is stored.
  'card.none.body':
    'Hinterlege einmal dein Hugging-Face-Token. Der Roboter bekommt es automatisch, sobald du angemeldet bist, '
    + 'und vergisst es beim Abmelden.',
  'card.none.step1': 'Öffne huggingface.co und melde dich an.',
  'card.none.step2': 'Gehe zu „Settings“ und dann zu „Access Tokens“.',
  'card.none.step3': 'Erstelle ein neues Token vom Typ „Write“ (mit Schreibrechten).',
  'card.none.step4': 'Kopiere das Token und füge es unten ein.',

  'card.input.placeholder': 'hf_…',
  'card.input.aria': 'Hugging-Face-Token',
  'card.save': 'Token speichern',
  'card.saving': 'Wird geprüft …',

  // The stored view.
  'card.stored.as': 'Verbunden als',
  'card.stored.hint': 'Token',
  'card.stored.checked': 'Zuletzt geprüft',
  'card.replace': 'Token ersetzen',
  'card.remove': 'Entfernen',
  'card.removeConfirm': 'Wirklich entfernen',
  'card.removeAbort': 'Abbrechen',
  'card.verify': 'Erneut prüfen',
  'card.retry': 'Erneut übertragen',
  'card.reload': 'Erneut laden',

  // States of the account half.
  'card.unusable':
    'Dein gespeichertes Token kann nicht mehr verwendet werden. Bitte speichere es neu.',
  'card.unsupported': 'Dieser Server unterstützt das Speichern des Tokens noch nicht.',
  'card.unavailable':
    'Das Speichern des Tokens ist auf dem Server noch nicht eingerichtet. Sag deiner Lehrkraft Bescheid.',
  'card.loadError': 'Der Token-Status konnte nicht geladen werden.',
  'card.unknown': 'Der Token-Status wird geladen …',
  // „Ohne Anmeldung fortfahren": without a login the account cannot be asked.
  'card.offline':
    'Ohne Anmeldung kann dein Token nicht geladen werden. Melde dich an, dann bekommt der Roboter es '
    + 'automatisch.',
  // Since migration 043 register_dataset_safe accepts the stored token's PROVEN
  // account beside the oldest dataset's author, so new recordings register;
  // dataset_sweep and POST /datasets/sync list only the CURRENT account, so the
  // old account's registered rows stay and nothing new of it is found.
  'card.accountChanged':
    'Dieses Token gehört zu einem anderen Hugging-Face-Konto als bisher. Neue Aufnahmen werden ab jetzt in '
    + 'diesem Konto gespeichert und für das Training angemeldet. Datensätze deines alten Kontos, die schon '
    + 'in deiner Liste stehen, bleiben dort; weitere werden nicht mehr automatisch gefunden.',

  // The robot half (pills).
  'card.pill.active': 'auf dem Roboter aktiv',
  'card.pill.working': 'wird übertragen …',
  'card.pill.waiting': 'wartet auf den Roboter',
  'card.pill.failed': 'Übertragung fehlgeschlagen',
  'card.pill.noLink': 'Roboter nicht verbunden',
  'card.pill.takenOver': 'Roboter hat ein anderes Token',
  'card.pill.notAccepted': 'Roboter nimmt kein Token an',
  'card.pill.none': 'nicht hinterlegt',
  'card.pill.stored': 'gespeichert',

  // Notes under the pills.
  // Every cause of a busy robot (the node's _hf_token_busy): a recording, or
  // the Hugging Face worker uploading, downloading or fetching a list.
  'card.waitingNote':
    'Der Roboter ist gerade beschäftigt: Er nimmt auf, lädt etwas hoch oder herunter oder fragt eine Liste '
    + 'bei Hugging Face ab. Das Token wird danach übertragen.',
  'card.takenOverNote':
    'Ein anderes Konto hat sein Token auf diesen Roboter gelegt. Drücke „Erneut übertragen“, wenn du wieder dran bist.',
  'card.failedNote':
    'Das Token konnte nicht auf den Roboter übertragen werden. Prüfe die Verbindung und versuche es erneut.',
  'card.jetsonNote': 'Der Klassen-Jetson nutzt ein eigenes Token. Dein Token bleibt gespeichert.',
  'card.errorGeneric': 'Das hat nicht geklappt. Bitte versuche es noch einmal.',
  'card.removeError': 'Das Token konnte nicht entfernt werden. Bitte versuche es noch einmal.',

  // Sentences the channel to the robot answers with when the robot itself said
  // nothing usable (its own German `message` wins whenever it sent one).
  'robot.noLink': 'Keine Verbindung zum Roboter.',
  'robot.timeout': 'Der Roboter hat nicht rechtzeitig geantwortet.',
  'robot.failed': 'Der Roboter hat das Token nicht angenommen.',
});

/** The sentence for `key`; an unknown key is a programming error, never shown. */
export function hfCopy(key) {
  if (!Object.prototype.hasOwnProperty.call(HF_TOKEN_COPY, key)) {
    throw new Error(`Unknown hf-token copy key: ${String(key)}`);
  }
  return HF_TOKEN_COPY[key];
}

export default HF_TOKEN_COPY;
