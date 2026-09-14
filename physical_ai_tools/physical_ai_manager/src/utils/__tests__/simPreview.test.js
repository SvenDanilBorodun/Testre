/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The preview message mapping: exactly ONE server sentence (the replay lead-in
// refusal, whose „lift the arm" advice cannot be followed in a simulator) is
// replaced; every other refusal stays verbatim. The constant is pinned against
// the server source, so a reworded server sentence cannot silently turn the
// mapping into a no-op.

import fs from 'fs';
import path from 'path';
import { DE } from '../../components/Workshop/blocks/messages_de';
import { REPLAY_LEAD_IN_BELOW_TABLE_SERVER, previewMessageDe } from '../simPreview';

describe('previewMessageDe', () => {
  test('maps exactly the lead-in sentence', () => {
    expect(previewMessageDe(REPLAY_LEAD_IN_BELOW_TABLE_SERVER)).toBe(DE.PREVIEW_LEAD_IN_BELOW_TABLE);
    expect(DE.PREVIEW_LEAD_IN_BELOW_TABLE).toBe(
      'Die Aufnahme beginnt unter dem Tisch des Simulators — im Simulator kann sie nicht abgespielt werden.',
    );
  });

  test.each([
    'Die Aufnahme führt unter die Tischebene — bitte eine neue Aufnahme oberhalb des Tisches machen.',
    'Zielpunkt liegt unter der Tischebene.',
    `${REPLAY_LEAD_IN_BELOW_TABLE_SERVER} `,
    REPLAY_LEAD_IN_BELOW_TABLE_SERVER.slice(0, -1),
    '',
  ])('returns %j unchanged', (message) => {
    expect(previewMessageDe(message)).toBe(message);
  });

  test('the seam refusal stays verbatim', () => {
    // handlers/trajectory.py::refuse_wrapped_joint_jumps, formatted for joint 4.
    const seam = 'Die Aufnahme wurde bei Gelenk 4 über die ±180°-Grenze gedreht. '
      + 'Der Motor kann diese Grenze nicht überfahren und würde beim Abspielen fast '
      + 'eine ganze Umdrehung zurückdrehen. Bitte neu aufnehmen, ohne Gelenk 4 über '
      + 'die Grenze zu drehen.';
    expect(previewMessageDe(seam)).toBe(seam);
  });
});

function readTrajectoryPy() {
  const file = path.resolve(
    process.cwd(),
    '../physical_ai_server/physical_ai_server/workflow/handlers/trajectory.py',
  );
  // A missing file FAILS — the lockstep must never pass vacuously.
  return fs.readFileSync(file, 'utf8');
}

describe('lockstep with handlers/trajectory.py', () => {
  test('the server source contains the constant once adjacent literals are joined', () => {
    const raw = readTrajectoryPy();
    const joined = raw.replace(/'\s*\n\s*'/g, '');
    expect(joined.includes(REPLAY_LEAD_IN_BELOW_TABLE_SERVER)).toBe(true);
  });
});
