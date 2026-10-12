// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// Nothing in features/cloudInference/ calls console.* (spec §6.5): this folder
// is where the connect token and the edge token pass through, and a log line
// is the classic way a secret leaves a browser. Also: no browser storage key
// (D3) and no Redux dispatch of either token.

import fs from 'fs';
import path from 'path';

// Code only: a comment may NAME console.* (as this folder's headers do).
const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:'"`])\/\/.*$/gm, '$1');

const DIR = path.join(__dirname, '..');
const sources = fs.readdirSync(DIR)
  .filter((name) => /\.(js|jsx)$/.test(name))
  .map((name) => [name, code(fs.readFileSync(path.join(DIR, name), 'utf8'))]);

describe('features/cloudInference', () => {
  it('has the modules the spec names', () => {
    expect(sources.map(([n]) => n).sort()).toEqual(
      ['controller.js', 'gating.js', 'robotCalls.js', 'robotContract.js', 'taskInfoWire.js'],
    );
  });

  it('never calls console.*', () => {
    for (const [name, src] of sources) {
      expect({ name, hit: /\bconsole\s*\./.test(src) }).toEqual({ name, hit: false });
    }
  });

  it('writes no browser storage and dispatches nothing', () => {
    for (const [name, src] of sources) {
      expect({ name, hit: /\b(localStorage|sessionStorage|indexedDB)\b/.test(src) }).toEqual({ name, hit: false });
      expect({ name, hit: /\bdispatch\s*\(/.test(src) }).toEqual({ name, hit: false });
    }
  });

  it('the hooks and the components around it do not log either', () => {
    const more = [
      path.join(DIR, '..', '..', 'hooks', 'useCloudInferenceSession.js'),
      path.join(DIR, '..', '..', 'hooks', 'useCloudInferenceState.js'),
      path.join(DIR, '..', '..', 'services', 'cloudInferenceApi.js'),
      ...fs.readdirSync(path.join(DIR, '..', '..', 'components', 'CloudInference'))
        .filter((n) => /\.(js|jsx)$/.test(n))
        .map((n) => path.join(DIR, '..', '..', 'components', 'CloudInference', n)),
    ];
    for (const file of more) {
      expect({ file, hit: /\bconsole\s*\./.test(code(fs.readFileSync(file, 'utf8'))) }).toEqual({ file, hit: false });
    }
  });
});
