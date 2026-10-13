// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The 3D twin's screen colours (UrdfTwin), decided ONCE when a twin mounts.
// Under <html data-ui="sucher"> (the student build, components/sucher/ui.js)
// the scene sits on the Screen colour (`--su-scr`) with a warm, quiet grid
// mixed from the screen ink (`--su-scr-ink-3`); without it (the teacher web
// build, owner decision D1) the colours are exactly the ones UrdfTwin always
// had. Pure: no three.js here, colours are plain 0xRRGGBB numbers.

/** The web build's colours, unchanged. */
export const WEB_TWIN_PALETTE = Object.freeze({
  sucher: false,
  background: 0x1a1d23,
  gridCenter: 0x3a3f47,
  gridLines: 0x2a2e34,
});

/**
 * index.css's `--su-scr` / `--su-scr-ink-3`, used only when the computed token
 * cannot be read (a test DOM without the stylesheet). Pinned to index.css by
 * twinPalette.test.js, so they cannot drift from the token.
 */
export const SCREEN_TOKEN_FALLBACK = Object.freeze({ scr: 0x141210, ink3: 0x9c9488 });

/** How much screen ink the grid's centre line and its other lines carry. */
export const GRID_CENTER_INK = 0.18;
export const GRID_LINES_INK = 0.1;

/** '#RRGGBB' or '#RGB' (whitespace allowed) → 0xRRGGBB; anything else → null. */
export function parseHexColor(value) {
  if (typeof value !== 'string') return null;
  const s = value.trim();
  let m = /^#([0-9a-f]{6})$/i.exec(s);
  if (m) return parseInt(m[1], 16);
  m = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(s);
  if (m) return parseInt(m[1] + m[1] + m[2] + m[2] + m[3] + m[3], 16);
  return null;
}

/** `a` moved toward `b` by `t` (0..1), per channel, rounded. */
export function mixColor(a, b, t) {
  const ch = (c, shift) => (c >> shift) & 0xff;
  const mix = (shift) => Math.round(ch(a, shift) + (ch(b, shift) - ch(a, shift)) * t);
  return (mix(16) << 16) | (mix(8) << 8) | mix(0);
}

function readToken(style, name, fallback) {
  try {
    const parsed = parseHexColor(style ? style.getPropertyValue(name) : null);
    return parsed === null ? fallback : parsed;
  } catch {
    return fallback;
  }
}

/**
 * The palette for a twin mounting now. `root` defaults to <html>; `getStyle`
 * to window.getComputedStyle (both injectable for tests).
 */
export function readTwinPalette(root, getStyle) {
  const el = root !== undefined ? root : (typeof document !== 'undefined' ? document.documentElement : null);
  if (!el || typeof el.getAttribute !== 'function' || el.getAttribute('data-ui') !== 'sucher') {
    return WEB_TWIN_PALETTE;
  }
  let style = null;
  try {
    const gcs = getStyle || (typeof window !== 'undefined' ? window.getComputedStyle : null);
    style = typeof gcs === 'function' ? gcs(el) : null;
  } catch {
    style = null;
  }
  const scr = readToken(style, '--su-scr', SCREEN_TOKEN_FALLBACK.scr);
  const ink3 = readToken(style, '--su-scr-ink-3', SCREEN_TOKEN_FALLBACK.ink3);
  return Object.freeze({
    sucher: true,
    background: scr,
    gridCenter: mixColor(scr, ink3, GRID_CENTER_INK),
    gridLines: mixColor(scr, ink3, GRID_LINES_INK),
  });
}
