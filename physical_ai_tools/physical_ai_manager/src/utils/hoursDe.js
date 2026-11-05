// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.

// The ONE way the app writes an hour count (credit system v2, spec §8.1 /
// §10, review 2 m7). No German sentence puts a verb or an inflected adjective
// next to a count; the count is always this noun phrase:
//
//   exactly 1          → „1 Stunde"
//   any other integer  → „0 Stunden", „2 Stunden", „1.000 Stunden"
//   a non-integer      → one decimal with a comma, always plural:
//                        „3,5 Stunden", „0,5 Stunden", „1,0 Stunden"
//
// The rounding is defined on the number's SHORTEST decimal spelling (what
// `String(x)` gives here and `repr(x)` gives in Python), half away from zero,
// so the cloud API's Python twin (`credit_texts_de.hours_de`) can give the
// same text for every value. Both are held to one case table,
// `__tests__/hoursDe.cases.json` (written here, read by the API's
// `app/tests/test_hours_de.py`).

/** `x` (finite, ≥ 0) as a plain decimal string, never in exponent form. */
function plainDecimal(x) {
  const s = String(x);
  if (!/e/i.test(s)) return s;
  const [mantissa, expText] = s.toLowerCase().split('e');
  const exp = Number(expText);
  const [intPart, frac = ''] = mantissa.split('.');
  const digits = intPart + frac;
  const point = intPart.length + exp;
  if (point <= 0) return `0.${'0'.repeat(-point)}${digits}`;
  if (point >= digits.length) return digits + '0'.repeat(point - digits.length);
  return `${digits.slice(0, point)}.${digits.slice(point)}`;
}

/** '1234567' → '1.234.567' (the de-DE thousands dot). */
function groupDe(intDigits) {
  return intDigits.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

/** '999' → '1000': a decimal digit string plus one. */
function incrementDigits(digits) {
  const out = digits.split('');
  for (let i = out.length - 1; i >= 0; i -= 1) {
    if (out[i] !== '9') {
      out[i] = String(Number(out[i]) + 1);
      return out.join('');
    }
    out[i] = '0';
  }
  return `1${out.join('')}`;
}

/** A finite number, or null for anything that is not one (null is not 0). */
function finiteOrNull(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * An integer with the de-DE thousands dot („7.500"), or „—" for an unknown
 * value. For step numbers and the „Std." abbreviation.
 */
export function numberDe(value) {
  const n = finiteOrNull(value);
  if (n === null) return '—';
  const r = Math.round(Math.abs(n));
  const text = groupDe(plainDecimal(r));
  return n < 0 && r !== 0 ? `-${text}` : text;
}

/** The hour count as a German noun phrase (see the header); „—" when unknown. */
export function hoursDe(value) {
  const n = finiteOrNull(value);
  if (n === null) return '—';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (Number.isInteger(abs)) {
    const text = groupDe(plainDecimal(abs));
    if (abs === 0) return '0 Stunden';
    return `${sign}${text} ${abs === 1 ? 'Stunde' : 'Stunden'}`;
  }
  const [intText, fracText = ''] = plainDecimal(abs).split('.');
  let intDigits = intText;
  let tenth = Number(fracText[0] || '0');
  if (Number(fracText[1] || '0') >= 5) {
    tenth += 1;
    if (tenth === 10) {
      tenth = 0;
      intDigits = incrementDigits(intDigits);
    }
  }
  return `${sign}${groupDe(intDigits)},${tenth} Stunden`;
}

/** Lost seconds as whole minutes for „Verlorene Zeit … Min." (spec §10). */
export function lostMinutes(seconds) {
  const n = finiteOrNull(seconds);
  return n === null ? 0 : Math.round(n / 60);
}

// ── A training's hour lines (spec §10.2), shared by „Meine Modelle", the live
// chart and the teacher's training history. ────────────────────────────────

const TERMINAL = new Set(['succeeded', 'failed', 'canceled']);

/**
 * What a training cost (credit system v2, spec §10): „Berechnet: 1 Stunde." /
 * „Es wurde nichts berechnet." for a row the hour ledger settled, null for a
 * pre-052 row (its credit was paid under the old rules). Never a refund claim.
 */
export function chargeSentence(job) {
  const c = job?.charged_hours;
  if (typeof c !== 'number' || !Number.isFinite(c)) return null;
  return c > 0 ? `Berechnet: ${hoursDe(c)}.` : 'Es wurde nichts berechnet.';
}

/**
 * The lost time [Q-B]: the unsaved part of lost attempts, which counts against
 * the training's time limit and is never charged. Shown from one minute on (spec §10.2).
 */
export function lostTimeLine(job) {
  const s = job?.lost_seconds;
  if (typeof s !== 'number' || !(s >= 60)) return null;
  const m = lostMinutes(s);
  return TERMINAL.has(job.status)
    ? `Verlorene Zeit: ${m} Min. (nicht berechnet)`
    : `Verlorene Zeit bisher: ${m} Min. – zählt zum Zeitlimit, wird nicht berechnet.`;
}

/**
 * The card's hour lines: the reservation while active, the charge once
 * settled, and (on the student's card) that the teacher stopped it.
 */
export function hourLines(job, { chargeShownElsewhere = false, withStopper = true } = {}) {
  const lines = [];
  const terminal = TERMINAL.has(job?.status);
  if (!terminal && typeof job?.reserved_hours === 'number') {
    lines.push(`Reserviert: ${hoursDe(job.reserved_hours)}`);
  }
  if (terminal && !chargeShownElsewhere && typeof job?.charged_hours === 'number') {
    lines.push(`Berechnet: ${hoursDe(job.charged_hours)}`);
  }
  if (withStopper && job?.stop_requested_by_role === 'teacher') lines.push('Von deiner Lehrkraft gestoppt');
  return lines;
}

// ── The teacher's hour panels (spec §10.4) ──────────────────────────────────

/** An hour field of a summary as a number (0 for an answer without it). */
export const hoursOf = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);

/** „3 Std." — the invariant abbreviation, so no verb or adjective meets the count. */
export const std = (n) => `${numberDe(n)} Std.`;

export const RECLAIM_FOOTNOTE_DE =
  'Zurückholen geht nur mit freien Stunden; reservierte und verbrauchte Stunden bleiben.';

/** The reclaim key's title: only free hours can be reclaimed. */
export function reclaimTitle(canReclaim, free) {
  if (canReclaim) return 'Zurückholen';
  return free === 0 ? 'Keine freien Stunden zum Zurückholen' : `Max. ${numberDe(free)} zurückholbar`;
}
