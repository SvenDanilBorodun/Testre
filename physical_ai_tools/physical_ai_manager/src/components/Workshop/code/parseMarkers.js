/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// A16 — live marking of what the editor's own parser could not read.
//
// Both language packages are Lezer parsers, which "proceed through any text,
// no matter how badly it fits the grammar": a token the parser had to skip is
// wrapped in an error node, and the point where it skipped ahead is marked with
// one (`NodeType.isError`). This module turns those nodes into REGIONS and the
// regions into diagnostics. It is PURE — it takes the Lezer `Tree` and the
// CodeMirror `Text` as arguments and imports nothing from CodeMirror, so it is
// unit-testable against the real parsers and can never reach the entry bundle.
//
// Two rules keep the marker honest while a student types:
//   * an error-recovering parser reports a failure on almost every keystroke
//     (`if ` alone does not parse), so the CURSOR'S LINE is never marked: a
//     half-typed line is on that line by construction, and a defect on any
//     OTHER line is marked once the idle window (CODE_LINT_IDLE_MS) has passed;
//   * a parse failure is not proof of a mistake, so the severity is a warning
//     and the sentence says „das kann ich noch nicht lesen" — never „Fehler".
//
// SHIPS DISABLED (CODE_LINT_LANGUAGES is empty). The noise gate — one defect
// may light at most lines k and k+1 — failed on the measured corpus for BOTH
// parsers (an unclosed `(` on Python line 8 lights 9, 11, 12, 14; a missing `;`
// after a Java call on line 7 lights 8 and 9), and the written exit is to
// narrow this list rather than tune the cap or the merge: a red line a student
// learns to ignore is worse than no line. The measurement and what re-enabling
// takes are in docs/KNOWN-ISSUES.md; the gate test in parseMarkers.test.js is
// kept as an expected failure naming the corpus.
//
// Plain module constants, NOT env knobs: an environment name here would need a
// compose forward per the env-forwarding guard for a value nobody tunes per
// rig (the `FOREVER_MIN_CYCLE_S` idiom).

/** Idle time after a change before the parse is judged (ms). */
export const CODE_LINT_IDLE_MS = 750;
/** Regions shown, by position. Past three the parser is lost (an unclosed
 *  brace near the top) and painting the rest of the file teaches the student to
 *  ignore red. */
export const CODE_LINT_MAX_REGIONS = 3;
/** Never `'error'`: a parse failure is not a verdict. */
export const CODE_LINT_SEVERITY = 'warning';
/** Work `ensureSyntaxTree` may do before the source declines to judge. */
export const CODE_LINT_PARSE_BUDGET_MS = 100;
/** The languages the markers are ON for. Empty: the §3.11 exit (see the
 *  header). Widening it is an owner decision recorded in KNOWN-ISSUES. */
export const CODE_LINT_LANGUAGES = Object.freeze([]);
export const CODE_LINT_MARK_CLASS = 'cm-edubotics-unparsed';
export const CODE_PARSE_NOTICE_DE = Object.freeze({
  python: 'Das kann ich noch nicht lesen — fehlt vielleicht ein Doppelpunkt, eine Klammer '
    + 'oder ein Anführungszeichen?',
  java: 'Das kann ich noch nicht lesen — fehlt vielleicht ein Semikolon, eine Klammer '
    + 'oder ein Anführungszeichen?',
});

/**
 * Every error node of `tree` as a region `{from, to}`, merged where two
 * overlap, touch, or share a line, sorted by position. A zero-length node (a
 * missing token) is widened to its whole line so it has something to underline.
 */
export function collectUnparsedRegions(tree, doc) {
  const raw = [];
  tree.iterate({
    enter: (node) => {
      if (!node.type.isError) return undefined;
      let { from, to } = node;
      if (from === to) {
        const line = doc.lineAt(Math.min(from, doc.length));
        from = line.from;
        to = line.to;
      }
      raw.push({ from, to });
      return undefined;
    },
  });
  raw.sort((a, b) => a.from - b.from || a.to - b.to);
  const merged = [];
  for (const r of raw) {
    const last = merged[merged.length - 1];
    if (last && (r.from <= last.to || doc.lineAt(r.from).number <= doc.lineAt(last.to).number)) {
      if (r.to > last.to) last.to = r.to;
    } else {
      merged.push({ from: r.from, to: r.to });
    }
  }
  return merged;
}

/**
 * The diagnostics for `regions`: the first CODE_LINT_MAX_REGIONS that do not
 * intersect `cursorLine` (1-based), each carrying the language's notice. The
 * mechanism, independent of the ship list — `markersFor` is what gates it.
 */
export function diagnosticsForRegions(regions, doc, cursorLine, language) {
  const message = CODE_PARSE_NOTICE_DE[language];
  if (!message) return [];
  const out = [];
  for (const region of regions) {
    const fromLine = doc.lineAt(region.from).number;
    const toLine = doc.lineAt(region.to).number;
    if (cursorLine >= fromLine && cursorLine <= toLine) continue;
    out.push({
      from: region.from,
      to: region.to,
      severity: CODE_LINT_SEVERITY,
      message,
      markClass: CODE_LINT_MARK_CLASS,
      source: 'parse',
    });
    if (out.length >= CODE_LINT_MAX_REGIONS) break;
  }
  return out;
}

/** What the editor shows: nothing for a language outside CODE_LINT_LANGUAGES. */
export function markersFor(tree, doc, cursorLine, language) {
  if (!CODE_LINT_LANGUAGES.includes(language)) return [];
  return diagnosticsForRegions(collectUnparsedRegions(tree, doc), doc, cursorLine, language);
}
