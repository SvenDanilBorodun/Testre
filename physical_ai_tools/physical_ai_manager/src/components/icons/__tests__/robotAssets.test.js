/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Every robot the app draws is a render of its real URDF, and it is the robot
// the student started with. This pins the resolver (a profile maps to ITS OWN
// picture, an unknown profile to none) and the shipped files against it: the
// numbers in robotAssets.js are the render script's output, so a file that was
// re-rendered without updating them fails here.

import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';
import {
  GLYPH_CELL_PX, GLYPH_FILE, GLYPH_ORDER, PHASE_KINDS, PHASE_PICTURES, phasePicture, robotGlyphName, robotGlyphUrl,
} from '../robotAssets';
import { robotSpriteName } from '../../sucher/RobotIcon';

const APP = path.resolve(__dirname, '../../../..');
const ROBOTS = path.join(APP, 'public', 'robots');
const PROFILES = ['omx_full', 'omx_follower', 'edu6_studio', 'edu1_studio'];
const NOT_A_PROFILE = [null, undefined, '', 'omx_f', 'omx_l', 'edu6', 'OMX_FULL', 'toString', '__proto__', 'constructor', 42, {}, []];

/** Pixel size of a PNG (IHDR) or a WebP (VP8X canvas, VP8 or VP8L), without a decoder. */
function imageSize(file) {
  const b = fs.readFileSync(file);
  if (b.subarray(0, 8).toString('hex') === '89504e470d0a1a0a') return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), bytes: b.length };
  expect(b.subarray(0, 4).toString()).toBe('RIFF');
  expect(b.subarray(8, 12).toString()).toBe('WEBP');
  const kind = b.subarray(12, 16).toString();
  if (kind === 'VP8X') return { w: 1 + b.readUIntLE(24, 3), h: 1 + b.readUIntLE(27, 3), bytes: b.length };
  if (kind === 'VP8 ') return { w: b.readUInt16LE(26) & 0x3fff, h: b.readUInt16LE(28) & 0x3fff, bytes: b.length };
  throw new Error(`unsupported WebP chunk ${kind}`);
}

describe('robotGlyphName: a profile maps to its OWN robot, an unknown one to none', () => {
  it.each([
    ['omx_full', 'omx_full', 'omx_f', 'omx_l'],
    ['omx_follower', 'omx_f', 'omx_f', null],
    ['edu6_studio', 'edu6', 'edu6', null],
    ['edu1_studio', 'edu1', 'edu1', null],
  ])('%s → robot %s, arm %s, leader %s', (profile, robot, arm, leader) => {
    expect(robotGlyphName(profile)).toBe(robot);
    expect(robotGlyphName(profile, 'robot')).toBe(robot);
    expect(robotGlyphName(profile, 'arm')).toBe(arm);
    expect(robotGlyphName(profile, 'leader')).toBe(leader);
  });

  it.each(NOT_A_PROFILE)('%j draws nothing, whatever the part', (profile) => {
    for (const part of ['robot', 'arm', 'leader']) expect(robotGlyphName(profile, part)).toBeNull();
  });

  it('an unknown part draws nothing', () => {
    expect(robotGlyphName('omx_full', 'toString')).toBeNull();
    expect(robotGlyphName('omx_full', 'hand')).toBeNull();
  });

  it('agrees with the Sucher sprites (RobotIcon): the same robot per profile', () => {
    for (const profile of PROFILES) expect(robotGlyphName(profile)).toBe(robotSpriteName(profile));
  });

  it('names a cell the sheet has, for every profile and part', () => {
    for (const profile of PROFILES) {
      for (const part of ['robot', 'arm', 'leader']) {
        const name = robotGlyphName(profile, part);
        expect(name === null || GLYPH_ORDER.includes(name)).toBe(true);
      }
    }
  });

  it('every profile the server knows has a glyph (the registry exists three times, this is the React-side fence)', () => {
    const file = path.resolve(APP, '..', 'physical_ai_server', 'physical_ai_server', 'robot_profiles.py');
    if (!fs.existsSync(file)) return; // a checkout of the app alone: nothing to compare against
    const ids = [...fs.readFileSync(file, 'utf8').matchAll(/profile_id='([a-z0-9_]+)'/g)].map((m) => m[1]);
    expect(ids.sort()).toEqual([...PROFILES].sort());
    for (const id of ids) expect(robotGlyphName(id)).not.toBeNull();
  });
});

describe('phasePicture: only a profile that reaches the screen has one', () => {
  it('omx_full has all three kinds; the URL points into /robots', () => {
    for (const kind of PHASE_KINDS) {
      const pic = phasePicture('omx_full', kind);
      expect(pic).not.toBeNull();
      expect(pic.url).toBe(`/robots/${PHASE_PICTURES.omx_full[kind].file}`);
      expect(pic.file).toMatch(/^[a-z]+(-[a-z]+)?-omx_full\.webp$/);
    }
  });

  it.each(['omx_follower', 'edu6_studio', 'edu1_studio', ...NOT_A_PROFILE])(
    '%j has no phase picture (recording and the collision e-stop are omx_full only)',
    (profile) => {
      for (const kind of PHASE_KINDS) expect(phasePicture(profile, kind)).toBeNull();
    }
  );

  it.each([null, undefined, '', 'toString', 'warm-up', 'hand'])('an unknown kind %j has none', (kind) => {
    expect(phasePicture('omx_full', kind)).toBeNull();
  });

  it('the anchors are fractions inside the frame', () => {
    for (const set of Object.values(PHASE_PICTURES)) {
      for (const pic of Object.values(set)) {
        for (const [x, y] of Object.values(pic.anchors)) {
          expect(x).toBeGreaterThan(0); expect(x).toBeLessThan(1);
          expect(y).toBeGreaterThan(0); expect(y).toBeLessThan(1);
        }
      }
    }
  });
});

describe('the shipped files are the ones robotAssets.js describes', () => {
  it('glyphs.png is one row of GLYPH_ORDER.length square cells', () => {
    const { w, h, bytes } = imageSize(path.join(ROBOTS, GLYPH_FILE));
    expect(GLYPH_ORDER).toEqual(['omx_full', 'omx_f', 'edu6', 'edu1', 'omx_l']);
    expect(h).toBe(GLYPH_CELL_PX);
    expect(w).toBe(GLYPH_CELL_PX * GLYPH_ORDER.length);
    expect(bytes).toBeLessThan(20 * 1024); // a handful of KB: it is an icon
    expect(robotGlyphUrl()).toBe(`/robots/${GLYPH_FILE}`);
  });

  it.each(Object.entries(PHASE_PICTURES).flatMap(([profile, set]) => Object.entries(set).map(([kind, pic]) => [profile, kind, pic])))(
    '%s %s: frames × frame width wide, one frame high, within the budget',
    (_profile, kind, pic) => {
      const { w, h, bytes } = imageSize(path.join(ROBOTS, pic.file));
      expect(w).toBe(pic.frames * pic.width);
      expect(h).toBe(pic.height);
      expect(w).toBeLessThanOrEqual(16383); // WebP's hard limit
      expect(bytes).toBeLessThanOrEqual(300 * 1024);
      // record.css steps through the 24 frames of the reset strip; warm-up and collision are one still each
      expect(pic.frames).toBe(kind === 'reset' ? 24 : 1);
    }
  );

  it('record.css steps through the reset strip\'s 24 frames and holds it on its last one; warm-up is a still', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../Record/record.css'), 'utf8');
    // ONE keyframe rule carries both the value and the steps(): split in two, Chrome drops the easing
    expect(css).toMatch(/14% \{ transform: translateX\(0\); animation-timing-function: steps\(23, end\); \}/);
    expect(css).toContain('translateX(-95.8333%)'); // 23 / 24 of the strip: frame index 23
    // the warm-up picture is a single frame: its strip never moves, only the hand does
    expect(css).not.toMatch(/\.rec-pic-warmup \.rec-pic-strip \{ animation/);
    expect(css).not.toContain('rec-pic-loop');
    for (const kf of ['rec-pic-hand', 'rec-pic-fist', 'rec-pic-ring', 'rec-pic-arrive']) expect(css).toContain(`@keyframes ${kf}`);
    // every animated piece is switched off under reduced motion (page switch and media query) and then shows its finished state
    for (const sel of ['.rec-pic-hand', '.rec-pic-fist', '.rec-pic-ring', '.rec-pic-chip--arrive']) {
      expect(css).toContain(`.rec-page.rec-reduced ${sel},`);
    }
    for (const gone of ['rec-p-arm', 'rec-p-cube', 'rec-p-hand']) expect(css).not.toContain(gone);
  });

  it('no file of public/robots/ is left unreferenced (a generated asset nothing draws is dead weight)', () => {
    const known = new Set([
      GLYPH_FILE,
      ...Object.values(PHASE_PICTURES).flatMap((set) => Object.values(set).map((p) => p.file)),
      // the Sucher sprites and their light variants (RobotIcon.jsx, rendered by render-icons.sh / render-robot-pictures.sh)
      'omx_full.webp', 'omx_f.webp', 'edu6.webp', 'edu1.webp',
      'omx_full_light.webp', 'omx_f_light.webp', 'edu6_light.webp', 'edu1_light.webp',
    ]);
    expect(fs.readdirSync(ROBOTS).sort()).toEqual([...known].sort());
  });
});
