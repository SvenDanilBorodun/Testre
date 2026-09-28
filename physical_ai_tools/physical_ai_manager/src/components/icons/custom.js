/*
 * Copyright 2026 EduBotics
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */

// The four icons Lucide does not have, drawn in Lucide's own rules so they sit
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

// A follower arm on its base: turret, upper arm, elbow, forearm, open jaws.
export const ROBOT_ARM_TREE = Object.freeze(tree([
  path('M4 21h9'),
  path('M6 21v-2a2.5 2.5 0 0 1 5 0v2'),
  path('m8.5 16.5 3-8'),
  circle(12, 7, 1.5),
  path('M13.5 7h5'),
  path('M18.5 7v3'),
  path('M17 13l1.5-3 1.5 3'),
]));

// The leader arm: the same arm, a round grip where the jaws would be (it is
// the arm a student moves by hand).
export const LEADER_ARM_TREE = Object.freeze(tree([
  path('M4 21h9'),
  path('M6 21v-2a2.5 2.5 0 0 1 5 0v2'),
  path('m8.5 16.5 3-8'),
  circle(12, 7, 1.5),
  path('M13.5 7h5'),
  path('M18.5 7v2'),
  circle(18.5, 11.5, 2.5),
]));

// A two-jaw gripper, open.
export const GRIPPER_TREE = Object.freeze(tree([
  path('M8 4h8'),
  path('M12 4v5'),
  path('M7 9h10'),
  path('M7 9v7l2 3'),
  path('M17 9v7l-2 3'),
]));

// A snake for Python: an S-shaped body, a round head, an eye and a forked
// tongue.
export const PYTHON_TREE = Object.freeze(tree([
  path('M4 20h10a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h5'),
  circle(16.5, 8, 2.5),
  path('M16.5 7h.01'),
  path('M19 8h2'),
  path('m21 8 1-1'),
  path('m21 8 1 1'),
]));

export const CUSTOM_TREES = Object.freeze({
  robotArm: ROBOT_ARM_TREE,
  leaderArm: LEADER_ARM_TREE,
  gripper: GRIPPER_TREE,
  python: PYTHON_TREE,
});

// Components with react-icons' own signature (`(props) => element`).
export const IconRobotArm = (props) => GenIcon(ROBOT_ARM_TREE)(props);
export const IconLeaderArm = (props) => GenIcon(LEADER_ARM_TREE)(props);
export const IconGripper = (props) => GenIcon(GRIPPER_TREE)(props);
export const IconPython = (props) => GenIcon(PYTHON_TREE)(props);
