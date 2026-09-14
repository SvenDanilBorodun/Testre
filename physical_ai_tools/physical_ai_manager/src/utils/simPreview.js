/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Simulator previews („▶" on a Sammlung card or in the drawer) — pure helpers.
//
// A preview is an ordinary sim run of a tiny generated program, so every
// refusal it can meet is a server sentence. Those are shown VERBATIM, with
// exactly one exception: the replay lead-in refusal tells the student to lift
// the arm first, which nobody can do to the virtual arm. That one sentence is
// mapped to a simulator-true one; everything else (the seam refusal, a
// recording that dips below the table later, width, zones) passes unchanged.

import { DE } from '../components/Workshop/blocks/messages_de';

// `physical_ai_server/workflow/handlers/trajectory.py`, verbatim (Python's
// adjacent string literals joined). `utils/__tests__/simPreview.test.js` reads
// that file and fails if the server sentence changes without this one.
export const REPLAY_LEAD_IN_BELOW_TABLE_SERVER =
  'Der Arm ist zu nah an der Tischebene, um sicher zur Aufnahme-Startstellung zu fahren. '
  + 'Bitte den Arm zuerst anheben und erneut abspielen.';

/** The German message a PREVIEW shows for a server message. Exact match only. */
export function previewMessageDe(message) {
  return message === REPLAY_LEAD_IN_BELOW_TABLE_SERVER
    ? DE.PREVIEW_LEAD_IN_BELOW_TABLE
    : message;
}
