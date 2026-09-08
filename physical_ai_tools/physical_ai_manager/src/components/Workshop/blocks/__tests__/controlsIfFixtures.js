/*
 * Copyright 2025 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

/**
 * Saved-workspace fixtures for the `controls_if` else-clause round-trip proofs.
 *
 * Deliberately NOT a `*.test.js` file: `vite.config.js` restricts the vitest
 * `include` to `src/**\/*.{test,spec}.{js,jsx}`, so this module is imported by
 * the suites and never collected as one of its own.
 *
 * These literals are the EXACT bytes Blockly core 12.5.1 and
 * @blockly/block-plus-minus 9.0.10 write today. `controlsIfElse.test.js`
 * asserts that our re-registered mutator produces them too, and
 * `controlsIfElseBackCompat.test.js` — which never registers our mutator —
 * asserts they still load. That pairing is the cross-version proof: same
 * bytes in, same bytes out, either way round.
 */

/** if / then only — no `extraState` key AT ALL (saveExtraState returns null). */
export const IF_PLAIN = {
  blocks: {
    languageVersion: 0,
    blocks: [
      {
        type: 'controls_if',
        id: 'if-plain-0000000000',
        x: 20,
        y: 20,
        inputs: {
          DO0: {
            block: { type: 'edubotics_home', id: 'do0-plain-000000000' },
          },
        },
      },
    ],
  },
};

/** if / then / else. */
export const IF_WITH_ELSE = {
  blocks: {
    languageVersion: 0,
    blocks: [
      {
        type: 'controls_if',
        id: 'if-else-00000000000',
        x: 20,
        y: 20,
        extraState: { hasElse: true },
        inputs: {
          DO0: {
            block: { type: 'edubotics_home', id: 'do0-else-0000000000' },
          },
          ELSE: {
            block: { type: 'edubotics_home', id: 'else-body-000000000' },
          },
        },
      },
    ],
  },
};

/** if / else-if x2 / else — both keys, in the order both writers emit them. */
export const IF_ELSEIF_ELSE = {
  blocks: {
    languageVersion: 0,
    blocks: [
      {
        type: 'controls_if',
        id: 'if-both-00000000000',
        x: 20,
        y: 20,
        extraState: { elseIfCount: 2, hasElse: true },
        inputs: {
          DO0: {
            block: { type: 'edubotics_home', id: 'do0-both-0000000000' },
          },
          DO1: {
            block: { type: 'edubotics_home', id: 'do1-both-0000000000' },
          },
          DO2: {
            block: { type: 'edubotics_home', id: 'do2-both-0000000000' },
          },
          ELSE: {
            block: { type: 'edubotics_home', id: 'else-both-000000000' },
          },
        },
      },
    ],
  },
};

/** if / else-if x1, NO else. */
export const IF_ELSEIF_NO_ELSE = {
  blocks: {
    languageVersion: 0,
    blocks: [
      {
        type: 'controls_if',
        id: 'if-eif-000000000000',
        x: 20,
        y: 20,
        extraState: { elseIfCount: 1 },
        inputs: {
          DO0: {
            block: { type: 'edubotics_home', id: 'do0-eif-00000000000' },
          },
          DO1: {
            block: { type: 'edubotics_home', id: 'do1-eif-00000000000' },
          },
        },
      },
    ],
  },
};

export const ALL_FIXTURES = [
  ['if / dann', IF_PLAIN],
  ['if / dann / sonst', IF_WITH_ELSE],
  ['if / sonst wenn x2 / sonst', IF_ELSEIF_ELSE],
  ['if / sonst wenn x1, kein sonst', IF_ELSEIF_NO_ELSE],
];
