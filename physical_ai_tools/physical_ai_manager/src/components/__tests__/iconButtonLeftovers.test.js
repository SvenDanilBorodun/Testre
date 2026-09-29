// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Final review, minors 2 + 3, pinned at the source because both components
// need a store, a tutorial loader or a modal state machine to render:
//   * the tutorial's „Vorheriger Schritt" draws the chevron icon, not a typed
//     arrow (the glyph fence allows ← → as key names, so it cannot see this);
//   * the policy-download modal's icon-only close button has a German name.

import fs from 'fs';
import path from 'path';

const read = (rel) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');

test('the tutorial back button draws the chevron icon, not an arrow glyph', () => {
  const src = read('Workshop/SkillmapPlayer.jsx');
  expect(src).not.toMatch(/←\s*\{DE\.TUTORIAL_PREV\}/);
  expect(src).toMatch(/<Icon name="chevronLeft"[^>]*\/>\s*\{DE\.TUTORIAL_PREV\}/);
});

test('the policy-download close button has a German accessible name', () => {
  const src = read('PolicyDownloadModal.js');
  const close = src.slice(src.indexOf('onClick={onClose}') - 20, src.indexOf('<Icon name="close"'));
  expect(close).toMatch(/aria-label="Schließen"/);
});

// Final review nits: „Aufnahme gestartet!" shows the record SYMBOL (a ring with
// its dot) — a plain filled dot, white on the green toast, read as a bullet;
// a spinner that only says „läuft / wird geladen" is the loader icon, while a
// refresh BUTTON may still turn its own arrows while it reloads.
test('the recording toast carries the record symbol', async () => {
  const { TOAST_ICONS } = await import('../icons/toast');
  expect(TOAST_ICONS.record).toBe('record');
});

test('a pure „läuft" spinner is the loader icon, never the refresh arrows', () => {
  expect(read('MyModels.js')).toMatch(/running: \{ icon: 'loading'/);
  expect(read('TrainingLiveChart.js')).toMatch(/running: \{[^}]*icon: 'loading'/);
  for (const rel of ['TrainingLiveChart.js', 'DatasetSelector.js']) {
    expect(read(rel)).not.toMatch(/name="refresh"[^>]*className="[^"]*animate-spin/);
  }
});
