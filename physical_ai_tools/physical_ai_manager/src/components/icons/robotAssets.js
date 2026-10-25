/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// Which picture of WHICH robot the student app shows. Every robot the app
// draws is a render of the real URDF (public/omx-urdf, v1-urdf, and
// open_manipulator_description for the leader arm), and it is the robot
// the student started EduBotics with: the profile id the server sends on
// /task/status (`taskStatus.robotProfile`). An unknown profile (before the
// first status tick, or an id this build has no picture for) draws NOTHING —
// never another robot as a stand-in: a V1 student must not see an OMX.
//
// Two kinds of picture live in public/robots/:
//   * the glyph sheet `glyphs.png`: four 64 px tonal alpha cells (RobotGlyph
//     paints one with CSS mask-image in `currentColor`, like a Lucide icon);
//   * the phase pictures, sprite strips and a still: Aufnahme's warm-up and
//     reset, and the collision modal's (warm-up is one still, reset 24
//     frames). They exist only for a profile that reaches that screen
//     (recording and the collision e-stop are `omx_full` only), so
//     `phasePicture` answers null for every other profile.
//
// All of it is rendered by tools/frontend-harness/render-robot-pictures.sh
// (dev only, deterministic). The numbers below are that script's output; the
// robotAssets test reads the shipped files and fails when they drift apart.

const hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

/** The cells of public/robots/glyphs.png, left to right. */
export const GLYPH_ORDER = Object.freeze(['omx_full', 'omx_f', 'v1', 'omx_l']);
export const GLYPH_CELL_PX = 64;
export const GLYPH_FILE = 'glyphs.png';

// part 'robot': the robot the student started with (omx_full = both arms);
// part 'arm': the arm a program drives (the follower: the one that moves);
// part 'leader': the arm a student moves by hand (only a profile with one).
const GLYPH_OF = Object.freeze({
  robot: Object.freeze({
    omx_full: 'omx_full', omx_follower: 'omx_f', v1_studio: 'v1',
  }),
  arm: Object.freeze({
    omx_full: 'omx_f', omx_follower: 'omx_f', v1_studio: 'v1',
  }),
  leader: Object.freeze({ omx_full: 'omx_l' }),
});

/** The glyph cell name for a profile id and part, or null (unknown → no picture). */
export function robotGlyphName(profile, part = 'robot') {
  const table = hasOwn(GLYPH_OF, part) ? GLYPH_OF[part] : null;
  if (!table || typeof profile !== 'string' || !hasOwn(table, profile)) return null;
  return table[profile];
}

export function robotGlyphUrl() {
  return `${process.env.PUBLIC_URL || ''}/robots/${GLYPH_FILE}`;
}

// Strips are laid out as `frames` cells of width × height pixels side by side;
// `anchors` are fractions of one cell (x, y) where a label belongs.
const strip = (file, frames, width, height, anchors) => Object.freeze({
  file, frames, width, height, anchors: Object.freeze(anchors),
});

export const PHASE_PICTURES = Object.freeze({
  omx_full: Object.freeze({
    // One still (nothing moves while warming up); `handle` is where the hand takes the Leader.
    warmup: strip('rec-warmup-omx_full.webp', 1, 400, 182, {
      follower: [0.4923, 0.6814],
      handle: [0.3117, 0.0868],
      leader: [0.214, 0.4239],
    }),
    // Both arms glide back to the Grundstellung; `home` is under the Follower's base.
    reset: strip('rec-reset-omx_full.webp', 24, 400, 203, {
      home: [0.4923, 0.7142],
    }),
    collision: strip('collision-omx_full.webp', 1, 440, 277, {
      ghost: [0.7133, 0.7234],
      obstacle: [0.6073, 0.5846],
    }),
  }),
});

export const PHASE_KINDS = Object.freeze(['warmup', 'reset', 'collision']);

/**
 * The picture of `kind` ('warmup' | 'reset' | 'collision') for a profile, as
 * `{url, file, frames, width, height, anchors}`, or null when this profile has
 * none (an unknown profile included).
 */
export function phasePicture(profile, kind) {
  if (typeof profile !== 'string' || !hasOwn(PHASE_PICTURES, profile)) return null;
  const set = PHASE_PICTURES[profile];
  if (typeof kind !== 'string' || !hasOwn(set, kind)) return null;
  const pic = set[kind];
  return { ...pic, url: `${process.env.PUBLIC_URL || ''}/robots/${pic.file}` };
}
