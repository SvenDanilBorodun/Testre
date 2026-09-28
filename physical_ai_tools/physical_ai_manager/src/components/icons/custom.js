/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The three icons Lucide does not have, drawn in Lucide's own rules so they sit
// beside it unnoticed: a 24×24 viewBox, no fill, `currentColor` stroke of
// width 2, round caps and joins, every coordinate within [1, 23]
// (components/icons/__tests__/icons.test.jsx checks each rule). They are
// `GenIcon` trees exactly like `react-icons/lu`'s, so the one walker in svg.js
// reads both. The owner approves their look on the visual gate
// (docs/KNOWN-ISSUES.md).

import { GenIcon } from 'react-icons/lib';

const ROOT_ATTR = Object.freeze({
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: '2',
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
});

const path = (d) => ({ tag: 'path', attr: { d }, child: [] });
const circle = (cx, cy, r) => ({ tag: 'circle', attr: { cx: String(cx), cy: String(cy), r: String(r) }, child: [] });
const tree = (child) => ({ tag: 'svg', attr: { ...ROOT_ATTR }, child });

// A follower arm: base plate and turret, the upper arm up to the elbow joint,
// the forearm out to the wrist and an open claw hanging from it.
export const ROBOT_ARM_TREE = Object.freeze(tree([
  path('M3 21h11'),
  path('M5.5 21v-2a3 3 0 0 1 6 0v2'),
  path('m9 16 3.6-8.7'),
  circle(13.5, 5, 2.5),
  path('M16 5h4v3'),
  path('M17.5 13v-2.5a2.5 2.5 0 0 1 5 0V13'),
]));

// The leader arm — the one a student moves by hand: the same arm, ending in a
// closed handle grip instead of the open claw, so the two read apart at 16 px.
export const LEADER_ARM_TREE = Object.freeze(tree([
  path('M3 21h11'),
  path('M5.5 21v-2a3 3 0 0 1 6 0v2'),
  path('m9 16 3.6-8.7'),
  circle(13.5, 5, 2.5),
  path('M16 5h4v2.5'),
  path('M17.5 10a2.5 2.5 0 0 1 5 0v5a2.5 2.5 0 0 1-5 0z'),
]));

// A snake for Python: an S-coiled body from the tail (bottom left) up to a
// round head with a forked tongue (top right).
export const PYTHON_TREE = Object.freeze(tree([
  path('M13.5 6H9a3.5 3.5 0 0 0 0 7h6a3.5 3.5 0 0 1 0 7H3'),
  circle(16.5, 5.5, 3),
  path('M19.5 5.5h1.5l1.5-1.5'),
  path('m21 5.5 1.5 1.5'),
]));

export const CUSTOM_TREES = Object.freeze({
  robotArm: ROBOT_ARM_TREE,
  leaderArm: LEADER_ARM_TREE,
  python: PYTHON_TREE,
});

// Components with react-icons' own signature (`(props) => element`).
export const IconRobotArm = (props) => GenIcon(ROBOT_ARM_TREE)(props);
export const IconLeaderArm = (props) => GenIcon(LEADER_ARM_TREE)(props);
export const IconPython = (props) => GenIcon(PYTHON_TREE)(props);
