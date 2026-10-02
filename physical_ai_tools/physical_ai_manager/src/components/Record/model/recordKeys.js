// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// The Aufnahme keyboard (spec §3.6). A student demonstrating with both hands
// on the leader arm reaches for one key, so every key maps to exactly one
// action per view and nothing fires while the page must not act: a held key
// (repeat), a command in flight, a collision, a lost link, typing into a
// field, or Space/Enter on a focused button (the button handles that itself).
// Pure: the page's window listener calls it and prevents the default only
// when an action comes back.

import RECORD_COPY from './recordCopy';

export const KEY_LABELS = Object.freeze({
  start: RECORD_COPY.kbd.space,
  skip: RECORD_COPY.kbd.right,
  saveNow: RECORD_COPY.kbd.right,
  redo: RECORD_COPY.kbd.left,
  end: RECORD_COPY.kbd.end,
  close: RECORD_COPY.kbd.esc,
});

const FIELD_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

function isTypingTarget(target) {
  if (!target) return false;
  if (target.isContentEditable) return true;
  return FIELD_TAGS.has(String(target.tagName || '').toUpperCase());
}

const isButton = (target) => String(target?.tagName || '').toUpperCase() === 'BUTTON';
const isSpace = (event) => event.key === ' ' || event.key === 'Spacebar' || event.code === 'Space';
const isEndChord = (event) => (event.ctrlKey || event.metaKey) && event.shiftKey
  && (String(event.key).toLowerCase() === 'x' || event.code === 'KeyX');

/**
 * The action a keydown asks for, or null.
 * @param event  a KeyboardEvent (or its shape)
 * @param ctx    {view, question, busy, collisionActive, connected, startBlocked, saveNowEnabled}
 * @returns {null | 'start'|'skip'|'saveNow'|'redo'|'end'|'closeQuestion'}
 */
export function keyToAction(event, {
  view, question = null, busy = false, collisionActive = false, connected = true,
  startBlocked = false, saveNowEnabled = true,
} = {}) {
  if (!event || event.repeat || busy || collisionActive || !connected) return null;
  const target = event.target;
  if (isTypingTarget(target)) return null;
  if (isButton(target) && (isSpace(event) || event.key === 'Enter')) return null;

  if (question) return event.key === 'Escape' ? 'closeQuestion' : null;

  if (isEndChord(event)) {
    return view === 'WARMUP' || view === 'RESETTING' || view === 'RECORDING' ? 'end' : null;
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return null;
  if (isSpace(event)) return view === 'READY' && !startBlocked ? 'start' : null;
  if (event.key === 'ArrowRight') {
    if (view === 'WARMUP' || view === 'RESETTING') return 'skip';
    if (view === 'RECORDING' && saveNowEnabled) return 'saveNow';
    return null;
  }
  if (event.key === 'ArrowLeft') return view === 'RECORDING' ? 'redo' : null;
  return null;
}
