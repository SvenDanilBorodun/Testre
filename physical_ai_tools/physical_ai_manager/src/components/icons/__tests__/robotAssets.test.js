/*
 * Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
 *
 * Proprietary and confidential. Licensed under the EduBotics End-User
 * Licence Agreement; see LICENSE at the repository root.
 */

// Every robot the app draws is a render of its real URDF, and it is the robot
// the student started with. This pins the resolver (a profile maps to ITS OWN
// picture, an unknown profile to none) and the shipped files against it: the
// numbers in robotAssets.js are the render script's output, so a file that was
// re-rendered without updating them fails here.

import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { describe, expect, it } from 'vitest';
import {
  GLYPH_CELL_PX, GLYPH_FILE, GLYPH_ORDER, PHASE_KINDS, PHASE_PICTURES, phasePicture, robotGlyphName, robotGlyphUrl,
  TOWER_HINT_PICTURES, TOWER_HINT_SCENES, towerHintPicture,
} from '../robotAssets';
import { robotSpriteName } from '../../sucher/RobotIcon';

const APP = path.resolve(__dirname, '../../../..');
const ROBOTS = path.join(APP, 'public', 'robots');
const PROFILES = ['omx_full', 'omx_follower', 'v1_studio'];
const NOT_A_PROFILE = [null, undefined, '', 'omx_f', 'omx_l', 'v1', 'OMX_FULL', 'toString', '__proto__', 'constructor', 42, {}, []];

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
    ['v1_studio', 'v1', 'v1', null],
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

  it.each(['omx_follower', 'v1_studio', ...NOT_A_PROFILE])(
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

// WP-ART's list (scratch/art/SHA256SUMS and meta.json of the render, D88: the
// standing strip is rendered but not shipped). The files are its renders, copied
// byte for byte; a re-render changes these numbers on purpose, never silently.
const WP_ART_TOWER_STRIPS = {
  'v1_tower_hint_forward.webp': { bytes: 101572, sha256: 'd65d77e28937edf56ca0ade68a708dd5c6bd9d2bb05c52431ceb070e9a7e728e' },
  'v1_tower_hint_forward_light.webp': { bytes: 114284, sha256: '97ee2bead57b87850e25e912ee935858ca0957472c17a6572bf263a227dd5c4e' },
  'v1_tower_hint_fall.webp': { bytes: 112504, sha256: '00bc248f8c73c2b90ffe80b4644b0df91166b94721aa8687cacfa2325cc589c2' },
  'v1_tower_hint_fall_light.webp': { bytes: 122132, sha256: 'eb2b0be84e4f8bd3c3263ccde88d56d858ea2f20fb9313b306cea72a5b9b8b38' },
};

describe('towerHintPicture: only the V1 has the camera-tower hint', () => {
  it.each(TOWER_HINT_SCENES)('v1_studio %s: the strip and its cream twin, in /robots', (scene) => {
    const dark = towerHintPicture('v1_studio', scene);
    const light = towerHintPicture('v1_studio', scene, { light: true });
    expect(dark.file).toBe(`v1_tower_hint_${scene}.webp`);
    expect(light.file).toBe(`v1_tower_hint_${scene}_light.webp`);
    expect(dark.url).toBe(`/robots/${dark.file}`);
    expect(light.url).toBe(`/robots/${light.file}`);
    for (const pic of [dark, light]) {
      expect(pic).toMatchObject({ frames: 24, width: 400, height: 295, safeFrame: 23 });
      expect(pic).not.toHaveProperty('fileLight');
    }
  });

  it('ships exactly the forward and the fall strip (D88: forward only, no standing strip)', () => {
    expect(TOWER_HINT_SCENES).toEqual(['forward', 'fall']);
    expect(Object.keys(TOWER_HINT_PICTURES)).toEqual(['v1_studio']);
    expect(Object.keys(TOWER_HINT_PICTURES.v1_studio)).toEqual(['forward', 'fall']);
    expect(towerHintPicture('v1_studio', 'standing')).toBeNull();
    expect(fs.existsSync(path.join(ROBOTS, 'v1_tower_hint_standing.webp'))).toBe(false);
    expect(fs.existsSync(path.join(ROBOTS, 'v1_tower_hint_standing_light.webp'))).toBe(false);
  });

  it.each(['omx_full', 'omx_follower', ...NOT_A_PROFILE])('%j has no tower hint (the tower is the V1\'s)', (profile) => {
    for (const scene of TOWER_HINT_SCENES) {
      expect(towerHintPicture(profile, scene)).toBeNull();
      expect(towerHintPicture(profile, scene, { light: true })).toBeNull();
    }
  });

  it.each([null, undefined, '', 'toString', 'standing', 'fileLight'])('an unknown scene %j has none', (scene) => {
    expect(towerHintPicture('v1_studio', scene)).toBeNull();
  });

  it('the reduced-motion frame is the last one, the safe pose of both strips', () => {
    for (const pic of Object.values(TOWER_HINT_PICTURES.v1_studio)) expect(pic.safeFrame).toBe(pic.frames - 1);
  });
});

describe('the shipped files are the ones robotAssets.js describes', () => {
  it('glyphs.png is one row of GLYPH_ORDER.length square cells', () => {
    const { w, h, bytes } = imageSize(path.join(ROBOTS, GLYPH_FILE));
    expect(GLYPH_ORDER).toEqual(['omx_full', 'omx_f', 'v1', 'omx_l']);
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

  it.each(Object.values(TOWER_HINT_PICTURES.v1_studio).flatMap((pic) => [[pic.file, pic], [pic.fileLight, pic]]))(
    '%s: WP-ART\'s render byte for byte, frames × frame width wide, within its 250 KB budget',
    (file, pic) => {
      const full = path.join(ROBOTS, file);
      const { w, h, bytes } = imageSize(full);
      expect(w).toBe(pic.frames * pic.width);
      expect(h).toBe(pic.height);
      expect(bytes).toBeLessThanOrEqual(250 * 1024);
      expect(bytes).toBe(WP_ART_TOWER_STRIPS[file].bytes);
      expect(crypto.createHash('sha256').update(fs.readFileSync(full)).digest('hex')).toBe(WP_ART_TOWER_STRIPS[file].sha256);
    }
  );

  it('the WP-ART list names exactly the tower strips robotAssets.js describes', () => {
    const described = Object.values(TOWER_HINT_PICTURES.v1_studio).flatMap((p) => [p.file, p.fileLight]);
    expect(described.sort()).toEqual(Object.keys(WP_ART_TOWER_STRIPS).sort());
  });

  it('no file of public/robots/ is left unreferenced (a generated asset nothing draws is dead weight)', () => {
    const known = new Set([
      GLYPH_FILE,
      ...Object.values(PHASE_PICTURES).flatMap((set) => Object.values(set).map((p) => p.file)),
      ...Object.values(TOWER_HINT_PICTURES).flatMap((set) => Object.values(set).flatMap((p) => [p.file, p.fileLight])),
      // the Sucher sprites and their light variants (RobotIcon.jsx, rendered by render-icons.sh / render-robot-pictures.sh)
      'omx_full.webp', 'omx_f.webp', 'v1.webp',
      'omx_full_light.webp', 'omx_f_light.webp', 'v1_light.webp',
    ]);
    expect(fs.readdirSync(ROBOTS).sort()).toEqual([...known].sort());
  });
});
