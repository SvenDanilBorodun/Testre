// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

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
      intDigits = (BigInt(intDigits) + 1n).toString();
    }
  }
  return `${sign}${groupDe(intDigits)},${tenth} Stunden`;
}

/** Lost seconds as whole minutes for „Verlorene Zeit … Min." (spec §10). */
export function lostMinutes(seconds) {
  const n = finiteOrNull(seconds);
  return n === null ? 0 : Math.round(n / 60);
}
