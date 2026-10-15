// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// The small moving picture of the Aufwärmen / Zurücksetzen overlay: a sprite
// strip rendered from the REAL robot (the shipped URDFs, poses from the
// server's own IK — tools/frontend-harness/render-robot-pictures.sh), of the
// robot the student started with. Warm-up: the student moves the Leader-Arm
// and the Follower-Arm mirrors it. Reset: the arm and the 30 mm cube go back
// to the dashed start mark. No live WebGL context (the Windows PCs have no
// GPU) and no three.js: 24 frames in one lazy image, stepped by CSS
// (record.css, `.rec-pic`). Under reduced motion the strip stands still on its
// first frame. A profile with no picture, or none known yet, draws nothing.
//
// The labels are real text pinned to anchors measured in the render (fractions
// of one frame), so they stay on the arm they name.

import React from 'react';
import { phasePicture } from '../icons/robotAssets';

function Chip({ anchor, text }) {
  if (!anchor || !text) return null;
  return (
    <span className="rec-pic-chip" style={{ left: `${anchor[0] * 100}%`, top: `${anchor[1] * 100}%` }}>
      {text}
    </span>
  );
}

/**
 * @param {?string} profile  the robot profile id (taskStatus.robotProfile)
 * @param {'warmup'|'reset'} kind
 * @param {{leader?: string, follower?: string, mark?: string}} labels  German chips
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
        </>
      ) : (
        <Chip anchor={a.mark} text={labels.mark} />
      )}
    </div>
  );
}
