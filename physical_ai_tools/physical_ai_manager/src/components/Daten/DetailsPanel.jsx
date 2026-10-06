// Copyright 2026 EduBotics
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0

// „Episode N · Details" (owner decision D10, spec §F8): every row has a real
// source in the dataset — the rest is dropped (v3.0 stores no per-episode
// recording time, so the mockup's „Aufgenommen" is gone). The size is the
// episode's exact video bytes plus an estimate of its data, hence „≈". The
// „Auf Hugging Face ansehen" link appears when the dataset is proven on the hub
// (§G14).

import React from 'react';
import Icon from '../icons/Icon';
import COPY from '../../features/editDataset/datenCopy';
import { fill, fmtBytes, fmtFps, fmtTime } from '../../features/editDataset/model/format';
import { cameraLabel, codecName, orderCameras, robotName } from '../../features/editDataset/model/labels';

const D = COPY.details;

export default function DetailsPanel({ summary, episode, link }) {
  const ep = summary && summary.episodes ? summary.episodes[episode] : null;
  if (!ep) return <div className="dat-panel dat-details" />;
  const fps = Number(summary.fps) || 0;
  const cameras = orderCameras(summary.cameras);
  const first = cameras[0] || {};
  const bytes = ep.bytes || {};
  const size = (Number(bytes.video) || 0) + (Number(bytes.data_estimate) || 0);
  const joints = summary.joints || {};
  return (
    <div className="dat-panel dat-details">
      <h2><Icon name="info" size={16} />{fill(COPY.player.detailsTitle, { n: episode + 1 })}</h2>
      <dl>
        <dt>{D.duration}</dt>
        <dd className="dat-mono">{fill(D.durationValue, { t: fmtTime(ep.duration_s ?? (ep.length / (fps || 1))), n: ep.length })}</dd>
        <dt>{D.cameras}</dt>
        <dd>
          {cameras.map((c, i) => (
            <React.Fragment key={c.index}>
              {i ? <br /> : null}
              {fill(D.cameraFrames, { camera: cameraLabel(c), n: ep.frames ? (ep.frames[String(c.index)] ?? '–') : '–' })}
            </React.Fragment>
          ))}
        </dd>
        <dt>{D.size}</dt>
        <dd className="dat-mono">{fill(D.sizeValue, { size: fmtBytes(size) })}</dd>
        <dt>{D.robot}</dt>
        <dd>{fill(D.robotValue, { name: robotName(summary.robot_type), id: summary.robot_type || '–' })}</dd>
        <dt>{D.joints}</dt>
        <dd>{fill(D.jointsValue, { state: (joints.state || []).length, action: (joints.action || []).length })}</dd>
        <dt>{D.video}</dt>
        <dd className="dat-mono">
          {fill(D.videoValue, { codec: codecName(first.codec), w: first.width ?? '–', h: first.height ?? '–', fps: fmtFps(first.fps || fps) })}
        </dd>
        <dt>{D.task}</dt>
        <dd>{ep.task ? fill(D.taskValue, { task: ep.task }) : '–'}</dd>
      </dl>
      <div className="dat-note2 dat-mono">{fill(D.index, { i: ep.i ?? episode })}</div>
      {link ? (
        <a className="dat-hublink" href={link.href} target="_blank" rel="noopener noreferrer" title={link.title}>
          <Icon name="externalLink" size={14} />
          {COPY.menu.hubLink}
        </a>
      ) : null}
    </div>
  );
}
