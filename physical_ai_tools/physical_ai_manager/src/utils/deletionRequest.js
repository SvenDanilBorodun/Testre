// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// GDPR Art. 17 deletion requests (launch plan step 5, owner decision „teacher
// confirms"): the German copy, the date and deadline helpers and the one
// reading of a deletion's outcome, shared by the student SPA's notice, the
// teacher dashboard and the admin dashboard. The cloud computes the deadline
// (`due_by`: one calendar month after the request, Art. 12(3)); this file only
// shows it.

export const DELETION_DE = Object.freeze({
  // Teacher web + admin
  title: 'Löschanfragen',
  openCount: (n) => (n === 1 ? '1 offene Löschanfrage' : `${n} offene Löschanfragen`),
  none: 'Keine offenen Löschanfragen.',
  deadlineNote:
    'Nach Art. 12 Abs. 3 DSGVO muss eine Löschanfrage unverzüglich ausgeführt werden, '
    + 'spätestens einen Monat nach Eingang. Die Löschung entfernt das Konto und alle Daten '
    + 'in EduBotics endgültig.',
  teacherOnlyOwn: 'Hier stehen die Anfragen der Schülerinnen und Schüler deiner Klassen.',
  requestedOn: (date) => `Beantragt am ${date}`,
  dueBy: (date) => `fällig bis ${date}`,
  overdue: (date) => `überfällig seit ${date}`,
  noClassroom: 'keine Klasse',
  teacherAccount: 'Lehrkraft-Konto',
  confirm: 'Löschung ausführen',
  confirmTitle: 'Löschung ausführen',
  confirmBody: (name) =>
    `Das Konto von ${name} und alle Daten in EduBotics werden endgültig gelöscht: Programme, `
    + 'Bewegungen, Abgaben, Trainings und das Stundenkonto. Datensätze und Modelle auf '
    + 'Hugging Face werden gelöscht, soweit die Plattform das kann. Das lässt sich nicht '
    + 'rückgängig machen.',
  confirmTeacherBody: (name) =>
    `Das Lehrkraft-Konto von ${name} wird endgültig gelöscht. Das geht erst, wenn es keine `
    + 'Klassen mehr hat.',
  confirmGo: 'Endgültig löschen',
  cancel: 'Abbrechen',
  loadFailed: 'Die Löschanfragen konnten nicht geladen werden.',
  studentDeleted: 'Schüler gelöscht',
  teacherDeleted: 'Lehrkraft gelöscht',
  // Roster pill
  rosterPill: 'Löschung beantragt',
  // Student SPA
  studentNotice: (requested, due) =>
    `Du hast am ${requested} beantragt, dass dein Konto und deine Daten gelöscht werden. `
    + `Deine Lehrkraft führt die Löschung aus – spätestens bis zum ${due}. Bis dahin kannst `
    + 'du alles weiter benutzen.',
  withdraw: 'Antrag zurückziehen',
  withdrawAsk: 'Löschanfrage wirklich zurückziehen?',
  withdrawYes: 'Ja, zurückziehen',
  withdrawFailed: 'Die Löschanfrage konnte nicht zurückgezogen werden.',
  // An older Cloud API's bare `{ok: true}` and the partial outcome (see announceStudentDeletion).
  hfRemaining: (n) =>
    `Das Konto wurde gelöscht, aber ${n} HuggingFace-`
    + 'Repository/-s konnten NICHT gelöscht werden — die Daten '
    + 'liegen noch dort. Bitte im HuggingFace-Konto der '
    + 'Schülerin/des Schülers manuell löschen.',
});

const DATE_DE = new Intl.DateTimeFormat('de-DE', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  timeZone: 'Europe/Berlin',
});

/** `dd.mm.yyyy` (Berlin) of an ISO timestamp, or „–" when it cannot be read. */
export function formatDateDe(iso) {
  if (typeof iso !== 'string' || !iso) return '–';
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '–';
  return DATE_DE.format(new Date(t));
}

const DAY_MS = 24 * 60 * 60 * 1000;

/** 'overdue' | 'soon' (≤ 7 days left) | 'ok' | 'unknown' for a `due_by`. */
export function deadlineState(dueBy, now = Date.now()) {
  const t = typeof dueBy === 'string' ? Date.parse(dueBy) : NaN;
  if (!Number.isFinite(t)) return 'unknown';
  if (t < now) return 'overdue';
  if (t - now <= 7 * DAY_MS) return 'soon';
  return 'ok';
}

/**
 * Tell the teacher (or the admin) what a student deletion actually did.
 *
 * The answer of `DELETE /teacher/students/{id}` and of a confirmed deletion
 * request: 200 with `hf_erasure_complete` + `hf_failures` + a German `detail`
 * whenever the ACCOUNT was deleted but the student's HuggingFace repos were not
 * — the ORDINARY outcome while the platform token does not own a student's
 * namespace. Gated on an EXPLICIT `false`: an older Cloud API answers a bare
 * `{ok: true}` and keeps the plain success message.
 */
export function announceStudentDeletion(res, toastApi) {
  if (res?.hf_erasure_complete === false) {
    const remaining = Array.isArray(res.hf_failures) ? res.hf_failures.length : 0;
    // Longer than the 6 s error default: this is an instruction to go and do
    // something on another site, not a status line.
    toastApi.error(res.detail || DELETION_DE.hfRemaining(remaining), { duration: 15000 });
    return 'partial';
  }
  toastApi.success(DELETION_DE.studentDeleted);
  return 'complete';
}
