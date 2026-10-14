// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The scope switch (sucher/ui.js, spec §3.1) and the toast options (§6.3):
// the student build marks <html data-ui="sucher"> and gets the paper toast;
// the web build is left exactly as before.

import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '../../..');

let mockMode = 'student';
vi.mock('../../../constants/appMode', () => ({
  get APP_MODE() { return mockMode; },
}));

async function freshImport(rel) {
  vi.resetModules();
  return import(rel);
}

describe('ui.js — the scope switch', () => {
  afterEach(() => { delete document.documentElement.dataset.ui; });

  it('the student build sets data-ui="sucher" on <html>', async () => {
    mockMode = 'student';
    await freshImport('../ui');
    expect(document.documentElement.getAttribute('data-ui')).toBe('sucher');
  });

  it('the web build leaves <html> alone', async () => {
    mockMode = 'web';
    await freshImport('../ui');
    expect(document.documentElement.hasAttribute('data-ui')).toBe(false);
  });

  it('App.js imports it FIRST, then sucher.css', () => {
    const app = fs.readFileSync(path.join(SRC, 'App.js'), 'utf8');
    const imports = [...app.matchAll(/^import .*$/gm)].map((m) => m[0]);
    expect(imports[0]).toBe("import './components/sucher/ui';");
    expect(imports[1]).toBe("import './components/sucher/sucher.css';");
  });
});

describe('toasterOptions', () => {
  it('web: byte-for-byte the values it had before', async () => {
    mockMode = 'web';
    const { TOASTER_OPTIONS } = await freshImport('../../../toasterOptions');
    const { toastIcon } = await import('../../icons/toast');
    const TEXT = { maxWidth: '500px', wordWrap: 'break-word', whiteSpace: 'pre-wrap', lineHeight: '1.4' };
    expect(TOASTER_OPTIONS).toStrictEqual({
      duration: 3000,
      style: { background: '#363636', color: '#fff', ...TEXT },
      success: { duration: 3000, icon: toastIcon('success'), style: { background: '#10b981', ...TEXT } },
      error: { duration: 6000, icon: toastIcon('error'), style: { background: '#ef4444', ...TEXT } },
    });
    expect(Object.isFrozen(TOASTER_OPTIONS)).toBe(true);
  });

  it('student: a paper slip; every per-type style is complete on its own', async () => {
    mockMode = 'student';
    const { TOASTER_OPTIONS } = await freshImport('../../../toasterOptions');
    const { toastIcon } = await import('../../icons/toast');
    expect(TOASTER_OPTIONS.className).toBe('su-toast');
    const base = TOASTER_OPTIONS.style;
    expect(base).toMatchObject({
      background: '#FFFFFF',
      color: '#232322',
      borderRadius: '0 0 12px 12px',
      boxShadow: '0 0 0 1px #D2CEC5, 0 10px 20px -12px rgba(0,0,0,.4)',
      maxWidth: '500px',
      whiteSpace: 'pre-wrap',
    });
    expect(TOASTER_OPTIONS.success.style).toStrictEqual({ ...base, borderLeft: '4px solid #1B6C32' });
    expect(TOASTER_OPTIONS.error.style).toStrictEqual({ ...base, borderLeft: '4px solid #B42318' });
    expect(TOASTER_OPTIONS.success.icon).toBe(toastIcon('success'));
    expect(TOASTER_OPTIONS.error.icon).toBe(toastIcon('error'));
    expect(TOASTER_OPTIONS.success.duration).toBe(3000);
    expect(TOASTER_OPTIONS.error.duration).toBe(6000);
  });
});
