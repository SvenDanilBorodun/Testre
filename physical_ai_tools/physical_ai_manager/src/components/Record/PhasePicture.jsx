// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The small picture of the Aufwärmen / Zurücksetzen overlay, rendered from the
// REAL robot (the shipped URDFs, poses from the server's own IK —
// tools/frontend-harness/render-robot-pictures.sh), of the robot the student
// started with.
//   * Warm-up: ONE still of the Leader-Arm and the Follower-Arm in the
//     Grundstellung. Nothing moves: the student only takes the Leader in the
//     hand and waits for „Los!" (moving it is for the episode). A hand (the
//     registry's `hand`, then `grab`) opens, comes to the Leader's handle,
//     closes and holds while a ring pulses around it (record.css, `.rec-pic-handle`).
//   * Reset: 24 frames, stepped by CSS: both arms glide back to the Grundstellung
//     (no object), and the „Ausgangslage" chip shows once they are there.
// No live WebGL context (the Windows PCs have no GPU) and no three.js: one lazy
// image. Under reduced motion the finished picture stands still (the hand holds
// the Leader; the arms are home, the chip is there). A profile with no picture,
// or none known yet, draws nothing.
//
// The labels and the hand are real elements pinned to anchors measured in the
// render (fractions of one frame), so they stay on the arm they name.

import React from 'react';
import Icon from '../icons/Icon';
import { phasePicture } from '../icons/robotAssets';

const at = (anchor) => ({ left: `${anchor[0] * 100}%`, top: `${anchor[1] * 100}%` });

function Chip({ anchor, text, arrive = false }) {
  if (!anchor || !text) return null;
  return (
    <span className={arrive ? 'rec-pic-chip rec-pic-chip--arrive' : 'rec-pic-chip'} style={at(anchor)}>
      {text}
    </span>
  );
}

// The student's hand taking the Leader: an open hand, a closed one, a pulsing ring.
function Handle({ anchor }) {
  if (!anchor) return null;
  return (
    <span className="rec-pic-handle" style={at(anchor)} data-testid="rec-pic-handle">
      <span className="rec-pic-ring" />
      <Icon name="hand" className="rec-pic-hand" size="100%" />
      <Icon name="grab" className="rec-pic-fist" size="100%" />
    </span>
  );
}

/**
 * @param {?string} profile  the robot profile id (taskStatus.robotProfile)
 * @param {'warmup'|'reset'} kind
 * @param {{leader?: string, follower?: string, home?: string}} labels  German chips
 */
export default function PhasePicture({ profile, kind, labels = {} }) {
  const pic = phasePicture(profile, kind);
  if (!pic) return null;
  const a = pic.anchors;
  return (
    <div
      className={`rec-picto rec-pic rec-pic-${kind}`}
      data-picto={kind}
      data-robot-picture={pic.file}
      aria-hidden="true"
      style={{ aspectRatio: `${pic.width} / ${pic.height}`, '--rec-frames': pic.frames, '--rec-pic-ar': pic.width / pic.height }}
    >
      <div className="rec-pic-clip">
        <img className="rec-pic-strip" src={pic.url} alt="" draggable="false" decoding="async" />
      </div>
      {kind === 'warmup' ? (
        <>
          <Chip anchor={a.leader} text={labels.leader} />
          <Chip anchor={a.follower} text={labels.follower} />
          <Handle anchor={a.handle} />
        </>
      ) : (
        <Chip anchor={a.home} text={labels.home} arrive />
      )}
    </div>
  );
}
