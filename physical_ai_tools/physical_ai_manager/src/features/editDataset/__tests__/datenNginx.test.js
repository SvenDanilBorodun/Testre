// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
//
// The /daten-api/ proxy in BOTH nginx configs (the Windows student rig and the
// Orange Pi template): a link token rides in the URL PATH (/daten-api/v1/ds/<T>/…,
// valid 30 min), and nginx's default access log writes every request line —
// hundreds of tokens per session (V1-6). The block must not log its requests.

import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../../../..');
const CONFIGS = ['nginx.conf', 'nginx.opi.conf.template'];

function datenBlock(text) {
  const m = /location \/daten-api\/ \{([\s\S]*?)\n {4}\}/.exec(text);
  return m ? m[1] : null;
}

describe.each(CONFIGS)('%s: the /daten-api/ block', (name) => {
  const text = fs.readFileSync(path.join(ROOT, name), 'utf8');
  const block = datenBlock(text);

  it('exists', () => {
    expect(block).not.toBeNull();
  });

  it('writes no access log (its URLs carry the link tokens)', () => {
    const lines = block.split('\n').map((l) => l.replace(/#.*/, '').trim()).filter(Boolean);
    expect(lines).toContain('access_log off;');
    expect(lines.filter((l) => l.startsWith('access_log'))).toEqual(['access_log off;']);
  });

  it('the parser has teeth: without the line the check fails', () => {
    const stripped = block.replace(/\n\s*access_log off;/, '');
    const lines = stripped.split('\n').map((l) => l.replace(/#.*/, '').trim()).filter(Boolean);
    expect(lines).not.toContain('access_log off;');
  });
});
