// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.

// The Aufnahme keyboard (spec §3.6): which key does what in which view, and
// when the page must not listen at all.

import { KEY_LABELS, keyToAction } from '../recordKeys';

const key = (k, extra = {}) => ({ key: k, repeat: false, target: { tagName: 'DIV' }, ...extra });
const ctx = (view, extra = {}) => ({
  view, question: null, busy: false, collisionActive: false, connected: true, startBlocked: false,
  saveNowEnabled: true, ...extra,
});

describe('keyToAction', () => {
  it('Space starts, only in READY and only when Start is not refused', () => {
    expect(keyToAction(key(' '), ctx('READY'))).toBe('start');
    expect(keyToAction(key(' '), ctx('READY', { startBlocked: true }))).toBeNull();
    expect(keyToAction(key(' '), ctx('RECORDING'))).toBeNull();
    expect(keyToAction(key(' '), ctx('STARTING'))).toBeNull();
  });

  it('→ skips the warm-up / reset, saves early while recording (from the first second)', () => {
    expect(keyToAction(key('ArrowRight'), ctx('WARMUP'))).toBe('skip');
    expect(keyToAction(key('ArrowRight'), ctx('RESETTING'))).toBe('skip');
    expect(keyToAction(key('ArrowRight'), ctx('RECORDING'))).toBe('saveNow');
    expect(keyToAction(key('ArrowRight'), ctx('RECORDING', { saveNowEnabled: false }))).toBeNull();
    expect(keyToAction(key('ArrowRight'), ctx('SAVING'))).toBeNull();
    expect(keyToAction(key('ArrowRight'), ctx('READY'))).toBeNull();
  });

  it('← redoes only while recording', () => {
    expect(keyToAction(key('ArrowLeft'), ctx('RECORDING'))).toBe('redo');
    expect(keyToAction(key('ArrowLeft'), ctx('RESETTING'))).toBeNull();
  });

  it('Ctrl/Cmd+Shift+X ends (in RECORDING the page asks first)', () => {
    const ctrl = key('X', { ctrlKey: true, shiftKey: true });
    const cmd = key('x', { metaKey: true, shiftKey: true });
    expect(keyToAction(ctrl, ctx('WARMUP'))).toBe('end');
    expect(keyToAction(cmd, ctx('RESETTING'))).toBe('end');
    expect(keyToAction(ctrl, ctx('RECORDING'))).toBe('end');
    expect(keyToAction(ctrl, ctx('SAVING'))).toBeNull();
    expect(keyToAction(ctrl, ctx('READY'))).toBeNull();
    expect(keyToAction(key('x', { ctrlKey: true }), ctx('WARMUP'))).toBeNull();
    expect(keyToAction(key('x', { shiftKey: true }), ctx('WARMUP'))).toBeNull();
    // a layout that reports the code but another key
    expect(keyToAction(key('Χ', { code: 'KeyX', ctrlKey: true, shiftKey: true }), ctx('WARMUP'))).toBe('end');
  });

  it('with a question open only Escape does anything, and it closes the question', () => {
    const q = { kind: 'end', episode: 2 };
    expect(keyToAction(key('Escape'), ctx('RECORDING', { question: q }))).toBe('closeQuestion');
    expect(keyToAction(key('ArrowLeft'), ctx('RECORDING', { question: q }))).toBeNull();
    expect(keyToAction(key('ArrowRight'), ctx('RECORDING', { question: q }))).toBeNull();
    expect(keyToAction(key('Escape'), ctx('RECORDING'))).toBeNull();
  });

  it('ignores repeats, a busy page, a collision and a lost link', () => {
    expect(keyToAction(key(' ', { repeat: true }), ctx('READY'))).toBeNull();
    expect(keyToAction(key(' '), ctx('READY', { busy: true }))).toBeNull();
    expect(keyToAction(key('ArrowRight'), ctx('WARMUP', { collisionActive: true }))).toBeNull();
    expect(keyToAction(key('ArrowRight'), ctx('WARMUP', { connected: false }))).toBeNull();
  });

  it('ignores typing into a field', () => {
    for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
      expect(keyToAction(key(' ', { target: { tagName } }), ctx('READY'))).toBeNull();
    }
    expect(keyToAction(key(' ', { target: { tagName: 'DIV', isContentEditable: true } }), ctx('READY'))).toBeNull();
  });

  it('leaves Space and Enter on a focused button to the button', () => {
    expect(keyToAction(key(' ', { target: { tagName: 'BUTTON' } }), ctx('READY'))).toBeNull();
    expect(keyToAction(key('Enter', { target: { tagName: 'BUTTON' } }), ctx('READY'))).toBeNull();
    // arrows still work with a button focused
    expect(keyToAction(key('ArrowRight', { target: { tagName: 'BUTTON' } }), ctx('WARMUP'))).toBe('skip');
  });

  it('names the keys as words', () => {
    expect(KEY_LABELS).toEqual({ start: 'Leertaste', skip: '→', saveNow: '→', redo: '←', end: 'Strg+Umschalt+X', close: 'Esc' });
  });
});
