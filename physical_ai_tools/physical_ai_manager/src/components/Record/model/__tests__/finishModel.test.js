// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The finish card (spec §3.11 table) and „Diese Sitzung" (§3.10).

import {
  EMPTY_FINISH, EMPTY_RECORD_SESSION, COLLISION_END_NOTE_DE, ERROR_STOP_INCOMPLETE_DE, ERROR_STOP_SAVED_DE,
} from '../../../../features/tasks/recordSession';
import RECORD_COPY from '../recordCopy';
import {
  POINTS_TO_DATEN_TAB, UPLOAD_START_GRACE_MS, datasetIdOf, finishSteps, sessionView,
} from '../finishModel';

const NOW = 1_700_000_000_000;
const F = RECORD_COPY.finish;
const SNAP = {
  taskName: 'Würfel', userId: 'schule-A', robotType: 'omx_f', pushToHub: true, privateMode: true,
  numEpisodes: 3, episodeTime: 20, warmupTime: 5, resetTime: 5, fps: 30,
};
const REPO = 'schule-A/omx_f_Wuerfel';

const session = (finish, patch = {}) => ({
  ...EMPTY_RECORD_SESSION, id: 1, savedCount: 2, snapshot: SNAP, startedHere: true,
  finish: { ...EMPTY_FINISH, endedAt: NOW - 1000, ...finish }, ...patch,
});
const states = (card) => card.steps.map((s) => `${s.key}:${s.state}`);
const actionIds = (card) => card.actions.map((a) => a.id);

describe('finishSteps', () => {
  it('hidden for idle, nothing and a dismissed finish', () => {
    expect(finishSteps(session({ state: 'idle' })).visible).toBe(false);
    expect(finishSteps(session({ state: 'nothing' })).visible).toBe(false);
    expect(finishSteps(session({ state: 'uploading', dismissed: true })).visible).toBe(false);
    expect(finishSteps(null).visible).toBe(false);
  });

  it('finalizing: step 1 now, no buttons', () => {
    const c = finishSteps(session({ state: 'finalizing' }), { nowWallMs: NOW });
    expect(states(c)).toEqual(['finalize:now', 'upload:', 'register:']);
    expect(c.title).toBe('2 Episoden werden gespeichert');
    expect(c.steps[1].label).toBe('Zu Hugging Face hochladen (privat)');
    expect(c.actions).toEqual([]);
  });

  it('uploading: step 2 now with the percentage and the bar', () => {
    const c = finishSteps(session({ state: 'uploading', expectedRepoId: REPO, repoId: REPO, uploadPct: 42 }), { nowWallMs: NOW });
    expect(states(c)).toEqual(['finalize:done', 'upload:now', 'register:']);
    expect(c.steps[1].pct).toBe('42 %');
    expect(c.barPct).toBe(42);
    expect(c.hint).toBe('Das Hochladen läuft im Hintergrund weiter.');
    expect(actionIds(c)).toEqual(['newRecording']);
  });

  it('uploading with the link lost: the state is unknown', () => {
    const c = finishSteps(session({ state: 'uploading', linkLost: true }), { nowWallMs: NOW + 60000 });
    expect(states(c)).toEqual(['finalize:done', 'upload:unknown', 'register:']);
    expect(c.steps[1].detail).toBe(F.unknown);
    expect(c.barPct).toBeNull();
  });

  it('no status 15 s after the end while connected: it never began', () => {
    const s = session({ state: 'uploading', expectedRepoId: REPO, endedAt: NOW });
    expect(states(finishSteps(s, { nowWallMs: NOW + UPLOAD_START_GRACE_MS - 1 }))).toEqual(['finalize:done', 'upload:now', 'register:']);
    const c = finishSteps(s, { nowWallMs: NOW + UPLOAD_START_GRACE_MS });
    expect(states(c)).toEqual(['finalize:done', 'upload:failed', 'register:skipped']);
    expect(c.steps[1].detail).toBe('Das Hochladen hat nicht begonnen.');
    expect(actionIds(c)).toEqual(['newRecording']);
    // not while the link is down — then nothing is known
    expect(states(finishSteps(s, { nowWallMs: NOW + 60000, heartbeat: 'timeout' }))[1]).toBe('upload:now');
    // and not once a status arrived
    const started = session({ state: 'uploading', expectedRepoId: REPO, repoId: REPO, uploadPct: 1, endedAt: NOW });
    expect(states(finishSteps(started, { nowWallMs: NOW + 60000 }))[1]).toBe('upload:now');
  });

  it('registering: step 3 now', () => {
    expect(states(finishSteps(session({ state: 'registering', repoId: REPO }))))
      .toEqual(['finalize:done', 'upload:done', 'register:now']);
  });

  it('done: all done, the saved name, „Weiter zum Training"', () => {
    const c = finishSteps(session({ state: 'done', repoId: REPO, registerState: 'done' }));
    expect(states(c)).toEqual(['finalize:done', 'upload:done', 'register:done']);
    expect(c.title).toBe('Dein Datensatz ist bereit');
    expect(c.savedAs).toEqual({ label: 'Privat gespeichert als', repoId: REPO });
    expect(c.hint).toBe('');
    expect(actionIds(c)).toEqual(['toTraining', 'newRecording']);
    expect(c.actions[0]).toMatchObject({ label: 'Weiter zum Training', variant: 'primary' });
  });

  it('done but not registered: step 3 says so, with the hint (V2-5)', () => {
    const c = finishSteps(session({ state: 'done', repoId: REPO, registerState: 'skipped' }));
    expect(c.hint).toBe(F.registerHint);
    expect(states(c)).toEqual(['finalize:done', 'upload:done', 'register:skipped']);
    const failed = finishSteps(session({ state: 'done', repoId: REPO, registerState: 'failed' }));
    expect(states(failed)).toEqual(['finalize:done', 'upload:done', 'register:failed']);
    expect(failed.hint).toBe(F.registerHint);
    const pub = finishSteps(session({ state: 'done', repoId: REPO }, { snapshot: { ...SNAP, privateMode: false } }));
    expect(pub.savedAs.label).toBe('Öffentlich gespeichert als');
    expect(pub.steps[1].label).toBe('Zu Hugging Face hochladen (öffentlich)');
  });

  it('upload_failed: the server text and where to upload later', () => {
    const c = finishSteps(session({ state: 'upload_failed', message: 'Dieser Namensraum gehört nicht zu deinem Konto.' }));
    // round 7: step 3 will not happen, so it is skipped, never „still to come"
    expect(states(c)).toEqual(['finalize:done', 'upload:failed', 'register:skipped']);
    expect(c.steps[1].detail)
      .toBe('Dieser Namensraum gehört nicht zu deinem Konto. Du kannst den Datensatz später im Tab Daten hochladen.');
    expect(c.title).toBe('Hochladen fehlgeschlagen');
    expect(c.savedAs).toEqual({ label: 'Auf diesem Rechner gespeichert als', repoId: REPO });
    expect(actionIds(c)).toEqual(['newRecording']);
  });

  it('local_done: upload and list skipped', () => {
    const c = finishSteps(session({ state: 'local_done' }, { snapshot: { ...SNAP, pushToHub: false } }));
    expect(states(c)).toEqual(['finalize:done', 'upload:skipped', 'register:skipped']);
    expect(c.steps[1].detail).toBe('Nicht hochgeladen (Hochladen ist ausgeschaltet)');
    expect(c.savedAs.repoId).toBe(REPO);
  });

  it('stopped_error with nothing saved: the reason, nothing claimed failed or done', () => {
    const c = finishSteps(session({ state: 'stopped_error' }, { savedCount: 0, errorText: 'Die Kameras senden keine Bilder.' }));
    expect(states(c)).toEqual(['finalize:skipped', 'upload:skipped', 'register:skipped']);
    expect(c.note).toBe('Die Kameras senden keine Bilder.');
    expect(c.title).toBe('Die Aufnahme wurde gestoppt');
    expect(actionIds(c)).toEqual(['newRecording']);
  });

  it('F3: stopped_error whose saved episodes are safe: finalize ✓, upload skipped with where to do it', () => {
    const why = 'Aufnahme gestoppt: Frame konnte nicht gespeichert werden.';
    const c = finishSteps(session({ state: 'stopped_error' }, { errorText: `${why} ${ERROR_STOP_SAVED_DE}` }));
    expect(states(c)).toEqual(['finalize:done', 'upload:skipped', 'register:skipped']);
    expect(c.note).toBe(why);
    expect(c.steps[1].detail).toBe(ERROR_STOP_SAVED_DE);
    expect(c.steps.some((x) => x.state === 'failed')).toBe(false);
    expect(c.savedAs).toEqual({ label: 'Auf diesem Rechner gespeichert als', repoId: REPO });
  });

  it('F3: stopped_error whose finalize failed: a clear incomplete dataset', () => {
    const why = 'Aufnahme gestoppt: Frame konnte nicht gespeichert werden.';
    const c = finishSteps(session({ state: 'stopped_error' }, { errorText: `${why} ${ERROR_STOP_INCOMPLETE_DE}` }));
    expect(states(c)).toEqual(['finalize:failed', 'upload:skipped', 'register:skipped']);
    expect(c.title).toBe('Datensatz unvollständig');
    expect(c.steps[0].detail).toBe(ERROR_STOP_INCOMPLETE_DE);
    expect(c.note).toBe(why);
    expect(c.savedAs).toBeNull();
  });

  it('stopped_error from an older robot with saved episodes: the dataset state is unknown, not failed', () => {
    const c = finishSteps(session({ state: 'stopped_error' }, { errorText: 'Die Kameras senden keine Bilder.' }));
    expect(states(c)).toEqual(['finalize:unknown', 'upload:skipped', 'register:skipped']);
    expect(c.note).toBe('Die Kameras senden keine Bilder.');
  });

  it('upload_failed whose sentence already says where to upload later: said once', () => {
    const stall = 'Das Hochladen kommt nicht mehr voran. Prüfe die Internetverbindung des Roboters. Der Datensatz '
      + 'bleibt auf dem Roboter gespeichert; du kannst ihn später im Tab Daten hochladen.';
    const c = finishSteps(session({ state: 'upload_failed', message: stall }));
    expect(c.steps[1].detail).toBe(stall);
  });

  // G-9 (Daten 2.0): every failure sentence that already sends the student to
  // the Daten tab is said once — no second „später im Tab Daten hochladen".
  // The robot's sentences (record_texts_de, spec §J.6 [R]) verbatim.
  it.each([
    // V1-5: the truth of G-2, the core sentence the page's „Beide behalten" tip shares
    ['HUB_CHANGED_SINCE_CHECK_DE', 'Auf Hugging Face hat sich der Datensatz inzwischen geändert. Es wurde nichts hochgeladen. Öffne den Tab Daten und entscheide, welche Version du behalten willst. „Beide behalten“ behält alle neuen Episoden von hier und von Hugging Face. Was seit dem letzten Abgleich auf einer Seite gelöscht oder ersetzt wurde, bleibt weg.'],
    ['UPLOAD_HUB_DIFFERS_DE', 'Auf Hugging Face gibt es diesen Datensatz schon in einer anderen Version. Es wurde nichts überschrieben. Öffne den Tab Daten, vergleiche beide Versionen und entscheide dort.'],
    // T1-3: neither assumes an online version (the local gate refuses a copy that was never uploaded too)
    ['UPLOAD_IN_SESSION_DE', 'Nicht hochgeladen: Die Aufnahme dieses Datensatzes wurde unterbrochen und nicht sauber beendet. Auf Hugging Face wurde nichts verändert. Lösche ihn im Tab Daten oder lade dort die Online-Version, falls es eine gibt.'],
    ['UPLOAD_BROKEN_DE', 'Nicht hochgeladen: Der Datensatz auf dem Roboter ist unvollständig oder beschädigt. Auf Hugging Face wurde nichts verändert. Lösche ihn im Tab Daten oder lade dort die Online-Version, falls es eine gibt.'],
    ['SYNC_CONFLICT_DE', 'Dieser Datensatz wurde hier geändert, und auf Hugging Face gibt es inzwischen eine neuere Version. Entscheide im Tab Daten, welche du behalten willst, und starte dann die Aufnahme neu.'],
    ['SYNC_UNKNOWN_DE', 'Der Datensatz hier und der auf Hugging Face sind verschieden, und EduBotics kann nicht erkennen, welcher neuer ist. Entscheide im Tab Daten, welche Version du behalten willst, und starte dann die Aufnahme neu.'],
    ['AUTO_UPLOAD_NO_WORKER_DE', 'Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des Roboters konnte nicht starten. Lade den Datensatz später im Tab Daten hoch.'],
    ['AUTO_UPLOAD_BUSY_DE', 'Automatisches Hochladen übersprungen: Gerade läuft ein anderer Hugging-Face-Vorgang. Wenn er beendet ist, lade den Datensatz im Tab Daten hoch.'],
    ['AUTO_UPLOAD_REFUSED_DE', 'Automatisches Hochladen fehlgeschlagen: Der Hugging-Face-Dienst des Roboters hat die Anfrage abgelehnt. Lade den Datensatz später im Tab Daten hoch.'],
    ['AUTO_UPLOAD_FAILED_DE', 'Automatisches Hochladen fehlgeschlagen. Lade den Datensatz später im Tab Daten hoch.'],
    ['sync_disk_de', 'Die neuere Version von Hugging Face braucht 2,1 GB, frei sind 1,4 GB, und für Aufnahmen müssen 3 GB frei bleiben. Die Aufnahme wurde nicht gestartet. Lösche zuerst alte Datensätze im Tab Daten.'],
  ])('upload_failed with %s: the sentence alone, no second „später hochladen"', (_name, sentence) => {
    expect(POINTS_TO_DATEN_TAB.test(sentence)).toBe(true);
    const c = finishSteps(session({ state: 'upload_failed', message: sentence }));
    expect(c.steps[1].detail).toBe(sentence);
    expect(c.steps[1].detail).not.toContain(F.later);
  });

  it('G-9: every sentence the old rule matched still matches', () => {
    expect(POINTS_TO_DATEN_TAB.test('… du kannst ihn später im Tab Daten hochladen.')).toBe(true);
    expect(POINTS_TO_DATEN_TAB.test('Lade den Datensatz später im Tab Daten hoch.')).toBe(true);
    expect(POINTS_TO_DATEN_TAB.test('Dieser Namensraum gehört nicht zu deinem Konto.')).toBe(false);
    expect(POINTS_TO_DATEN_TAB.test('Datentab')).toBe(false);
  });

  it('F4: local_done for a session that ran without upload names why, once (in the note)', () => {
    const off = 'Aufnahme ohne Hochladen: Auf dem Roboter ist kein Hugging-Face-Token gespeichert.';
    const c = finishSteps(session({ state: 'local_done', uploadOff: off, endNote: off }));
    expect(states(c)).toEqual(['finalize:done', 'upload:skipped', 'register:skipped']);
    expect(c.note).toBe(off);
    expect(c.steps[1].detail).toBe('Nicht hochgeladen (Hochladen war für diese Aufnahme aus)');
    expect(c.steps[1].label).toBe('Zu Hugging Face hochladen');
  });

  it('round 7: an upload that never began skips step 3 too', () => {
    const c = finishSteps(session({ state: 'uploading', endedAt: NOW - UPLOAD_START_GRACE_MS - 1 }), { nowWallMs: NOW });
    expect(states(c)).toEqual(['finalize:done', 'upload:failed', 'register:skipped']);
  });

  it('finalize_failed (V1-2): step 1 failed with the server text, nothing uploaded, nothing claimed ready', () => {
    const why = 'Datensatz konnte nicht abgeschlossen werden — die Aufnahme ist unvollständig und muss neu aufgenommen werden.';
    const local = finishSteps(session({ state: 'finalize_failed', message: why }, { snapshot: { ...SNAP, pushToHub: false } }));
    expect(states(local)).toEqual(['finalize:failed', 'upload:skipped', 'register:skipped']);
    expect(local.steps[0].detail).toBe(why);
    expect(local.title).toBe('Datensatz unvollständig');
    expect(local.title).not.toBe(F.titleDone);
    expect(local.savedAs).toBeNull();
    expect(actionIds(local)).toEqual(['newRecording']);
    // upload ON: the steps that will not happen are skipped, not „still to come"
    const hub = finishSteps(session({ state: 'finalize_failed', message: why }));
    expect(states(hub)).toEqual(['finalize:failed', 'upload:skipped', 'register:skipped']);
    expect(hub.steps[1].label).toBe('Zu Hugging Face hochladen (privat)');
    expect(hub.steps[1].detail).toBe('');
  });

  it('the title counts right (V2-4): one episode, several, none', () => {
    expect(finishSteps(session({ state: 'finalizing' }, { savedCount: 1 })).title).toBe('1 Episode wird gespeichert');
    expect(finishSteps(session({ state: 'finalizing' }, { savedCount: 3 })).title).toBe('3 Episoden werden gespeichert');
    expect(finishSteps(session({ state: 'finalizing' }, { savedCount: 0 })).title).toBe('Aufnahme wird abgeschlossen …');
  });

  it('after an end the link did not see, the upload is unknown — never „nicht begonnen" (V2-6)', () => {
    const c = finishSteps(session({ state: 'uploading', expectedRepoId: REPO, endedAt: NOW, linkLost: true }), {
      nowWallMs: NOW + 60000, heartbeat: 'connected',
    });
    expect(states(c)).toEqual(['finalize:done', 'upload:unknown', 'register:']);
    expect(c.steps[1].detail).not.toBe(F.notStarted);
  });

  it('the end note is one line under the title', () => {
    const c = finishSteps(session({ state: 'uploading', endNote: COLLISION_END_NOTE_DE }), { nowWallMs: NOW });
    expect(c.note).toBe('Die Aufnahme wurde nach der Kollision beendet.');
  });

  it('an adopted session (privacy unknown) names the step without a claim', () => {
    const c = finishSteps(session({ state: 'done', repoId: REPO }, { snapshot: { ...SNAP, privateMode: null } }));
    expect(c.steps[1].label).toBe('Zu Hugging Face hochladen');
    expect(c.savedAs.label).toBe('Gespeichert als');
  });
});

describe('datasetIdOf', () => {
  it('prefers the reported repo, then the expected one, then the local name', () => {
    expect(datasetIdOf(session({ repoId: 'x/y', expectedRepoId: REPO }))).toBe('x/y');
    expect(datasetIdOf(session({ expectedRepoId: REPO }))).toBe(REPO);
    expect(datasetIdOf(session({}))).toBe(REPO);
    expect(datasetIdOf(session({}, { snapshot: { ...SNAP, userId: '' } }))).toBe('omx_f_Wuerfel');
  });
});

describe('sessionView', () => {
  const at = new Date(2026, 8, 29, 10, 7).getTime();
  const s = {
    ...EMPTY_RECORD_SESSION,
    id: 4,
    savedCount: 3,
    adopted: true,
    adoptedSavedCount: 1,
    snapshot: SNAP,
    episodes: [
      { n: 2, outcome: 'saved', durationS: 20, wallMs: at, early: false },
      { n: 3, outcome: 'collision', durationS: 6.4, wallMs: at, early: false },
      { n: 3, outcome: 'saved', durationS: 12, wallMs: at, early: true },
    ],
  };

  it('rows newest first; saved rows numbered, discarded „–", each with a duration', () => {
    const v = sessionView(s, { nowWallMs: at + 60000 });
    expect(v.rows.map((r) => [r.num, r.title, r.sub, r.duration, r.discarded])).toEqual([
      ['3', 'Episode 3', 'Gespeichert (vorzeitig) · 10:07', '0:12', false],
      ['–', 'Kollision, verworfen', 'Episode 3 · 10:07', '0:06', true],
      ['2', 'Episode 2', 'Gespeichert · 10:07', '0:20', false],
    ]);
    expect(v.rows.every((r) => !r.isNew)).toBe(true);
  });

  it('the chip, the adopted note and the sum line', () => {
    const v = sessionView(s, { nowWallMs: at });
    expect(v.chip).toEqual({ text: '2 gespeichert', ok: true });
    expect(v.note).toBe('1 Episode wurde vor dem Neuladen gespeichert.');
    expect(sessionView({ ...s, adoptedSavedCount: 2 }).note).toBe('2 Episoden wurden vor dem Neuladen gespeichert.');
    expect(v.sum).toEqual({ left: '3 von 3 Episoden', right: 'Gesamt 0:32 min' });
    expect(v.rows[0].isNew).toBe(true);
    expect(v.raw).toBe(s);
  });

  it('empty session', () => {
    const v = sessionView(EMPTY_RECORD_SESSION, { numEpisodes: 5 });
    expect(v.rows).toEqual([]);
    expect(v.emptyText).toBe('Noch keine Episode in dieser Sitzung.');
    expect(v.chip).toEqual({ text: '0 gespeichert', ok: false });
    expect(v.sum.left).toBe('0 von 5 Episoden');
  });

  it('every discard label', () => {
    for (const [outcome, label] of [['redo', 'Wiederholt, verworfen'], ['collision', 'Kollision, verworfen'],
      ['drop', 'Bildverlust, verworfen'], ['ended', 'Beim Beenden verworfen']]) {
      const v = sessionView({ ...EMPTY_RECORD_SESSION, episodes: [{ n: 1, outcome, durationS: 3, wallMs: NOW }] });
      expect(v.rows[0].title).toBe(label);
    }
  });
});

describe('sessionView, round 7: an incomplete dataset', () => {
  const rows = (finish, patch = {}) => sessionView(session(finish, {
    episodes: [
      { n: 1, outcome: 'saved', durationS: 20, wallMs: NOW - 60000, early: false },
      { n: 2, outcome: 'saved', durationS: 8, wallMs: NOW - 30000, early: true },
    ],
    ...patch,
  }), { nowWallMs: NOW }).rows.map((r) => r.sub);

  it('a failed finalize marks every saved row', () => {
    const subs = rows({ state: 'finalize_failed', message: 'Datensatz konnte nicht abgeschlossen werden …' });
    expect(subs.every((t) => t.startsWith('Gespeichert, Datensatz unvollständig'))).toBe(true);
  });

  it('an error stop whose finalize failed marks them too; a safe one does not', () => {
    const bad = rows({ state: 'stopped_error' }, { errorText: `Gestoppt. ${ERROR_STOP_INCOMPLETE_DE}` });
    expect(bad.every((t) => t.startsWith('Gespeichert, Datensatz unvollständig'))).toBe(true);
    const ok = rows({ state: 'stopped_error' }, { errorText: `Gestoppt. ${ERROR_STOP_SAVED_DE}` });
    expect(ok.some((t) => t.includes('unvollständig'))).toBe(false);
  });
});
