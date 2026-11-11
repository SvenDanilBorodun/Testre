// Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
//
// Proprietary and confidential. Licensed under the EduBotics End-User
// Licence Agreement; see LICENSE at the repository root.
//
// SA-21 (2026-11-13): the Windows GUI's :8769 bridge now REFUSES a POST
// without an Origin (gui/app/roboter_studio_control.py::_origin_allowed). That
// is safe only because the SPA's switch always carries one: its fetch is a
// cross-origin request (http://localhost → http://localhost:8769) in the
// default CORS mode, for which the Fetch standard always serializes the page's
// origin into `Origin` — whatever the referrer policy. A `mode: 'no-cors'` or
// `mode: 'same-origin'`, or a `referrerPolicy: 'no-referrer'` on a no-cors
// request, would change that, so this file fences the call's shape.

import fs from 'node:fs';
import path from 'node:path';

const SRC = fs.readFileSync(path.join(__dirname, '..', 'LeaderToggle.jsx'), 'utf8');

describe('the leader switch always carries an Origin', () => {
  it('posts through rsFetch with the default (CORS) mode and nothing that strips Origin', () => {
    expect(SRC).toMatch(/rsFetch\(rsControlBase\(piMode\), path, \{ method: 'POST' \}, TOGGLE_TIMEOUT_MS\)/);
    expect(SRC).toMatch(/await fetch\(`\$\{base\}\$\{path\}`, \{ \.\.\.options, signal: ctrl\.signal \}\)/);
    expect(SRC).not.toMatch(/\bmode\s*:/);
    expect(SRC).not.toMatch(/referrerPolicy/);
    expect(SRC).not.toMatch(/no-cors/);
  });

  it('the Windows base is another origin than the page, so the request is cross-origin', async () => {
    const { rsControlBase } = await import('../../../utils/piMode');
    expect(rsControlBase(false)).toBe('http://localhost:8769');
    expect(rsControlBase(true)).toBe('/api/system');
  });
});
