/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// ── [VAR:name=json] — which names the variable inspector accepts ─────────────
// ONE definition, TWO call sites: `hooks/useRosTopicSubscription` (the wire
// gate, before dispatching) and `features/workshop/workshopSlice::setVariable`
// (the store gate, before writing). It lives here rather than in the hook
// because the slice may not import the hook: `useRosTopicSubscription` imports
// `store/store`, which imports `workshopSlice` — so a slice→hook import closes
// a module cycle around `configureStore`. `utils/` is where this codebase
// already keeps pure decisions two layers share (`authGate`, `navGating`,
// `homeHealth`).
//
// The sentinel carries the student's OWN variable name (it used to carry the
// 20-char Blockly id), so this gate has to match the names Blockly actually
// hands back — not a programmer's idea of an identifier.
//
// Measured against Blockly 12.5.1's real interactive gate,
// `Variables.promptName`, which is what both „Variable erstellen" and Rename
// funnel through. Its entire normalization is
//     name.replace(/[\s\xa0]+/g, ' ').trim()
// and an empty result creates nothing. So a student CAN produce:
//     "meine Zahl"  "Anzahl Würfel"  "zähler-2"  "2te_zahl"  "3"  "Öl-Stand"
//     "Test 123"    "weiß der Geier"  "a.b"  "a(b)"  "a'b"  "a/b"  "a[0]"
// — 28 of the 32 realistic names probed. The OLD regex
//     /^[A-Za-zÄÖÜäöüß_][A-Za-zÄÖÜäöüß0-9_]*$/
// accepted only the 4 that happen to look like C identifiers and silently
// dropped every other one, so the inspector stayed empty for a student who had
// simply typed a space.
//
// What is still REFUSED, and why each one is not negotiable:
//   • empty / whitespace-only — Blockly never creates such a variable, and a
//     blank row in the inspector names nothing.
//   • > 64 chars — unchanged cap, enforced alongside the 4096-char value cap.
//   • control characters (C0 \x00-\x1F, DEL \x7F, C1 \x80-\x9F). Blockly's own
//     collapse only folds `\s`, and \x00 / \x07 / \x1B are NOT `\s` (verified),
//     so they survive it — and this sentinel arrives over rosbridge, which
//     authenticates nobody, so the wire is untrusted regardless of what the
//     editor can produce.
//   • `=` — the sentinel is framed `[VAR:name=json]` and parsed with
//     `^\[VAR:([^=]+)=(.*)\]$`; a name containing `=` re-splits the frame and
//     the value silently absorbs the rest of the name.
//   • `[` and `]` — the frame's own delimiters.
// `__proto__` / `constructor` / `prototype` are NOT checked here: that is a
// prototype-pollution guard, not a display question, and each call site keeps
// its own so deleting one is a change a test can see. Everything else is
// inert: the name becomes a plain object key and React escapes it on render.
//
// NEVER re-narrow this to an identifier shape. That is the whole defect
// (RS-49) — see CLAUDE.md, „Editor UI invariants".

// Matching control characters is the POINT of this class: the sentinel
// arrives over an unauthenticated rosbridge channel, and \x00 / \x07 /
// \x1B all survive Blockly's `\s`-only whitespace collapse.
// eslint-disable-next-line no-control-regex
const VAR_NAME_FORBIDDEN_RE = /[\u0000-\u001F\u007F-\u009F=[\]]/;

export const VAR_NAME_MAX_LEN = 64;

export function isDisplayableVariableName(name) {
  if (typeof name !== 'string') return false;
  if (name.length === 0 || name.length > VAR_NAME_MAX_LEN) return false;
  // Blockly trims, so a name that is only whitespace cannot come from the
  // editor — and it would render as a nameless row.
  if (name.trim() === '') return false;
  return !VAR_NAME_FORBIDDEN_RE.test(name);
}
