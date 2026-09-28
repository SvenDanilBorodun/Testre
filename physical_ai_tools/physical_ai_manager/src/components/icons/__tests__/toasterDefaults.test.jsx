/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Review round 1 (B5 of the visual list): a bare toast.success/toast.error drew
// react-hot-toast's own animated check and cross beside every Lucide toast.
// The ONE Toaster (App.js, student and teacher web alike) now takes the Lucide
// success/error icons as its defaults, and every toast icon is drawn in the
// toast's own text colour.

import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import toast, { Toaster } from 'react-hot-toast';
import { TOASTER_OPTIONS } from '../../../toasterOptions';
import { toastIcon, TOAST_ICONS, TOAST_ICON_KINDS } from '../toast';

const SRC = path.resolve(__dirname, '../../..');

test('the defaults are the Lucide success and error icons', () => {
  expect(TOASTER_OPTIONS.success.icon).toBe(toastIcon('success'));
  expect(TOASTER_OPTIONS.error.icon).toBe(toastIcon('error'));
  expect(TOAST_ICONS.success).toBe('checkCircle');
  expect(TOAST_ICONS.error).toBe('failed');
});

test('a toast icon takes the toast\'s text colour (no colour class of its own)', () => {
  for (const kind of TOAST_ICON_KINDS) {
    expect(toastIcon(kind).props.className || '').not.toMatch(/\btext-/);
  }
});

test('App.js renders the one Toaster, with these options', () => {
  const app = fs.readFileSync(path.join(SRC, 'App.js'), 'utf8');
  expect(app).toMatch(/<Toaster[^>]*toastOptions=\{TOASTER_OPTIONS\}/);
  const walk = (dir, out = []) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) { if (e.name !== '__tests__' && e.name !== 'node_modules') walk(p, out); } else if (/\.jsx?$/.test(p) && !/\.test\./.test(p)) out.push(p);
    }
    return out;
  };
  const withToaster = walk(SRC).filter((f) => /<Toaster\s/.test(fs.readFileSync(f, 'utf8')));
  expect(withToaster.map((f) => path.relative(SRC, f))).toEqual(['App.js']);
});

test('a bare toast.error and toast.success draw the Lucide icons', async () => {
  // react-hot-toast asks for prefers-reduced-motion; jsdom has no matchMedia.
  if (!window.matchMedia) {
    window.matchMedia = () => ({
      matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {},
    });
  }
  render(<Toaster toastOptions={TOASTER_OPTIONS} />);
  act(() => {
    toast.error('Befehlsausführung fehlgeschlagen');
    toast.success('Gespeichert');
  });
  const err = await screen.findByText('Befehlsausführung fehlgeschlagen');
  const ok = await screen.findByText('Gespeichert');
  const iconOf = (el) => el.closest('[role="status"], [role="alert"], div').parentElement.querySelector('svg[data-icon]'); // eslint-disable-line testing-library/no-node-access
  expect(iconOf(err).getAttribute('data-icon')).toBe('failed');
  expect(iconOf(ok).getAttribute('data-icon')).toBe('checkCircle');
  act(() => { toast.remove(); });
});
